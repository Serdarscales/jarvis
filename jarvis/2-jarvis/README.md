# Jarvis: Complete Build Kit (part 2, add-on for the Telegram bridge)

> **Easiest path:** install part 1 first (`../1-telegram-bridge`, the Telegram bridge) and check that the bot answers. Then open a terminal in this folder (`2-jarvis`), start Claude Code (`claude`), and say: **"Read README.md and set Jarvis up for me step by step."** Claude runs the whole install with you as a short interview, verifying every step. Everything below also works as a manual guide.

## Why This Exists

The Telegram bridge already gives you Claude Code in your pocket: one topic, one conversation. Jarvis turns one of those topics into a **voice-first chief of staff**. You send a voice note while driving ("research X, draft Y, what's open?"), and Jarvis answers with a **voice note plus a readable text**, hands the real work to background agents, keeps track of everything that is open, and **pings you on its own** when something finishes or needs a decision. Every morning at 8 it gives you one brief. Overnight it writes its own memory journal so it never loses the plot.

A typical exchange: you voice-note "spin up a thread that compares the three CRM options we talked about and drafts a recommendation". Jarvis confirms by voice in five seconds and goes quiet. Twenty minutes later your phone buzzes: a voice note saying the comparison is done, the recommendation, and the one question it needs you to answer, with the full document attached as a file.

## What You're Building

- **One Telegram topic = Jarvis.** Voice or text in; every reply comes back as a voice note AND a text that stands on its own (questions numbered, so you can answer from a meeting without playing audio).
- **One-shot workers** (`jarvis-spawn`): a detached headless Claude Code run for a single task. Guaranteed completion ping, even if the worker crashes.
- **Threads** (`jarvis-thread-*`): resumable multi-turn workers, like a Claude Code tab that keeps driving toward a goal on its own. They only interrupt you for a real decision, a finished deliverable, or a check-in every 15 self-driven turns. Several can run at once (cap 8). Draft-only: they never message other people.
- **Open-items queue**: everything waiting on you. "What's open?" reads it back.
- **Proactive pings** through the bridge's trigger system. Bursts of finished threads are merged into one message. Background wakes with nothing worth saying stay silent (the `[[SILENT]]` sentinel), so you get no "all quiet" spam.
- **Memory**: a journal (narrative memory) and standing orders (your ongoing preferences, grown by voice: "from now on..."). Every fresh session loads the standing orders and the last 14 days of the journal, and searches older entries on demand, so Jarvis never says it forgot.
- **Daily rhythm** (macOS LaunchAgents): 03:00 journal wrap + session reset, 07:30 desktop sync (checks the queue against what you actually did in desktop Claude Code sessions), 08:00 morning brief, a watcher every 20 minutes (stuck workers, stalled threads, lost pings, oversized context), and a re-orient on every login.

```
 you (phone, voice)                       your Mac
 ───────────────────        ─────────────────────────────────────────────
 Telegram "Jarvis" topic <-> bridge.py (patched) <-> claude session = Jarvis
        ^                        ^    trigger files        |  jarvis-say (voice out)
        |                        |  <──────────────┐       |  jarvis-spawn / jarvis-thread-new
        |  voice notes, files    |                 |       v
        └──── jarvis-say, ───────┘          workers + threads (headless claude)
              jarvis-send-*                        ^
                                                   |  LaunchAgents: 03:00 wrap, 07:30 sync,
                                                   └─ 08:00 brief, every 20 min watch, on login
```

**Stack:** bash + Python standard library (no pip installs), the Claude Code CLI, ffmpeg, your existing bridge. Everything lives in one folder: `~/.claude/jarvis/`.

**Cost:** no new subscription. Workers and threads run on your Claude plan, and heavy use on Opus eats into your plan's usage limits (a Max plan is the realistic tier). Voice is free with the built-in macOS voice; ElevenLabs sounds far better (their Starter plan covers a few hundred replies a month; check elevenlabs.io/pricing).

## Read This First: Safety

