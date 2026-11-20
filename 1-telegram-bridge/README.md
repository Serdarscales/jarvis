# Claude Code Telegram Bridge: Complete Build Kit

> **Easiest path:** put this folder somewhere on your Mac, open a terminal in it, start Claude Code (`claude`), and say: **"Read README.md and set the Telegram bridge up for me step by step."** Claude will run the whole install with you, verifying each step. Everything below also works as a manual guide.

This is **part 1** of the kit and the base for everything else. Part 2 in `../2-jarvis/` (Jarvis, a voice orchestrator that lives in one topic) is an add-on that needs this part installed and working first. Install this part, confirm the bot answers, then add Jarvis.

## Why This Exists

Claude Code is the most capable AI assistant you can run, but it lives in a terminal on your Mac. This bridge puts it in your pocket: a private Telegram group where **every topic is its own persistent Claude Code conversation**. You text it, voice-note it, or send it photos and files from anywhere, and it answers with full access to your Mac, your files, and every tool Claude Code has (web search, bash, file editing, subagents).

In practice that means: you are walking somewhere, you voice-note a thought into the "Ideas" topic, and Claude transcribes it, thinks about it, and files it properly in your notes. You send a screenshot of an error into the "Debugging" topic and Claude reads it and fixes the code. Days later you come back to any topic and the conversation continues where it left off.

## What You're Building

- A Telegram bot bridged to Claude Code CLI sessions on your Mac
- **One Telegram forum topic = one persistent Claude session** (context survives restarts)
- Live streaming responses: the message edits itself as Claude thinks, with a tool-activity indicator ("Searching web: ...", "Running: ...") and a Stop button
- **Voice notes** transcribed locally (free, private, mlx-whisper on Apple Silicon) or via OpenAI/Groq
- **Photos and files**: sent to Claude with the local path so it can read them; albums are handled as one message; oversized images auto-downscaled; uploads auto-purged after 48h
- Commands: `/clear`, `/stop`, `/topic <name>`, `/context` (token usage), `/status`, `/id`, `/help`
- A **trigger system**: any script or cron job can drop a JSON file into a folder and the bridge runs it through a topic's session and posts the result (this is how you build scheduled morning briefings later)
- Battle-tested reliability layer: auto-retry on auth/API blips, requeue of failed scheduled messages, daily 4 AM self-restart for memory hygiene, log rotation, duplicate-instance protection
- **Model fallback:** when your model hits a usage cap, the bridge reruns the message on the next model in a fallback ladder (default Sonnet), tells you once in one line, and switches back on its own when the cap resets
- **Steady effort level:** the bridge pins Claude's reasoning effort (default `high`) so it does not silently change when you pick a different effort in the Claude desktop app or an IDE. Model and effort can also be set per topic
- Runs as a macOS LaunchAgent: starts at login, restarts if it crashes

```
Telegram Group (Topics)  <-->  bridge.py (this bot)  <-->  claude CLI (one session per topic)
        phone                     your Mac                   your Mac
```

**Stack:** one Python file (~3,300 lines), python-telegram-bot, Claude Code CLI. No server, no cloud, no database. **Cost:** nothing on top of your existing Claude subscription with local voice transcription (`WHISPER_PROVIDER=local`, free: your voice notes still travel through Telegram like any message, but no extra transcription service receives them). If you pick OpenAI or Groq for transcription instead, your voice notes are sent to that service and billed per minute on your own account with them. Claude usage counts against your plan's normal limits.

## Read This First: Security

The bridge runs Claude with `--dangerously-skip-permissions`. That is what makes it magic (no permission prompts blocking it while you are away from the Mac), and it means **whoever can message this bot can do anything on your Mac that you can do**. The protection model:

1. Only Telegram user IDs in `ALLOWED_USER_IDS` are accepted. Everyone else is silently ignored.
2. Optionally locked to one specific group via `TELEGRAM_GROUP_ID` (recommended, do it).
3. Keep the bot token secret and the group private, invite nobody.

Put only your own user ID in the allowlist. Do not add friends "to try it".

## Prerequisites

- A Mac (Apple Silicon recommended; required for free local voice transcription)
- **Claude Code installed, up to date and logged in**: `claude --version` works and you have used it at least once. Run `claude update` first if your install is old (the bridge passes `--effort`, which older versions do not know)
- **Python 3.10 or newer**: check with `python3 --version`
- A Telegram account
- 20-30 minutes

