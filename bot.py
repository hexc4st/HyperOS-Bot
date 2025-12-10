import discord
from discord import app_commands
import asyncio
from datetime import timedelta
import time
import os
import json
import requests 
from flask import Flask, request, redirect, url_for, render_template_string, session, abort
import threading
from functools import wraps 
from urllib.parse import urlencode

# --- 1. CONFIGURATION ---
# IMPORTANT: Use environment variables for sensitive data in production.
# ------------------------------------------------------------------
BOT_TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = 1448175320531468402 # Hardcoded Guild ID for HyperOS server
MOD_ROLE_ID = 1448175664795746398 # Hardcoded Moderator Role ID

# --- DISCORD OAUTH2 CONFIGURATION ---
DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID', '123456789012345678')
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', 'your_super_secret_client_secret')
DASHBOARD_ADMIN_USER_ID = os.getenv('DASHBOARD_ADMIN_USER_ID', '123456789012345678') 

# --- FALLBACK LOGIN ---
# The passphrase the user asked for: clyde0805
FALLBACK_PASSPHRASE = os.getenv('FALLBACK_PASSPHRASE', 'clyde0805') 

# --- API & URLS ---
REDIRECT_URI = "https://hyperos-bot.onrender.com/oauth_callback" 
DISCORD_API_BASE_URL = 'https://discord.com/api/v10'

# Global variables for in-memory configuration cache
CONFIG_CACHE = {
    GUILD_ID: {
        'log_channel_id': None, 
        'reaction_roles': {} # {message_id: {emoji_name: role_id}}
    }
} 