- Workers and threads run with `--dangerously-skip-permissions`, exactly like the bridge itself: they can do anything on your Mac that you can. The bridge's user allowlist is what protects you. Keep the group private.
- Threads are told to work **draft only**: their brief says never to send email, DMs or posts, but to write the draft and stop to ask you. This is an instruction to the model, not a technical block. Workers and threads can technically do anything the bridge can, so treat it as a strong habit, not a guarantee. Jarvis itself is told to show you the exact text before anything goes to another person.
- The kit patches your `bridge.py` (with a dated backup) and adds five LaunchAgents. The Removal section undoes all of it.

## Prerequisites

- **Part 1, the Telegram bridge, installed and working** (`launchctl list | grep claude-telegram` shows `com.user.claude-telegram-bridge`, and messaging the bot gets a reply). If you installed part 1 just before this, you are ready. This part was written against part 1's `bridge.py`; if you changed yours, the patch script tells you exactly which part does not match.
- **Recommended:** a 1M-context model for the Jarvis topic (`opus[1m]`, Step 4 sets it up). Jarvis carries a lot of memory into every fresh session, and its mid-day reset threshold assumes the 1M window.
- **Recommended:** the bridge's long-lived automation token (`claude setup-token`, part 1 README, Step 8). Threads use it too (with an automatic retry on your normal login if it ever fails), so they keep working overnight. One-shot workers, the nightly wrap and the desktop sync use your normal Claude Code login, which works as long as you stay logged in to Claude Code on this Mac.
- `ffmpeg` (Telegram voice notes need it): `command -v ffmpeg`, else `brew install ffmpeg`.
- `/usr/bin/python3` works (`/usr/bin/python3 --version`; if missing, run `xcode-select --install`).
- 30 to 45 minutes.

## Step-by-Step Setup

> **For Claude:** you are installing Jarvis for the person in front of you. Work through the steps in order, one at a time. Run each step's verification and show the result before moving on. Steps marked (phone) happen in Telegram: tell them exactly what to tap and wait for them to paste results back. Ask the interview questions ONE AT A TIME, offer options with honest tradeoffs, and use their answers in later steps. Never print API keys or the bot token back to the screen. `KIT` below means the absolute path of the folder this README is in; shell variables do not survive between separate commands, so set `KIT=...` at the start of every command block that uses it.

### Step 0: Check the bridge

```bash
launchctl list | grep claude-telegram
ls ~/.claude/telegram-bridge/bridge.py ~/.claude/telegram-bridge/.env
grep -E '^(WORKING_DIR|CLAUDE_MODEL|CLAUDE_TIMEOUT|TOPIC_MODELS)=' ~/.claude/telegram-bridge/.env
```

**Verify:** the agent is listed and both files exist. If the bridge lives somewhere else, note that path: it becomes `BRIDGE_DIR` in Step 3.

### Step 1: Interview