## Step-by-Step Setup

> **For Claude:** if the user asked you to run this setup, work through the steps in order. Run the verification at the end of each step before moving on, and show the user what you found. Steps 1 and 2 happen on the user's phone, so tell them exactly what to tap and wait for them to paste the results back.

### Step 1: Create the bot (on your phone)

1. In Telegram, open **@BotFather**, send `/newbot`
2. Pick a display name (e.g. "My Claude") and a username (must end in `bot`, e.g. `yourname_claude_bot`)
3. BotFather replies with a **bot token** like `1234567890:AAF...`. Save it, you need it in Step 4.
4. Still in BotFather: `/setprivacy`, select your bot, choose **Disable**. (This lets the bot see normal group messages, not just commands.)

**Verify:** you have a token that looks like `digits:letters`.

### Step 2: Create the group (on your phone)

1. Create a new Telegram group with just yourself in it (add the bot as the second member)
2. Group Settings > Edit > **Topics** > enable. This turns the group into a forum with topics.
3. Promote the bot to **admin** with at least the "Manage topics" permission
4. Get your own user ID: message **@userinfobot**, it replies with your numeric ID

**Verify:** the group shows a "General" topic, the bot is an admin, and you have your numeric user ID.

### Step 3: Install the files (on the Mac)

Open a terminal in this folder (`1-telegram-bridge`), then:

```bash
mkdir -p ~/.claude/telegram-bridge/logs
cp bridge.py requirements.txt .env.example com.user.claude-telegram-bridge.plist.template ~/.claude/telegram-bridge/
cd ~/.claude/telegram-bridge
python3 -m pip install -r requirements.txt
```

Note for Intel Macs: edit `requirements.txt` first and comment out the `mlx-whisper` line (local transcription needs Apple Silicon; use the `openai` or `groq` provider instead, or disable voice).

**If pip refuses with "externally-managed-environment"** (common with Homebrew Python): use a virtual environment instead:

```bash
python3 -m venv ~/.claude/telegram-bridge/venv
~/.claude/telegram-bridge/venv/bin/python3 -m pip install -r requirements.txt
```

If you do this, use `~/.claude/telegram-bridge/venv/bin/python3` everywhere the rest of this guide says `python3` (the foreground test in Step 5 and the `PYTHON3=` line in Step 6).

**Verify:** `python3 -c "import telegram, dotenv, httpx, telegramify_markdown, PIL; print('deps OK')"` prints `deps OK` (use the venv python if you made one).

### Step 4: Configure

```bash
cd ~/.claude/telegram-bridge
cp .env.example .env
chmod 600 .env
```

Edit `.env` and fill in:

- `TELEGRAM_BOT_TOKEN` = the token from Step 1
- `ALLOWED_USER_IDS` = your numeric ID from Step 2
- `TELEGRAM_GROUP_ID` = leave empty for now (filled in Step 5)
- `WORKING_DIR` = the folder where your projects live (Claude starts every session here). Your home folder is a fine default.
- `WHISPER_PROVIDER` = `local` on Apple Silicon, or `openai`/`groq` (+ `WHISPER_API_KEY`), or empty to skip voice
- `CLAUDE_MODEL` = `opus` is a good default (or `opus[1m]` for the 1M context window if your plan supports it). If you plan to add Jarvis (part 2), use `opus[1m]`
- Optional, defaults are fine: `BRIDGE_EFFORT` (reasoning effort, default `high`), `CLAUDE_MODEL_FALLBACKS` (models to switch to when yours hits a usage cap), and per-topic `TOPIC_MODELS` / `TOPIC_EFFORTS`. They are commented out in `.env.example` with examples

**Verify:** `grep -c "your-bot-token-here" .env` prints `0`.

### Step 5: First run (foreground test)

```bash
cd ~/.claude/telegram-bridge
python3 bridge.py
```

Leave it running. In your Telegram group, send `/id`. The bot replies with the Chat ID (a negative number like `-1001234567890`).

Now send a real message like "hi, what can you do?" in the General topic. The first reply can take 15-30 seconds (Claude cold start).

Then: stop the bridge (Ctrl+C), put the Chat ID into `.env` as `TELEGRAM_GROUP_ID`, and continue.

**Verify:** the bot answered your message, and `TELEGRAM_GROUP_ID` is now filled in.

