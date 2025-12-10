import discord
from discord import app_commands
import asyncio
from datetime import timedelta
import time
import os
import json
from flask import Flask, request, redirect, url_for, render_template_string, session, abort
import threading
from functools import wraps 
import requests # Needed for OAuth API calls

# --- 1. CONFIGURATION ---
# IMPORTANT: Use environment variables for sensitive data in production.
# ------------------------------------------------------------------
BOT_TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = 1448175320531468402 
MOD_ROLE_ID = 1448175664795746398 

# --- DISCORD OAUTH2 CONFIGURATION (MANDATORY FOR DASHBOARD) ---
DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID', '123456789012345678') # Replace with your Discord Application ID
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', 'your_super_secret_client_secret') # Replace with your Discord Application Secret
# IMPORTANT: This User ID must match the Discord ID of the user allowed to access the dashboard.
DASHBOARD_ADMIN_USER_ID = os.getenv('DASHBOARD_ADMIN_USER_ID', '123456789012345678') 
# The port must match the port the Flask app is running on (usually 5000 in environments like this)
REDIRECT_URI = "http://127.0.0.1:5000/oauth_callback" 
DISCORD_API_BASE_URL = 'https://discord.com/api/v10'

# Global variables for in-memory configuration cache
# Cache for persistent settings: {guild_id: {'log_channel_id': int, 'reaction_roles': {message_id: {emoji_name: role_id}}}}
CONFIG_CACHE = {} 

