import json
import os
import asyncio
from datetime import datetime, timedelta
import pytz
import discord
from discord import app_commands
from discord.ext import commands, tasks
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ==========================================
# FILE MANAGERS & PERSISTENCE
# ==========================================

BASE_DIR = "/app"
TOKEN_FILE = os.path.join(BASE_DIR, "token.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
MESSAGES_FILE = os.path.join(BASE_DIR, "messages.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LAST_RESULT_FILE = os.path.join(BASE_DIR, "last_result.json")
WINNER_HISTORY_FILE = os.path.join(BASE_DIR, "winner_history.json")
CREDS_FILE = os.path.join(BASE_DIR, "credentials.json")

def get_discord_token() -> str:
    """Retrieve token from token.txt, falling back to environment variable."""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
            if token:
                return token

    token = os.getenv("DISCORD_BOT_TOKEN")
    if token:
        return token

    raise ValueError(
        f"Discord Bot Token not found! Please place your token inside '{TOKEN_FILE}' "
        "or set the DISCORD_BOT_TOKEN environment variable."
    )

def load_json(filepath, default):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

config = load_json(CONFIG_FILE, {})
messages = load_json(MESSAGES_FILE, {})
state = load_json(STATE_FILE, {
    "phase": "IDLE",  # IDLE, NOMINATIONS, VOTING, TIEBREAK
    "nominations": {}, 
    "votes": {},       
    "tiebreak_votes": {},
    "end_time": None,
    "notification_message_id": None  # Tracks the active notification to delete later
})

# ==========================================
# GOOGLE CALENDAR UTILITY
# ==========================================

def update_google_calendar_event(winner_name):
    if not os.path.exists(CREDS_FILE):
        print("Google Calendar credentials.json not found. Skipping calendar update.")
        return False
    
    try:
        scopes = ['https://www.googleapis.com/auth/calendar']
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scopes)
        service = build('calendar', 'v3', credentials=creds)
        
        cal_id = config.get("calendar_address", "primary")
        target_title = config.get("calendar_event_title", "Presidential Inauguration")

        events_result = service.events().list(
            calendarId=cal_id,
            q=target_title,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        if not events:
            print(f"No Google Calendar event found with title '{target_title}'")
            return False

        event = events[0]
        current_desc = event.get('description', '')
        new_desc = f"{current_desc}\n\n🏆 Election Winner: {winner_name}".strip()
        event['description'] = new_desc

        service.events().update(calendarId=cal_id, eventId=event['id'], body=event).execute()
        return True
    except Exception as e:
        print(f"Error updating Google Calendar: {e}")
        return False

# ==========================================
# DISCORD BOT SETUP
# ==========================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Helper: Role Verification
def check_roles(interaction: discord.Interaction, member_role_key: str, alliance_role_key: str):
    user_role_ids = [role.id for role in interaction.user.roles]
    
    permitted_members = config.get(member_role_key, [])
    has_member_role = any(rid in user_role_ids for rid in permitted_members)
    
    permitted_alliances = config.get(alliance_role_key, [])
    user_alliance_roles = [rid for rid in user_role_ids if rid in permitted_alliances]
    
    if not has_member_role or not user_alliance_roles:
        return False, None
    
    return True, str(user_alliance_roles[0])

# Helper: Limits Validation
def validate_max_items_limit():
    alliances = len(config.get("permitted_nominate_alliance_role_ids", []))
    noms_per_alliance = config.get("nominations_per_alliance", 1)
    total = alliances * noms_per_alliance
    if total > 25:
        return False, f"Configuration invalid: Potential total nominations ({total}) exceeds Discord's max limit of 25 per menu."
    return True, ""

# ==========================================
# UI COMPONENTS (MODALS & VIEWS)
# ==========================================

class NonDiscordNomineeModal(discord.ui.Modal, title="Nominate Non-Discord Candidate"):
    candidate_name = discord.ui.TextInput(
        label="Candidate Name / ID",
        placeholder="Enter non-Discord member name...",
        required=True
    )

    def __init__(self, alliance_role_id, original_interaction: discord.Interaction):
        super().__init__()
        self.alliance_role_id = alliance_role_id
        self.original_interaction = original_interaction

    async def on_submit(self, interaction: discord.Interaction):
        nominee = self.candidate_name.value.strip()
        alliance_noms = state["nominations"].setdefault(self.alliance_role_id, [])
        alliance_noms.append(nominee)
        save_json(STATE_FILE, state)

        max_noms = config.get("nominations_per_alliance", 1)
        remaining = max_noms - len(alliance_noms)

        if remaining > 0:
            view = ContinueNominationView(self.alliance_role_id)
            await interaction.response.edit_message(
                content=f"✅ Successfully nominated **{nominee}**!\n"
                        f"You have **{remaining}** nomination(s) remaining for your alliance.\n"
                        f"Would you like to nominate another?",
                view=view
            )
        else:
            await interaction.response.edit_message(
                content=f"✅ Successfully nominated **{nominee}**!\n"
                        f"🎉 Your alliance has used all available nominations ({max_noms}/{max_noms}).",
                view=None
            )

class NominateDropdownView(discord.ui.View):
    def __init__(self, guild: discord.Guild, alliance_role_id: str):
        super().__init__(timeout=120)
        self.alliance_role_id = alliance_role_id

        options = []
        for m in guild.members:
            if not m.bot:
                options.append(discord.SelectOption(label=m.display_name, value=m.display_name))
                if len(options) == 25:
                    break

        if options:
            select = discord.ui.Select(placeholder="Select a Discord Member...", options=options)
            select.callback = self.select_callback
            self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        nominee = interaction.data["values"][0]
        alliance_noms = state["nominations"].setdefault(self.alliance_role_id, [])
        alliance_noms.append(nominee)
        save_json(STATE_FILE, state)

        max_noms = config.get("nominations_per_alliance", 1)
        remaining = max_noms - len(alliance_noms)

        if remaining > 0:
            view = ContinueNominationView(self.alliance_role_id)
            await interaction.response.edit_message(
                content=f"✅ Successfully nominated **{nominee}**!\n"
                        f"You have **{remaining}** nomination(s) remaining.\n"
                        f"Would you like to nominate another?",
                view=view
            )
        else:
            await interaction.response.edit_message(
                content=f"✅ Successfully nominated **{nominee}**!\n"
                        f"🎉 Your alliance has submitted all allowed nominations ({max_noms}/{max_noms}).",
                view=None
            )

    @discord.ui.button(label="Nominate Non-Discord Member", style=discord.ButtonStyle.secondary, row=2)
    async def non_discord_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NonDiscordNomineeModal(self.alliance_role_id, interaction))

class ContinueNominationView(discord.ui.View):
    def __init__(self, alliance_role_id):
        super().__init__(timeout=60)
        self.alliance_role_id = alliance_role_id

    @discord.ui.button(label="Nominate Another", style=discord.ButtonStyle.primary)
    async def nominate_more(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = NominateDropdownView(interaction.guild, self.alliance_role_id)
        all_noms = [nom for noms in state["nominations"].values() for nom in noms]
        all_noms_str = "\n".join([f"• {n}" for n in all_noms]) if all_noms else "None yet."
        
        await interaction.response.edit_message(
            content=f"**Current Nominees across all alliances:**\n{all_noms_str}\n\nSelect your next nominee:",
            view=view
        )

    @discord.ui.button(label="Done", style=discord.ButtonStyle.secondary)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="✅ Nomination process finished.", view=None)

class NominateButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Nominate", style=discord.ButtonStyle.success, custom_id="nominate_btn_main")
    async def nominate_click(self, interaction: discord.Interaction, button: discord.ui.Button):
        if state["phase"] != "NOMINATIONS":
            return await interaction.response.send_message("Nominations are currently closed.", ephemeral=True)

        valid, alliance_role_id = check_roles(
            interaction, 
            "permitted_nominate_member_role_ids", 
            "permitted_nominate_alliance_role_ids"
        )
        if not valid:
            return await interaction.response.send_message("❌ You do not have the required Member & Alliance roles to nominate.", ephemeral=True)

        max_noms = config.get("nominations_per_alliance", 1)
        current_noms = state["nominations"].get(alliance_role_id, [])

        if len(current_noms) >= max_noms:
            return await interaction.response.send_message(f"❌ Your alliance has already submitted the maximum allowed nominations ({max_noms}/{max_noms}).", ephemeral=True)

        all_noms = [nom for noms in state["nominations"].values() for nom in noms]
        all_noms_str = "\n".join([f"• {n}" for n in all_noms]) if all_noms else "None yet."

        view = NominateDropdownView(interaction.guild, alliance_role_id)
        
        # If triggered from the persistent button, we respond normally. If from a slash command, we edit or send.
        if interaction.response.is_done():
            await interaction.followup.send(
                f"**Currently Nominated Members (All Alliances):**\n{all_noms_str}\n\nPlease choose your candidate:",
                view=view,
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"**Currently Nominated Members (All Alliances):**\n{all_noms_str}\n\nPlease choose your candidate:",
                view=view,
                ephemeral=True
            )

class VoteDropdownView(discord.ui.View):
    def __init__(self, candidates: list, alliance_role_id: str, is_tiebreak: bool = False):
        super().__init__(timeout=120)
        self.alliance_role_id = alliance_role_id
        self.is_tiebreak = is_tiebreak

        options = [discord.SelectOption(label=c, value=c) for c in candidates[:25]]
        select = discord.ui.Select(placeholder="Select candidate to vote for...", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        selected_candidate = interaction.data["values"][0]
        store_key = "tiebreak_votes" if self.is_tiebreak else "votes"
        limit_key = "tiebreak_votes_per_alliance" if self.is_tiebreak else "votes_per_alliance"
        
        alliance_votes = state[store_key].setdefault(self.alliance_role_id, [])
        
        allow_multiple = config.get("allow_multiple_votes_same_candidate", False)
        if not allow_multiple and selected_candidate in alliance_votes:
            return await interaction.response.send_message(
                f"❌ Your alliance has already voted for **{selected_candidate}**. You cannot cast multiple votes for the same candidate.",
                ephemeral=True
            )

        alliance_votes.append(selected_candidate)
        save_json(STATE_FILE, state)

        max_votes = config.get(limit_key, 1)
        remaining = max_votes - len(alliance_votes)

        if remaining > 0:
            view = ContinueVoteView(self.alliance_role_id, self.is_tiebreak)
            await interaction.response.edit_message(
                content=f"✅ Vote cast for **{selected_candidate}**!\n"
                        f"You have **{remaining}** vote(s) remaining.\nWould you like to cast another?",
                view=view
            )
        else:
            await interaction.response.edit_message(
                content=f"✅ Vote cast for **{selected_candidate}**!\n"
                        f"🎉 Your alliance has submitted all allowed votes ({max_votes}/{max_votes}).",
                view=None
            )

class ContinueVoteView(discord.ui.View):
    def __init__(self, alliance_role_id, is_tiebreak=False):
        super().__init__(timeout=60)
        self.alliance_role_id = alliance_role_id
        self.is_tiebreak = is_tiebreak

    @discord.ui.button(label="Cast Another Vote", style=discord.ButtonStyle.primary)
    async def vote_more(self, interaction: discord.Interaction, button: discord.ui.Button):
        store_key = "tiebreak_votes" if self.is_tiebreak else "votes"
        cast_votes = state[store_key].get(self.alliance_role_id, [])
        votes_str = "\n".join([f"• {v}" for v in cast_votes]) if cast_votes else "None."

        candidates = state.get("tiebreak_candidates", []) if self.is_tiebreak else [c for noms in state["nominations"].values() for c in noms]
        view = VoteDropdownView(candidates, self.alliance_role_id, self.is_tiebreak)

        await interaction.response.edit_message(
            content=f"**Votes already cast by your alliance:**\n{votes_str}\n\nSelect candidate:",
            view=view
        )

    @discord.ui.button(label="Done", style=discord.ButtonStyle.secondary)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="✅ Voting process completed.", view=None)

class VoteButtonView(discord.ui.View):
    def __init__(self, is_tiebreak=False):
        super().__init__(timeout=None)
        self.is_tiebreak = is_tiebreak

    @discord.ui.button(label="Vote", style=discord.ButtonStyle.primary, custom_id="vote_btn_main")
    async def vote_click(self, interaction: discord.Interaction, button: discord.ui.Button):
        expected_phase = "TIEBREAK" if self.is_tiebreak else "VOTING"
        if state["phase"] != expected_phase:
            return await interaction.response.send_message("Voting is currently closed.", ephemeral=True)

        valid, alliance_role_id = check_roles(
            interaction, 
            "permitted_vote_member_role_ids", 
            "permitted_vote_alliance_role_ids"
        )
        if not valid:
            return await interaction.response.send_message("❌ You do not have permission/alliance roles to vote.", ephemeral=True)

        limit_key = "tiebreak_votes_per_alliance" if self.is_tiebreak else "votes_per_alliance"
        store_key = "tiebreak_votes" if self.is_tiebreak else "votes"
        
        max_votes = config.get(limit_key, 1)
        current_votes = state[store_key].get(alliance_role_id, [])

        if len(current_votes) >= max_votes:
            return await interaction.response.send_message(f"❌ Your alliance has used all allocated votes ({max_votes}/{max_votes}).", ephemeral=True)

        votes_str = "\n".join([f"• {v}" for v in current_votes]) if current_votes else "None."

        if self.is_tiebreak:
            candidates = state.get("tiebreak_candidates", [])
        else:
            candidates = [c for noms in state["nominations"].values() for c in noms]

        view = VoteDropdownView(candidates, alliance_role_id, self.is_tiebreak)
        
        if interaction.response.is_done():
            await interaction.followup.send(
                f"**Votes already cast by your alliance:**\n{votes_str}\n\nSelect candidate to vote for:",
                view=view,
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"**Votes already cast by your alliance:**\n{votes_str}\n\nSelect candidate to vote for:",
                view=view,
                ephemeral=True
            )

# ==========================================
# ELECTION CYCLE LOGIC
# ==========================================

async def delete_previous_notification():
    """Deletes the announcement message of the previous phase to keep the channel clean."""
    msg_id = state.get("notification_message_id")
    if msg_id:
        staff_chan = bot.get_channel(config.get("staff_channel_id"))
        if staff_chan:
            try:
                msg = await staff_chan.fetch_message(msg_id)
                await msg.delete()
            except discord.NotFound:
                pass  # Message was already deleted manually
            except discord.HTTPException as e:
                print(f"Failed to delete notification message: {e}")
    
    state["notification_message_id"] = None

async def start_nomination_phase():
    await delete_previous_notification()
    
    state["phase"] = "NOMINATIONS"
    state["nominations"] = {}
    state["votes"] = {}
    state["tiebreak_votes"] = {}
    
    hours = config.get("nomination_length_hours", 24)
    end_dt = datetime.now(pytz.utc) + timedelta(hours=hours)
    state["end_time"] = end_dt.isoformat()
    
    staff_chan = bot.get_channel(config.get("staff_channel_id"))
    if staff_chan:
        msg = messages.get("nomination_announcement", "Nominations Open!")
        sent_msg = await staff_chan.send(msg, view=NominateButtonView())
        state["notification_message_id"] = sent_msg.id
        
    save_json(STATE_FILE, state)

async def start_voting_phase():
    await delete_previous_notification()
    
    state["phase"] = "VOTING"
    hours = config.get("voting_length_hours", 24)
    end_dt = datetime.now(pytz.utc) + timedelta(hours=hours)
    state["end_time"] = end_dt.isoformat()

    candidates = [c for noms in state["nominations"].values() for c in noms]
    candidates_formatted = "\n".join([f"• {c}" for c in set(candidates)]) or "No candidates nominated."

    staff_chan = bot.get_channel(config.get("staff_channel_id"))
    if staff_chan:
        msg = messages.get("voting_announcement", "Voting Open!").format(candidate_list=candidates_formatted)
        sent_msg = await staff_chan.send(msg, view=VoteButtonView(is_tiebreak=False))
        state["notification_message_id"] = sent_msg.id
        
    save_json(STATE_FILE, state)

async def start_tiebreak_phase(tied_candidates):
    await delete_previous_notification()
    
    state["phase"] = "TIEBREAK"
    state["tiebreak_candidates"] = tied_candidates
    hours = config.get("tiebreak_length_hours", 12)
    end_dt = datetime.now(pytz.utc) + timedelta(hours=hours)
    state["end_time"] = end_dt.isoformat()

    cand_str = "\n".join([f"• {c}" for c in tied_candidates])
    staff_chan = bot.get_channel(config.get("staff_channel_id"))
    if staff_chan:
        msg = messages.get("tiebreak_announcement", "Tiebreak!").format(candidate_list=cand_str)
        sent_msg = await staff_chan.send(msg, view=VoteButtonView(is_tiebreak=True))
        state["notification_message_id"] = sent_msg.id
        
    save_json(STATE_FILE, state)

async def finalize_election():
    await delete_previous_notification()
    
    state["phase"] = "IDLE"
    state["end_time"] = None
    save_json(STATE_FILE, state)

    vote_source = state["tiebreak_votes"] if state.get("tiebreak_votes") else state["votes"]
    tally = {}
    for votes_list in vote_source.values():
        for candidate in votes_list:
            tally[candidate] = tally.get(candidate, 0) + 1

    if not tally:
        winner = "Nobody (No votes cast)"
        max_votes = 0
    else:
        max_votes = max(tally.values())
        winners = [cand for cand, count in tally.items() if count == max_votes]

        if len(winners) > 1 and state.get("phase") != "TIEBREAK":
            await start_tiebreak_phase(winners)
            return

        winner = winners[0] if winners else "None"

    detailed_result = {
        "timestamp": datetime.now(pytz.utc).isoformat(),
        "winner": winner,
        "vote_counts": tally,
        "nominations_by_alliance": state["nominations"],
        "votes_by_alliance": state["votes"],
        "tiebreak_votes_by_alliance": state["tiebreak_votes"]
    }
    save_json(LAST_RESULT_FILE, detailed_result)

    history = load_json(WINNER_HISTORY_FILE, [])
    history.append({"timestamp": datetime.now(pytz.utc).isoformat(), "winner": winner})
    save_json(WINNER_HISTORY_FILE, history)

    staff_chan = bot.get_channel(config.get("staff_channel_id"))
    if staff_chan:
        def build_audit_map(source_dict):
            audit_map = {}
            for alliance_id, item_list in source_dict.items():
                for item in item_list:
                    audit_map.setdefault(item, []).append(alliance_id)
            return audit_map

        audit_msg = f"📊 **ELECTION RESULTS SUMMARY**\n🏆 **Winner:** {winner}\n\n"
        
        noms_map = build_audit_map(state["nominations"])
        audit_msg += "**📜 Nominations Audit:**\n"
        if not noms_map:
            audit_msg += "• No nominations recorded.\n"
        for nom, alliances in noms_map.items():
            alliance_tags = ", ".join([f"<@&{aid}>" for aid in alliances])
            audit_msg += f"• **{nom}** (Nominated by: {alliance_tags})\n"

        votes_map = build_audit_map(state["votes"])
        audit_msg += "\n**🗳️ Standard Voting Audit:**\n"
        if not votes_map:
            audit_msg += "• No standard votes recorded.\n"
        for cand, alliances in votes_map.items():
            alliance_tags = ", ".join([f"<@&{aid}>" for aid in alliances])
            audit_msg += f"• **{cand}**: {len(alliances)} vote(s) (Voted by: {alliance_tags})\n"

        tb_votes_map = build_audit_map(state["tiebreak_votes"])
        if tb_votes_map:
            audit_msg += "\n**⚖️ Tie-Break Voting Audit:**\n"
            for cand, alliances in tb_votes_map.items():
                alliance_tags = ", ".join([f"<@&{aid}>" for aid in alliances])
                audit_msg += f"• **{cand}**: {len(alliances)} vote(s) (Voted by: {alliance_tags})\n"

        try:
            await staff_chan.send(audit_msg)
        except discord.HTTPException:
            await staff_chan.send("❌ The audit is too large to display in a single Discord message, but results have been saved to the logs.")
    # -----------------------------------

    member_chan = bot.get_channel(config.get("member_channel_id"))
    if member_chan:
        msg = messages.get("winner_announcement", "Winner: {winner}").format(winner=winner)
        await member_chan.send(msg)

    update_google_calendar_event(winner)

# Loop Task to manage transitions
@tasks.loop(seconds=30)
async def election_scheduler():
    if state["phase"] == "IDLE":
        start_str = config.get("initial_start_datetime")
        tz_str = config.get("timezone", "UTC")
        if start_str:
            tz = pytz.timezone(tz_str)
            target_start = tz.localize(datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S"))
            now = datetime.now(tz)
            if now >= target_start:
                freq = config.get("repeat_frequency_days", 30)
                next_start = target_start + timedelta(days=freq)
                config["initial_start_datetime"] = next_start.strftime("%Y-%m-%d %H:%M:%S")
                save_json(CONFIG_FILE, config)
                
                await start_nomination_phase()
    else:
        end_time_str = state.get("end_time")
        if end_time_str:
            end_dt = datetime.fromisoformat(end_time_str)
            if datetime.now(pytz.utc) >= end_dt:
                if state["phase"] == "NOMINATIONS":
                    await start_voting_phase()
                elif state["phase"] in ["VOTING", "TIEBREAK"]:
                    await finalize_election()

# ==========================================
# SLASH COMMANDS
# ==========================================

def is_mod():
    async def predicate(interaction: discord.Interaction):
        mod_role_id = config.get("mod_role_id")
        user_roles = [r.id for r in interaction.user.roles]
        if mod_role_id in user_roles or interaction.user.guild_permissions.administrator:
            return True
        await interaction.response.send_message("❌ You require Moderator permissions to run this command.", ephemeral=True)
        return False
    return app_commands.check(predicate)

async def setting_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    # Pull all current keys from the config file dynamically
    current_config = load_json(CONFIG_FILE, config)
    keys = list(current_config.keys())
    
    # Filter choices based on what the user is typing
    return [
        app_commands.Choice(name=key, value=key)
        for key in keys if current.lower() in key.lower()
    ][:25] # Discord allows a maximum of 25 autocomplete choices

@bot.tree.command(name="config_edit", description="Edit bot configuration settings (Mod Only)")
@app_commands.autocomplete(setting=setting_autocomplete)
@is_mod()
async def config_edit(interaction: discord.Interaction, setting: str, value: str):
    # Force load latest config
    current_config = load_json(CONFIG_FILE, config)
    
    if setting not in current_config:
        return await interaction.response.send_message(f"❌ Setting `{setting}` not found.", ephemeral=True)

    try:
        # Determine data type from existing config structure
        target_val = current_config[setting]
        if isinstance(target_val, int):
            parsed_val = int(value)
        elif isinstance(target_val, list):
            parsed_val = [int(x.strip()) for x in value.split(",")]
        else:
            parsed_val = value

        temp_config = current_config.copy()
        temp_config[setting] = parsed_val
        alliances_len = len(temp_config.get("permitted_nominate_alliance_role_ids", []))
        noms_per_alliance = temp_config.get("nominations_per_alliance", 1)
        
        if alliances_len * noms_per_alliance > 25:
            return await interaction.response.send_message(
                f"❌ Cannot accept update: `permitted_nominate_alliance_role_ids` count × `nominations_per_alliance` ({alliances_len * noms_per_alliance}) exceeds maximum dropdown size (25).",
                ephemeral=True
            )

        config[setting] = parsed_val
        save_json(CONFIG_FILE, config)
        await interaction.response.send_message(f"✅ Updated `{setting}` to: `{parsed_val}`", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Invalid value format provided: {e}", ephemeral=True)

@bot.tree.command(name="config_view", description="View bot configuration")
async def config_view(interaction: discord.Interaction):
    try:
        # Check if user has permitted nomination roles or voting roles
        nom_valid, _ = check_roles(
            interaction, 
            "permitted_nominate_member_role_ids", 
            "permitted_nominate_alliance_role_ids"
        )
        vote_valid, _ = check_roles(
            interaction, 
            "permitted_vote_member_role_ids", 
            "permitted_vote_alliance_role_ids"
        )
        
        # Check if user is a moderator or admin
        mod_role_id = config.get("mod_role_id")
        user_role_ids = [r.id for r in interaction.user.roles]
        is_mod_user = mod_role_id in user_role_ids or interaction.user.guild_permissions.administrator

        if not (nom_valid or vote_valid or is_mod_user):
            return await interaction.response.send_message(
                "❌ You do not have the required roles to view the configuration.", 
                ephemeral=True
            )

        print(f"Checking config file path: {os.path.abspath(CONFIG_FILE)}")
        print(f"File exists: {os.path.exists(CONFIG_FILE)}")
        
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw_data = f.read()
            
        print(f"Raw file contents read: {raw_data}")
        
        if not raw_data.strip():
            return await interaction.response.send_message("❌ Error: `config.json` is completely empty inside the container!", ephemeral=True)

        if len(raw_data) > 1900:
            raw_data = raw_data[:1900] + "\n... (truncated)"
            
        await interaction.response.send_message(f"```json\n{raw_data}\n```", ephemeral=True)
    except Exception as e:
        print(f"Error in config_view: {e}")
        await interaction.response.send_message(f"❌ Error reading config: {e}", ephemeral=True)

@bot.tree.command(name="election_start_early", description="Force start an election cycle immediately (Mod Only)")
@is_mod()
async def election_start_early(interaction: discord.Interaction):
    valid, err_msg = validate_max_items_limit()
    if not valid:
        return await interaction.response.send_message(f"❌ {err_msg}", ephemeral=True)

    await start_nomination_phase()
    await interaction.response.send_message("🚀 Election cycle manually initiated!", ephemeral=True)

@bot.tree.command(name="election_cancel", description="Cancel the currently running election (Mod Only)")
@is_mod()
async def election_cancel(interaction: discord.Interaction):
    if state["phase"] == "IDLE":
        return await interaction.response.send_message("❌ There is no active election to cancel.", ephemeral=True)

    # Clean up the ongoing channel notifications
    await delete_previous_notification()

    # Reset state to IDLE
    state["phase"] = "IDLE"
    state["nominations"] = {}
    state["votes"] = {}
    state["tiebreak_votes"] = {}
    state["end_time"] = None
    if "tiebreak_candidates" in state:
        del state["tiebreak_candidates"]

    save_json(STATE_FILE, state)

    await interaction.response.send_message("⏹️ The current election cycle has been manually cancelled and reset.", ephemeral=True)

@bot.tree.command(name="election_skip_phase", description="End the current phase and proceed to the next (Mod Only)")
@is_mod()
async def election_skip_phase(interaction: discord.Interaction):
    current_phase = state.get("phase")
    
    if current_phase == "IDLE":
        return await interaction.response.send_message(
            "❌ The election is currently IDLE. Use `/election_start_early` to begin a new cycle.", 
            ephemeral=True
        )
        
    elif current_phase == "NOMINATIONS":
        await start_voting_phase()
        await interaction.response.send_message(
            "⏭️ **Phase Skipped:** The nomination phase was ended early. Voting has now started!", 
            ephemeral=True
        )
        
    elif current_phase in ["VOTING", "TIEBREAK"]:
        # finalize_election automatically handles checking for tiebreaks
        # or officially ending the election and announcing the winner.
        await finalize_election()
        await interaction.response.send_message(
            "⏭️ **Phase Skipped:** The voting phase was ended early. The election is being finalized!", 
            ephemeral=True
        )

@bot.tree.command(name="nominate", description="Shortcut command to trigger nomination modal")
async def nominate_cmd(interaction: discord.Interaction):
    if state["phase"] != "NOMINATIONS":
        return await interaction.response.send_message("Nominations are not currently open.", ephemeral=True)
    
    view = NominateButtonView()
    await view.nominate_click.callback(view, interaction)

@bot.tree.command(name="vote", description="Shortcut command to trigger voting modal")
async def vote_cmd(interaction: discord.Interaction):
    if state["phase"] not in ["VOTING", "TIEBREAK"]:
        return await interaction.response.send_message("Voting is not currently open.", ephemeral=True)

    is_tb = (state["phase"] == "TIEBREAK")
    view = VoteButtonView(is_tiebreak=is_tb)
    await view.vote_click.callback(view, interaction)

@bot.tree.command(name="reset_alliance_nominations", description="Reset nominations for your alliance")
async def reset_alliance_nominations(interaction: discord.Interaction):
    # Verify the user has member and alliance roles, and fetch their alliance role ID
    valid, alliance_role_id = check_roles(
        interaction, 
        "permitted_nominate_member_role_ids", 
        "permitted_nominate_alliance_role_ids"
    )
    if not valid or not alliance_role_id:
        return await interaction.response.send_message("❌ You do not have the required Member & Alliance roles to manage nominations.", ephemeral=True)

    if alliance_role_id in state["nominations"]:
        del state["nominations"][alliance_role_id]
        save_json(STATE_FILE, state)
        await interaction.response.send_message(f"✅ Successfully reset all nominations for your alliance (<@&{alliance_role_id}>).", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Your alliance has not submitted any nominations to reset.", ephemeral=True)

@bot.tree.command(name="reset_alliance_votes", description="Reset votes for your alliance")
async def reset_alliance_votes(interaction: discord.Interaction):
    # Verify the user has member and alliance roles, and fetch their alliance role ID
    valid, alliance_role_id = check_roles(
        interaction, 
        "permitted_vote_member_role_ids", 
        "permitted_vote_alliance_role_ids"
    )
    if not valid or not alliance_role_id:
        return await interaction.response.send_message("❌ You do not have the required Member & Alliance roles to manage votes.", ephemeral=True)

    deleted = False
    if alliance_role_id in state["votes"]:
        del state["votes"][alliance_role_id]
        deleted = True
    if alliance_role_id in state["tiebreak_votes"]:
        del state["tiebreak_votes"][alliance_role_id]
        deleted = True

    if deleted:
        save_json(STATE_FILE, state)
        await interaction.response.send_message(f"✅ Successfully reset all votes for your alliance (<@&{alliance_role_id}>).", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Your alliance has not cast any votes to reset.", ephemeral=True)

@bot.tree.command(name="results_view", description="View latest election results")
async def results_view(interaction: discord.Interaction):
    last_res = load_json(LAST_RESULT_FILE, None)
    if not last_res:
        return await interaction.response.send_message("No previous election results found.", ephemeral=True)

    user_roles = [r.id for r in interaction.user.roles]
    is_staff = config.get("staff_channel_id") == interaction.channel_id or config.get("mod_role_id") in user_roles

    if is_staff:
        msg = f"📊 **FULL ELECTION RESULTS ({last_res.get('timestamp')})**\n"
        msg += f"🏆 **Winner:** {last_res.get('winner')}\n\n"
        
        def build_audit_map(source_dict):
            audit_map = {}
            for alliance_id, item_list in source_dict.items():
                for item in item_list:
                    audit_map.setdefault(item, []).append(alliance_id)
            return audit_map

        noms_map = build_audit_map(last_res.get("nominations_by_alliance", {}))
        msg += "**📜 Nominations Audit:**\n"
        if not noms_map:
            msg += "• No nominations recorded.\n"
        for nom, alliances in noms_map.items():
            alliance_tags = ", ".join([f"<@&{aid}>" for aid in alliances])
            msg += f"• **{nom}** (Nominated by: {alliance_tags})\n"

        votes_map = build_audit_map(last_res.get("votes_by_alliance", {}))
        msg += "\n**🗳️ Standard Voting Audit:**\n"
        if not votes_map:
            msg += "• No standard votes recorded.\n"
        for cand, alliances in votes_map.items():
            alliance_tags = ", ".join([f"<@&{aid}>" for aid in alliances])
            msg += f"• **{cand}**: {len(alliances)} vote(s) (Voted by: {alliance_tags})\n"

        tb_votes_map = build_audit_map(last_res.get("tiebreak_votes_by_alliance", {}))
        if tb_votes_map:
            msg += "\n**⚖️ Tie-Break Voting Audit:**\n"
            for cand, alliances in tb_votes_map.items():
                alliance_tags = ", ".join([f"<@&{aid}>" for aid in alliances])
                msg += f"• **{cand}**: {len(alliances)} vote(s) (Voted by: {alliance_tags})\n"

        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ The audit is too large to display in a single Discord message.", 
                ephemeral=True
            )

# ==========================================
# BOT EVENTS & EXECUTION
# ==========================================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    bot.add_view(NominateButtonView())
    bot.add_view(VoteButtonView(is_tiebreak=False))
    bot.add_view(VoteButtonView(is_tiebreak=True))
    
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    if not election_scheduler.is_running():
        election_scheduler.start()

if __name__ == "__main__":
    token = get_discord_token()
    bot.run(token)