**If the bot does not reply:** check the terminal output. "Claude CLI not found" means `claude` is not in PATH for that shell. Unauthorized users are ignored silently, so a wrong `ALLOWED_USER_IDS` looks like the bot being dead: re-check your ID.

### Step 6: Install as a LaunchAgent (always on)

Fill in the three placeholders in the plist template (copied in Step 3) and install it. The LaunchAgent label is `com.user.claude-telegram-bridge`; the add-on parts restart the bridge by that name.

```bash
cd ~/.claude/telegram-bridge
PYTHON3=$(which python3)       # or $HOME/.claude/telegram-bridge/venv/bin/python3 if you made a venv
CLAUDE_BIN_DIR=$(dirname "$(which claude)")
BRIDGE_DIR="$HOME/.claude/telegram-bridge"

mkdir -p ~/Library/LaunchAgents
sed -e "s|__PYTHON__|$PYTHON3|" \
    -e "s|__BRIDGE_DIR__|$BRIDGE_DIR|" \
    -e "s|__CLAUDE_BIN_DIR__|$CLAUDE_BIN_DIR|" \
    com.user.claude-telegram-bridge.plist.template \
    > ~/Library/LaunchAgents/com.user.claude-telegram-bridge.plist

launchctl load ~/Library/LaunchAgents/com.user.claude-telegram-bridge.plist
```

**Verify:** `launchctl list | grep claude-telegram` shows the agent, and messaging the bot gets a reply. `tail -f ~/.claude/telegram-bridge/logs/stderr.log` shows the startup lines, including `Claude model: ...`, `Claude effort: high ...` and `Model fallback ladder (General topic): ...`.

### Step 7: Full Disk Access (important, do not skip)

macOS blocks background processes from reading protected folders (Desktop, Documents, Downloads) with an invisible permission prompt. Because the bridge runs headless, that prompt can never be answered and Claude just **hangs silently** when it touches those folders.

Fix: System Settings > Privacy & Security > **Full Disk Access** > add the exact Python binary the LaunchAgent uses (the path you got from `which python3`; press Cmd+Shift+G in the file picker to paste it). If the binary is a symlink, resolve it first: `python3 -c "import sys; print(sys.executable)"`.

Be aware of the scope: macOS grants Full Disk Access to that Python *binary*, so any other script executed with the same interpreter also gets it. If that bothers you, install a dedicated Python (e.g. from python.org) just for the bridge and grant FDA only to that one.

Then restart the bridge:

```bash
launchctl unload ~/Library/LaunchAgents/com.user.claude-telegram-bridge.plist
launchctl load ~/Library/LaunchAgents/com.user.claude-telegram-bridge.plist
```

**Verify:** ask the bot "list the files on my Desktop". If it answers instead of hanging, you are done.

### Step 8 (optional but recommended): long-lived automation token

Claude Code's normal login token rotates every few hours, which can 401 the bridge at bad moments. Mint a 1-year automation token once:

```bash
claude setup-token
mkdir -p ~/.config/claude
# paste the printed token (starts with sk-ant-) into this file:
nano ~/.config/claude/automation-oauth-token.txt
chmod 600 ~/.config/claude/automation-oauth-token.txt
```

The bridge picks it up automatically (check with `grep "Preflight" ~/.claude/telegram-bridge/logs/stderr.log`), quarantines it automatically if it ever goes bad, and falls back to normal auth.

## Running It

- **Talk to it:** just type in any topic. Voice notes and photos work the same way. If Telegram's servers hiccup while a photo, file or voice note is being fetched, the bridge retries a few times on its own; if it still fails, it tells you in the chat to resend it, so nothing disappears silently.
- **New workstream:** `/topic Project X` creates a topic; every topic is an isolated Claude conversation with its own memory.
- **Reset a conversation:** `/clear` in that topic.
- **See context usage:** `/context` (the footer under each reply also shows it, e.g. `54k / 1M (5.4%)`).
- **Usage cap notice:** a reply that starts with a line like "Opus (1M) is out of usage credits until Sun 21:00, running on Sonnet (1M) until then." means the fallback kicked in. Nothing to do; it switches back by itself.
- **Stop a runaway response:** tap the Stop button or send `/stop`.
- **Start/stop the bridge:** `launchctl load` / `launchctl unload` on the plist.
- **Logs:** `tail -f ~/.claude/telegram-bridge/logs/stderr.log`

