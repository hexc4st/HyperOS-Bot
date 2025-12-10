import discord
from discord import app_commands
import asyncio
from datetime import timedelta
import time
import os
import json
import requests
from requests.exceptions import HTTPError
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
FALLBACK_PASSPHRASE = os.getenv('FALLBACK_PASSPHRASE', 'clyde0805') 

# --- API & URLS ---
REDIRECT_URI = "https://hyperos-bot.onrender.com/oauth_callback" 
DISCORD_API_BASE_URL = 'https://discord.com/api/v10'

# Global variables for in-memory configuration cache
CONFIG_CACHE = {
    GUILD_ID: {
        'log_channel_id': None, 
        'word_filter_list': ["badword", "anotherbadword"], # Initial word list
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
        print("✅ Configuration cache ready.")

    def get_log_channel(self) -> discord.TextChannel | None:
        """Retrieves the configured log channel object."""
        config = CONFIG_CACHE.get(GUILD_ID)
        if config and config['log_channel_id']:
            return self.get_channel(int(config['log_channel_id']))
        return None

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
        embed = discord.Embed(title="🗑️ Message Deleted", description=f"Message by {message.author.mention} deleted in {message.channel.mention}", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User", value=f"{message.author.name} ({message.author.id})", inline=True)
        embed.add_field(name="Channel", value=message.channel.name, inline=True)
        content = message.content or "*No content*"
        embed.add_field(name="Content", value=content[:1024], inline=False)
        await self._send_log_embed(embed)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Logs message edits."""
        if before.author.bot or not before.guild or before.guild.id != GUILD_ID or before.content == after.content: return
        embed = discord.Embed(title="📝 Message Edited", description=f"Message edited by {before.author.mention} in {before.channel.mention}", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User", value=f"{before.author.name} ({before.author.id})", inline=True)
        embed.add_field(name="Channel", value=before.channel.name, inline=True)
        embed.add_field(name="Before", value=before.content[:1024], inline=False)
        embed.add_field(name="After", value=after.content[:1024], inline=False)
        await self._send_log_embed(embed)
    
    # Reaction role listeners (unchanged)
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
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


    # --- MESSAGE HANDLING (MUTE ENFORCEMENT & WORD FILTER) ---
    async def on_message(self, message: discord.Message):
        """Checks for mute status and filtered words, deleting messages if necessary."""
        if message.author.bot or not message.guild or message.guild.id != GUILD_ID: return

        # 1. Mute Enforcement
        unmute_time = self.muted_users.get(message.author.id)
        if unmute_time:
            if time.time() < unmute_time:
                try:
                    await message.delete()
                    print(f"Deleted muted user's message: {message.author.name}")
                    return # Stop processing if deleted due to mute
                except discord.Forbidden:
                    print(f"Error: Missing permissions to delete message (mute) in {message.channel.name}")
                except Exception:
                    pass
            else:
                if message.author.id in self.muted_users:
                    del self.muted_users[message.author.id]

        # 2. Word Filter
        filter_list = CONFIG_CACHE[GUILD_ID]['word_filter_list']
        content_lower = message.content.lower()

        is_filtered = False
        filtered_word = None
        for word in filter_list:
            if word and word in content_lower:
                is_filtered = True
                filtered_word = word
                break

        if is_filtered:
            try:
                await message.delete()
                
                # Log the filter action
                embed = discord.Embed(title="🚫 Filter Violation", 
                                      description=f"{message.author.mention}'s message was deleted for violating the word filter.", 
                                      color=discord.Color.dark_red(), 
                                      timestamp=discord.utils.utcnow())
                embed.add_field(name="User", value=f"{message.author.name} ({message.author.id})", inline=True)
                embed.add_field(name="Channel", value=message.channel.name, inline=True)
                embed.add_field(name="Content (Deleted)", value=message.content[:1000], inline=False)
                embed.add_field(name="Trigger Word", value=filtered_word, inline=True)
                await self._send_log_embed(embed)
                
            except discord.Forbidden:
                print(f"Error: Missing permissions to delete message (filter) in {message.channel.name}")
            except Exception as e:
                print(f"Error during word filter action: {e}")


# Set up required intents
intents = discord.Intents.default()
intents.members = True 
intents.message_content = True 
intents.moderation = True
intents.guild_messages = True
intents.guild_reactions = True 

bot = HyperOSBot(intents=intents)


# --- 2. SLASH COMMANDS (Consolidated Admin Group and Reaction Role Utility) ---

@bot.tree.command(name="addreactionrole", description="Adds a reaction role binding to a specific message.")
@app_commands.describe(message_link="Link to the reaction role message", emoji="The emoji to use", role="The role to assign")
@app_commands.checks.has_permissions(administrator=True) 
async def add_reaction_role(interaction: discord.Interaction, message_link: str, emoji: str, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    try:
        parts = message_link.split('/')
        if len(parts) < 3:
             return await interaction.followup.send("❌ Invalid message link format. Ensure it's a full Discord message link.", ephemeral=True)
             
        message_id = parts[-1]
        channel_id = parts[-2]
        channel = bot.get_channel(int(channel_id))
        if not channel:
            return await interaction.followup.send("❌ Could not find the channel from the message link.", ephemeral=True)
            
        message = await channel.fetch_message(int(message_id))
        await message.add_reaction(emoji)
        
        rr_config = CONFIG_CACHE[GUILD_ID]['reaction_roles']
        if message_id not in rr_config: rr_config[message_id] = {}
        rr_config[message_id][emoji] = str(role.id)
        
        await interaction.followup.send(f"✅ Reaction Role set: {emoji} -> {role.name} for message ID {message_id}.", ephemeral=False)
    except discord.NotFound:
        await interaction.followup.send("❌ Message not found. Check the channel ID and message ID.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ I am missing permissions to add a reaction or assign the role.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error setting reaction role: {e}", ephemeral=True)


@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
class AdminCommands(app_commands.Group):
    """Administrator interface for managing bot configuration and moderation actions."""
    
    # Helper to get the guild member object needed for mod actions
    async def _get_member(self, interaction: discord.Interaction, member_id: str) -> discord.Member | None:
        if not member_id.isdigit():
            await interaction.followup.send("❌ Member ID must be a numeric ID.", ephemeral=True)
            return None
        
        try:
            member = await interaction.guild.fetch_member(int(member_id))
            return member
        except discord.NotFound:
            await interaction.followup.send("❌ User not found in this server. Check the ID.", ephemeral=True)
            return None
        except Exception as e:
            await interaction.followup.send(f"❌ Error fetching member: {e}", ephemeral=True)
            return None

    # --- CONFIGURATION ACTIONS ---

    @app_commands.command(name="set-log-channel", description="Sets the bot's logging channel ID.")
    async def set_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        CONFIG_CACHE[GUILD_ID]['log_channel_id'] = str(channel.id)
        await interaction.response.send_message(f"✅ Log channel set to {channel.mention}.", ephemeral=False)
        
    @app_commands.command(name="set-word-filter", description="Sets the blacklisted words (comma separated).")
    @app_commands.describe(words="Comma separated list of words to blacklist (e.g., word1,word2).")
    async def set_word_filter(self, interaction: discord.Interaction, words: str):
        new_word_list = [w.strip().lower() for w in words.split(',') if w.strip()]
        CONFIG_CACHE[GUILD_ID]['word_filter_list'] = new_word_list
        words_out = ", ".join(new_word_list) if new_word_list else "None"
        await interaction.response.send_message(f"✅ Word Filter List successfully updated to: `{words_out}`. Filter is now active.", ephemeral=False)

    @app_commands.command(name="view-status", description="Displays the current bot configuration.")
    async def view_status(self, interaction: discord.Interaction):
        config = CONFIG_CACHE.get(GUILD_ID, {})
        log_id = config.get('log_channel_id', 'Not Set')
        filter_list = ", ".join(config.get('word_filter_list', [])) or "None"
        rr_count = len(config.get('reaction_roles', {}))
        
        embed = discord.Embed(title="⚙️ HyperOS Bot Status", color=discord.Color.blurple())
        embed.add_field(name="Log Channel ID", value=log_id, inline=False)
        embed.add_field(name="Word Filter List", value=filter_list[:1024] or "None", inline=False)
        embed.add_field(name="Reaction Roles Configured", value=rr_count, inline=True)
        embed.add_field(name="Dashboard Admin ID", value=DASHBOARD_ADMIN_USER_ID, inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- BOT ACTIONS ---

    @app_commands.command(name="set-nickname", description="Changes the bot's nickname in the server.")
    async def set_nickname(self, interaction: discord.Interaction, nickname: str):
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.guild.me.edit(nick=nickname, reason=f"Nickname changed by {interaction.user.name} via /admin set-nickname.")
            await interaction.followup.send(f"✅ Bot nickname successfully changed to `{nickname}`.", ephemeral=False)
        except discord.Forbidden:
            await interaction.followup.send("❌ I do not have permissions to change my nickname (Manage Nicknames).", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ An error occurred: {e}", ephemeral=True)

    @app_commands.command(name="webhook-send", description="Send a message to a channel using a webhook.")
    @app_commands.describe(channel="The channel to send the message to", message="The content of the message", username="Impersonated username (optional)", avatar_url="Impersonated avatar URL (optional)")
    async def webhook_send(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str, username: str = None, avatar_url: str = None):
        await interaction.response.defer(ephemeral=True)
        try:
            # We will use the REST API helper for consistency with the dashboard
            webhook_id, webhook_token = _get_or_create_webhook(channel.id)

            payload = {"content": message}
            if username: payload["username"] = username
            if avatar_url: payload["avatar_url"] = avatar_url

            requests.post(
                f"{DISCORD_API_BASE_URL}/webhooks/{webhook_id}/{webhook_token}",
                json=payload
            ).raise_for_status()

            await interaction.followup.send(f"✅ Message sent successfully to {channel.mention}.", ephemeral=True)

        except Exception as e:
            error_msg = str(e)
            if "Failed to create webhook" in error_msg:
                 error_msg = "Failed to send message: Bot requires 'Manage Webhooks' permission in that channel."
            await interaction.followup.send(f"❌ Error sending webhook message: {error_msg}", ephemeral=True)


    # --- MODERATION ACTIONS ---

    @app_commands.command(name="prune", description="Deletes a bulk amount of recent messages in a channel (Max 100).")
    @app_commands.describe(channel="The channel to prune messages from", count="Number of messages to delete (1-100)")
    async def prune(self, interaction: discord.Interaction, channel: discord.TextChannel, count: app_commands.Range[int, 1, 100]):
        await interaction.response.defer(ephemeral=True)
        try:
            deleted_messages = await channel.purge(limit=count, reason=f"Pruned by {interaction.user.name} via /admin prune")
            await interaction.followup.send(f"✅ Successfully deleted {len(deleted_messages)} messages in {channel.mention}.", ephemeral=False)
        except discord.Forbidden:
            await interaction.followup.send("❌ I do not have permissions to manage messages in that channel.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ An unexpected error occurred: {e}", ephemeral=True)

    @app_commands.command(name="timeout", description="Mutes a member using Discord's timeout feature.")
    @app_commands.describe(member_id="The User ID of the member to mute", duration_minutes="Duration in minutes (up to 40320 / 28 days)", reason="Reason for the timeout")
    async def timeout(self, interaction: discord.Interaction, member_id: str, duration_minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided"):
        await interaction.response.defer(ephemeral=True)
        
        member = await self._get_member(interaction, member_id)
        if not member: return

        seconds = duration_minutes * 60
        timeout_until = time.time() + seconds
        timeout_dt = (discord.utils.utcnow() + timedelta(minutes=duration_minutes)).isoformat()
        
        try:
            # Apply Discord Timeout using REST API patch (more reliable for high concurrency)
            requests.patch(
                f"{DISCORD_API_BASE_URL}/guilds/{GUILD_ID}/members/{member_id}",
                headers=BOT_API_HEADERS,
                json={
                    "communication_disabled_until": timeout_dt,
                    "reason": reason
                }
            ).raise_for_status()

            # Update bot's in-memory silent mute (for message deletion)
            bot.muted_users[member.id] = timeout_until
            
            await interaction.followup.send(f"✅ Successfully timed out {member.mention} for {duration_minutes} minutes. Reason: `{reason}`", ephemeral=False)

        except HTTPError as e:
            return await interaction.followup.send(f"❌ Discord API Error: Could not timeout user. Status code: {e.response.status_code}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ An unexpected error occurred: {e}", ephemeral=True)
    
    @app_commands.command(name="untimeout", description="Removes timeout (unmutes) a member.")
    @app_commands.describe(member_id="The User ID of the member to unmute")
    async def untimeout(self, interaction: discord.Interaction, member_id: str):
        await interaction.response.defer(ephemeral=True)
        
        member = await self._get_member(interaction, member_id)
        if not member: return

        try:
            # Remove Discord Timeout
            requests.patch(
                f"{DISCORD_API_BASE_URL}/guilds/{GUILD_ID}/members/{member_id}",
                headers=BOT_API_HEADERS,
                json={
                    "communication_disabled_until": None,
                    "reason": "Unmuted via /admin untimeout."
                }
            ).raise_for_status()

            # Remove from bot's in-memory silent mute
            if member.id in bot.muted_users:
                del bot.muted_users[member.id]
            
            await interaction.followup.send(f"✅ Successfully removed timeout for {member.mention}.", ephemeral=False)

        except HTTPError as e:
            return await interaction.followup.send(f"❌ Discord API Error: Could not remove timeout. Status code: {e.response.status_code}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ An unexpected error occurred: {e}", ephemeral=True)

    @app_commands.command(name="kick", description="Kicks a member from the server.")
    @app_commands.describe(member_id="The User ID of the member to kick", reason="Reason for the kick")
    async def kick(self, interaction: discord.Interaction, member_id: str, reason: str = "No reason provided"):
        await interaction.response.defer(ephemeral=True)
        
        member = await self._get_member(interaction, member_id)
        if not member: return

        try:
            await member.kick(reason=reason)
            await interaction.followup.send(f"✅ Successfully kicked {member.name} (ID: {member_id}). Reason: `{reason}`", ephemeral=False)
        except discord.Forbidden:
            await interaction.followup.send("❌ I do not have permissions to kick this user, or their role is higher than mine.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ An unexpected error occurred: {e}", ephemeral=True)

    @app_commands.command(name="ban", description="Bans a user from the server (removes 1 day of messages).")
    @app_commands.describe(user_id="The User ID of the member to ban", reason="Reason for the ban")
    async def ban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
        await interaction.response.defer(ephemeral=True)
        
        if not user_id.isdigit():
            return await interaction.followup.send("❌ User ID must be a numeric ID.", ephemeral=True)

        try:
            await interaction.guild.ban(discord.Object(id=int(user_id)), reason=reason, delete_message_days=1)
            await interaction.followup.send(f"✅ Successfully banned user ID {user_id}. Reason: `{reason}`", ephemeral=False)
        except discord.Forbidden:
            await interaction.followup.send("❌ I do not have permissions to ban users.", ephemeral=True)
        except discord.NotFound:
            await interaction.followup.send("❌ User ID not found in Discord.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ An unexpected error occurred: {e}", ephemeral=True)

    @app_commands.command(name="unban", description="Unbans a user using their User ID.")
    @app_commands.describe(user_id="The User ID of the member to unban")
    async def unban(self, interaction: discord.Interaction, user_id: str):
        await interaction.response.defer(ephemeral=True)

        if not user_id.isdigit():
            return await interaction.followup.send("❌ User ID must be a numeric ID.", ephemeral=True)

        try:
            # Fetch the Banned Entry (required for unban)
            banned_user = discord.Object(id=int(user_id))
            await interaction.guild.unban(banned_user, reason=f"Unbanned by {interaction.user.name} via /admin unban")
            await interaction.followup.send(f"✅ Successfully unbanned user ID {user_id}.", ephemeral=False)
        except discord.Forbidden:
            await interaction.followup.send("❌ I do not have permissions to unban users.", ephemeral=True)
        except discord.NotFound:
            await interaction.followup.send("❌ User ID was not found in the ban list.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ An unexpected error occurred: {e}", ephemeral=True)

# Add the admin group to the command tree
bot.tree.add_command(AdminCommands(name="admin"))

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

        is_fallback_admin = is_authenticated and user_id == 'FALLBACK_ADMIN'
        is_oauth_admin = is_authenticated and str(user_id) == DASHBOARD_ADMIN_USER_ID
        
        if not (is_fallback_admin or is_oauth_admin):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# --- HELPER FUNCTIONS FOR FLASK (Discord API) ---

def _get_guild_channels():
    """Fetches text channels for the guild via REST API for dashboard dropdowns."""
    try:
        response = requests.get(
            f"{DISCORD_API_BASE_URL}/guilds/{GUILD_ID}/channels",
            headers=BOT_API_HEADERS
        )
        response.raise_for_status()
        channels = response.json()
        
        # Filter for text channels (type 0) and sort by position
        text_channels = sorted(
            [c for c in channels if c.get('type') == 0], 
            key=lambda x: x.get('position', 999)
        )
        return [{"id": c['id'], "name": c['name']} for c in text_channels]
    except HTTPError as e:
        print(f"Error fetching channels: {e.response.status_code} - {e.response.text}")
        return []
    except Exception as e:
        print(f"Unexpected error fetching channels: {e}")
        return []

def _get_or_create_webhook(channel_id):
    """Gets the first available webhook or creates a new one for a channel."""
    channel_id = str(channel_id)
    # 1. Try to find an existing webhook named 'HyperOS Impersonator'
    try:
        response = requests.get(
            f"{DISCORD_API_BASE_URL}/channels/{channel_id}/webhooks",
            headers=BOT_API_HEADERS
        )
        response.raise_for_status()
        webhooks = response.json()
        
        existing_webhook = next((w for w in webhooks if w['name'] == 'HyperOS Impersonator'), None)
        if existing_webhook:
            return existing_webhook['id'], existing_webhook['token']
    
    except HTTPError as e:
        print(f"Error checking webhooks: {e.response.text}")
        # Continue to creation if permission error or not found
    except Exception as e:
        print(f"Unexpected error during webhook check: {e}")

    # 2. If not found, create a new one
    try:
        creation_response = requests.post(
            f"{DISCORD_API_BASE_URL}/channels/{channel_id}/webhooks",
            headers=BOT_API_HEADERS,
            json={"name": "HyperOS Impersonator"}
        )
        creation_response.raise_for_status()
        new_webhook = creation_response.json()
        return new_webhook['id'], new_webhook['token']
    
    except HTTPError as e:
        print(f"Error creating webhook: {e.response.text}")
        raise Exception(f"Failed to create webhook (Check BOT permissions). API Error: {e.response.status_code}")
    except Exception as e:
        raise Exception(f"Unexpected error during webhook creation: {e}")


# --- TEMPLATES (Login omitted for brevity, it is unchanged) ---

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
            <p class="mt-1">Use the comprehensive Discord command <span class="font-mono text-indigo-300">/admin</span> if the dashboard is inaccessible.</p>
        </div>

        <!-- Moderation Actions Grid -->
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 mb-6">

            <!-- 1. PRUNE MESSAGES -->
            <div class="grid-card">
                <h2 class="form-heading">🧹 Prune Messages</h2>
                <p class="text-gray-400 text-sm mb-4">Delete a bulk amount of recent messages in a channel.</p>
                <form method="POST" action="{{ url_for('api_prune') }}">
                    <label class="block text-sm font-medium text-gray-300 mb-2">Target Channel</label>
                    <select name="channel_id" required class="input-style mb-4">
                        {% for channel in channels %}
                        <option value="{{ channel.id }}">#{{ channel.name }} ({{ channel.id }})</option>
                        {% endfor %}
                    </select>
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

            <!-- 3. KICK / BAN / UNBAN -->
            <div class="grid-card">
                <h2 class="form-heading">🔨 Kick / Ban Actions</h2>
                <p class="text-gray-400 text-sm mb-4">Hard moderation actions.</p>
                <form method="POST" action="{{ url_for('api_kick_ban') }}">
                    <label class="block text-sm font-medium text-gray-300 mb-2">Member ID</label>
                    <input type="text" name="member_id" placeholder="User ID" required class="input-style mb-4">
                    
                    <label class="block text-sm font-medium text-gray-300 mb-2">Action</label>
                    <select name="action_type" required class="input-style mb-4">
                        <option value="kick">Kick User</option>
                        <option value="tempban">Temporary Ban (1 Day of messages deleted)</option>
                        <option value="unban">Unban User</option>
                    </select>

                    <label class="block text-sm font-medium text-gray-300 mb-2">Reason</label>
                    <input type="text" name="reason" placeholder="Reason for action" class="input-style">
                    <button type="submit" class="btn-danger">Execute Action</button>
                </form>
            </div>
            
            <!-- 4. SEND MESSAGE (IMPERSONATION) -->
            <div class="grid-card">
                <h2 class="form-heading">🗣️ Send Message (Webhook Impersonation)</h2>
                <p class="text-gray-400 text-sm mb-4">Send a message that appears to be from a custom user/avatar.</p>
                <form method="POST" action="{{ url_for('api_send_message') }}">
                    <label class="block text-sm font-medium text-gray-300 mb-2">Target Channel</label>
                    <select name="channel_id" required class="input-style mb-4">
                        {% for channel in channels %}
                        <option value="{{ channel.id }}">#{{ channel.name }} ({{ channel.id }})</option>
                        {% endfor %}
                    </select>
                    
                    <label class="block text-sm font-medium text-gray-300 mb-2">Impersonated Username (Optional)</label>
                    <input type="text" name="username" placeholder="Leave blank for bot's name" class="input-style mb-4">

                    <label class="block text-sm font-medium text-gray-300 mb-2">Avatar URL (Optional)</label>
                    <input type="url" name="avatar_url" placeholder="Direct link to a profile image" class="input-style mb-4">
                    
                    <label class="block text-sm font-medium text-gray-300 mb-2">Message Content</label>
                    <textarea name="message" rows="3" placeholder="Type your message here..." required class="input-style"></textarea>
                    
                    <button type="submit" class="btn-primary">Send Impersonated Message</button>
                </form>
            </div>

            <!-- 5. BOT NICKNAME CHANGE -->
            <div class="grid-card">
                <h2 class="form-heading">👤 Change Bot Nickname</h2>
                <p class="text-gray-400 text-sm mb-4">Update the bot's display name in the server.</p>
                <form method="POST" action="{{ url_for('api_change_nickname') }}">
                    <label class="block text-sm font-medium text-gray-300 mb-2">New Nickname</label>
                    <input type="text" name="nickname" placeholder="e.g., HyperOS Mod | ⚙️" required class="input-style mb-4">
                    <button type="submit" class="btn-primary">Set Nickname</button>
                </form>
            </div>
            
             <!-- 6. UTILITY / UNMUTE -->
            <div class="grid-card">
                <h2 class="form-heading">ℹ️ Utility & Manual Unmute</h2>
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

        <!-- CONFIGURATION GRID -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <!-- 7. CONFIGURATION: Log Channel & Word Filter -->
            <div class="grid-card lg:col-span-1">
                <h2 class="form-heading">⚙️ Bot Configuration (In-Memory)</h2>
                <p class="mb-4 text-gray-400 text-sm">All settings reset on bot restart.</p>
                
                <form method="POST" action="{{ url_for('api_update_config') }}">
                    <label class="block text-sm font-medium text-gray-300 mb-2">Log Channel ID</label>
                    <input type="text" name="log_channel_id" value="{{ current_log_id or '' }}"
                           class="input-style mb-4"
                           placeholder="Enter a 18-digit Discord Channel ID">
                    <p class="mt-2 text-xs text-gray-500">Current Log: <span class="font-mono text-indigo-300">{{ current_log_id or 'Not Set' }}</span></p>
                    
                    <label class="block text-sm font-medium text-gray-300 mb-2 mt-6">Word Filter List (Comma Separated)</label>
                    <textarea name="word_filter_list" rows="3" class="input-style mb-4"
                              placeholder="word1, word2, word3">{{ current_word_filter | join(', ') }}</textarea>

                    <button type="submit" class="btn-primary">Update Configuration</button>
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

# --- FLASK ROUTES (Login and OAuth omitted, they are unchanged) ---

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
        """
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
        """, 
        oauth_url=oauth_url, 
        error=error,
        admin_id=DASHBOARD_ADMIN_USER_ID
    )

@app.route('/oauth_callback')
def oauth_callback():
    code = request.args.get('code')
    if not code:
        return redirect(url_for('login', error_msg='Authorization failed or was cancelled.'))

    data = {'client_id': DISCORD_CLIENT_ID, 'client_secret': DISCORD_CLIENT_SECRET, 'grant_type': 'authorization_code', 'code': code, 'redirect_uri': REDIRECT_URI, 'scope': 'identify'}
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    
    try:
        response = requests.post(f"{DISCORD_API_BASE_URL}/oauth2/token", data=data, headers=headers)
        response.raise_for_status()
        token_info = response.json()
        access_token = token_info['access_token']
        
        user_response = requests.get(f"{DISCORD_API_BASE_URL}/users/@me", headers={'Authorization': f"Bearer {access_token}"})
        user_response.raise_for_status()
        user_info = user_response.json()
        user_id = str(user_info['id'])

        if user_id == DASHBOARD_ADMIN_USER_ID:
            session['authenticated'] = True
            session['discord_user_id'] = user_id
            return redirect(url_for('dashboard', status="Successfully logged in with Discord."))
        else:
            return redirect(url_for('logout', error_msg=f'Access denied. Your ID ({user_id}) does not match the configured Admin ID.'))

    except HTTPError as e:
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
    """Displays the bot configuration dashboard, fetching channels dynamically."""
    
    # Fetch channels for dropdowns
    channels = _get_guild_channels()
    
    config = CONFIG_CACHE.get(GUILD_ID, {})
    log_id = config.get('log_channel_id', None)
    word_filter_list = config.get('word_filter_list', [])
    rr_messages = config.get('reaction_roles', {})
    rr_count = len(rr_messages)
    user_id = session.get('discord_user_id', 'Unknown')
    
    status = request.args.get('status', None)
    error = request.args.get('error', None)
    
    return render_template_string(
        DASHBOARD_TEMPLATE,
        current_log_id=log_id,
        current_word_filter=word_filter_list,
        rr_count=rr_count,
        guild_id=GUILD_ID,
        status=status,
        error=error,
        user_id=user_id,
        channels=channels
    )

# --- 4. DASHBOARD API ENDPOINTS (Direct Discord API Interactions) ---

def handle_api_error(e):
    """Helper to parse API errors and redirect."""
    error_message = "An unknown error occurred."
    try:
        response_json = e.response.json()
        if 'message' in response_json:
            error_message = f"Discord API Error ({e.response.status_code}): {response_json['message']}"
    except:
        error_message = f"HTTP Error {e.response.status_code}: Could not parse Discord response."
        
    return redirect(url_for('dashboard', error=error_message))

@app.route('/api/config', methods=['POST'])
@login_required
def api_update_config():
    """Updates in-memory configuration (Log Channel and Word Filter List)."""
    new_log_id_str = request.form.get('log_channel_id', '').strip()
    word_filter_raw = request.form.get('word_filter_list', '').strip()

    # Log Channel validation
    if new_log_id_str and not new_log_id_str.isdigit():
        return redirect(url_for('dashboard', error="Log Channel ID must be a number (the 18-digit Discord Channel ID)."))
    
    # Word Filter processing
    new_word_list = [w.strip().lower() for w in word_filter_raw.split(',') if w.strip()]
    
    CONFIG_CACHE[GUILD_ID]['log_channel_id'] = new_log_id_str if new_log_id_str else None
    CONFIG_CACHE[GUILD_ID]['word_filter_list'] = new_word_list

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
        # Fetch the message IDs to delete
        messages_response = requests.get(
            f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages?limit={count}",
            headers=BOT_API_HEADERS
        )
        messages_response.raise_for_status()
        messages = messages_response.json()
        
        message_ids = [msg['id'] for msg in messages]

        # Use bulk delete endpoint
        delete_response = requests.post(
            f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages/bulk-delete",
            headers=BOT_API_HEADERS,
            json={"messages": message_ids}
        )
        delete_response.raise_for_status()
        
        return redirect(url_for('dashboard', status=f"Successfully pruned {len(message_ids)} messages in channel {channel_id}."))

    except HTTPError as e:
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
    
    if unit == 'minutes': seconds = duration * 60
    elif unit == 'hours': seconds = duration * 3600
    elif unit == 'days': seconds = duration * 86400
    else: seconds = 0
    
    if seconds > (28 * 86400):
        return redirect(url_for('dashboard', error="Timeout duration cannot exceed 28 days."))

    timeout_until = (time.time() + seconds)
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
        # Note: This relies on Flask and bot running in the same process
        bot.muted_users[int(member_id)] = timeout_until
        
        return redirect(url_for('dashboard', status=f"Successfully timed out user {member_id} for {duration} {unit}."))

    except HTTPError as e:
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
        # 1. Remove Discord Timeout
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

    except HTTPError as e:
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
            # delete_message_days=1 ensures 1 day of messages are deleted
            requests.put(
                f"{DISCORD_API_BASE_URL}/guilds/{GUILD_ID}/bans/{member_id}",
                headers=BOT_API_HEADERS,
                json={"delete_message_days": 1},
                params={'reason': reason}
            ).raise_for_status()
            status_message = f"Successfully Banned user {member_id} (1 day of messages deleted)."

        elif action_type == 'unban':
            requests.delete(
                f"{DISCORD_API_BASE_URL}/guilds/{GUILD_ID}/bans/{member_id}",
                headers=BOT_API_HEADERS,
                params={'reason': "Unbanned from dashboard."}
            ).raise_for_status()
            status_message = f"Successfully Unbanned user {member_id}."
            
        return redirect(url_for('dashboard', status=status_message))

    except HTTPError as e:
        return handle_api_error(e)
    except Exception as e:
        return redirect(url_for('dashboard', error=f"An unexpected error occurred: {e}"))


@app.route('/api/change_nickname', methods=['POST'])
@login_required
def api_change_nickname():
    """Changes the bot's nickname in the guild."""
    nickname = request.form.get('nickname')
    
    if not nickname:
        return redirect(url_for('dashboard', error="Nickname cannot be empty."))

    try:
        current_bot_user_id = bot.user.id if bot.is_ready() else "@me" 

        requests.patch(
            f"{DISCORD_API_BASE_URL}/guilds/{GUILD_ID}/members/{current_bot_user_id}",
            headers=BOT_API_HEADERS,
            json={"nick": nickname}
        ).raise_for_status()
        
        return redirect(url_for('dashboard', status=f"Bot nickname successfully changed to '{nickname}'."))

    except HTTPError as e:
        return handle_api_error(e)
    except Exception as e:
        return redirect(url_for('dashboard', error=f"An unexpected error occurred: {e}"))


@app.route('/api/send_message', methods=['POST'])
@login_required
def api_send_message():
    """Sends a message using a webhook for username/avatar impersonation."""
    channel_id = request.form.get('channel_id')
    message = request.form.get('message')
    username = request.form.get('username')
    avatar_url = request.form.get('avatar_url')
    
    if not channel_id or not channel_id.isdigit():
        return redirect(url_for('dashboard', error="Invalid Channel ID for Message Send."))
    if not message:
        return redirect(url_for('dashboard', error="Message content cannot be empty."))

    try:
        # 1. Get or Create the Webhook
        webhook_id, webhook_token = _get_or_create_webhook(channel_id)
        
        payload = {"content": message}
        if username:
            payload["username"] = username
        if avatar_url:
            payload["avatar_url"] = avatar_url

        # 2. Execute Webhook
        requests.post(
            f"{DISCORD_API_BASE_URL}/webhooks/{webhook_id}/{webhook_token}",
            json=payload
        ).raise_for_status()
        
        return redirect(url_for('dashboard', status=f"Impersonated message successfully sent to channel {channel_id}."))

    except HTTPError as e:
        return handle_api_error(e)
    except Exception as e:
        # Catch exceptions thrown by _get_or_create_webhook as well
        return redirect(url_for('dashboard', error=f"An unexpected error occurred during webhook operation: {e}"))


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
