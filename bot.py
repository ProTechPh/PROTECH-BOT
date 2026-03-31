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

# Load environment
load_dotenv()

# Configuration
TOKEN = os.getenv('TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
RAILWAY_API_TOKEN = os.getenv('RAILWAY_API_TOKEN')
RAILWAY_PROJECT_ID = os.getenv('RAILWAY_PROJECT_ID')
RAILWAY_API_URL = os.getenv('RAILWAY_API_URL', 'https://backboard.railway.app/graphql/v2')

DEFAULT_RAM = os.getenv('DEFAULT_RAM', '2g')
DEFAULT_CPU = os.getenv('DEFAULT_CPU', '1')
DEFAULT_DISK = os.getenv('DEFAULT_DISK', '10g')
BOT_STATUS_NAME = os.getenv('BOT_STATUS_NAME', 'ProTechPh VPS')
WATERMARK = os.getenv('WATERMARK', 'Powered by ProTechPh VPS Bot')
SERVER_LIMIT = int(os.getenv('SERVER_LIMIT', 1))
VPS_HOSTNAME = os.getenv('VPS_HOSTNAME', 'vps-host')

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Intents
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# DB Setup
def get_db_connection():
    conn = sqlite3.connect('vps_database.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS vps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        container_id TEXT,
        container_name TEXT,
        os_type TEXT,
        hostname TEXT,
        status TEXT,
        ram TEXT,
        cpu TEXT,
        disk TEXT,
        created_at TEXT,
        expires_at TEXT,
        ssh_command TEXT,
        suspended INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bans (user_id INTEGER PRIMARY KEY)''')
    conn.commit()
    conn.close()

init_db()

# Railway API Helper
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
        mutation = """
        mutation serviceDomainCreate($input: ServiceDomainCreateInput!) {
          serviceDomainCreate(input: $input) {
            domain
          }
        }
        """
        variables = {"input": {"environmentId": environment_id, "serviceId": service_id}}
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
            return res["data"]["environments"]["edges"][0]["node"]["id"]
        except:
            return None

    @staticmethod
    def set_service_variable(service_id, environment_id, name, value):
        mutation = """
        mutation variableUpsert($input: VariableUpsertInput!) {
          variableUpsert(input: $input)
        }
        """
        variables = {
            "input": {
                "projectId": RAILWAY_PROJECT_ID,
                "environmentId": environment_id,
                "serviceId": service_id,
                "name": name,
                "value": value
            }
        }
        res = RailwayAPI.query(mutation, variables)
        return res and "data" in res and res["data"]["variableUpsert"]

# DB Queries
def add_vps(user_id, container_id, container_name, os_type, hostname, ssh, ram, cpu, disk, days=1):
    conn = get_db_connection()
    c = conn.cursor()
    created_at = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    c.execute('''INSERT INTO vps (user_id, container_id, container_name, os_type, hostname, status, ram, cpu, disk, created_at, expires_at, ssh_command)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (user_id, container_id, container_name, os_type, hostname, 'running', ram, cpu, disk, created_at, expires_at, ssh))
    conn.commit()
    conn.close()

def get_user_vps(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM vps WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_vps_by_identifier(user_id, identifier):
    vps_list = get_user_vps(user_id)
    if not vps_list: return None
    if identifier is None: return vps_list[0]
    for vps in vps_list:
        if vps['container_id'] == identifier or vps['container_name'] == identifier:
            return vps
    return None

def delete_vps(container_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM vps WHERE container_id = ?", (container_id,))
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM bans WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res is not None

def add_ban(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO bans (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def remove_ban(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# Bot Commands
@bot.tree.command(name="deploy", description="🚀 Deploy your dedicated Ubuntu Desktop VPS.")
@app_commands.describe(duration="Duration in days")
@app_commands.choices(duration=[
    app_commands.Choice(name="1 Day", value=1),
    app_commands.Choice(name="3 Days", value=3),
    app_commands.Choice(name="7 Days", value=7)
])
async def deploy(interaction: discord.Interaction, duration: int = 1):
    if is_banned(interaction.user.id):
        await interaction.response.send_message("❌ You are banned from using this bot.", ephemeral=True)
        return

    vps_list = get_user_vps(interaction.user.id)
    if len(vps_list) >= SERVER_LIMIT and interaction.user.id != ADMIN_ID:
        await interaction.response.send_message(f"❌ Limit reached! You can only have {SERVER_LIMIT} VPS.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    
    container_name = f"{VPS_HOSTNAME}-{interaction.user.id}-{random.randint(1000, 9999)}"
    service_id = RailwayAPI.create_service(container_name, "ubuntu-desktop")
    
    if not service_id:
        await interaction.followup.send("❌ Failed to create service in Railway.", ephemeral=True)
        return
        
    env_id = RailwayAPI.get_environment_id()
    if env_id:
        RailwayAPI.set_service_variable(service_id, env_id, "PORT", "6080")
        
    await asyncio.sleep(5)
    
    access_line = "Creating domain..."
    if env_id:
        domain = RailwayAPI.create_domain(service_id, env_id)
        if domain:
            access_line = f"https://{domain}/vnc.html"
            
    add_vps(interaction.user.id, service_id, container_name, "ubuntu-desktop", container_name, access_line, DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK, duration)
    
    embed = discord.Embed(
        title="✓ Desktop VPS Ready!",
        description=f"OS: Ubuntu Desktop\nDuration: **{duration} days**\nWeb Access Link:\n```{access_line}```",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    await interaction.followup.send(embed=embed, ephemeral=True)
    try:
        await interaction.user.send(embed=embed)
    except:
        pass

@bot.tree.command(name="status", description="📊 Check your Desktop VPS status.")
async def status(interaction: discord.Interaction):
    vps_list = get_user_vps(interaction.user.id)
    if not vps_list:
        await interaction.response.send_message("❌ No active VPS found.", ephemeral=True)
        return
        
    vps = vps_list[0]
    embed = discord.Embed(title="VPS Status", color=discord.Color.blue())
    embed.add_field(name="Name", value=vps['container_name'], inline=True)
    embed.add_field(name="OS", value="Ubuntu Desktop", inline=True)
    embed.add_field(name="Status", value="🟢 Online", inline=True)
    embed.add_field(name="Access Link", value=f"```{vps['ssh_command']}```", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="remove", description="🗑️ Stop and delete your VPS.")
async def remove(interaction: discord.Interaction):
    vps_list = get_user_vps(interaction.user.id)
    if not vps_list:
        await interaction.response.send_message("❌ No VPS to remove.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=True)
    vps = vps_list[0]
    RailwayAPI.delete_service(vps['container_id'])
    delete_vps(vps['container_id'])
    await interaction.followup.send("✅ VPS removed successfully.", ephemeral=True)

@bot.tree.command(name="about", description="ℹ️ About this bot.")
async def about(interaction: discord.Interaction):
    embed = discord.Embed(title="🤖 ProTechPh VPS Bot", description="Cloud-native Desktop VPS management via Railway API.", color=discord.Color.blue())
    embed.set_footer(text=WATERMARK)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Admin Commands
@bot.tree.command(name="admin-list", description="👑 Admin: List all VPS.")
async def admin_list(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID: return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM vps")
    rows = c.fetchall()
    conn.close()
    embed = discord.Embed(title="Global VPS List", color=discord.Color.gold())
    for r in rows:
        embed.add_field(name=f"User: {r['user_id']}", value=f"Name: {r['container_name']}\nID: `{r['container_id']}`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="admin-kill-all", description="👑 Admin: Cleanup all VPS.")
async def kill_all(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID: return
    await interaction.response.defer()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT container_id FROM vps")
    rows = c.fetchall()
    for r in rows:
        RailwayAPI.delete_service(r['container_id'])
    c.execute("DELETE FROM vps")
    conn.commit()
    conn.close()
    await interaction.followup.send("✅ All services purged.")

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user}')
    try:
        await bot.tree.sync()
        logger.info("Synced commands.")
    except Exception as e:
        logger.error(f"Sync failed: {e}")

if __name__ == "__main__":
    bot.run(TOKEN)