## How It Works (Architecture)

- **Session mapping:** `state/sessions.json` maps each topic ID to a Claude session UUID. Messages spawn `claude --resume <uuid> -p` in streaming JSON mode; the session file means context survives bridge restarts, Mac reboots, everything. The file is re-read on every message, so an outside script can reset a topic by deleting its row (the next message then starts a fresh session).
- **Context meter:** `state/token_usage.json` holds each topic's latest context size. The bridge writes only the row of the topic it just ran and keeps everyone else's row as it is on disk, so a row an outside script removed stays removed. `/context` reads this file.
- **Models and effort:** each spawn gets `--model` (per topic, else `CLAUDE_MODEL`) and `--effort` (per topic, else `BRIDGE_EFFORT`, default `high`). When the CLI answers with a usage-cap message instead of a reply, the bridge records the cap and its reset time in `state/model_caps.json`, reruns the turn on the next model in the fallback ladder, and keeps using the fallback until the reset time passes. A cap with no reset time expires after an hour.
- **Streaming:** the bridge reads Claude's `stream-json` events line by line, edits the Telegram "Thinking..." message every ~1.5s with accumulated text plus a live tool label, then replaces it with the final formatted reply (markdown converted to Telegram entities, tables converted to mobile-friendly cards, long replies split into `[1/N]` chunks).
- **Per-topic locking:** an asyncio lock per topic queues messages so two prompts never race one session. Different topics run fully in parallel.
- **Timeouts:** a normal reply has no total time limit, only an idle timeout (15 min without any output, 60 min while a background subagent runs), so hour-long agentic tasks work but true hangs get killed. The automatic re-run after an auth error or a usage cap is different: it has a total limit of `CLAUDE_IDLE_TIMEOUT` (15 min by default). Scheduled triggers use `CLAUDE_TIMEOUT` plus their own retry deadline.
- **Auth ladder:** each Claude spawn injects the best available credential: setup-token file, else the Keychain token if it has >10 min left, else nothing (CLI self-auth). Auth failures retry without an injected token, and a failing setup-token is quarantined for 30 min with a circuit breaker.
- **Triggers:** every 5s the bridge scans `state/triggers/` for JSON files shaped like `{"topic_id": 123, "prompt": "...", "sender": "my-cron"}`. Each is run through that topic's session and the answer is posted to the topic. Triggers run one after another, so a long one delays the ones behind it. Auth and temporary API errors (5xx, overloaded) are retried in place with backoff for up to about 12 minutes. If auth errors still persist, the trigger is requeued (10 min later, at most 2 times) and then moved to `state/triggers/dead/`; other errors are not requeued. If the reply was produced but Telegram could not be reached for several minutes, the whole trigger is requeued the same way (see the known limits below). A file that is not a JSON object with `topic_id` and `prompt` goes straight to `state/triggers/dead/`. A trigger response ending in `[[SILENT]]` posts nothing (lets scheduled checks stay quiet when there is nothing to say).
- **Housekeeping:** daily self-restart at 4 AM (launchd respawns it, frees Python heap; deferred while anything is running), upload purge after 48h, rotating logs (25 MB cap), and a global handler that exits cleanly if a second bot instance steals the token.

## Customization Guide

- **Scheduled messages:** cron/launchd job writes a trigger JSON (see above) into `~/.claude/telegram-bridge/state/triggers/`. Write to a `.tmp` name first, then rename to `.json` (atomic, so the watcher never reads a half-written file). Example morning brief: `{"topic_id": <your topic>, "prompt": "Check my calendar and the weather and post a short morning brief.", "sender": "morning-cron"}`.
- **Topic personas:** you can give a topic a standing role by just telling it ("In this topic you are my writing editor. Stay in that role."). It persists because the session persists.
- **Model per install:** `CLAUDE_MODEL` in `.env` (`opus`, `sonnet`, `opus[1m]`, ...). Per topic: `TOPIC_MODELS=123:sonnet` (topic id from `/id`). Fallbacks: `CLAUDE_MODEL_FALLBACKS`, or per topic `TOPIC_MODEL_FALLBACKS=123:haiku|sonnet`.
- **Reasoning effort:** `BRIDGE_EFFORT` (`low`, `medium`, `high`, `xhigh`, `max`; `default` follows your global Claude Code setting). Per topic: `TOPIC_EFFORTS=123:max`.
- **Restart hour, timeouts, upload retention:** all env vars in `.env.example`.
- **Custom commands:** copy the `cmd_clear` pattern in `bridge.py`: define an async handler, register it with `CommandHandler`, add it to the `set_my_commands` menu and `/help`.