> **For Claude:** ask these one at a time.
> 1. "What's your first name? Jarvis talks to you by name."
> 2. "Which folder should Jarvis's workers start in? Usually the same as the bridge's WORKING_DIR (your projects folder with your CLAUDE.md)." Suggest the WORKING_DIR value from Step 0.
> 3. "Which voice? (a) ElevenLabs: by far the most natural, needs an account and API key, a paid plan for daily use. (b) OpenAI TTS: good, cheap, needs an OpenAI API key. (c) The built-in macOS voice: free, robotic, works right now. You can switch any time." If ElevenLabs: ask whether they already have a voice in mind (from the Voice Library, or a clone of their own voice) and walk them to elevenlabs.io > Voices > the voice > copy Voice ID, and Profile > API Keys.
> 4. "Where does your world live? When you ask Jarvis 'where are things?', which sources should it check besides your recent Claude Code sessions: calendar, call recordings, team chat, CRM, analytics? For each, which skill or tool do you already use for it in Claude Code?" Look in `~/.claude/skills/` and `~/.claude/CLAUDE.md` to suggest what exists.
> 5. "Any numbers you want in the 8am brief every day?" (examples: yesterday's ad spend, new signups, MRR). Fine to skip.

### Step 2: Create the Jarvis topic (phone)

1. In your bridge group, send `/topic Jarvis`.
2. Open the new **Jarvis** topic and send `/id` there.
3. Copy the **Topic ID** number back to Claude.

**Verify:** you have a number (for example `412`). It is the topic, not the group id (the group id is negative).

### Step 3: Install the files

```bash
KIT="/absolute/path/to/claude-telegram-complete-kit/2-jarvis"   # the folder this README is in
mkdir -p ~/.claude/jarvis/state
cp -R "$KIT/jarvis/." ~/.claude/jarvis/
chmod +x ~/.claude/jarvis/bin/* ~/.claude/jarvis/cron/jarvis-*
# seed memory files only if they do not exist yet (never overwrite a live journal)
for f in standing-orders.md journal.md; do
  [ -f ~/.claude/jarvis/state/$f ] || cp ~/.claude/jarvis/state-template/$f ~/.claude/jarvis/state/$f
done
[ -f ~/.claude/jarvis/jarvis.env ] || cp ~/.claude/jarvis/jarvis.env.example ~/.claude/jarvis/jarvis.env
# owner-only before any API key goes in
chmod 700 ~/.claude/jarvis
chmod 600 ~/.claude/jarvis/jarvis.env
```

Personalize the persona and standing orders with the name from the interview (replace `NAME`):

```bash
sed -i '' 's/{{OWNER}}/NAME/g' ~/.claude/jarvis/jarvis-prompt.md ~/.claude/jarvis/state/standing-orders.md
```

**Verify:** `grep -c "{{OWNER}}" ~/.claude/jarvis/jarvis-prompt.md ~/.claude/jarvis/state/standing-orders.md` prints 0 for both, and `ls -l ~/.claude/jarvis/jarvis.env` starts with `-rw-------`.

### Step 4: Fill in jarvis.env

> **For Claude:** edit `~/.claude/jarvis/jarvis.env` with the interview answers: `JARVIS_OWNER`, `JARVIS_TOPIC_ID` (Step 2), `CLAUDE_BIN` (the output of `command -v claude`), `JARVIS_WORKSPACE`, the voice settings, and `BRIDGE_DIR` only if the bridge is not at `~/.claude/telegram-bridge`. Write keys into the file without echoing them. Then check the model:

```bash
"$(command -v claude)" -p "Reply with just OK" --model "opus[1m]"
```

**Verify:** it prints OK.

> **For Claude:** if it printed OK, make sure the Jarvis conversation itself also runs on the 1M window. Look at the bridge `.env` (Step 0 output): if `CLAUDE_MODEL` is already `opus[1m]`, nothing to do. Otherwise add one line to `~/.claude/telegram-bridge/.env`: `TOPIC_MODELS=<Jarvis topic id>:opus[1m]` (if a `TOPIC_MODELS=` line already exists, append `,<id>:opus[1m]` to it instead). The bridge restart in Step 6 picks it up.

If it errors about the model or the 1M window, your plan has no 1M window: set `JARVIS_MODEL=opus` and `JARVIS_CTX_THRESHOLD=150000` in jarvis.env (the default threshold, 500000 tokens, only fits a 1M-window model; on a 200K window the reset would never fire).

### Step 5: Offline self-test

```bash
bash ~/.claude/jarvis/bin/jarvis-selftest
```

It checks the config, then runs the queue, a one-shot worker and a full thread lifecycle (new, tell, kill, the ping merger, the cron sweeps) against a throwaway scratch folder with fake workers. It posts nothing and costs nothing.

**Verify:** `RESULT: N passed, 0 failed`. One warning ("bridge.py not patched yet") is expected at this point.

### Step 6: Patch the bridge

```bash
KIT="/absolute/path/to/claude-telegram-complete-kit/2-jarvis"   # the folder this README is in
python3 "$KIT/bridge-patch/patch_bridge.py" --check     # dry run: every line must say OK
python3 "$KIT/bridge-patch/patch_bridge.py"             # applies it, writes a dated backup
```

It adds four things to `bridge.py` and nothing else: reading the Jarvis topic id, the **sticky primer** (every fresh Jarvis session gets the persona, standing orders, journal tail and a boot check), the **ping merger** in the trigger watcher, and the **/jarvis** command. If `--check` reports a mismatch, the bridge was modified: apply the same blocks by hand (each one is a readable text block inside the script), then run `python3 -m py_compile bridge.py`.

Then give scheduled Jarvis turns enough time, and restart the bridge:

```bash
cd ~/.claude/telegram-bridge
grep -q '^CLAUDE_TIMEOUT=' .env && sed -i '' 's/^CLAUDE_TIMEOUT=.*/CLAUDE_TIMEOUT=600/' .env || echo 'CLAUDE_TIMEOUT=600' >> .env
launchctl unload ~/Library/LaunchAgents/com.user.claude-telegram-bridge.plist
launchctl load ~/Library/LaunchAgents/com.user.claude-telegram-bridge.plist
sleep 5; grep "Jarvis pending-pings coalescer" logs/stderr.log | tail -1
```

(Use your bridge plist's real name if it differs: `ls ~/Library/LaunchAgents | grep -i telegram`.)

**Verify:** the log shows `Jarvis pending-pings coalescer active (topic <your id>)`. The Jarvis conversation runs on the bridge's model for that topic (its `TOPIC_MODELS` entry, else `CLAUDE_MODEL`) at the bridge's effort (`BRIDGE_EFFORT`, default `high`). Recommended: `opus[1m]` at `high`, with `sonnet[1m]` as the fallback when Opus hits a usage cap. In the Jarvis topic, `/context` should now show a window of `1,000,000` after the first reply.

### Step 7: Live voice test

```bash
bash ~/.claude/jarvis/bin/jarvis-selftest --live
```

**Verify (phone):** a voice note and a short text arrive in the Jarvis topic. If the voice sounds robotic although you chose ElevenLabs, the API call failed and it fell back to the Mac voice: check the key and voice id.

### Step 8: First conversation (phone)

1. In the Jarvis topic, send `/jarvis`. Jarvis greets you with a voice note plus text.
2. Send a voice note: "Spin up a thread that writes me a four-line poem about Mondays, and tell me when it's done."
3. Within a minute or two you should get a completion ping by voice with the result.

**Verify:** both messages arrive as voice + text. If the ping never comes, see Troubleshooting.

### Step 9: Schedule the daily rhythm

The nightly wrap and the desktop sync start through the same Python the bridge uses, the one that got Full Disk Access in part 1, Step 7. Started from plain bash instead, macOS would credit folder access to the claude program itself and ask again after every Claude update, in a dialog nobody answers at 3 AM, and the job would hang.

```bash
KIT="/absolute/path/to/claude-telegram-complete-kit/2-jarvis"   # the folder this README is in
mkdir -p ~/Library/LaunchAgents
CLAUDE_DIR="$(dirname "$(command -v claude)")"
BRIDGE_PY="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' ~/Library/LaunchAgents/com.user.claude-telegram-bridge.plist)"
echo "bridge python: $BRIDGE_PY"
for f in "$KIT"/launchagents/*.plist.template; do
  name=$(basename "$f" .template)
  sed -e "s|__HOME__|$HOME|g" -e "s|__CLAUDE_BIN_DIR__|$CLAUDE_DIR|g" -e "s|__PYTHON__|$BRIDGE_PY|g" "$f" > ~/Library/LaunchAgents/"$name"
  plutil -lint ~/Library/LaunchAgents/"$name" && launchctl load ~/Library/LaunchAgents/"$name"
done
launchctl list | grep com.jarvis
```

**Verify:** `bridge python:` printed a real path (if it is empty, the bridge plist has another name: `ls ~/Library/LaunchAgents | grep -i telegram`, then set `BRIDGE_PY` by hand to the Python path inside it). Five `com.jarvis.*` agents listed (night 03:00, desktopsync 07:30, morning 08:00, watch every 20 min, bootreconcile on login). `grep -l "__" ~/Library/LaunchAgents/com.jarvis.*.plist` prints nothing (no placeholder left). `cat ~/.claude/jarvis/state/cron-bootreconcile.out` shows a run from just now. The times are your Mac's local time.

### Step 10: Teach it your world

> **For Claude:** open `~/.claude/jarvis/state/standing-orders.md` and fill in from the interview: the sources under "Where my world lives" (one line each: what it is + which skill or tool reads it), and the daily numbers under "Morning brief". Remove preferences the person does not want. Show them the result.

Done. From here on Jarvis grows by voice: "from now on...", "every morning also...", "remember I prefer..." go straight into standing orders.

## Using Jarvis Day to Day

- Talk normally. "Research X", "draft an email to Y" (it shows you the draft first), "what's open", "what's running", "remember that for later", "close the pricing thread", "kill it".
- Say "I'm driving" and it goes audio-only, parking anything visual for later.
- `/jarvis` re-primes it if it ever acts generic. `/clear` in the Jarvis topic also works: the primer reloads automatically.
- Files: it sends images, PDFs and docs into the topic as real attachments.

## How It Works

- **Sticky primer.** The bridge starts a new Claude session when a topic is fresh (after `/clear`, the nightly reset, or the first trigger of the day). For the Jarvis topic, the patched bridge prepends `jarvis-prompt.md`, the standing orders, every journal entry from the last 14 days (about 15,000 tokens at typical use, capped at 100,000 characters) and a boot check. For anything older, Jarvis is told to search the journal and its archives instead of guessing. That is why Jarvis is always "up to speed".
- **Turn order.** The bridge posts only the text written after the last tool call. Jarvis is told to run its tools first, then `jarvis-say`, then write the full text last. That rule is the difference between a full text reply and one thin line.
- **Voice note last, and only one.** `jarvis-say` makes the audio during the turn, but parks it in a per-turn folder the bridge hands it (`JARVIS_VOICE_DEFER_DIR`); the bridge posts it right AFTER the text, so the voice note is always the last message, ready to play. A long reply is spoken in chunks and merged into ONE voice note (the bitrate drops as it gets longer, to stay under Telegram's 1 MB voice-note limit); only past about 9 minutes of speech does it become two notes. Outside a turn (workers, scheduled jobs) it sends right away. `jarvis-say.log`: "queued voice" = parked for the bridge, WARN = a chunk came back short and was regenerated.
- **Triggers.** Anything that wants to wake Jarvis drops a JSON file (`topic_id`, `prompt`, `sender`) into the bridge's `state/triggers/`, written atomically (`.json.tmp`, then rename). The bridge checks the folder every 5 seconds and runs triggers one after another (a long one delays the ones behind it), then posts the reply, unless the reply ends in `[[SILENT]]`.
- **Workers vs threads.** `jarvis-spawn` = one detached `claude -p` run whose wrapper always calls `jarvis-notify` on exit. Threads = a row in `threads.json` plus a Claude session id: turn 1 uses `--session-id`, later turns `--resume`. Each turn holds a per-thread file lock (no two turns of one thread at once) and a slot in a run cap of 8. A busy thread queues your instruction and delivers it when the turn ends. Every turn ends with a status marker (`DONE`, `NEEDS-INPUT`, `WORKING`); `WORKING` continues automatically, everything else pings Jarvis.
- **Ping merger.** Thread turns append to `pending-pings.json`; the bridge drains a settled burst (8 seconds quiet) into one trigger, so five threads finishing together wake Jarvis once.
- **Memory.** 03:00: a headless Claude reads the end of the day's Jarvis transcript and appends a dated wrap to `journal.md`, then the topic session is reset. The watcher does the same mid-day if the conversation passes the token threshold (`JARVIS_CTX_THRESHOLD`, default 500,000 tokens, sized for a 1M-window model), but only when the bridge is idle and no worker or thread turn is running. If you message Jarvis while the wrap is being written (it takes a few minutes), that last exchange may not make it into the journal before the reset; the full conversation stays on disk in Claude Code's own history.
- **How a reset works.** The wrap removes the Jarvis topic's row from the bridge's `state/sessions.json` and `state/token_usage.json`. The bridge re-reads `sessions.json` on every message, so the next message starts a fresh session (and gets the sticky primer). The bridge only ever writes its own topic's row back into `token_usage.json`, so the removed meter row stays removed and the watcher does not fire a second reset.
- **Safety net.** Every 20 minutes the watcher flags one-shot workers running over 35 minutes, threads that claim to run but have no live process, finished turns whose ping got lost, inputs stuck in the queue, and Claude CLI updates (it then asks Jarvis to rerun the self-test).
- **Desktop sync.** 07:30: a worker greps the last 26 hours of your Claude Code transcripts for keywords from each open item and writes a report (done on desktop / progressed / superseded / no activity). Proposal only: the morning brief offers the closes, you confirm.

## Files

```
~/.claude/jarvis/
  jarvis-prompt.md          persona + operating manual (edit freely)
  jarvis.env                your config
  bin/  jarvis-say, jarvis-send-text, jarvis-send-file     voice/text/file out
        jarvis-spawn, jarvis-notify                         one-shot workers
        jarvis-thread-{new,tell,show,list,done,kill,run,reconcile}   threads
        _state.py, _threadlib.py, _threadshow.py, _env.sh   engine + config loader
        jarvis-selftest                                     offline + --live checks
  cron/ jarvis-handover, jarvis-desktop-sync, jarvis-morning, jarvis-watch,
        jarvis-boot-reconcile, jarvis-prune                 the LaunchAgent jobs
        _threadcore.py, _threadcron.py                      shared core (both sides)
  charters/thread-charter-template.md                       every thread's system brief
  state/ standing-orders.md, journal.md, open-items.json, jobs.json, threads.json,
         outputs/, threads/, desktop-sync/, logs (*.log, cron-*.out)
```

## Customization

- **Personality and rules:** `jarvis-prompt.md`. Changes apply on the next fresh session (send `/jarvis`).
- **Preferences, morning brief, sources:** `state/standing-orders.md`, or just tell Jarvis by voice.
- **Brief time / wrap time:** edit the `Hour`/`Minute` in the plist, then `launchctl unload` + `load` it.
- **Thread behavior:** `JARVIS_THREAD_EFFORT`, `JARVIS_RUN_CAP`, `JARVIS_CHECKIN_TURNS` in jarvis.env. One thread turn may run up to 2 hours (`JARVIS_TURN_CEILING`, seconds); a turn that shows no activity for 15 minutes is stopped earlier, unless a command it started is still running (then the 2-hour limit applies). Thread system brief: `charters/thread-charter-template.md`.
- **Voice:** `JARVIS_TTS` and the voice settings in jarvis.env; takes effect on the next reply.

## Troubleshooting

1. **No voice note arrives.** `tail ~/.claude/jarvis/state/jarvis-say.log`. No new line = Jarvis skipped `jarvis-say` (send `/jarvis`). Test directly: `bash ~/.claude/jarvis/bin/jarvis-say "test"` shows the exact error.
2. **Text arrives as one thin line.** Jarvis wrote text before its last tool call. Send `/jarvis`; the persona's TURN ORDER rule fixes it.
3. **Jarvis acts generic.** The patch is not live: `grep -c '^async def cmd_jarvis(' ~/.claude/telegram-bridge/bridge.py` must print 1, and the bridge must have been restarted after patching. Also check `JARVIS_TOPIC_ID` matches the topic (send `/id` there).
4. **A worker or thread never reports back.** `bash ~/.claude/jarvis/bin/jarvis-thread-list all`, then `tail ~/.claude/jarvis/state/threads/<id>.run.log`. One-shot workers: `~/.claude/jarvis/state/outputs/<job>.log`. Bridge side: `grep -i trigger ~/.claude/telegram-bridge/logs/stderr.log | tail`.
5. **Workers fail with 401 / auth errors at night.** Threads: set up the bridge's long-lived automation token (`claude setup-token`); threads pick it up from `~/.config/claude/automation-oauth-token.txt`. One-shot workers, the nightly wrap and the desktop sync use your normal Claude Code login: run `claude` once in a terminal and check it answers without asking you to log in.
6. **No morning brief.** `cat ~/.claude/jarvis/state/cron-morning.out` should say "morning trigger dropped" at 08:00. If yes, the problem is on the bridge side (log above). If no, `launchctl list | grep com.jarvis.morning`. A Mac asleep at 08:00 runs it on wake.
7. **Nightly wrap never resets the session.** `tail ~/.claude/jarvis/state/handover.log`. "reset DEFERRED" means `bridge.py` changed since the bridge started: restart the bridge.
8. **The mid-day reset never fires, or the conversation auto-compacts first.** The threshold does not fit the model. `/context` in the Jarvis topic shows the window: with `1,000,000` the default 500000 is right; with `200,000` set `JARVIS_CTX_THRESHOLD=150000` in jarvis.env, or move the topic to `opus[1m]` (Step 4).
9. **The nightly wrap or desktop sync hangs, or never writes anything, after a Claude update.** Check that their plists start with the bridge's Python, not `/bin/bash`: `/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' ~/Library/LaunchAgents/com.jarvis.night.plist`. If it says `/bin/bash`, redo Step 9.
10. **"/jarvis only works in the Jarvis topic".** You sent it elsewhere, or `JARVIS_TOPIC_ID` is wrong. Fix jarvis.env and restart the bridge.
11. **After a Claude CLI update something feels off.** `bash ~/.claude/jarvis/bin/jarvis-selftest` (the watcher also asks Jarvis to run it automatically).
12. **`handover.log` says "session NOT reset (no clean journal update)".** The journal writer failed or hit its time limit (possibly after writing part of an entry), so the conversation was kept on purpose (nothing is lost; a partial entry may sit at the end of `journal.md`). The next watcher run (context still over the threshold) or the next night tries again. If it keeps failing, `cat ~/.claude/jarvis/state/.handover-journaler.log` shows why.

### Known limits

- **A voice note Telegram refuses or cannot take is not re-sent automatically.** After three tries the bridge keeps it in `~/.claude/telegram-bridge/state/voice-defer/unsent/` and posts a one-line notice under the text. Ask Jarvis to say it again if you want the audio. Delete that folder's contents whenever you like.

- **Reset while a reply is running:** send `/jarvis` (or `/clear`) only after Jarvis has finished its current reply. A reset sent while a reply is still running can be partly undone when that reply finishes.
- **A session that cannot be resumed starts without the persona:** if the bridge cannot resume the Jarvis session (rare, for example after a Claude Code update removed the old session file), it starts a fresh one with your message only, without the persona and memory. If Jarvis suddenly sounds generic, send `/jarvis`.

## Removal (full undo)

**Read this before running anything.** Removing Jarvis from `bridge.py` means restoring the backup the Jarvis patch wrote (`bridge.py.backup-<date>-pre-jarvis`): the bridge exactly as it was right before Jarvis. If you edited `bridge.py` by hand after patching, remove the Jarvis blocks by hand instead (each one is a plain-text block in `bridge-patch/patch_bridge.py`).

```bash
for a in night desktopsync morning watch bootreconcile; do
  launchctl unload ~/Library/LaunchAgents/com.jarvis.$a.plist; rm ~/Library/LaunchAgents/com.jarvis.$a.plist
done
# restore the pre-jarvis backup (put in its real date: ls ~/.claude/telegram-bridge/bridge.py.backup-*-pre-jarvis)
cp ~/.claude/telegram-bridge/bridge.py.backup-<date>-pre-jarvis ~/.claude/telegram-bridge/bridge.py
launchctl unload ~/Library/LaunchAgents/com.user.claude-telegram-bridge.plist
launchctl load ~/Library/LaunchAgents/com.user.claude-telegram-bridge.plist
rm -rf ~/.claude/jarvis
```

**Verify:** `launchctl list | grep com.jarvis` prints nothing, `grep -c '^async def cmd_jarvis(' ~/.claude/telegram-bridge/bridge.py` prints 0, and the bot still answers in a normal topic.

If you added a `TOPIC_MODELS` entry for the Jarvis topic in Step 4, remove it from the bridge `.env` (then restart the bridge). Then delete the Jarvis topic in Telegram.
