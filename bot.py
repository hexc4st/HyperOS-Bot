import discord
from discord import app_commands
import asyncio
from datetime import timedelta
import time
import os
from flask import Flask
import threading

# --- 1. CONFIGURATION ---
# IMPORTANT: Use environment variables for sensitive data in production.
# ------------------------------------------------------------------
# 1. BOT_TOKEN: Fetched securely from the system's environment variables (e.g., set on Render).
# Ensure you set the name 'DISCORD_TOKEN' in the Render environment variables.
BOT_TOKEN = os.getenv('DISCORD_TOKEN')

# 2. GUILD_ID: The ID of your server ("HyperOS").
GUILD_ID = 1448175320531468402 

# 3. MOD_ROLE_ID: The ID of the role allowed to use moderation commands.
MOD_ROLE_ID = 1448175664795746398 

# Define the custom Bot class
class HyperOSBot(discord.Client):
    """The core Discord client for the HyperOS bot."""
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        # Dictionary to track muted users: {user_id: unmute_timestamp (float)}
        self.muted_users = {}

    # --- 2. COMMAND SYNCHRONIZATION ---
    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        try:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Synced commands to HyperOS server.")
        except Exception as e:
            print(f"Error syncing commands: {e}")

    # --- 3. MESSAGE HANDLING (MUTE ENFORCEMENT) ---
    async def on_message(self, message: discord.Message):
        """Checks if a user is muted. If so, silently deletes the message."""
        # 1. Safety checks: Ignore bots, DMs, and bot's own messages
        if message.author.bot or not message.guild:
            return

        # 2. Check if user is in the mute list
        unmute_time = self.muted_users.get(message.author.id)

        if unmute_time:
            # 3. Check if mute is still active
            if time.time() < unmute_time:
                try:
                    # Silently delete the user's message
                    await message.delete()
                    print(f"Silently deleted message from muted user: {message.author.name}")
                except discord.Forbidden:
                    print(f"Error: Missing permissions to delete message in {message.channel.name}")
            else:
                # Mute expired: remove from list
                if message.author.id in self.muted_users:
                    del self.muted_users[message.author.id]

# Set up required intents
intents = discord.Intents.default()
intents.members = True 
intents.message_content = True # Critical for on_message to work
intents.moderation = True

bot = HyperOSBot(intents=intents)


# --- 4. HELPER FUNCTIONS ---

def is_moderator(interaction: discord.Interaction) -> bool:
    if not interaction.guild: return False
    mod_role = interaction.guild.get_role(MOD_ROLE_ID)
    return mod_role in interaction.user.roles

def duration_to_seconds(duration: int, unit: str) -> int:
    if unit == 'minutes': return duration * 60
    elif unit == 'hours': return duration * 3600
    elif unit == 'days': return duration * 86400
    return 0

# --- 5. SLASH COMMANDS ---

@bot.tree.command(name="tempmute", description="Mutes a user by deleting their messages (Silent).")
@app_commands.describe(duration="Length of mute", unit="minutes/hours/days", reason="Reason for mute")
@app_commands.checks.has_permissions(moderate_members=True) 
async def temp_mute(interaction: discord.Interaction, member: discord.Member, duration: int, unit: str, reason: str = "No reason provided."):
    # Defer first to prevent timeout
    await interaction.response.defer(ephemeral=False) 

    if not is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return

    seconds = duration_to_seconds(duration, unit)
    if seconds <= 0:
        await interaction.followup.send("❌ Invalid duration.", ephemeral=True)
        return

    unmute_timestamp = time.time() + seconds
    
    # Store mute in memory
    bot.muted_users[member.id] = unmute_timestamp
    
    unmute_dt = discord.utils.format_dt(discord.Object(round(unmute_timestamp)), 'R')
    
    await interaction.followup.send(
        f"🔇 **Mute Action:** {member.mention} has been muted for **{duration} {unit}** (until {unmute_dt}).\n"
        f"Reason: *{reason}*"
    )

@bot.tree.command(name="unmute", description="Manually unmutes a user.")
@app_commands.checks.has_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, member: discord.Member):
    # Defer first to prevent timeout
    await interaction.response.defer(ephemeral=False)

    if not is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return

    if member.id in bot.muted_users:
        del bot.muted_users[member.id]
        await interaction.followup.send(f"✅ **Unmute Action:** {member.mention} has been manually unmuted. They can now speak again.")
    else:
        await interaction.followup.send(f"ℹ️ **{member.display_name}** is not currently muted.", ephemeral=True) 