## Troubleshooting

1. **Bot never replies, no errors:** your user ID is not in `ALLOWED_USER_IDS` (unauthorized senders are ignored silently), or a second bridge instance is running with the same token (check the log for "Another bot instance").
2. **Claude hangs on anything touching Desktop/Documents/Downloads:** Full Disk Access is missing for the Python binary (Step 7). This is the number one gotcha.
3. **"Claude CLI not found":** the `PATH` in the LaunchAgent plist does not include the folder `which claude` prints. Regenerate the plist (Step 6).
4. **`API Error: 401` in replies:** auth token rotated. The bridge retries automatically; if it persists, mint/refresh the setup token (Step 8).
5. **Messages get one checkmark and the bot goes quiet, but internet works:** some networks (hotel/ISP) silently drop TLS to `api.telegram.org` while the Telegram app itself still works. Test with `curl -m10 https://api.telegram.org/` (instant response = fine, timeout = blocked). Fix: VPN. Nothing is wrong with the bridge.
6. **Voice fails with an mlx error:** you are on an Intel Mac or the wrong (x86) Python. Use `WHISPER_PROVIDER=openai` or `groq` instead.
7. **First reply after a quiet period is slow:** cold start (Claude CLI boot plus, for voice, Whisper model load). Normal.
8. **`Conflict` errors in the log:** two copies polling with one token; the bridge exits on purpose and launchd restarts one clean instance. If it loops, find the stray copy: `ps aux | grep bridge.py`.
9. **Replies show `[1/3]` prefixes:** normal, Telegram's 4,096-char limit forces splitting.
10. **Bot cannot create topics with `/topic`:** it is not an admin with "Manage topics" in the group (Step 2).
11. **Every reply fails right away even though `claude` works in a terminal:** your Claude Code may be too old to know the `--effort` flag. Check with `claude --help | grep -- --effort` (no output = too old). Run `claude update`, or set `BRIDGE_EFFORT=default` in `.env` and restart the bridge.
12. **"Every model I can run is out of usage credits right now":** the whole fallback ladder is capped. Wait for the reset time it names, or add usage at claude.ai/settings/usage.

### Known limits

- **Reset while a reply is running:** send `/clear` only after the current reply has finished. A reset sent while Claude is still answering can be partly undone when that reply finishes (the context meter comes back, and that reply still belongs to the old conversation).
- **Rare model mix-up under usage caps:** if two topics hit a usage cap at almost the same moment, the bridge can also mark the fallback model as capped, so a topic may say every model is out of credits a little early. It clears by itself at the reset time (or after an hour when no reset time was given).
- **Scheduled messages and Telegram outages:** when a trigger's reply cannot be delivered for several minutes, the trigger is run again later, not just re-sent. Keep scheduled prompts safe to run twice (reports and checks are fine; avoid "send this email" style triggers).

## Removal (full undo)

> **For Claude:** if the user asks to remove the bridge, first ask whether they also installed part 2 (Jarvis): remove it first, using the Removal section in its own README, because it depends on this bridge.

```bash
launchctl unload ~/Library/LaunchAgents/com.user.claude-telegram-bridge.plist
rm ~/Library/LaunchAgents/com.user.claude-telegram-bridge.plist
rm -rf ~/.claude/telegram-bridge          # code, .env (bot token), sessions, logs
rm -f ~/.config/claude/automation-oauth-token.txt   # only if you made one in Step 8 and nothing else uses it
```

**Verify:** `launchctl list | grep claude-telegram` prints nothing, and the bot no longer answers.

Optional: delete the bot itself in Telegram (**@BotFather**, `/deletebot`, pick the bot), then delete the group. Your Claude Code conversations stay in `~/.claude/projects/` like any other Claude Code session. Full Disk Access was given to a Python binary in Step 7; remove that entry in System Settings > Privacy & Security > Full Disk Access if nothing else needs it.

## Extending It

The trigger folder is the integration point for everything: scheduled agents, webhooks (a tiny web server writing trigger files), home automation, git hooks that ask Claude to review a push. Anything that can write a JSON file can start a Claude conversation turn in any topic and have the result appear on your phone.