# Define the custom Bot class
class HyperOSBot(discord.Client):
    """The core Discord client for the HyperOS bot with enhanced features."""
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        # Dictionary to track muted users: {user_id: unmute_timestamp (float)}
        self.muted_users = {}

    # --- 2. INITIALIZATION AND CACHE LOADING (DB removed) ---
    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        await self._load_initial_config()

        try:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Synced commands to HyperOS server.")
        except Exception as e:
            print(f"Error syncing commands: {e}")

    async def _load_initial_config(self):
        """Initializes in-memory configuration cache with defaults."""
        print("Initializing in-memory configuration cache.")
        # Initialize cache with defaults since no persistent storage is used
        CONFIG_CACHE[GUILD_ID] = {
            'log_channel_id': None, 
            'reaction_roles': {} # {message_id: {emoji_name: role_id}}
        }
        print("✅ Configuration cache ready.")

    # --- 3. HELPER FUNCTIONS ---

    def is_moderator(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild: return False
        mod_role = interaction.guild.get_role(MOD_ROLE_ID)
        return mod_role in interaction.user.roles

    def get_log_channel(self) -> discord.TextChannel | None:
        """Retrieves the configured log channel object."""
        config = CONFIG_CACHE.get(GUILD_ID)
        if config and config['log_channel_id']:
            # The channel ID is stored as an integer, need to cast for self.get_channel
            return self.get_channel(int(config['log_channel_id']))
        return None

    def duration_to_seconds(self, duration: int, unit: str) -> int:
        if unit == 'minutes': return duration * 60
        elif unit == 'hours': return duration * 3600
        elif unit == 'days': return duration * 86400
        return 0

    # --- 4. EVENT LISTENERS (Logging and Reaction Roles) ---

    async def _send_log_embed(self, embed: discord.Embed):
        """Sends an embed to the configured moderation log channel."""
        log_channel = self.get_log_channel()
        if log_channel:
            try:
                await log_channel.send(embed=embed)
            except discord.Forbidden:
                print(f"Error: Cannot send message to log channel {log_channel.name}")
            except Exception as e:
                print(f"Error sending log embed: {e}")

    async def on_message_delete(self, message: discord.Message):
        """Logs message deletions."""
        if message.author.bot or not message.guild or message.guild.id != GUILD_ID:
            return
            
        embed = discord.Embed(
            title="🗑️ Message Deleted",
            description=f"Message by {message.author.mention} deleted in {message.channel.mention}",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="User", value=f"{message.author.name} ({message.author.id})", inline=True)
        embed.add_field(name="Channel", value=message.channel.name, inline=True)
        # Truncate content if too long for embed field
        content = message.content or "*No content (e.g., embed only)*"
        embed.add_field(name="Content", value=content[:1024], inline=False)
        
        await self._send_log_embed(embed)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Logs message edits."""
        if before.author.bot or not before.guild or before.guild.id != GUILD_ID or before.content == after.content:
            return

        embed = discord.Embed(
            title="📝 Message Edited",
            description=f"Message edited by {before.author.mention} in {before.channel.mention}",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="User", value=f"{before.author.name} ({before.author.id})", inline=True)
        embed.add_field(name="Channel", value=before.channel.name, inline=True)
        embed.add_field(name="Before", value=before.content[:1024], inline=False)
        embed.add_field(name="After", value=after.content[:1024], inline=False)
        
        await self._send_log_embed(embed)
    
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handles adding roles via Reaction Roles."""
        if payload.guild_id != GUILD_ID or payload.member.bot:
            return

        rr_config = CONFIG_CACHE.get(GUILD_ID, {}).get('reaction_roles', {})
        
        if str(payload.message_id) in rr_config:
            # Get the mapping for this specific reaction role message
            message_map = rr_config[str(payload.message_id)]
            
            # Determine the emoji identifier (custom vs. standard)
            emoji_id = str(payload.emoji)
            
            if emoji_id in message_map:
                role_id = message_map[emoji_id]
                guild = self.get_guild(payload.guild_id)
                role = guild.get_role(int(role_id))
                
                if role and payload.member:
                    try:
                        await payload.member.add_roles(role)
                        print(f"Assigned role {role.name} to {payload.member.name} via RR.")
                    except discord.Forbidden:
                        print(f"Error: Missing permissions to add role {role.name}")
                    except Exception as e:
                        print(f"Error adding role: {e}")

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Handles removing roles via Reaction Roles."""
        if payload.guild_id != GUILD_ID:
            return

        rr_config = CONFIG_CACHE.get(GUILD_ID, {}).get('reaction_roles', {})

        if str(payload.message_id) in rr_config:
            message_map = rr_config[str(payload.message_id)]
            emoji_id = str(payload.emoji)
            
            if emoji_id in message_map:
                role_id = message_map[emoji_id]
                guild = self.get_guild(payload.guild_id)
                member = guild.get_member(payload.user_id)
                role = guild.get_role(int(role_id))
                
                if role and member:
                    try:
                        await member.remove_roles(role)
                        print(f"Removed role {role.name} from {member.name} via RR.")
                    except discord.Forbidden:
                        print(f"Error: Missing permissions to remove role {role.name}")
                    except Exception as e:
                        print(f"Error removing role: {e}")

    # --- 5. MESSAGE HANDLING (MUTE ENFORCEMENT) ---
    async def on_message(self, message: discord.Message):
        """Checks if a user is muted. If so, silently deletes the message."""
        if message.author.bot or not message.guild:
            return

        unmute_time = self.muted_users.get(message.author.id)

        if unmute_time:
            if time.time() < unmute_time:
                try:
                    await message.delete()
                    print(f"Silently deleted message from muted user: {message.author.name}")
                except discord.Forbidden:
                    print(f"Error: Missing permissions to delete message in {message.channel.name}")
            else:
                if message.author.id in self.muted_users:
                    del self.muted_users[message.author.id]

# Set up required intents
intents = discord.Intents.default()
intents.members = True 
intents.message_content = True 
intents.moderation = True
intents.guild_messages = True
intents.guild_reactions = True # Required for raw reaction events

bot = HyperOSBot(intents=intents)


# --- 6. SLASH COMMANDS (MODERATION & SETUP) ---

# --- LOGGING & SETUP COMMANDS ---

@bot.tree.command(name="setlogchannel", description="Sets the channel where moderation and server logs are posted.")
@app_commands.checks.has_permissions(administrator=True) 
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)

    guild_id = GUILD_ID
    
    try:
        # Update cache only (no database saving)
        CONFIG_CACHE[guild_id]['log_channel_id'] = str(channel.id)

        await interaction.followup.send(f"✅ Log channel successfully set to {channel.mention}. Message edits/deletions will now be logged here (Configuration is not persistent without a database).", ephemeral=False)
    except Exception as e:
        await interaction.followup.send(f"❌ Error setting log channel: {e}", ephemeral=True)


# --- REACTION ROLE COMMANDS ---

@bot.tree.command(name="addreactionrole", description="Adds a reaction role binding to a specific message.")
@app_commands.describe(message_link="Link to the reaction role message", emoji="The emoji to use", role="The role to assign")
@app_commands.checks.has_permissions(administrator=True) 
async def add_reaction_role(interaction: discord.Interaction, message_link: str, emoji: str, role: discord.Role):
    await interaction.response.defer(ephemeral=True)

    try:
        # Extract message ID from link (Discord link format: /channels/guild_id/channel_id/message_id)
        parts = message_link.split('/')
        if len(parts) < 3:
            await interaction.followup.send("❌ Invalid message link format.", ephemeral=True)
            return

        message_id = parts[-1]
        channel_id = parts[-2]
        
        # 1. Check if message exists and react to it
        channel = bot.get_channel(int(channel_id))
        if not channel:
            await interaction.followup.send("❌ Could not find the channel from the link.", ephemeral=True)
            return

        try:
            message = await channel.fetch_message(int(message_id))
            # React to the message with the configured emoji to make it active
            await message.add_reaction(emoji)
        except discord.NotFound:
            await interaction.followup.send("❌ Message not found. Please provide a valid message link.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Could not react with that emoji (check if it's a valid emoji or a custom guild emoji the bot can use). Error: {e}", ephemeral=True)
            return

        # 2. Update cache only (no database saving)
        rr_config = CONFIG_CACHE[GUILD_ID]['reaction_roles']
        if message_id not in rr_config:
            rr_config[message_id] = {}
        rr_config[message_id][emoji] = str(role.id)

        await interaction.followup.send(
            f"✅ Reaction Role set up on message: Reacting with **{emoji}** now grants the **{role.name}** role. (Configuration is not persistent without a database).", 
            ephemeral=False
        )

    except Exception as e:
        await interaction.followup.send(f"❌ An unexpected error occurred: {e}", ephemeral=True)


# --- CORE MODERATION COMMANDS (Remaining commands use in-memory state or Discord API only) ---


@bot.tree.command(name="prune", description="Deletes a specific number of messages from the channel.")
@app_commands.describe(count="Number of messages to delete (max 100)")
@app_commands.checks.has_permissions(manage_messages=True) 
async def prune(interaction: discord.Interaction, count: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)

    if not bot.is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return
    
    try:
        deleted = await interaction.channel.purge(limit=count)
        
        embed = discord.Embed(
            description=f"✅ Successfully deleted **{len(deleted)}** messages.", 
            color=discord.Color.green()
        )
        
        # Send the confirmation ephemerally, and delete it after a few seconds
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Log the action
        log_embed = discord.Embed(
            title="🧹 Messages Pruned",
            description=f"**{len(deleted)}** messages cleared by {interaction.user.mention} in {interaction.channel.mention}",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow()
        )
        await bot._send_log_embed(log_embed)

    except discord.Forbidden:
        await interaction.followup.send("❌ I do not have permissions to manage messages in this channel.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error during pruning: {e}", ephemeral=True)


@bot.tree.command(name="tempmute", description="Mutes a user by deleting their messages (Silent).")
@app_commands.describe(duration="Length of mute", unit="minutes/hours/days", reason="Reason for mute")
@app_commands.checks.has_permissions(moderate_members=True) 
async def temp_mute(interaction: discord.Interaction, member: discord.Member, duration: int, unit: str, reason: str = "No reason provided."):
    await interaction.response.defer(ephemeral=False) 

    if not bot.is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return

    seconds = bot.duration_to_seconds(duration, unit)
    if seconds <= 0:
        await interaction.followup.send("❌ Invalid duration.", ephemeral=True)
        return

    unmute_timestamp = time.time() + seconds
    
    # Store mute in memory for message deletion tracking
    bot.muted_users[member.id] = unmute_timestamp
    
    unmute_dt = discord.utils.format_dt(discord.Object(round(unmute_timestamp)), 'R')
    
    await interaction.followup.send(
        f"🔇 **Mute Action:** {member.mention} has been silenced for **{duration} {unit}** (until {unmute_dt}).\n"
        f"Reason: *{reason}*"
    )
    
    # Log the action
    log_embed = discord.Embed(
        title="🔇 User Muted",
        description=f"{member.mention} was silenced by {interaction.user.mention}.",
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    log_embed.add_field(name="Duration", value=f"{duration} {unit}", inline=True)
    log_embed.add_field(name="Expires", value=unmute_dt, inline=True)
    log_embed.add_field(name="Reason", value=reason, inline=False)
    await bot._send_log_embed(log_embed)


@bot.tree.command(name="unmute", description="Manually unmutes a user (removes silent deletion).")
@app_commands.checks.has_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=False)

    if not bot.is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return

    if member.id in bot.muted_users:
        del bot.muted_users[member.id]
        await interaction.followup.send(f"✅ **Unmute Action:** {member.mention} has been manually unmuted. They can now speak again.")
        
        # Log the action
        log_embed = discord.Embed(
            title="🔊 User Unmuted",
            description=f"{member.mention} was unmuted by {interaction.user.mention}.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        await bot._send_log_embed(log_embed)

    else:
        await interaction.followup.send(f"ℹ️ **{member.display_name}** is not currently muted.", ephemeral=True) 


@bot.tree.command(name="tempban", description="Temporarily bans a user.")
@app_commands.checks.has_permissions(ban_members=True)
async def temp_ban(interaction: discord.Interaction, member: discord.Member, duration: int, unit: str, reason: str = "No reason provided."):
    await interaction.response.defer(ephemeral=False)
    
    if not bot.is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return
    
    seconds = bot.duration_to_seconds(duration, unit)
    
    try:
        await member.ban(reason=f"Temp Ban: {reason} - expires in {duration} {unit}")
        await interaction.followup.send(f"🔨 **Temporary Ban:** {member.display_name} has been banned for {duration} {unit}.\nReason: *{reason}*")
        
        # Log the action
        log_embed = discord.Embed(
            title="🔨 User Temp-Banned",
            description=f"{member.mention} was temporarily banned by {interaction.user.mention}.",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow()
        )
        log_embed.add_field(name="Duration", value=f"{duration} {unit}", inline=True)
        log_embed.add_field(name="Reason", value=reason, inline=False)
        await bot._send_log_embed(log_embed)

        # Unban process in the background
        await asyncio.sleep(seconds)
        user = discord.Object(id=member.id)
        await interaction.guild.unban(user, reason="Temp ban expired")
        
        # Log unban
        log_embed_unban = discord.Embed(
            title="🔓 Temp-Ban Expired",
            description=f"{user.id} has been automatically unbanned.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        await bot._send_log_embed(log_embed_unban)

    except Exception as e:
        await interaction.followup.send(f"Error executing ban: {e}", ephemeral=True) 


@bot.tree.command(name="kick", description="Kicks a user from the server.")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided."):
    await interaction.response.defer(ephemeral=False)
    
    if not bot.is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return
        
    try:
        await member.kick(reason=reason)
        await interaction.followup.send(f"👢 **Kick Action:** {member.display_name} has been kicked from the server.\nReason: *{reason}*")
        
        # Log the action
        log_embed = discord.Embed(
            title="👢 User Kicked",
            description=f"{member.mention} was kicked by {interaction.user.mention}.",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        log_embed.add_field(name="Reason", value=reason, inline=False)
        await bot._send_log_embed(log_embed)

    except Exception as e:
        await interaction.followup.send(f"Error executing kick: {e}", ephemeral=True)


@bot.tree.command(name="warn", description="Issues a formal warning.")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    await interaction.response.defer(ephemeral=False)

    if not bot.is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return
    
    await interaction.followup.send(f"⚠️ **Warning Issued:** {member.mention} has received a formal warning.\nReason: *{reason}*")
    
    # Log the action
    log_embed = discord.Embed(
        title="⚠️ Formal Warning",
        description=f"Warning issued to {member.mention} by {interaction.user.mention}.",
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow()
    )
    log_embed.add_field(name="Reason", value=reason, inline=False)
    await bot._send_log_embed(log_embed)


@bot.tree.command(name="addrole", description="Assigns a role to a member.")
@app_commands.checks.has_permissions(manage_roles=True)
async def add_dynamic_role(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    await interaction.response.defer(ephemeral=False)

    if not bot.is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return
        
    try:
        await member.add_roles(role)
        await interaction.followup.send(f"✨ **Role Assignment:** Granted **{role.name}** role to {member.mention}.")
    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True) 


@bot.tree.command(name="sendasbot", description="Sends a message in the current channel as the bot.")
@app_commands.describe(message="The message content to send.")
async def send_as_bot(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=True) 

    if not bot.is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return

    try:
        await interaction.channel.send(message)
        await interaction.followup.send("✅ Message sent successfully, utilizing Discord's 15-minute follow-up window.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error sending message: {e}", ephemeral=True)

# --- RENDER HEALTH CHECK AND WEB DASHBOARD ---

# Flask app setup
app = Flask(__name__)

# Function to check for authentication status
def login_required(f):
    """Decorator to ensure user is logged in via Discord OAuth2 and authorized."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if user ID is in session and matches the admin ID
        if 'discord_user_id' not in session or str(session['discord_user_id']) != DASHBOARD_ADMIN_USER_ID:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# --- TEMPLATES ---

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bot Login</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
        body { font-family: 'Inter', sans-serif; }
    </style>
</head>
<body class="bg-gray-900 text-white min-h-screen flex items-center justify-center p-4">
    <div class="w-full max-w-md bg-gray-800 p-8 rounded-xl shadow-2xl border border-indigo-700">
        <h1 class="text-3xl font-bold mb-6 text-center text-indigo-400">HyperOS Dashboard Login</h1>
        
        {% if error %}
        <div class="bg-red-900 border border-red-700 text-white p-3 rounded-lg mb-4 text-sm">
            {{ error }}
        </div>
        {% endif %}

        <p class="text-gray-400 mb-6 text-center">
            You must log in with the Discord account matching the configured Admin User ID ({{ admin_id }}).
        </p>

        <a href="{{ oauth_url }}" class="flex items-center justify-center w-full bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-2.5 px-4 rounded-lg transition duration-300 shadow-md hover:shadow-lg">
            <svg class="w-6 h-6 mr-3" fill="currentColor" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">
                <path d="M19.1 1.0H.9C.5 1.0.1 1.4.1 1.8v16.4c0 .4.4.8.8.8h18.2c.4 0 .8-.4.8-.8V1.8c0-.4-.4-.8-.8-.8zM6.9 14.1c-1.5 0-2.8-1.3-2.8-2.8s1.3-2.8 2.8-2.8 2.8 1.3 2.8 2.8-1.3 2.8-2.8 2.8zm6.2 0c-1.5 0-2.8-1.3-2.8-2.8s1.3-2.8 2.8-2.8 2.8 1.3 2.8 2.8-1.3 2.8-2.8 2.8z"/>
            </svg>
            Log In with Discord
        </a>
    </div>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HyperOS Bot Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
        body { font-family: 'Inter', sans-serif; }
    </style>
</head>
<body class="bg-gray-900 text-white min-h-screen p-8">
    <div class="max-w-4xl mx-auto">
        <div class="flex justify-between items-center mb-6 border-b border-gray-700 pb-4">
            <h1 class="text-4xl font-bold text-indigo-400">HyperOS Bot Configuration</h1>
            <a href="{{ url_for('logout') }}" class="text-sm text-red-400 hover:text-red-500 transition duration-150 p-2 border border-red-400 rounded-lg">Log Out</a>
        </div>
        
        <div class="mb-6 bg-green-900/50 border border-green-700 text-white p-4 rounded-lg">
            <p class="font-semibold">Logged in as Discord User ID:</p>
            <p class="font-mono">{{ user_id }}</p>
        </div>

        {% if error %}
        <div class="bg-indigo-900/50 border border-indigo-700 text-white p-4 rounded-lg mb-6">
            <p class="font-semibold">Status:</p>
            <p>{{ error }}</p>
        </div>
        {% endif %}

        <div class="bg-gray-800 p-6 rounded-xl shadow-2xl">
            <h2 class="text-2xl font-semibold mb-4 border-b border-gray-700 pb-2">Moderation Log Setup (In-Memory)</h2>
            <p class="mb-4 text-gray-400">Configure the Discord Channel ID where all moderation actions will be posted. **Note: This configuration is reset when the bot restarts.**</p>
            
            <form method="POST" action="{{ url_for('update_config') }}">
                <div class="mb-4">
                    <label for="log_channel_id" class="block text-sm font-medium text-gray-300 mb-2">Current Log Channel ID (Discord Channel ID)</label>
                    <input type="text" id="log_channel_id" name="log_channel_id" value="{{ current_log_id or '' }}"
                           class="w-full px-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:ring-indigo-500 focus:border-indigo-500 transition duration-150"
                           placeholder="Enter a 18-digit Discord Channel ID">
                    <p class="mt-2 text-xs text-gray-500">Current Value: <span class="font-mono text-indigo-300">{{ current_log_id or 'Not Set' }}</span></p>
                </div>
                
                <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-2 px-4 rounded-lg transition duration-300 shadow-md hover:shadow-lg">
                    Update Log Channel
                </button>
            </form>
        </div>
        
        <div class="mt-8 bg-gray-800 p-6 rounded-xl shadow-2xl">
             <h2 class="text-2xl font-semibold mb-4 border-b border-gray-700 pb-2">Reaction Roles Status</h2>
             <p class="text-gray-400">Reaction Role setup must be done via the Discord command `/addreactionrole`. Status: **{{ rr_count }}** messages configured.</p>
        </div>
        
        <footer class="mt-10 text-center text-sm text-gray-500 border-t border-gray-800 pt-6">
            HyperOS Discord Bot | Guild ID: {{ guild_id }}
        </footer>
    </div>
</body>
</html>
"""

# --- FLASK ROUTES ---

@app.route('/login')
def login():
    """Step 1: Redirects the user to the Discord authorization page."""
    error = request.args.get('error_msg')
    
    oauth_url = (
        f"https://discord.com/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify" # Only need 'identify' scope to get user ID
    )
    
    return render_template_string(
        LOGIN_TEMPLATE, 
        oauth_url=oauth_url, 
        error=error,
        admin_id=DASHBOARD_ADMIN_USER_ID
    )

@app.route('/oauth_callback')
def oauth_callback():
    """Step 2 & 3: Handles the OAuth response, exchanges code for token, and fetches user ID."""
    
    code = request.args.get('code')
    if not code:
        # User denied access or an error occurred
        return redirect(url_for('login', error_msg='Authorization failed or was cancelled.'))

    # --- Step 2: Exchange code for access token ---
    data = {
        'client_id': DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT_URI,
        'scope': 'identify'
    }
    
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    
    try:
        response = requests.post(
            f"{DISCORD_API_BASE_URL}/oauth2/token",
            data=data,
            headers=headers
        )
        response.raise_for_status() # Raise exception for bad status codes
        token_info = response.json()
        access_token = token_info['access_token']
        
        # --- Step 3: Fetch user information ---
        user_response = requests.get(
            f"{DISCORD_API_BASE_URL}/users/@me",
            headers={'Authorization': f"Bearer {access_token}"}
        )
        user_response.raise_for_status()
        user_info = user_response.json()
        user_id = str(user_info['id'])

        # --- Step 4: Authorization Check ---
        if user_id == DASHBOARD_ADMIN_USER_ID:
            session['discord_user_id'] = user_id
            # Logged in and authorized
            return redirect(url_for('dashboard'))
        else:
            # Logged in but NOT authorized
            return redirect(url_for('logout', error_msg=f'Access denied. Your ID ({user_id}) does not match the configured Admin ID.'))

    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error during OAuth flow: {e.response.status_code} - {e.response.text}")
        return redirect(url_for('login', error_msg=f'Discord API error during login: {e.response.status_code}'))
    except Exception as e:
        print(f"Unexpected error during OAuth flow: {e}")
        return redirect(url_for('login', error_msg='An unexpected error occurred during the login process.'))


@app.route('/logout')
def logout():
    session.pop('discord_user_id', None)
    error_msg = request.args.get('error_msg', 'You have been successfully logged out.')
    return redirect(url_for('login', error_msg=error_msg))


@app.route('/')
@login_required
def home():
    """Simple health check endpoint redirects to the protected dashboard."""
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
@login_required
def dashboard():
    """Displays the bot configuration dashboard."""
    log_id = CONFIG_CACHE.get(GUILD_ID, {}).get('log_channel_id', None)
    rr_messages = CONFIG_CACHE.get(GUILD_ID, {}).get('reaction_roles', {})
    rr_count = len(rr_messages)
    user_id = session.get('discord_user_id', 'Unknown')
    
    # We use 'error' in request.args for both error and success messages
    status_message = request.args.get('error', None)
    
    return render_template_string(
        DASHBOARD_TEMPLATE,
        current_log_id=log_id,
        rr_count=rr_count,
        guild_id=GUILD_ID,
        error=status_message,
        user_id=user_id
    )

@app.route('/config', methods=['POST'])
@login_required
def update_config():
    """Handles POST request to update configurations (currently only log_channel_id)."""

    new_log_id_str = request.form.get('log_channel_id', '').strip()
    
    # Validation
    if new_log_id_str and not new_log_id_str.isdigit():
        return redirect(url_for('dashboard', error="Log Channel ID must be a number (the 18-digit Discord Channel ID)."))
    
    new_log_id = str(new_log_id_str) if new_log_id_str else None

    # Update in-memory cache
    CONFIG_CACHE[GUILD_ID]['log_channel_id'] = new_log_id

    # Redirect back to the dashboard with a success message
    success_msg = "Configuration successfully updated (In-Memory)!"
    return redirect(url_for('dashboard', error=success_msg))


def run_web_server():
    """Runs the Flask web server in a separate thread."""
    port = int(os.environ.get('PORT', 5000))
    # Flask requires a secret key for session management
    app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_hyperos') 
    
    # Check if we are running in an environment where we need to override the Redirect URI host
    if os.environ.get('CANVAS_HOST'):
        global REDIRECT_URI
        # Construct the redirect URI using the dynamically provided hostname
        REDIRECT_URI = f"http://{os.environ['CANVAS_HOST']}:{port}/oauth_callback"
        print(f"Overriding Redirect URI for Canvas: {REDIRECT_URI}")


    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    
    if BOT_TOKEN is None:
        print("ERROR: BOT_TOKEN not found. Please set the 'DISCORD_TOKEN' environment variable.")
    elif DISCORD_CLIENT_ID == '123456789012345678' or DISCORD_CLIENT_SECRET == 'your_super_secret_client_secret':
        print("ERROR: Discord OAuth credentials are using default values. Please set DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET environment variables for the dashboard to work.")
    elif DASHBOARD_ADMIN_USER_ID == '123456789012345678':
         print("WARNING: DASHBOARD_ADMIN_USER_ID is using a default placeholder. Change this to your actual Discord User ID to gain access after OAuth.")
    else:
        # Start the Flask server in a background thread
        server_thread = threading.Thread(target=run_web_server)
        server_thread.daemon = True
        server_thread.start()
        print(f"Web server started on port {os.environ.get('PORT', 5000)} for health check and dashboard.")

        # Run the Discord bot
        bot.run(BOT_TOKEN)