# --- STANDARD MOD COMMANDS ---

@bot.tree.command(name="tempban", description="Temporarily bans a user.")
@app_commands.checks.has_permissions(ban_members=True)
async def temp_ban(interaction: discord.Interaction, member: discord.Member, duration: int, unit: str, reason: str = "No reason provided."):
    # Defer first to prevent timeout
    await interaction.response.defer(ephemeral=False)
    
    if not is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return
    
    seconds = duration_to_seconds(duration, unit)
    
    try:
        await member.ban(reason=f"Temp Ban: {reason}")
        await interaction.followup.send(f"🔨 **Temporary Ban:** {member.display_name} has been banned for {duration} {unit}.\nReason: *{reason}*")
        
        # Unban process in the background
        await asyncio.sleep(seconds)
        # Fetch the user object again for unbanning
        user = discord.Object(id=member.id)
        await interaction.guild.unban(user, reason="Temp ban expired")
    except Exception as e:
        await interaction.followup.send(f"Error executing ban: {e}", ephemeral=True) 


@bot.tree.command(name="permban", description="Permanently bans a user.")
@app_commands.checks.has_permissions(ban_members=True)
async def perm_ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided."):
    # Defer first to prevent timeout
    await interaction.response.defer(ephemeral=False)

    if not is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return
        
    try:
        await member.ban(reason=reason)
        await interaction.followup.send(f"🚫 **Permanent Ban:** {member.display_name} has been permanently removed from the server.\nReason: *{reason}*")
    except Exception as e:
        await interaction.followup.send(f"Error executing ban: {e}", ephemeral=True) 


@bot.tree.command(name="kick", description="Kicks a user from the server.")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided."):
    # Defer first to prevent timeout
    await interaction.response.defer(ephemeral=False)
    
    if not is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return
        
    try:
        await member.kick(reason=reason)
        await interaction.followup.send(f"👢 **Kick Action:** {member.display_name} has been kicked from the server.\nReason: *{reason}*")
    except Exception as e:
        await interaction.followup.send(f"Error executing kick: {e}", ephemeral=True)


@bot.tree.command(name="warn", description="Issues a formal warning.")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    # Defer first to prevent timeout
    await interaction.response.defer(ephemeral=False)

    if not is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return
    
    await interaction.followup.send(f"⚠️ **Warning Issued:** {member.mention} has received a formal warning.\nReason: *{reason}*")


@bot.tree.command(name="addrole", description="Assigns a role to a member.")
@app_commands.checks.has_permissions(manage_roles=True)
async def add_dynamic_role(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    # Defer first to prevent timeout
    await interaction.response.defer(ephemeral=False)

    if not is_moderator(interaction):
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
    # Defer ephemerally since the actual output is the public message
    await interaction.response.defer(ephemeral=True) 

    if not is_moderator(interaction):
        await interaction.followup.send("❌ Moderator role required.", ephemeral=True)
        return

    try:
        # Send the user's message as the bot
        await interaction.channel.send(message)
        # Confirm action to the moderator ephemerally
        await interaction.followup.send("✅ Message sent successfully.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error sending message: {e}", ephemeral=True)

# --- RENDER HEALTH CHECK ---

# Flask app setup
app = Flask(__name__)

@app.route('/')
def home():
    """Simple health check endpoint."""
    return 'HyperOS Bot is running and healthy!', 200

def run_web_server():
    """Runs the Flask web server in a separate thread."""
    port = int(os.environ.get('PORT', 5000))
    # Note: host='0.0.0.0' is required for Render to bind correctly
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    if BOT_TOKEN is None:
        print("ERROR: BOT_TOKEN not found. Please set the 'DISCORD_TOKEN' environment variable.")
    else:
        # 1. Start the Flask server in a background thread to satisfy Render's health check
        server_thread = threading.Thread(target=run_web_server)
        server_thread.daemon = True # Allows the main program to exit even if the thread is running
        server_thread.start()
        print(f"Web server started on port {os.environ.get('PORT', 5000)} for Render health check.")

        # 2. Run the Discord bot in the main thread (blocking call)
        bot.run(BOT_TOKEN)
