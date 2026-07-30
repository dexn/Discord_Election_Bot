# Discord Alliance Election Bot

This is a fully automated Discord bot designed to manage cyclic, alliance-based elections within a server. It handles everything from scheduled nominations and voting phases to tiebreaks, result auditing, and Google Calendar integration.

The bot is highly configurable, ensuring that only specific roles (e.g., verified members of registered alliances) can participate in the election process, while server moderators retain full control over the configuration and lifecycle.

---

## Features

* **Automated Election Cycles:** Automatically triggers elections on a recurring schedule based on your configured timezone and start date.
* **Phase Management:** Seamlessly transitions between `IDLE`, `NOMINATIONS`, `VOTING`, and `TIEBREAK` phases based on configurable timers.
* **Role-Based Access Control:** Validates both a "Member" role and a specific "Alliance" role before allowing a user to nominate or vote.
* **Persistent State:** Saves election progress and historical data to local JSON files, allowing the bot to survive restarts without losing votes.
* **Interactive UI:** Utilizes modern Discord features like Slash Commands, Dropdowns, and Modals for a clean user experience.
* **Google Calendar Integration:** Automatically updates a Google Calendar event with the name of the election winner.

---

## Prerequisites

* Python 3.8+
* A Discord Bot Token
* Google Cloud Service Account Credentials (for Google Calendar integration)
* Required Python packages: `discord.py`, `pytz`, `google-api-python-client`, `google-auth-oauthlib`

---

## Setup & Installation

1. **Clone the repository** and install dependencies:
```bash
pip install discord.py pytz google-api-python-client google-auth-oauthlib

```


2. **Add your Discord Token:**
Place your token inside a file named `token.txt` in the root directory, or set it as an environment variable: `DISCORD_BOT_TOKEN`.
3. **Add Google Credentials:**
Place your Google Service Account JSON file in the root directory and name it `credentials.json`.
4. **Create `config.json`:**
Create a `config.json` file in the root directory (see the Configuration section below).
5. **Run the bot:**
```bash
python bot.py

```



---

## Configuration (`config.json`)

The bot relies heavily on `config.json` to define roles, channels, and timings. Below is a breakdown of the required keys:

### Roles, Channels & Permissions

* **`mod_role_id`**: The ID of the role allowed to edit configs and force-start/cancel elections.
* **`staff_channel_id`**: Where the bot posts phase announcements, voting buttons, and detailed audit logs.
* **`member_channel_id`**: Where the bot posts the final public winner announcement.
* **`permitted_nominate_member_role_ids`**: List of role IDs that verify a user is an official member allowed to nominate.
* **`permitted_nominate_alliance_role_ids`**: List of valid alliance role IDs allowed to submit nominations.
* **`nominations_per_alliance`**: Number of candidates each alliance can nominate.
* **`permitted_vote_member_role_ids`**: List of role IDs verifying a user is allowed to vote.
* **`permitted_vote_alliance_role_ids`**: List of valid alliance role IDs allowed to cast votes.
* **`votes_per_alliance`**: Number of votes each alliance gets in standard voting.
* **`tiebreak_votes_per_alliance`**: Number of votes each alliance gets in a tiebreak.

> **Note:** The bot strictly enforces Discord's UI limits. The number of allowed alliances multiplied by `nominations_per_alliance` must not exceed 25, or the dropdown menus will fail to render.

### Timing & Scheduling

* **`timezone`**: The timezone for scheduling (e.g., `"Australia/Brisbane"`, `"UTC"`).
* **`initial_start_datetime`**: When the next election should start, formatted as `"YYYY-MM-DD HH:MM:SS"`.
* **`repeat_frequency_days`**: How many days between automatic elections (e.g., `30`).
* **`nomination_length_hours`**: Duration of the nomination phase.
* **`voting_length_hours`**: Duration of the voting phase.
* **`tiebreak_length_hours`**: Duration of the tiebreak phase.

###  Calendar Event

* **`calendar_address`**: The Google Calendar ID to update.
* **`calendar_event_title`**: The exact title of the recurring event to append the winner's name to.

---

## How It Works (The Election Cycle)

1. **IDLE:** The bot waits until the current time matches the `initial_start_datetime` (adjusted for the specified `timezone`).
2. **NOMINATIONS:** The bot posts an announcement with a "Nominate" button in the staff channel. Authorized alliance members can nominate Discord users via dropdowns or non-Discord members via a text modal.
3. **VOTING:** Once the timer expires, the bot compiles all nominees into a dropdown list. Authorized members cast votes on behalf of their alliances.
4. **TIEBREAK (If necessary):** If two or more candidates tie for first place, a sudden-death voting phase begins exclusively between the tied candidates.
5. **RESOLUTION:** The winner is calculated and announced. A detailed audit log is posted to the staff channel, the public announcement is made in the member channel, and the associated Google Calendar event is updated with the winner's name. The bot schedules the next election and returns to `IDLE`.

---

## Slash Commands

### Moderator Commands

* **`/config_edit <setting> <value>`**: Edit any value in `config.json` dynamically. Features autocomplete for setting names.
* **`/election_start_early`**: Bypasses the scheduler and forces the nomination phase to begin immediately.
* **`/election_cancel`**: Aborts the current election, deletes active menus, and resets the bot to the IDLE state.

### General / Alliance Commands

* **`/config_view`**: Prints the current configuration data.
* **`/nominate`**: A shortcut command to trigger the nomination interface without scrolling up to find the original button.
* **`/vote`**: A shortcut command to trigger the voting interface.
* **`/reset_alliance_nominations`**: Wipes all currently submitted nominations for the user's alliance.
* **`/reset_alliance_votes`**: Wipes all currently cast votes for the user's alliance.
* **`/results_view`**: Displays the results of the most recently completed election. (Shows detailed audits for staff, and a simple summary for standard members).
