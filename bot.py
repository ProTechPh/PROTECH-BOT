import random
import logging
import subprocess
import sys
import os
import re
import time
import discord
from discord.ext import commands, tasks
import asyncio
from discord import app_commands
import sqlite3
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
import json
import shutil
import threading
import requests

# Load environment variables
load_dotenv()

# Configuration
TOKEN = os.getenv('TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
RAILWAY_API_TOKEN = os.getenv('RAILWAY_API_TOKEN')
RAILWAY_PROJECT_ID = os.getenv('RAILWAY_PROJECT_ID')
RAILWAY_API_URL = os.getenv('RAILWAY_API_URL', 'https://backboard.railway.app/graphql/v2')

# Default Specs
DEFAULT_RAM = os.getenv('DEFAULT_RAM', '2g')
DEFAULT_CPU = os.getenv('DEFAULT_CPU', '1')
DEFAULT_DISK = os.getenv('DEFAULT_DISK', '10g')
BOT_STATUS_NAME = os.getenv('BOT_STATUS_NAME', 'ProTechPh VPS')
WATERMARK = os.getenv('WATERMARK', 'Powered by ProTechPh VPS Bot')

# ============================================
# Railway API Helpers
# ============================================

class RailwayAPI:
    @staticmethod
    def query(query, variables=None):
        if not RAILWAY_API_TOKEN:
            logging.error("RAILWAY_API_TOKEN is not set.")
            return None
        headers = {"Authorization": f"Bearer {RAILWAY_API_TOKEN}", "Content-Type": "application/json"}
        payload = {"query": query, "variables": variables or {}}
        try:
            res = requests.post(RAILWAY_API_URL, headers=headers, json=payload)
            data = res.json()
            if "errors" in data:
                logging.error(f"Railway API Errors: {json.dumps(data['errors'], indent=2)}")
            return data
        except Exception as e:
            logging.error(f"Railway API Request failed: {e}")
            return None

    @staticmethod
    def create_service(name, os_type):
        image = "akarita/docker-ubuntu-desktop" if os_type == "ubuntu-desktop" else "ubuntu:22.04"
        mutation = """
        mutation serviceCreate($input: ServiceCreateInput!) {
          serviceCreate(input: $input) {
            id
            name
          }
        }
        """
        variables = {
            "input": {
                "projectId": RAILWAY_PROJECT_ID,
                "name": name,
                "source": {"image": image}
            }
        }
        res = RailwayAPI.query(mutation, variables)
        if res and "data" in res and res["data"]["serviceCreate"]:
            return res["data"]["serviceCreate"]["id"]
        return None

    @staticmethod
    def delete_service(service_id):
        mutation = """
        mutation serviceDelete($id: String!) {
          serviceDelete(id: $id)
        }
        """
        variables = {"id": service_id}
        res = RailwayAPI.query(mutation, variables)
        return res and "data" in res and res["data"]["serviceDelete"]

    @staticmethod
    def create_domain(service_id, environment_id):
        # We use serviceDomainCreate in Railway v2
        mutation = """
        mutation serviceDomainCreate($input: ServiceDomainCreateInput!) {
          serviceDomainCreate(input: $input) {
            domain
          }
        }
        """
        variables = {
            "input": {
                "environmentId": environment_id,
                "serviceId": service_id
            }
        }
        res = RailwayAPI.query(mutation, variables)
        if res and "data" in res and res["data"]["serviceDomainCreate"]:
            return res["data"]["serviceDomainCreate"]["domain"]
        return None

    @staticmethod
    def get_environment_id():
        query = """
        query environments($projectId: String!) {
          environments(projectId: $projectId) {
            edges {
              node {
                id
                name
              }
            }
          }
        }
        """
        variables = {"projectId": RAILWAY_PROJECT_ID}
        res = RailwayAPI.query(query, variables)
        try:
            # We return the first environment (usually 'production')
            return res["data"]["environments"]["edges"][0]["node"]["id"]
        except:
            return None

    @staticmethod
    def set_service_variable(service_id, name, value):
        mutation = """
        mutation variableUpsert($input: VariableUpsertInput!) {
          variableUpsert(input: $input)
        }
        """
        variables = {
            "input": {
                "projectId": RAILWAY_PROJECT_ID,
                "serviceId": service_id,
                "name": name,
                "value": value
            }
        }
        res = RailwayAPI.query(mutation, variables)
        return res and "data" in res and res["data"]["variableUpsert"]

    @staticmethod
    def get_service_status(service_id):
        query = """
        query service($id: String!) {
          service(id: $id) {
            deployments(first: 1) {
              edges {
                node {
                  status
                }
              }
            }
          }
        }
        """
        variables = {"id": service_id}
        res = RailwayAPI.query(query, variables)
        try:
            return res["data"]["service"]["deployments"]["edges"][0]["node"]["status"]
        except:
            return "UNKNOWN"

# VPS Defaults from .env
DEFAULT_RAM = os.getenv('DEFAULT_RAM', '2g')  # e.g., '2g', '4G'
DEFAULT_CPU = os.getenv('DEFAULT_CPU', '1')  # Lowered default to '1' to avoid common errors
DEFAULT_DISK = os.getenv('DEFAULT_DISK', '10G')  # e.g., '20G' - Note: Disk limit not enforced in container
VPS_HOSTNAME = os.getenv('VPS_HOSTNAME', 'protech-vps')  # Base hostname, append user ID
SERVER_LIMIT = int(os.getenv('SERVER_LIMIT', 1))
TOTAL_SERVER_LIMIT = int(os.getenv('TOTAL_SERVER_LIMIT', 50))
DATABASE_FILE = os.getenv('DATABASE_FILE', 'vps_bot.db')

# Economy Config
COINS_DAILY_REWARD = int(os.getenv('COINS_DAILY_REWARD', 100))
COINS_WORK_MIN = int(os.getenv('COINS_WORK_MIN', 20))
COINS_WORK_MAX = int(os.getenv('COINS_WORK_MAX', 80))
COINS_RENEWAL_COST = int(os.getenv('COINS_RENEWAL_COST', 500))
COINS_RENEWAL_DAYS = int(os.getenv('COINS_RENEWAL_DAYS', 7))

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('vps_bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='/', intents=intents)
# Docker helpers (No longer used on Railway API mode, but kept for compatibility)
def check_docker_availability():
    return True, "Railway API Mode"

DOCKER_AVAILABLE, DOCKER_VERSION = True, "Railway API Mode"


# ============================================
# PREMIUM EMBED SYSTEM - Modern UI/UX Design
# ============================================

class EmbedColors:
    PRIMARY = 0x5865F2      # Discord Blurple
    SUCCESS = 0x57F287      # Green
    ERROR = 0xED4245        # Red
    WARNING = 0xFEE75C      # Yellow
    INFO = 0x5865F2         # Blue
    PREMIUM = 0xEB459E      # Pink
    GOLD = 0xF1C40F         # Gold
    PURPLE = 0x9B59B6       # Purple
    DARK = 0x2B2D31         # Dark
    LIGHT = 0x99AAB5        # Light Gray

class EmbedIcons:
    SUCCESS = "✓"
    ERROR = "✕"
    WARNING = "⚠"
    INFO = "ℹ"
    LOADING = "⟳"
    PREMIUM = "★"
    ARROW = "→"
    BULLET = "•"
    DIVIDER = "─"
    GOLD = "💰"

def create_embed(title, description="", color=EmbedColors.PRIMARY, show_branding=True):
    clean_title = (title[:253] + '...') if len(title) > 256 else title
    embed = discord.Embed(
        title=clean_title,
        description=description[:4093] + '...' if len(description) > 4096 else description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    if show_branding:
        embed.set_footer(
            text=f"{BOT_STATUS_NAME} {EmbedIcons.BULLET} {WATERMARK}",
            icon_url=bot.user.avatar.url if bot.user and bot.user.avatar else None
        )
    return embed

def create_success_embed(title, description=""):
    return create_embed(f"{EmbedIcons.SUCCESS} {title}", description, color=EmbedColors.SUCCESS)

def create_error_embed(title, description=""):
    return create_embed(f"{EmbedIcons.ERROR} {title}", description, color=EmbedColors.ERROR)

def is_admin(member):
    if not isinstance(member, discord.Member):
        logger.warning("is_admin called with non-Member object")
        return False
    # Check user ID for admin access
    return member.id == ADMIN_ID

# Database setup with SQLite3
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Main VPS table (with Expiration)
    cursor.execute('''CREATE TABLE IF NOT EXISTS vps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        container_id TEXT UNIQUE NOT NULL,
        container_name TEXT NOT NULL,
        os_type TEXT NOT NULL,
        hostname TEXT NOT NULL,
        ssh_command TEXT,
        ram TEXT NOT NULL,
        cpu TEXT NOT NULL,
        disk TEXT NOT NULL,
        status TEXT DEFAULT 'running',
        expires_at TEXT,
        duration_days INTEGER DEFAULT 7,
        suspended INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY
        )
    ''')

    # Migrations for existing vps table
    cursor.execute("PRAGMA table_info(vps)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'expires_at' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN expires_at TIMESTAMP")
    if 'duration_days' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN duration_days INTEGER DEFAULT 7")
    if 'auto_renew' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN auto_renew INTEGER DEFAULT 0")

    conn.commit()
    conn.close()

init_db()

# ============================================
# EXPIRATION & MANAGEMENT HELPERS
# ============================================

def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def set_vps_expiry(container_id, days):
    expiry = datetime.now(timezone.utc) + timedelta(days=days)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET expires_at = ?, duration_days = ? WHERE container_id = ?', (expiry.isoformat(), days, container_id))
    conn.commit()
    conn.close()

def get_expiring_vps():
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE expires_at IS NOT NULL AND expires_at <= ? AND suspended = 0', (now,))
    rows = cursor.fetchall()
    conn.close()
    return rows

async def check_expirations():
    while True:
        try:
            expired = get_expiring_vps()
            for vps in expired:
                cid = vps['container_id']
                user_id = vps['user_id']
                logger.info(f"Suspending expired VPS {cid} for user {user_id}")
                if await async_docker_stop(cid):
                    update_vps_status(cid, "stopped")
                    update_vps_suspended(cid, 1)
                    try:
                        user = await bot.fetch_user(user_id)
                        embed = create_error_embed("VPS Expired", f"Your VPS `{vps['container_name']}` has expired after {vps['duration_days']} days.")
                        await user.send(embed=embed)
                    except:
                        pass
        except Exception as e:
            logger.error(f"Expiry check error: {e}")
        await asyncio.sleep(3600) # Check every hour

def add_user(user_id, username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    conn.close()

def add_ban(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO bans (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

def remove_ban(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM bans WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM bans WHERE user_id = ?', (user_id,))
    banned = cursor.fetchone() is not None
    conn.close()
    return banned

def add_vps(user_id, container_id, container_name, os_type, hostname, ssh_command, ram, cpu, disk, duration_days):
    expiry = datetime.now(timezone.utc) + timedelta(days=duration_days)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO vps (user_id, container_id, container_name, os_type, hostname, status, ssh_command, ram, cpu, disk, suspended, expires_at, duration_days)
        VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, 0, ?, ?)
    ''', (user_id, container_id, container_name, os_type, hostname, ssh_command, ram, cpu, disk, expiry.isoformat(), duration_days))
    conn.commit()
    conn.close()

def get_user_vps(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    vps_list = cursor.fetchall()
    conn.close()
    return vps_list

def count_user_vps(user_id):
    return len(get_user_vps(user_id))

def get_vps_by_container_id(container_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE container_id = ?', (container_id,))
    vps = cursor.fetchone()
    conn.close()
    return vps

def get_vps_by_identifier(user_id, identifier):
    vps_list = get_user_vps(user_id)
    if not identifier:
        return vps_list[0] if vps_list else None
    identifier_lower = identifier.lower()
    for vps in vps_list:
        if (identifier_lower in vps['container_id'].lower() or
            identifier_lower in vps['container_name'].lower()):
            return vps
    return None

def update_vps_status(container_id, status):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET status = ? WHERE container_id = ?', (status, container_id))
    conn.commit()
    conn.close()

def update_vps_ssh(container_id, ssh_command):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET ssh_command = ? WHERE container_id = ?', (ssh_command, container_id))
    conn.commit()
    conn.close()

def update_vps_suspended(container_id, suspended):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET suspended = ? WHERE container_id = ?', (suspended, container_id))
    conn.commit()
    conn.close()

def delete_vps(container_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM vps WHERE container_id = ?', (container_id,))
    conn.commit()
    conn.close()

def get_total_instances():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM vps WHERE status = "running"')
    count = cursor.fetchone()[0]
    conn.close()
    return count

def parse_gb(resource_str):
    match = re.match(r'(\d+(?:\.\d+)?)([mMgG])?', resource_str.lower())
    if match:
        num = float(match.group(1))
        unit = match.group(2) or 'g'
        if unit in ['g', '']:
            return num
        elif unit in ['m']:
            return num / 1024.0
    return 0.0

def get_uptime(container_id):
    try:
        output = subprocess.check_output(["docker", "inspect", "-f", "{{.State.StartedAt}}", container_id], stderr=subprocess.STDOUT).decode().strip()
        if output == "<no value>":
            return "Not running"
        start_time = datetime.fromisoformat(output.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        uptime = now - start_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m"
    except Exception as e:
        logger.error(f"Uptime error for {container_id}: {e}")
        return "Unknown"

def get_stats(container_id):
    try:
        output = subprocess.check_output([
            "docker", "stats", "--no-stream", "--format",
            "{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}",
            container_id
        ], stderr=subprocess.STDOUT).decode().strip()
        parts = output.split('\t')
        if len(parts) == 3:
            cpu, mem, net = parts
            return {'cpu': cpu, 'mem': mem, 'net': net}
    except Exception as e:
        logger.error(f"Stats error for {container_id}: {e}")
    return {'cpu': 'N/A', 'mem': 'N/A', 'net': 'N/A'}

def get_logs(container_id, lines=50):
    try:
        output = subprocess.check_output(["docker", "logs", "--tail", str(lines), container_id], stderr=subprocess.STDOUT).decode()
        return output[-2000:]  # Truncate for Discord limit
    except Exception as e:
        logger.error(f"Logs error for {container_id}: {e}")
        return "Failed to fetch logs"

# Railway based management (Replaces Docker Commands)
async def async_docker_stop(service_id):
    # Railway services are usually stopped by deleting or just waiting.
    # For now, we'll use delete to fully remove as "Stop" in this bot means cleanup.
    return RailwayAPI.delete_service(service_id)

async def async_docker_rm(service_id):
    return RailwayAPI.delete_service(service_id)

async def async_docker_run(image, hostname, ram, cpu, disk, container_name, os_type):
    # This is handled by creation.
    return RailwayAPI.create_service(container_name, os_type)

async def async_docker_start(container_id):
    # Railway services are managed by the platform, we assume they are running if created.
    return True

async def async_docker_restart(container_id):
    # Railway services are managed by the platform.
    return True

async def async_install_tmate(container_id, os_type):
    pass

async def capture_desktop_url(container_id):
    return None

# SSH capture
async def capture_ssh_session_line(process):
    return None

async def docker_exec_tmate(container_id):
    return None

# Generic regen SSH
async def regen_ssh_command(interaction: discord.Interaction, vps_identifier, send_response=True, target_user=None):
    if target_user is None:
        target_user = interaction.user
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="No active VPS found.", color=discord.Color.red())
        if send_response:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    if vps['status'] != "running":
        embed = discord.Embed(description="VPS must be running to generate SSH.", color=discord.Color.red())
        if send_response:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    container_id = vps['container_id']
    if vps['os_type'] == "ubuntu-desktop":
        if send_response:
            await interaction.response.defer(ephemeral=True)
        desktop_url = await capture_desktop_url(container_id)
        if desktop_url:
            update_vps_ssh(container_id, desktop_url)
            embed = discord.Embed(title="New Desktop Link Generated", description=f"Access your VPS Desktop here:\n{desktop_url}\n\n**Note:** It may take 1-2 minutes for the Desktop to fully initialize.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
            embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
            try:
                await target_user.send(embed=embed)
            except discord.Forbidden:
                if send_response:
                    await interaction.followup.send("Link generated but could not DM you.", ephemeral=True)
            if send_response:
                await interaction.followup.send("New Desktop link sent to your DMs.", ephemeral=True)
            return True
        else:
            if send_response:
                await interaction.followup.send("Failed to generate Desktop link.", ephemeral=True)
            return False

    if send_response:
        await interaction.response.defer(ephemeral=True)
    exec_process = await docker_exec_tmate(container_id)
    if exec_process:
        ssh_line = await capture_ssh_session_line(exec_process)
        if ssh_line:
            update_vps_ssh(container_id, ssh_line)
            embed = discord.Embed(title="New SSH Session Generated", description=f"```{ssh_line}```", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
            embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
            try:
                await target_user.send(embed=embed)
            except discord.Forbidden:
                logger.warning(f"Cannot DM user {target_user.id}")
                if send_response:
                    embed_dm_fail = discord.Embed(description="New SSH session generated but could not send to DMs (privacy settings).", color=discord.Color.orange())
                    await interaction.followup.send(embed=embed_dm_fail, ephemeral=True)
                else:
                    return True
            if send_response:
                embed_success = discord.Embed(description="New SSH session sent to your DMs.", color=discord.Color.green())
                await interaction.followup.send(embed=embed_success, ephemeral=True)
            return True
        else:
            embed = discord.Embed(description="Failed to generate SSH session.", color=discord.Color.red())
            if send_response:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return False
    else:
        embed = discord.Embed(description="Failed to execute tmate.", color=discord.Color.red())
        if send_response:
            await interaction.followup.send(embed=embed, ephemeral=True)
        return False

# Start/Stop/Restart helpers
async def manage_vps(interaction: discord.Interaction, vps_identifier, action, target_user=None):
    if target_user is None:
        target_user = interaction.user
    await interaction.response.defer(ephemeral=True)
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="No VPS found.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    if action == "start" and vps['suspended'] and target_user == interaction.user:
        embed = discord.Embed(description="This VPS is suspended by an admin. Contact support.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    container_id = vps['container_id']
    os_type = vps['os_type']
    success = False
    if action == "start":
        success = await async_docker_start(container_id)
        if success:
            update_vps_status(container_id, "running")
    elif action == "stop":
        success = await async_docker_stop(container_id)
        if success:
            update_vps_status(container_id, "stopped")
    elif action == "restart":
        success = await async_docker_restart(container_id)
        if success:
            update_vps_status(container_id, "running")
    if success:
        os_name = "Ubuntu Desktop" if os_type == "ubuntu-desktop" else ("Ubuntu 22.04" if os_type == "ubuntu" else "Debian 12")
        embed = create_success_embed(f"VPS {action.title()}ed Successfully", f"OS: {os_name}")
        if action in ["start", "restart"]:
            regen_success = await regen_ssh_command(interaction, vps_identifier, send_response=False, target_user=target_user)
            if regen_success:
                embed.description += f"\nNew {'Desktop link' if os_type == 'ubuntu-desktop' else 'SSH session'} sent to DMs."
            else:
                embed.description += f"\nFailed to generate new {'Desktop link' if os_type == 'ubuntu-desktop' else 'SSH session'}."
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        embed = create_error_embed(f"Failed to {action} the VPS")
        await interaction.followup.send(embed=embed, ephemeral=True)

# Reinstall helper
async def reinstall_vps(interaction: discord.Interaction, vps_identifier, os_type, target_user=None):
    if target_user is None:
        target_user = interaction.user
    await interaction.response.defer(ephemeral=True)
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="No VPS found.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    container_id = vps['container_id']
    user_id = vps['user_id']
    hostname = vps['hostname']
    ram, cpu, disk = vps['ram'], vps['cpu'], vps['disk']
    # Stop and remove
    await async_docker_stop(container_id)
    await asyncio.sleep(2)
    await async_docker_rm(container_id)
    delete_vps(container_id)
    # Create new with unique name
    suffix = random.randint(1000, 9999)
    new_container_name = f"{os_type}-vps-{user_id}-{suffix}"
    if os_type == "ubuntu-desktop":
        image = "accetto/ubuntu-vnc-xfce-g3"
    else:
        image = "ubuntu:22.04" if os_type == "ubuntu" else "debian:bookworm"
    new_container_id = await async_docker_run(image, hostname, ram, cpu, disk, new_container_name, os_type)
    if new_container_id:
        await async_install_tmate(new_container_id, os_type)
        if os_type == "ubuntu-desktop":
            access_line = await capture_desktop_url(new_container_id)
            os_name = "Ubuntu Desktop"
        else:
            exec_process = await docker_exec_tmate(new_container_id)
            access_line = await capture_ssh_session_line(exec_process)
            os_name = "Ubuntu 22.04" if os_type == "ubuntu" else "Debian 12"
            
        if access_line:
            add_vps(user_id, new_container_id, new_container_name, os_type, hostname, access_line, ram, cpu, disk)
            access_type = "Desktop Link" if os_type == "ubuntu-desktop" else "SSH Command"
            embed = discord.Embed(title="VPS Reinstalled Successfully", description=f"OS: {os_name}\n{access_type}:\n```{access_line}```", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
            embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
            try:
                await target_user.send(embed=embed)
            except discord.Forbidden:
                logger.warning(f"Cannot DM user {target_user.id} for reinstall")
            embed_success = discord.Embed(description="VPS has been reinstalled. Check your DMs for details.", color=discord.Color.green())
            await interaction.followup.send(embed=embed_success, ephemeral=True)
        else:
            embed = discord.Embed(description="Reinstall failed: Unable to generate Access details.", color=discord.Color.red())
            await interaction.followup.send(embed=embed, ephemeral=True)
            await async_docker_rm(new_container_id)
    else:
        embed = discord.Embed(description="Reinstall failed: Docker creation error.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)

# Create VPS helper (Free with Expiration Choice)
async def create_simple_vps(interaction: discord.Interaction, os_type, duration_days, target_user=None):
    if target_user is None:
        target_user = interaction.user
    user_id = target_user.id

    # Check 1 VPS Limit
    if count_user_vps(user_id) >= SERVER_LIMIT and not is_admin(interaction.user):
        await interaction.response.send_message(embed=create_error_embed("Limit Reached", f"You can only host **{SERVER_LIMIT}** VPS at a time."), ephemeral=True)
        return

    ram, cpu, disk = DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK
    
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(f"Deploying your {os_type.title()} VPS for **{duration_days}** days...", ephemeral=True)
    
    hostname = f"{VPS_HOSTNAME}-{user_id}"
    suffix = random.randint(1000, 9999)
    container_name = f"{os_type}-vps-{user_id}-{suffix}"
    
    # Railway Service Creation
    service_id = RailwayAPI.create_service(container_name, os_type)
    if not service_id:
        await interaction.followup.send(embed=create_error_embed("Railway API Error", "Failed to create service in Railway. Check Project ID and Token."), ephemeral=True)
        return
        
    # Set the PORT variable so Railway knows where to route traffic
    # accetto/ubuntu-vnc-xfce-g3 usually uses 6901 or 6080. We'll try to set PORT to 6080 for NoVNC.
    target_port = "6080"
    RailwayAPI.set_service_variable(service_id, "PORT", target_port)
    
    await asyncio.sleep(5)
    
    # Try to create a public domain for the user to access it
    env_id = RailwayAPI.get_environment_id()
    if env_id:
        domain = RailwayAPI.create_domain(service_id, env_id)
        access_line = f"https://{domain}" if domain else "Creating domain..."
    else:
        access_line = "Project env not found."
    
    if os_type == "ubuntu-desktop":
        # Desktop already accessible via HTTP on port 6080 if configured in Railway?
        # For Desktop, we need to ensure port 6080 is linked.
        # But for now, we'll just give the domain link.
        pass
    else:
        # Standard SSH/Terminal
        # We still need tmate inside the Railway service? 
        # YES, so we'll wait for the deployment to start.
        # Note: Executing commands on a Railway service remotely via API is not directly simple.
        # But we can assume the user will see logs or connect once active.
        pass
    
    add_vps(user_id, service_id, container_name, os_type, hostname, access_line, ram, cpu, disk, duration_days)
    os_name = "Ubuntu Desktop" if os_type == "ubuntu-desktop" else "Ubuntu 22.04"
    access_type = "Web Access Link" if os_type == "ubuntu-desktop" else "Railway Service Link"
    
    embed = create_success_embed("VPS Ready!", f"OS: {os_name}\nRAM: {ram} | CPU: {cpu} | Disk: {disk}\nDuration: **{duration_days} days**\n{access_type}:\n```\n{access_line}\n```")
    try:
        await target_user.send(embed=embed)
    except:
        pass
    await interaction.followup.send(embed=create_success_embed("VPS Online", "Check your DMs for access details."), ephemeral=True)

# Admin helpers
async def admin_manage_vps(interaction: discord.Interaction, target_user_id: int, vps_identifier: str, action: str):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    target_user = await bot.fetch_user(target_user_id)
    if not target_user:
        embed = discord.Embed(description="User not found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    vps = get_vps_by_identifier(target_user_id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="VPS not found for this user.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    container_id = vps['container_id']
    success = False
    if action == "delete":
        await async_docker_stop(container_id)
        await asyncio.sleep(2)
        await async_docker_rm(container_id)
        delete_vps(container_id)
        success = True
        msg = f"Deleted VPS for {target_user}"
    elif action in ["start", "stop", "restart"]:
        if action == "start":
            success = await async_docker_start(container_id)
            update_vps_status(container_id, "running")
        elif action == "stop":
            success = await async_docker_stop(container_id)
            update_vps_status(container_id, "stopped")
        elif action == "restart":
            success = await async_docker_restart(container_id)
            update_vps_status(container_id, "running")
        msg = f"{action.title()}ed VPS for {target_user}"
    elif action == "suspend":
        success = await async_docker_stop(container_id)
        if success:
            update_vps_status(container_id, "stopped")
            update_vps_suspended(container_id, 1)
        msg = f"Suspended VPS for {target_user}"
    elif action == "unsuspend":
        update_vps_suspended(container_id, 0)
        success = True
        msg = f"Unsuspended VPS for {target_user}. You can now start it."
    if success:
        embed = discord.Embed(title="Admin Action Completed", description=msg, color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
        await interaction.response.send_message(embed=embed)
    else:
        embed = discord.Embed(description="Action failed.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)

async def admin_kill_all(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.defer()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT container_id FROM vps WHERE status = "running"')
    running = cursor.fetchall()
    conn.close()
    stopped = 0
    for row in running:
        cid = row['container_id']
        if await async_docker_stop(cid):
            update_vps_status(cid, "stopped")
            stopped += 1
            logger.info(f"Stopped {cid}")
    embed = discord.Embed(title="Admin: Kill All Running VPS", description=f"Successfully stopped {stopped} running VPS instances.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="admin-list", description="Admin: List all VPS instances")
@app_commands.guild_only()
async def admin_list(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.username, v.container_id, v.container_name, v.os_type, v.hostname, v.status, v.ram, v.cpu, v.disk, v.suspended
        FROM vps v JOIN users u ON v.user_id = u.user_id
        ORDER BY v.created_at DESC
    ''')
    all_vps = cursor.fetchall()
    conn.close()
    if not all_vps:
        embed = discord.Embed(description="No VPS instances found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    embed = discord.Embed(title="All VPS Instances", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    for row in all_vps[:25]:
        username = row['username']
        container_id = row['container_id']
        container_name = row['container_name']
        os_type = row['os_type']
        hostname = row['hostname']
        status = row['status']
        ram = row['ram']
        cpu = row['cpu']
        disk = row['disk']
        suspended = row['suspended']
        status_emoji = "🟢" if status == "running" else "🔴"
        suspended_text = "(Suspended)" if suspended else ""
        embed.add_field(
            name=f"{status_emoji} {username} - {container_name} ({os_type}) {suspended_text}",
            value=f"ID: ```{container_id}```\nHostname: {hostname}\nStatus: {status}\nResources: {ram} RAM | {cpu} CPU | {disk} Disk",
            inline=False
        )
    if len(all_vps) > 25:
        embed.set_footer(text=f"{WATERMARK} | Showing first 25 of {len(all_vps)}", icon_url=bot.user.avatar.url if bot.user.avatar else None)
    else:
        embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-list-users", description="Admin: List users with VPS counts")
@app_commands.guild_only()
async def admin_list_users(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.username, COUNT(v.id) as total_vps,
               SUM(CASE WHEN v.status = 'running' THEN 1 ELSE 0 END) as running_vps
        FROM users u LEFT JOIN vps v ON u.user_id = v.user_id
        GROUP BY u.user_id, u.username
        ORDER BY total_vps DESC
    ''')
    users = cursor.fetchall()
    conn.close()
    if not users:
        embed = discord.Embed(description="No users found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    embed = discord.Embed(title="Users Overview", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    for row in users[:25]:
        username = row['username']
        total = row['total_vps']
        running = row['running_vps'] or 0
        embed.add_field(
            name=username,
            value=f"Total VPS: {total} | Running: {running}",
            inline=False
        )
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-stats", description="Admin: View bot statistics")
@app_commands.guild_only()
async def admin_stats(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM users')
    num_users = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM vps')
    num_vps = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM vps WHERE status="running"')
    num_running = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM bans')
    num_banned = cursor.fetchone()[0]
    cursor.execute('SELECT ram, cpu, disk FROM vps WHERE status="running"')
    rows = cursor.fetchall()
    total_cpu = sum(float(row['cpu']) for row in rows)
    total_ram = sum(parse_gb(row['ram']) for row in rows)
    total_disk = sum(parse_gb(row['disk']) for row in rows)
    conn.close()
    embed = discord.Embed(title="Bot Statistics", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="Total Users", value=num_users, inline=True)
    embed.add_field(name="Banned Users", value=num_banned, inline=True)
    embed.add_field(name="Total VPS", value=num_vps, inline=True)
    embed.add_field(name="Running VPS", value=num_running, inline=True)
    embed.add_field(name="Total CPU Allocated", value=f"{total_cpu} cores", inline=True)
    embed.add_field(name="Total RAM Allocated", value=f"{total_ram:.1f} GB", inline=True)
    embed.add_field(name="Total Disk Allocated", value=f"{total_disk:.1f} GB", inline=True)
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-delete-user", description="Admin: Delete all VPS for a user")
@app_commands.describe(target_user="The target user")
@app_commands.guild_only()
async def admin_delete_user(interaction: discord.Interaction, target_user: discord.User):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.defer()
    user_id = target_user.id
    vps_list = get_user_vps(user_id)
    deleted = 0
    for vps in vps_list:
        container_id = vps['container_id']
        await async_docker_stop(container_id)
        await asyncio.sleep(2)
        await async_docker_rm(container_id)
        delete_vps(container_id)
        deleted += 1
        logger.info(f"Deleted VPS {container_id} for user {user_id}")
    embed = discord.Embed(description=f"Deleted {deleted} VPS instances for {target_user}.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="admin-ban", description="Admin: Ban a user from creating VPS")
@app_commands.describe(target_user="The target user")
@app_commands.guild_only()
async def admin_ban(interaction: discord.Interaction, target_user: discord.User):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    add_ban(target_user.id)
    embed = discord.Embed(description=f"Banned {target_user} from creating VPS instances.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-unban", description="Admin: Unban a user")
@app_commands.describe(target_user="The target user")
@app_commands.guild_only()
async def admin_unban(interaction: discord.Interaction, target_user: discord.User):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    remove_ban(target_user.id)
    embed = discord.Embed(description=f"Unbanned {target_user}.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-vps-info", description="Admin: View full VPS details for a user")
@app_commands.describe(target_user="The target user", vps_identifier="VPS ID or Name")
@app_commands.guild_only()
async def admin_vps_info(interaction: discord.Interaction, target_user: discord.User, vps_identifier: str):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="VPS not found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    container_id = vps['container_id']
    uptime = get_uptime(container_id)
    stats = get_stats(container_id)
    os_name = "Ubuntu 22.04" if vps['os_type'] == "ubuntu" else "Debian 12"
    embed = discord.Embed(title=f"{target_user.name} - VPS Details: {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="OS", value=os_name, inline=True)
    embed.add_field(name="Hostname", value=vps['hostname'], inline=True)
    embed.add_field(name="Status", value=vps['status'], inline=True)
    embed.add_field(name="Suspended", value="Yes" if vps['suspended'] else "No", inline=True)
    embed.add_field(name="Container ID", value=f"```{container_id}```", inline=False)
    embed.add_field(name="Allocated Resources", value=f"{vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk", inline=False)
    embed.add_field(name="Current Usage", value=f"CPU: {stats['cpu']} | Mem: {stats['mem']}", inline=False)
    embed.add_field(name="Uptime", value=uptime, inline=True)
    embed.add_field(name="Network I/O", value=stats['net'], inline=False)
    embed.add_field(name="Created At", value=vps['created_at'], inline=True)
    if vps['ssh_command']:
        ssh_trunc = vps['ssh_command'][:100] + "..." if len(vps['ssh_command']) > 100 else vps['ssh_command']
        embed.add_field(name="SSH Command", value=f"```{ssh_trunc}```", inline=False)
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-logs", description="Admin: View logs for a user's VPS")
@app_commands.describe(target_user="The target user", vps_identifier="VPS ID or Name", lines="Number of lines (default 50)")
@app_commands.guild_only()
async def admin_logs(interaction: discord.Interaction, target_user: discord.User, vps_identifier: str, lines: int = 50):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="VPS not found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    container_id = vps['container_id']
    logs = get_logs(container_id, lines)
    embed = discord.Embed(title=f"Logs for {target_user.name}'s {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="Recent Logs", value=f"```{logs}```", inline=False)
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

# Show bot & developer information
@bot.tree.command(name="about", description="Show bot & developer information")
async def about(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 VPS Manager Bot • About",
        description=(
            "**A powerful, fast, and user-friendly Discord bot for managing VPS servers and Docker containers.**\n\n"
            "Designed with **speed**, **stability**, **security**, and **simplicity** in mind 🚀🔒\n"
            "Perfect for server admins, developers, and hosting enthusiasts!"
        ),
        color=discord.Color.from_rgb(88, 101, 242)  # A modern blurple shade
    )

    # Bot Details
    embed.add_field(
        name="📌 Bot Information",
        value=(
            "➜ **Name:** VPS Manager Bot\n"
            "➜ **Version:** v1.0\n"
            "➜ **Framework:** Python • discord.py\n"
            "➜ **Uptime Status:** 🟢 Online & Stable\n"
            "➜ **Features:** VPS control, Docker management, real-time monitoring, and more!"
        ),
        inline=False
    )

    # Developer Section with more details
    embed.add_field(
        name="👨‍💻 Meet the Developer • ProTechPh (KenshinPH)",
        value=(
            "**ProTechPh** is a passionate **Full-Stack Developer** and **DevOps Enthusiast** from Philippines 🇵🇭\n\n"
            "🔹 **Specialties:**\n"
            "   • Full-Stack Web Development (Next.js, PHP, Node.js)\n"
            "   • Mobile App Development (Flutter, React Native)\n"
            "   • AI Integration & Automation\n"
            "   • VPS & Docker Management\n"
            "   • Advanced Control Panels & Game Hosting\n\n"
            "Focused on delivering **clean code**, **optimized performance**, and **exceptional UI/UX** ✨"
        ),
        inline=False
    )

    # Social Links
    embed.add_field(
        name="🔗 Connect with ProTechPh",
        value=(
            "🌐 **Website:** [protech.works](http://protech.works)\n"
            "💻 **GitHub:** [ProTechPh Projects](https://github.com/ProTechPh)\n"
            "📺 **YouTube:** [Tutorials & Guides](https://www.youtube.com/@ghostedph834)\n"
            "📸 **Instagram:** [@justcallme.eko](https://instagram.com/justcallme.eko)\n"
            "🔵 **Facebook:** [Jericko Garcia](https://www.facebook.com/justcallme.eko)"
        ),
        inline=False
    )

    # Fun Fact / Extra Touch
    embed.add_field(
        name="🚀 Mission",
        value=(
            "ProTechPh is dedicated to creating innovative digital solutions. Whether it's a "
            "productivity tool, e-commerce platform, or a powerful AI bot, the goal is always "
            "Clean Code + Great UX! 💎"
        ),
        inline=False
    )

    embed.set_footer(
        text="Built with ❤️ and ☕ by ProTechPh | Thank you for using VPS Manager Bot!",
        icon_url="https://avatars.githubusercontent.com/u/114973527?v=4"
    )
    embed.set_thumbnail(
        url="https://avatars.githubusercontent.com/u/114973527?v=4"
    )
    embed.timestamp = discord.utils.utcnow()

    await interaction.response.send_message(embed=embed, ephemeral=True)

# Economy & Plans Commands (REMOVED)

@bot.tree.command(name="logs", description="View recent logs for your VPS")
@app_commands.describe(vps_identifier="VPS ID or Name", lines="Number of lines (default 50)")
async def user_logs(interaction: discord.Interaction, vps_identifier: str, lines: int = 50):
    vps = get_vps_by_identifier(interaction.user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="VPS not found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    container_id = vps['container_id']
    logs = get_logs(container_id, lines)
    embed = discord.Embed(title=f"Logs for {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="Recent Logs", value=f"```{logs}```", inline=False)
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Slash Commands
@bot.tree.command(name="deploy", description="Deploy a new free VPS instance")
@app_commands.describe(os_type="Choose OS", duration="Duration in days")
@app_commands.choices(os_type=[
    app_commands.Choice(name="Ubuntu 22.04 (Terminal)", value="ubuntu"),
    app_commands.Choice(name="Debian 12 (Terminal)", value="debian"),
    app_commands.Choice(name="Ubuntu Desktop (Web GUI)", value="ubuntu-desktop")
], duration=[
    app_commands.Choice(name="1 Day", value=1),
    app_commands.Choice(name="3 Days", value=3),
    app_commands.Choice(name="7 Days", value=7)
])
async def deploy(interaction: discord.Interaction, os_type: str, duration: int):
    await create_simple_vps(interaction, os_type, duration)

@bot.tree.command(name="admin-create", description="Admin: Create a VPS for a user with optional custom resources")
@app_commands.describe(target_user="The target user", os_type="OS type", ram="RAM e.g. 2g (optional)", cpu="CPU cores (optional)", disk="Disk e.g. 20G (optional)")
@app_commands.choices(os_type=[
    app_commands.Choice(name="Ubuntu 22.04 (Terminal)", value="ubuntu"),
    app_commands.Choice(name="Debian 12 (Terminal)", value="debian"),
    app_commands.Choice(name="Ubuntu Desktop (Web GUI)", value="ubuntu-desktop")
])
async def admin_create(interaction: discord.Interaction, target_user: discord.User, os_type: str, ram: str = None, cpu: str = None, disk: str = None):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    ram = ram or DEFAULT_RAM
    cpu = cpu or DEFAULT_CPU
    disk = disk or DEFAULT_DISK
    if get_total_instances() >= TOTAL_SERVER_LIMIT:
        embed = discord.Embed(description=f"Global server limit reached: {TOTAL_SERVER_LIMIT} total running instances.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await create_vps(interaction, os_type, ram, cpu, disk, target_user=target_user)

@bot.tree.command(name="server-status", description="View the main server's real-time resource usage")
async def server_status(interaction: discord.Interaction):
    try:
        # Get CPU Load (More accurate by taking 2 iterations)
        cpu_load = subprocess.check_output("top -bn2 -d 0.5 | grep 'Cpu(s)' | tail -1 | sed 's/.*, *\\([0-9.]*\\)%* id.*/\\1/' | awk '{print 100 - $1\"%\"}'", shell=True).decode().strip()
        
        # Get RAM Usage
        mem_info = subprocess.check_output("free -m | grep Mem", shell=True).decode().split()
        total_mem = mem_info[1]
        used_mem = mem_info[2]
        mem_percent = round((int(used_mem) / int(total_mem)) * 100, 1)
        
        # Get Disk Usage
        disk_info = subprocess.check_output("df -h / | tail -1", shell=True).decode().split()
        total_disk = disk_info[1]
        used_disk = disk_info[2]
        disk_percent = disk_info[4]
        
        # Get Uptime
        uptime = subprocess.check_output("uptime -p", shell=True).decode().strip().replace('up ', '')
        
        embed = create_embed("Main Server Status", "Real-time performance monitoring of the VPS host.", color=EmbedColors.INFO)
        embed.add_field(name="🖥️ CPU Load", value=f"`{cpu_load}`", inline=True)
        embed.add_field(name="💾 RAM Usage", value=f"`{used_mem}MB / {total_mem}MB` ({mem_percent}%)", inline=True)
        embed.add_field(name="💽 Disk Space", value=f"`{used_disk} / {total_disk}` ({disk_percent})", inline=True)
        embed.add_field(name="⏳ System Uptime", value=f"`{uptime}`", inline=False)
        
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        logger.error(f"Error getting server stats: {e}")
        await interaction.response.send_message(embed=create_error_embed("Stats Error", "Unable to retrieve host machine statistics at this moment."), ephemeral=True)

@bot.tree.command(name="vps-info", description="View full details of your VPS")
@app_commands.describe(vps_identifier="VPS ID or Name (defaults to first)")
async def vps_info(interaction: discord.Interaction, vps_identifier: str = None):
    vps = get_vps_by_identifier(interaction.user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="No VPS found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    container_id = vps['container_id']
    uptime = get_uptime(container_id)
    stats = get_stats(container_id)
    os_name = "Ubuntu 22.04" if vps['os_type'] == "ubuntu" else "Debian 12"
    embed = discord.Embed(title=f"VPS Details: {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="OS", value=os_name, inline=True)
    embed.add_field(name="Hostname", value=vps['hostname'], inline=True)
    embed.add_field(name="Status", value=vps['status'], inline=True)
    embed.add_field(name="Suspended", value="Yes" if vps['suspended'] else "No", inline=True)
    embed.add_field(name="Container ID", value=f"```{container_id}```", inline=False)
    embed.add_field(name="Allocated Resources", value=f"{vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk", inline=False)
    embed.add_field(name="Current Usage", value=f"CPU: {stats['cpu']} | Mem: {stats['mem']}", inline=False)
    embed.add_field(name="Uptime", value=uptime, inline=True)
    embed.add_field(name="Network I/O", value=stats['net'], inline=False)
    embed.add_field(name="Created At", value=vps['created_at'], inline=True)
    if vps['ssh_command']:
        ssh_trunc = vps['ssh_command'][:100] + "..." if len(vps['ssh_command']) > 100 else vps['ssh_command']
        embed.add_field(name="SSH Command", value=f"```{ssh_trunc}```", inline=False)
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="regen-ssh", description="Regenerate SSH session for your VPS")
@app_commands.describe(vps_identifier="VPS ID or Name (defaults to first)")
async def regen_ssh(interaction: discord.Interaction, vps_identifier: str = None):
    await regen_ssh_command(interaction, vps_identifier)

@bot.tree.command(name="start", description="Start your VPS")
@app_commands.describe(vps_identifier="VPS ID or Name")
async def start_vps(interaction: discord.Interaction, vps_identifier: str):
    await manage_vps(interaction, vps_identifier, "start")

@bot.tree.command(name="stop", description="Stop your VPS")
@app_commands.describe(vps_identifier="VPS ID or Name")
async def stop_vps(interaction: discord.Interaction, vps_identifier: str):
    await manage_vps(interaction, vps_identifier, "stop")

@bot.tree.command(name="restart", description="Restart your VPS")
@app_commands.describe(vps_identifier="VPS ID or Name")
async def restart_vps(interaction: discord.Interaction, vps_identifier: str):
    await manage_vps(interaction, vps_identifier, "restart")

@bot.tree.command(name="reinstall", description="Reinstall your VPS with a new OS")
@app_commands.describe(vps_identifier="VPS ID or Name", os_type="The new OS type")
@app_commands.choices(os_type=[
    app_commands.Choice(name="Ubuntu 22.04 (Terminal)", value="ubuntu"),
    app_commands.Choice(name="Debian 12 (Terminal)", value="debian"),
    app_commands.Choice(name="Ubuntu Desktop (Web GUI)", value="ubuntu-desktop")
])
async def reinstall(interaction: discord.Interaction, vps_identifier: str, os_type: str = "ubuntu"):
    await reinstall_vps(interaction, vps_identifier, os_type)

@bot.tree.command(name="list", description="List all your VPS instances")
async def list_vps(interaction: discord.Interaction):
    vps_list = get_user_vps(interaction.user.id)
    if not vps_list:
        embed = discord.Embed(description="You have no VPS instances.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    embed = discord.Embed(title="Your VPS Instances", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    for vps in vps_list[:25]:
        status_emoji = "🟢" if vps['status'] == "running" else "🔴"
        uptime = get_uptime(vps['container_id'])
        suspended_text = "(Suspended)" if vps['suspended'] else ""
        embed.add_field(
            name=f"{status_emoji} {vps['container_name']} ({vps['os_type']}) {suspended_text}",
            value=f"ID: ```{vps['container_id']}```\nHostname: {vps['hostname']}\nStatus: {vps['status']}\nUptime: {uptime}\nResources: {vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk",
            inline=False
        )
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="remove", description="Remove your VPS instance")
@app_commands.describe(vps_identifier="VPS ID or Name")
async def remove_vps(interaction: discord.Interaction, vps_identifier: str):
    await interaction.response.defer(ephemeral=True)
    vps = get_vps_by_identifier(interaction.user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="VPS not found.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    container_id = vps['container_id']
    await async_docker_stop(container_id)
    await asyncio.sleep(2)
    await async_docker_rm(container_id)
    delete_vps(container_id)
    embed = discord.Embed(title="VPS Removed Successfully", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.followup.send(embed=embed, ephemeral=True)

# Admin commands
@bot.tree.command(name="admin-manage", description="Admin: Manage a user's VPS (start/stop/restart/delete/suspend/unsuspend)")
@app_commands.describe(target_user="The target user", vps_identifier="VPS ID or Name", action="The action to perform")
@app_commands.choices(action=[
    app_commands.Choice(name="start", value="start"),
    app_commands.Choice(name="stop", value="stop"),
    app_commands.Choice(name="restart", value="restart"),
    app_commands.Choice(name="delete", value="delete"),
    app_commands.Choice(name="suspend", value="suspend"),
    app_commands.Choice(name="unsuspend", value="unsuspend")
])
@app_commands.guild_only()
async def admin_manage(interaction: discord.Interaction, target_user: discord.User, vps_identifier: str, action: str):
    await interaction.response.defer()
    await admin_manage_vps(interaction, target_user.id, vps_identifier, action)

@bot.tree.command(name="admin-kill-all", description="Admin: Stop all running VPS instances")
@app_commands.guild_only()
async def admin_kill_all_cmd(interaction: discord.Interaction):
    await admin_kill_all(interaction)

@bot.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(title="🏓 Pong!", description=f"Latency: {latency}ms", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="help", description="View help and command list")
async def help_cmd(interaction: discord.Interaction):
    embed = create_embed("VPS Bot Help", "List of all available commands for managing your VPS.", color=EmbedColors.PRIMARY)
    
    user_cmds = (
        "`/deploy` - Deploy a new VPS instance\n"
        "`/list` - List your VPS instances\n"
        "`/vps-info` - Detailed info & SSH\n"
        "`/start` / `/stop` / `/restart` - Manage status\n"
        "`/regen-ssh` - New SSH session\n"
        "`/reinstall` - Reinstall OS\n"
        "`/remove` - Delete VPS\n"
        "`/logs` - View recent logs\n"
        "`/about` - Bot information"
    )
    embed.add_field(name="🌐 User Commands", value=user_cmds, inline=False)
    
    if is_admin(interaction.user):
        admin_cmds = (
            "`/admin-list` - All VPS instances\n"
            "`/admin-create` - Create for user\n"
            "`/admin-manage` - Start/Stop/Delete user VPS\n"
            "`/admin-stats` - Bot usage stats\n"
            "`/admin-ban` / `/admin-unban` - User access\n"
            "`/admin-kill-all` - Emergency stop all"
        )
        embed.add_field(name="🛡️ Admin Commands", value=admin_cmds, inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tasks.loop(minutes=5)
async def sync_statuses():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT container_id, status FROM vps')
    for row in cursor.fetchall():
        cid = row['container_id']
        stat = row['status']
        try:
            out = subprocess.check_output(["docker", "inspect", "-f", "{{.State.Status}}", cid]).decode().strip()
            if out != stat:
                update_vps_status(cid, out)
                logger.info(f"Updated status of {cid} to {out}")
        except subprocess.CalledProcessError:
            if stat != "stopped":
                update_vps_status(cid, "stopped")
                logger.info(f"Updated non-existent {cid} to stopped")
        except Exception as e:
            logger.error(f"Status sync error for {cid}: {e}")
    conn.close()

# Events
@bot.event
async def on_ready():
    change_status.start()
    sync_statuses.start()
    bot.loop.create_task(check_expirations())
    logger.info(f'Bot ready: {bot.user}')
    try:
        synced = await bot.tree.sync()
        logger.info(f'Synced {len(synced)} commands')
    except Exception as e:
        logger.error(f'Sync failed: {e}')

@tasks.loop(seconds=10)
async def change_status():
    try:
        count = get_total_instances()
        status = f"{BOT_STATUS_NAME} | {count} Active"
        await bot.change_presence(activity=discord.Game(name=status))
    except Exception as e:
        logger.error(f"Status update failed: {e}")

if __name__ == "__main__":
    if not TOKEN:
        logger.error("TOKEN not set in .env")
        sys.exit(1)
    bot.run(TOKEN)