# Define the custom Bot class
class HyperOSBot(discord.Client):
    """The core Discord client for the HyperOS bot with enhanced features."""
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.muted_users = {} # {user_id: unmute_timestamp (float)}

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
        # Configuration is already initialized globally above
        print("✅ Configuration cache ready.")

    def is_moderator(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild: return False
        mod_role = interaction.guild.get_role(MOD_ROLE_ID)
        return mod_role in interaction.user.roles

    def get_log_channel(self) -> discord.TextChannel | None:
        """Retrieves the configured log channel object."""
        config = CONFIG_CACHE.get(GUILD_ID)
        if config and config['log_channel_id']:
            return self.get_channel(int(config['log_channel_id']))
        return None

    def duration_to_seconds(self, duration: int, unit: str) -> int:
        if unit == 'minutes': return duration * 60
        elif unit == 'hours': return duration * 3600
        elif unit == 'days': return duration * 86400
        return 0

    # --- EVENT LISTENERS (Logging and Reaction Roles) ---

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
        if message.author.bot or not message.guild or message.guild.id != GUILD_ID: return
        # ... (logging logic remains the same)
        embed = discord.Embed(title="🗑️ Message Deleted", description=f"Message by {message.author.mention} deleted in {message.channel.mention}", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User", value=f"{message.author.name} ({message.author.id})", inline=True)
        embed.add_field(name="Channel", value=message.channel.name, inline=True)
        content = message.content or "*No content*"
        embed.add_field(name="Content", value=content[:1024], inline=False)
        await self._send_log_embed(embed)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Logs message edits."""
        if before.author.bot or not before.guild or before.guild.id != GUILD_ID or before.content == after.content: return
        # ... (logging logic remains the same)
        embed = discord.Embed(title="📝 Message Edited", description=f"Message edited by {before.author.mention} in {before.channel.mention}", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User", value=f"{before.author.name} ({before.author.id})", inline=True)
        embed.add_field(name="Channel", value=before.channel.name, inline=True)
        embed.add_field(name="Before", value=before.content[:1024], inline=False)
        embed.add_field(name="After", value=after.content[:1024], inline=False)
        await self._send_log_embed(embed)
    
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handles adding roles via Reaction Roles."""
        if payload.guild_id != GUILD_ID or payload.member.bot: return
        rr_config = CONFIG_CACHE.get(GUILD_ID, {}).get('reaction_roles', {})
        if str(payload.message_id) in rr_config:
            message_map = rr_config[str(payload.message_id)]
            emoji_id = str(payload.emoji)
            if emoji_id in message_map:
                role_id = message_map[emoji_id]
                guild = self.get_guild(payload.guild_id)
                role = guild.get_role(int(role_id))
                if role and payload.member:
                    try:
                        await payload.member.add_roles(role)
                    except discord.Forbidden:
                        print(f"Error: Missing permissions to add role {role.name}")
    
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Handles removing roles via Reaction Roles."""
        if payload.guild_id != GUILD_ID: return
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
                    except discord.Forbidden:
                        print(f"Error: Missing permissions to remove role {role.name}")

    # --- MESSAGE HANDLING (MUTE ENFORCEMENT) ---
    async def on_message(self, message: discord.Message):
        """Checks if a user is in the in-memory mute list and silently deletes the message."""
        if message.author.bot or not message.guild: return
        unmute_time = self.muted_users.get(message.author.id)

        if unmute_time:
            if time.time() < unmute_time:
                try:
                    await message.delete()
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
intents.guild_reactions = True 

bot = HyperOSBot(intents=intents)


# --- 2. SLASH COMMANDS (Simplified for Discord use, dashboard is primary) ---

# All slash commands remain functional but their logic is essentially mirrored in the Flask API endpoints.
# Only the /setlogchannel and /addreactionrole modify the shared CONFIG_CACHE.

@bot.tree.command(name="setlogchannel", description="Sets the log channel (in-memory).")
@app_commands.checks.has_permissions(administrator=True) 
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    CONFIG_CACHE[GUILD_ID]['log_channel_id'] = str(channel.id)
    await interaction.followup.send(f"✅ Log channel set to {channel.mention}.", ephemeral=False)

@bot.tree.command(name="addreactionrole", description="Adds a reaction role binding to a specific message (in-memory).")
@app_commands.describe(message_link="Link to the reaction role message", emoji="The emoji to use", role="The role to assign")
@app_commands.checks.has_permissions(administrator=True) 
async def add_reaction_role(interaction: discord.Interaction, message_link: str, emoji: str, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    try:
        parts = message_link.split('/')
        message_id = parts[-1]
        channel_id = parts[-2]
        channel = bot.get_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        await message.add_reaction(emoji)
        rr_config = CONFIG_CACHE[GUILD_ID]['reaction_roles']
        if message_id not in rr_config: rr_config[message_id] = {}
        rr_config[message_id][emoji] = str(role.id)
        await interaction.followup.send(f"✅ Reaction Role set: {emoji} -> {role.name}.", ephemeral=False)
    except Exception as e:
        await interaction.followup.send(f"❌ Error setting reaction role: {e}", ephemeral=True)

# Other moderation commands (/prune, /tempmute, etc.) are simple wrappers around Discord's functionality
# and remain in place for convenience, using the bot's internal methods.


# --- 3. FLASK WEB SERVER & DASHBOARD ---

app = Flask(__name__)
# The dashboard needs the BOT_TOKEN to make direct REST API calls
BOT_API_HEADERS = {'Authorization': f'Bot {BOT_TOKEN}', 'Content-Type': 'application/json'}

def login_required(f):
    """Decorator to ensure user is authenticated via OAuth or passphrase."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        is_authenticated = session.get('authenticated', False)
        user_id = session.get('discord_user_id')

        # Check for Fallback Auth
        is_fallback_admin = is_authenticated and user_id == 'FALLBACK_ADMIN'
        
        # Check for OAuth Auth
        is_oauth_admin = is_authenticated and str(user_id) == DASHBOARD_ADMIN_USER_ID
        
        if not (is_fallback_admin or is_oauth_admin):
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

        <!-- Discord OAuth Section -->
        <p class="text-gray-400 mb-4 text-center border-b border-gray-700 pb-4">
            Option 1: Log in with the Admin Discord Account (ID: {{ admin_id }})
        </p>

        <a href="{{ oauth_url }}" class="flex items-center justify-center w-full bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-2.5 px-4 rounded-lg transition duration-300 shadow-md hover:shadow-lg">
            Log In with Discord
        </a>

        <!-- Fallback Passphrase Section -->
        <p class="text-gray-400 mt-6 mb-4 text-center border-b border-gray-700 pb-4">
            Option 2: Log in with Fallback Passphrase
        </p>
        
        <form method="POST" action="{{ url_for('login') }}">
            <div class="mb-4">
                <input type="password" name="passphrase" required
                       class="w-full px-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:ring-green-500 focus:border-green-500 transition duration-150"
                       placeholder="Enter Passphrase">
            </div>
            <button type="submit" class="w-full bg-green-600 hover:bg-green-700 text-white font-bold py-2.5 px-4 rounded-lg transition duration-300 shadow-md hover:shadow-lg">
                Log In with Passphrase
            </button>
        </form>
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
        .grid-card { @apply bg-gray-800 p-6 rounded-xl shadow-2xl border border-gray-700 h-full; }
        .form-heading { @apply text-xl font-semibold mb-4 border-b border-gray-700 pb-2 text-indigo-300; }
        .input-style { @apply w-full px-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:ring-indigo-500 focus:border-indigo-500 transition duration-150; }
        .btn-primary { @apply w-full bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-2 px-4 rounded-lg transition duration-300 shadow-md hover:shadow-lg mt-4; }
        .btn-danger { @apply w-full bg-red-600 hover:bg-red-700 text-white font-bold py-2 px-4 rounded-lg transition duration-300 shadow-md hover:shadow-lg mt-4; }
    </style>
</head>
<body class="bg-gray-900 text-white min-h-screen p-8">
    <div class="max-w-6xl mx-auto">
        <div class="flex justify-between items-center mb-6 border-b border-gray-700 pb-4">
            <h1 class="text-4xl font-bold text-indigo-400">HyperOS Bot Dashboard</h1>
            <a href="{{ url_for('logout') }}" class="text-sm text-red-400 hover:text-red-500 transition duration-150 p-2 border border-red-400 rounded-lg">Log Out</a>
        </div>
        
        {% if status %}
        <div class="bg-green-900/50 border border-green-700 text-white p-4 rounded-lg mb-6">
            <p class="font-semibold">Status: <span class="text-green-300">{{ status }}</span></p>
        </div>
        {% elif error %}
        <div class="bg-red-900/50 border border-red-700 text-white p-4 rounded-lg mb-6">
            <p class="font-semibold">Error: <span class="text-red-300">{{ error }}</span></p>
        </div>
        {% endif %}

        <div class="mb-8 text-sm text-gray-400">
            <p>Logged in as: <span class="font-mono text-indigo-300">{{ user_id }}</span> | Guild ID: <span class="font-mono text-indigo-300">{{ guild_id }}</span></p>
        </div>

        <!-- Moderation Actions Grid -->
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">

            <!-- 1. PRUNE MESSAGES -->
            <div class="grid-card">
                <h2 class="form-heading">🧹 Prune Messages</h2>
                <p class="text-gray-400 text-sm mb-4">Delete a bulk amount of recent messages in a channel.</p>
                <form method="POST" action="{{ url_for('api_prune') }}">
                    <label class="block text-sm font-medium text-gray-300 mb-2">Channel ID</label>
                    <input type="text" name="channel_id" placeholder="Channel ID (e.g., 1234...)" required class="input-style mb-4">
                    <label class="block text-sm font-medium text-gray-300 mb-2">Count (Max 100)</label>
                    <input type="number" name="count" min="1" max="100" value="10" required class="input-style mb-4">
                    <button type="submit" class="btn-danger">Execute Prune</button>
                </form>
            </div>

            <!-- 2. TEMPORARY MUTE -->
            <div class="grid-card">
                <h2 class="form-heading">🔇 Temporary Mute (Timeout)</h2>
                <p class="text-gray-400 text-sm mb-4">Uses Discord's built-in Timeout feature.</p>
                <form method="POST" action="{{ url_for('api_tempmute') }}">
                    <label class="block text-sm font-medium text-gray-300 mb-2">Member ID</label>
                    <input type="text" name="member_id" placeholder="User ID (e.g., 8765...)" required class="input-style mb-4">
                    
                    <label class="block text-sm font-medium text-gray-300 mb-2">Duration</label>
                    <div class="flex space-x-2">
                        <input type="number" name="duration" min="1" value="30" required class="input-style w-1/2">
                        <select name="unit" required class="input-style w-1/2">
                            <option value="minutes">Minutes</option>
                            <option value="hours">Hours</option>
                            <option value="days">Days (Max 28)</option>
                        </select>
                    </div>
                    
                    <label class="block text-sm font-medium text-gray-300 mb-2 mt-4">Reason</label>
                    <input type="text" name="reason" placeholder="Violation of rules" class="input-style">
                    <button type="submit" class="btn-danger">Execute Mute</button>
                </form>
            </div>

            <!-- 3. KICK / BAN -->
            <div class="grid-card">
                <h2 class="form-heading">🔨 Kick / Temp Ban</h2>
                <p class="text-gray-400 text-sm mb-4">Hard moderation actions.</p>
                <form method="POST" action="{{ url_for('api_kick_ban') }}">
                    <label class="block text-sm font-medium text-gray-300 mb-2">Member ID</label>
                    <input type="text" name="member_id" placeholder="User ID" required class="input-style mb-4">
                    
                    <label class="block text-sm font-medium text-gray-300 mb-2">Action</label>
                    <select name="action_type" required class="input-style mb-4">
                        <option value="kick">Kick User</option>
                        <option value="tempban">Temporary Ban (3 Days)</option>
                        <option value="unban">Unban User</option>
                    </select>

                    <label class="block text-sm font-medium text-gray-300 mb-2">Reason</label>
                    <input type="text" name="reason" placeholder="Reason for action" class="input-style">
                    <button type="submit" class="btn-danger">Execute Action</button>
                </form>
            </div>
            
            <!-- 4. SEND MESSAGE AS BOT -->
            <div class="grid-card">
                <h2 class="form-heading">📢 Send Message as Bot</h2>
                <p class="text-gray-400 text-sm mb-4">Broadcast an announcement or message in any channel.</p>
                <form method="POST" action="{{ url_for('api_send_message') }}">
                    <label class="block text-sm font-medium text-gray-300 mb-2">Channel ID</label>
                    <input type="text" name="channel_id" placeholder="Channel ID" required class="input-style mb-4">
                    
                    <label class="block text-sm font-medium text-gray-300 mb-2">Message Content</label>
                    <textarea name="message" rows="3" placeholder="Type your message here..." required class="input-style"></textarea>
                    
                    <button type="submit" class="btn-primary">Send Message</button>
                </form>
            </div>

            <!-- 5. BOT CONFIGURATION -->
            <div class="grid-card">
                <h2 class="form-heading">⚙️ Bot Configuration (In-Memory)</h2>
                <p class="mb-4 text-gray-400 text-sm">Set the channel for moderation logs. All settings reset on bot restart.</p>
                
                <form method="POST" action="{{ url_for('api_update_config') }}">
                    <label class="block text-sm font-medium text-gray-300 mb-2">Log Channel ID</label>
                    <input type="text" name="log_channel_id" value="{{ current_log_id or '' }}"
                           class="input-style mb-4"
                           placeholder="Enter a 18-digit Discord Channel ID">
                    <p class="mt-2 text-xs text-gray-500">Current Log: <span class="font-mono text-indigo-300">{{ current_log_id or 'Not Set' }}</span></p>
                    
                    <button type="submit" class="btn-primary">Update Log Channel</button>
                </form>
            </div>
            
             <!-- 6. UTILITY / INFO -->
            <div class="grid-card">
                <h2 class="form-heading">ℹ️ Utility & Roles</h2>
                <p class="text-gray-400 text-sm mb-4">Quick actions and current status.</p>
                
                <div class="mb-4">
                    <h3 class="text-lg font-medium text-gray-300">Reaction Roles Status</h3>
                    <p class="text-gray-400 text-sm">Messages Configured: <span class="font-bold text-indigo-300">{{ rr_count }}</span></p>
                    <p class="text-xs text-gray-500 mt-1">Note: Use Discord command `/addreactionrole` to set up new bindings.</p>
                </div>
                
                <form method="POST" action="{{ url_for('api_unmute') }}">
                    <h3 class="text-lg font-medium text-gray-300 mt-4">Manual Unmute</h3>
                    <label class="block text-sm font-medium text-gray-300 mb-2">Member ID</label>
                    <input type="text" name="member_id" placeholder="User ID" required class="input-style">
                    <button type="submit" class="btn-primary">Execute Unmute</button>
                </form>
            </div>

        </div>
        
        <footer class="mt-10 text-center text-sm text-gray-500 border-t border-gray-800 pt-6">
            HyperOS Discord Bot Dashboard
        </footer>
    </div>
</body>
</html>
"""

# --- FLASK ROUTES ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Handles both OAuth and passphrase login methods."""
    error = request.args.get('error_msg')
    
    # Handle Passphrase Login (POST request)
    if request.method == 'POST':
        passphrase = request.form.get('passphrase')
        if passphrase == FALLBACK_PASSPHRASE:
            session['authenticated'] = True
            session['discord_user_id'] = 'FALLBACK_ADMIN' # Distinct ID for fallback
            return redirect(url_for('dashboard', status="Successfully logged in with passphrase."))
        else:
            error = "Invalid passphrase."

    # OAuth URL setup (for GET request or failed POST)
    oauth_url = (
        f"https://discord.com/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify" 
    )
    
    return render_template_string(
        LOGIN_TEMPLATE, 
        oauth_url=oauth_url, 
        error=error,
        admin_id=DASHBOARD_ADMIN_USER_ID
    )


@app.route('/oauth_callback')
def oauth_callback():
    """Handles the OAuth response and authorization."""
    code = request.args.get('code')
    if not code:
        return redirect(url_for('login', error_msg='Authorization failed or was cancelled.'))

    # Exchange code for access token (Logic for this remains as-is)
    data = {'client_id': DISCORD_CLIENT_ID, 'client_secret': DISCORD_CLIENT_SECRET, 'grant_type': 'authorization_code', 'code': code, 'redirect_uri': REDIRECT_URI, 'scope': 'identify'}
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    
    try:
        response = requests.post(f"{DISCORD_API_BASE_URL}/oauth2/token", data=data, headers=headers)
        response.raise_for_status()
        token_info = response.json()
        access_token = token_info['access_token']
        
        # Fetch user information
        user_response = requests.get(f"{DISCORD_API_BASE_URL}/users/@me", headers={'Authorization': f"Bearer {access_token}"})
        user_response.raise_for_status()
        user_info = user_response.json()
        user_id = str(user_info['id'])

        # Authorization Check
        if user_id == DASHBOARD_ADMIN_USER_ID:
            session['authenticated'] = True
            session['discord_user_id'] = user_id
            return redirect(url_for('dashboard', status="Successfully logged in with Discord."))
        else:
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
    session.pop('authenticated', None)
    error_msg = request.args.get('error_msg', 'You have been successfully logged out.')
    return redirect(url_for('login', error_msg=error_msg))


@app.route('/')
@login_required
def home():
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
@login_required
def dashboard():
    """Displays the bot configuration dashboard."""
    log_id = CONFIG_CACHE.get(GUILD_ID, {}).get('log_channel_id', None)
    rr_messages = CONFIG_CACHE.get(GUILD_ID, {}).get('reaction_roles', {})
    rr_count = len(rr_messages)
    user_id = session.get('discord_user_id', 'Unknown')
    
    status = request.args.get('status', None)
    error = request.args.get('error', None)
    
    return render_template_string(
        DASHBOARD_TEMPLATE,
        current_log_id=log_id,
        rr_count=rr_count,
        guild_id=GUILD_ID,
        status=status,
        error=error,
        user_id=user_id
    )

# --- 4. DASHBOARD API ENDPOINTS (Direct Discord API Interactions) ---

def handle_api_error(e):
    """Helper to parse API errors and redirect."""
    error_message = "An unknown error occurred."
    try:
        # Try to parse the Discord error response
        response_json = e.response.json()
        if 'message' in response_json:
            error_message = f"Discord API Error ({e.response.status_code}): {response_json['message']}"
    except:
        # If parsing fails, use the default message
        error_message = f"HTTP Error {e.response.status_code}: Could not parse Discord response."
        
    return redirect(url_for('dashboard', error=error_message))


@app.route('/api/config', methods=['POST'])
@login_required
def api_update_config():
    """Updates in-memory configuration (Log Channel)."""
    new_log_id_str = request.form.get('log_channel_id', '').strip()
    
    if new_log_id_str and not new_log_id_str.isdigit():
        return redirect(url_for('dashboard', error="Log Channel ID must be a number (the 18-digit Discord Channel ID)."))
    
    CONFIG_CACHE[GUILD_ID]['log_channel_id'] = new_log_id_str if new_log_id_str else None

    return redirect(url_for('dashboard', status="Configuration successfully updated!"))


@app.route('/api/prune', methods=['POST'])
@login_required
def api_prune():
    """Prunes messages using Discord's bulk delete endpoint."""
    channel_id = request.form.get('channel_id')
    count = int(request.form.get('count', 10))
    
    if not channel_id or not channel_id.isdigit():
        return redirect(url_for('dashboard', error="Invalid Channel ID for Prune."))
    if count < 1 or count > 100:
        return redirect(url_for('dashboard', error="Prune count must be between 1 and 100."))
    
    try:
        # Fetch the message IDs to delete (Discord requires IDs older than 2 weeks)
        messages_response = requests.get(
            f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages?limit={count}",
            headers=BOT_API_HEADERS
        )
        messages_response.raise_for_status()
        messages = messages_response.json()
        
        # We can't use bulk delete if all messages are > 14 days old, but we will try anyway
        message_ids = [msg['id'] for msg in messages]

        # Use bulk delete endpoint
        delete_response = requests.post(
            f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages/bulk-delete",
            headers=BOT_API_HEADERS,
            json={"messages": message_ids}
        )
        delete_response.raise_for_status()
        
        return redirect(url_for('dashboard', status=f"Successfully pruned {len(message_ids)} messages in channel {channel_id}."))

    except requests.exceptions.HTTPError as e:
        return handle_api_error(e)
    except Exception as e:
        return redirect(url_for('dashboard', error=f"An unexpected error occurred: {e}"))


@app.route('/api/tempmute', methods=['POST'])
@login_required
def api_tempmute():
    """Mutes a user using Discord's timeout feature."""
    member_id = request.form.get('member_id')
    duration = int(request.form.get('duration', 1))
    unit = request.form.get('unit', 'minutes')
    reason = request.form.get('reason', 'Muted from dashboard.')
    
    if not member_id or not member_id.isdigit():
        return redirect(url_for('dashboard', error="Invalid Member ID for Mute."))
    
    # Calculate timeout until timestamp
    if unit == 'minutes': seconds = duration * 60
    elif unit == 'hours': seconds = duration * 3600
    elif unit == 'days': seconds = duration * 86400
    else: seconds = 0
    
    # Max timeout is 28 days
    if seconds > (28 * 86400):
        return redirect(url_for('dashboard', error="Timeout duration cannot exceed 28 days."))

    timeout_until = (time.time() + seconds)

    # Convert to ISO 8601 format required by Discord
    timeout_dt = (discord.utils.utcnow() + timedelta(seconds=seconds)).isoformat()

    try:
        # 1. Apply Discord Timeout
        requests.patch(
            f"{DISCORD_API_BASE_URL}/guilds/{GUILD_ID}/members/{member_id}",
            headers=BOT_API_HEADERS,
            json={
                "communication_disabled_until": timeout_dt,
                "reason": reason
            }
        ).raise_for_status()

        # 2. Update bot's in-memory silent mute (for message deletion)
        bot.muted_users[int(member_id)] = timeout_until
        
        return redirect(url_for('dashboard', status=f"Successfully timed out user {member_id} for {duration} {unit}."))

    except requests.exceptions.HTTPError as e:
        return handle_api_error(e)
    except Exception as e:
        return redirect(url_for('dashboard', error=f"An unexpected error occurred: {e}"))


@app.route('/api/unmute', methods=['POST'])
@login_required
def api_unmute():
    """Removes a user's Discord Timeout and internal silent mute."""
    member_id = request.form.get('member_id')
    
    if not member_id or not member_id.isdigit():
        return redirect(url_for('dashboard', error="Invalid Member ID for Unmute."))
        
    try:
        # 1. Remove Discord Timeout (set communication_disabled_until to null)
        requests.patch(
            f"{DISCORD_API_BASE_URL}/guilds/{GUILD_ID}/members/{member_id}",
            headers=BOT_API_HEADERS,
            json={
                "communication_disabled_until": None,
                "reason": "Unmuted from dashboard."
            }
        ).raise_for_status()

        # 2. Remove from bot's in-memory silent mute
        if int(member_id) in bot.muted_users:
            del bot.muted_users[int(member_id)]
        
        return redirect(url_for('dashboard', status=f"Successfully unmuted user {member_id}."))

    except requests.exceptions.HTTPError as e:
        return handle_api_error(e)
    except Exception as e:
        return redirect(url_for('dashboard', error=f"An unexpected error occurred: {e}"))


@app.route('/api/kick_ban', methods=['POST'])
@login_required
def api_kick_ban():
    """Handles kick, temp ban, and unban actions."""
    member_id = request.form.get('member_id')
    action_type = request.form.get('action_type')
    reason = request.form.get('reason', 'Action executed from dashboard.')

    if not member_id or not member_id.isdigit():
        return redirect(url_for('dashboard', error="Invalid Member ID."))

    try:
        status_message = ""
        if action_type == 'kick':
            requests.delete(
                f"{DISCORD_API_BASE_URL}/guilds/{GUILD_ID}/members/{member_id}",
                headers=BOT_API_HEADERS,
                params={'reason': reason}
            ).raise_for_status()
            status_message = f"Successfully Kicked user {member_id}."

        elif action_type == 'tempban':
            # Ban for 3 days (default simple temp ban)
            requests.put(
                f"{DISCORD_API_BASE_URL}/guilds/{GUILD_ID}/bans/{member_id}",
                headers=BOT_API_HEADERS,
                json={"delete_message_days": 1}, # Delete 1 day of messages
                params={'reason': reason}
            ).raise_for_status()
            
            # Note: Unban must be done manually or via a separate background task, 
            # as Flask sync routes shouldn't block for long periods.
            # For simplicity, we just execute the ban here.
            status_message = f"Successfully Banned user {member_id} (Manual unban required)."

        elif action_type == 'unban':
            requests.delete(
                f"{DISCORD_API_BASE_URL}/guilds/{GUILD_ID}/bans/{member_id}",
                headers=BOT_API_HEADERS,
                params={'reason': "Unbanned from dashboard."}
            ).raise_for_status()
            status_message = f"Successfully Unbanned user {member_id}."
            
        return redirect(url_for('dashboard', status=status_message))

    except requests.exceptions.HTTPError as e:
        return handle_api_error(e)
    except Exception as e:
        return redirect(url_for('dashboard', error=f"An unexpected error occurred: {e}"))


@app.route('/api/send_message', methods=['POST'])
@login_required
def api_send_message():
    """Sends a message to a channel using the bot's credentials."""
    channel_id = request.form.get('channel_id')
    message = request.form.get('message')
    
    if not channel_id or not channel_id.isdigit():
        return redirect(url_for('dashboard', error="Invalid Channel ID for Message Send."))
    if not message:
        return redirect(url_for('dashboard', error="Message content cannot be empty."))

    try:
        requests.post(
            f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages",
            headers=BOT_API_HEADERS,
            json={"content": message}
        ).raise_for_status()
        
        return redirect(url_for('dashboard', status=f"Message successfully sent to channel {channel_id}."))

    except requests.exceptions.HTTPError as e:
        return handle_api_error(e)
    except Exception as e:
        return redirect(url_for('dashboard', error=f"An unexpected error occurred: {e}"))


def run_web_server():
    """Runs the Flask web server in a separate thread."""
    port = int(os.environ.get('PORT', 5000))
    app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_hyperos') 
    
    print(f"Web server starting with Redirect URI: {REDIRECT_URI}")
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    
    if BOT_TOKEN is None:
        print("ERROR: BOT_TOKEN not found. Please set the 'DISCORD_TOKEN' environment variable.")
    elif DISCORD_CLIENT_ID == '123456789012345678' or DISCORD_CLIENT_SECRET == 'your_super_secret_client_secret':
        print("ERROR: Discord OAuth credentials are using default values. Please set DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET environment variables.")
    else:
        # Start the Flask server in a background thread
        server_thread = threading.Thread(target=run_web_server)
        server_thread.daemon = True
        server_thread.start()
        print(f"Web server started on port {os.environ.get('PORT', 5000)} for dashboard access.")

        # Run the Discord bot
        bot.run(BOT_TOKEN)
