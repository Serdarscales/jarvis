#!/usr/bin/env python3
"""Add Jarvis to the Claude Code Telegram bridge (bridge.py from part 1 of this kit).

What it adds (nothing else in the bridge changes):
  1. Config: reads JARVIS_TOPIC_ID from ~/.claude/jarvis/jarvis.env at startup.
  2. Sticky primer: every FRESH session in the Jarvis topic gets the persona,
     the standing orders, the last 14 days of the journal and a boot check prepended, so Jarvis
     never forgets who it is (after /clear, the nightly reset, or a cold wake).
  3. Ping coalescer: the 5s trigger watcher also drains thread completion pings
     (pending-pings.json) into ONE trigger per burst.
  4. /jarvis command: resets the topic and re-primes it as Jarvis.

Usage:
  python3 patch_bridge.py [path/to/bridge.py]      (default ~/.claude/telegram-bridge/bridge.py)
  python3 patch_bridge.py --check [path]           (dry run: report anchors, write nothing)

Safety: every edit must match its anchor EXACTLY ONCE or the script aborts
without writing. A dated backup is written next to bridge.py first. The result
must compile before it replaces the original. Restart the bridge afterwards.

Stacking: every anchor is left intact (the new code goes before or after it),
so other add-on patches can anchor on the same lines and run before or after
this one.
"""
import os
import py_compile
import shutil
import sys
import tempfile
from datetime import datetime

CONFIG_ANCHOR = 'TRIGGER_DIR = STATE_DIR / "triggers"\n'
CONFIG_BLOCK = '''
# --- Jarvis (voice orchestrator) --------------------------------------------
# One topic runs Jarvis. Its id lives in ~/.claude/jarvis/jarvis.env
# (JARVIS_TOPIC_ID), read once at startup: restart the bridge after changing it.
JARVIS_HOME = Path(os.environ.get("JARVIS_HOME") or "~/.claude/jarvis").expanduser()


def _read_jarvis_env(name: str) -> str:
    try:
        for line in (JARVIS_HOME / "jarvis.env").read_text().splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip("'\\"")
    except OSError:
        pass
    return ""


try:
    JARVIS_TOPIC_ID = int(_read_jarvis_env("JARVIS_TOPIC_ID") or 0) or None
except ValueError:
    JARVIS_TOPIC_ID = None
JARVIS_OWNER = _read_jarvis_env("JARVIS_OWNER") or "the user"
# topic_id -> persona file. A fresh session in that topic gets the persona
# prepended (see _maybe_prime_sticky).
STICKY_PRIMERS = (
    {JARVIS_TOPIC_ID: {"path": str(JARVIS_HOME / "jarvis-prompt.md")}}
    if JARVIS_TOPIC_ID else {}
)
'''

PRIMER_ANCHOR = "async def _process_prompt(\n"
PRIMER_FUNC = '''# How much of Jarvis's journal a fresh session loads: every entry dated within
# the last JARVIS_JOURNAL_DAYS days (default 14), capped at
# JARVIS_JOURNAL_MAX_CHARS (default 100000, about 25K tokens) so one heavy week
# cannot bloat the boot. Older history stays on disk; the persona tells Jarvis
# to search journal.md and the journal-archive-*.md files for anything older.
JARVIS_JOURNAL_DAYS = int(os.environ.get("JARVIS_JOURNAL_DAYS") or 14)
JARVIS_JOURNAL_MAX_CHARS = int(os.environ.get("JARVIS_JOURNAL_MAX_CHARS") or 100000)
_JOURNAL_HEADER_RE = re.compile(r"^## (\\d{4}-\\d{2}-\\d{2})", re.M)


def _journal_recent(text):
    """Every journal entry dated within the last JARVIS_JOURNAL_DAYS days, cut
    only at an entry header. No dated entry in that window (or no dated
    headers at all) falls back to the last 7000 chars."""
    cutoff = (datetime.now() - timedelta(days=JARVIS_JOURNAL_DAYS)).strftime("%Y-%m-%d")
    start = None
    for m in _JOURNAL_HEADER_RE.finditer(text):
        if m.group(1) >= cutoff:
            start = m.start()
            break
    recent = text[start:] if start is not None else text[-7000:]
    if len(recent) > JARVIS_JOURNAL_MAX_CHARS:
        tail = recent[-JARVIS_JOURNAL_MAX_CHARS:]
        nxt = tail.find("\\n## ")
        recent = tail[nxt + 1:] if nxt >= 0 else tail
    return recent.strip()


def _maybe_prime_sticky(topic_id, is_new, prompt):
    """Prepend Jarvis's persona + memory when its session is fresh, so it never breaks character."""
    if not is_new:
        return prompt
    cfg = STICKY_PRIMERS.get(topic_id)
    if not cfg:
        return prompt
    try:
        with open(cfg["path"]) as f:
            persona = f.read().strip()
    except OSError:
        return prompt
    state = JARVIS_HOME / "state"
    extras = ""
    try:
        extras += ("\\n\\n## Standing orders (%s's ongoing instructions)\\n" % JARVIS_OWNER
                   + (state / "standing-orders.md").read_text().strip())
    except OSError:
        pass
    try:
        journal_recent = _journal_recent((state / "journal.md").read_text())
        extras += ("\\n\\n## Recent journal (every entry from the last %d days; for anything older, "
                   "search journal.md and the journal-archive-*.md files in %s)\\n"
                   % (JARVIS_JOURNAL_DAYS, state) + journal_recent)
    except OSError:
        pass
    extras += (
        "\\n\\n## Boot check\\nYou just started a fresh session. Before answering, know your live state: read "
        f"{state}/open-items.json (the queue), {state}/jobs.json (workers), and run "
        f"bash {JARVIS_HOME}/bin/jarvis-thread-list for your live resumable threads. "
        "Do NOT mention that you reset or re-primed. Just continue as if the conversation never broke."
    )
    return (
        f"{persona}{extras}\\n\\n---\\n\\n"
        "(The above is who you are, your memory, and your live state. Now handle this message.)\\n\\n"
        f"{prompt}"
    )


'''

PROMPT_SESSION = "        session_id, is_new = get_or_create_session(topic_id)\n"
PROMPT_SESSION_NEW = PROMPT_SESSION + "        prompt = _maybe_prime_sticky(topic_id, is_new, prompt)\n"

TRIGGER_SESSION = "        initial_session_id, initial_is_new = get_or_create_session(topic_id)\n"
TRIGGER_SESSION_NEW = TRIGGER_SESSION + "        prompt = _maybe_prime_sticky(topic_id, initial_is_new, prompt)\n"

WATCHER_START_ANCHOR = '''    logger.info("Trigger watcher started (dir=%s)", TRIGGER_DIR)
'''
WATCHER_START_NEW = WATCHER_START_ANCHOR + '''    # Jarvis threads: the bridge owns the pending-pings flush. Once a burst of
    # thread completions has settled (~8s quiet), drain them into ONE trigger
    # file, which this same loop then processes. Lazy import + hard guards:
    # Jarvis-side breakage must never take the bridge down.
    _threadcore = None
    if JARVIS_TOPIC_ID:
        try:
            import sys as _sys
            _jc = str(JARVIS_HOME / "cron")
            if _jc not in _sys.path:
                _sys.path.insert(0, _jc)
            import _threadcore as _tc
            _threadcore = _tc
            logger.info("Jarvis pending-pings coalescer active (topic %s)", JARVIS_TOPIC_ID)
        except Exception as e:
            logger.warning("Jarvis pending-pings coalescer unavailable: %s", e)
'''

WATCHER_TICK_ANCHOR = "            await asyncio.sleep(5)\n"
WATCHER_TICK_NEW = WATCHER_TICK_ANCHOR + '''
            if _threadcore is not None:
                try:
                    n = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: _threadcore.flush_pending_pings(str(TRIGGER_DIR)))
                    if n:
                        logger.info("Coalesced %d Jarvis thread ping(s) into one trigger", n)
                except Exception as e:
                    logger.warning("pending-pings flush error: %s", e)
'''

CMD_ANCHOR = "async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):\n"
CMD_FUNC = '''async def cmd_jarvis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset the session and prime it as Jarvis. Only works in the Jarvis topic."""
    if not _is_authorized(update):
        return

    topic_id = update.message.message_thread_id
    # The persona itself is injected by the sticky primer (_maybe_prime_sticky)
    # the moment the freshly reset session sees its first prompt, so the
    # message below is NOT the persona; it is a short message FROM the owner.
    primer_path = STICKY_PRIMERS.get(topic_id, {}).get("path")
    if not primer_path or not os.path.exists(primer_path):
        await safe_send(
            update.message,
            f"/jarvis only works in the Jarvis topic (JARVIS_TOPIC_ID={JARVIS_TOPIC_ID}, this topic={topic_id}).",
        )
        return
    reset_session(topic_id)

    prime_msg = (
        f"{JARVIS_OWNER} just re-primed you with /jarvis. This is a MESSAGE FROM {JARVIS_OWNER}, "
        "not a background wake: reply normally (jarvis-say voice first, "
        "then your full text as the final block) and never end this reply with "
        "[[SILENT]]. Do your boot check silently, then greet them in a couple of "
        "short lines: you're up to speed, plus anything that genuinely needs them."
    )
    await _process_prompt(update, context, prime_msg)


'''

HELP_ANCHOR = '        "/clear - Start fresh session\\n"\n'
HELP_NEW = HELP_ANCHOR + '        "/jarvis - Jarvis voice orchestrator mode\\n"\n'

BOTCMD_ANCHOR = '        BotCommand("clear", "Reset current session"),\n'
BOTCMD_NEW = BOTCMD_ANCHOR + '        BotCommand("jarvis", "Jarvis voice orchestrator mode"),\n'

HANDLER_ANCHOR = '    app.add_handler(CommandHandler("clear", cmd_clear))\n'
HANDLER_NEW = HANDLER_ANCHOR + '    app.add_handler(CommandHandler("jarvis", cmd_jarvis))\n'

EDITS = [
    ("config block", CONFIG_ANCHOR, CONFIG_ANCHOR + CONFIG_BLOCK),
    ("sticky primer function", PRIMER_ANCHOR, PRIMER_FUNC + PRIMER_ANCHOR),
    ("prime typed messages", PROMPT_SESSION, PROMPT_SESSION_NEW),
    ("prime trigger wakes", TRIGGER_SESSION, TRIGGER_SESSION_NEW),
    ("ping coalescer setup (watcher start)", WATCHER_START_ANCHOR, WATCHER_START_NEW),
    ("ping coalescer flush (watcher tick)", WATCHER_TICK_ANCHOR, WATCHER_TICK_NEW),
    ("/jarvis command", CMD_ANCHOR, CMD_FUNC + CMD_ANCHOR),
    ("/jarvis help line", HELP_ANCHOR, HELP_NEW),
    ("/jarvis menu entry", BOTCMD_ANCHOR, BOTCMD_NEW),
    ("/jarvis handler", HANDLER_ANCHOR, HANDLER_NEW),
]

# Names this patch defines. If any already exists without cmd_jarvis, a
# different edit took the name and patching would silently shadow it.
TAKEN_NAMES = ["def _maybe_prime_sticky(", "STICKY_PRIMERS =", "JARVIS_HOME =",
               "def _read_jarvis_env(", "def _journal_recent("]

# Functions the patch calls that must already exist in the bridge.
REQUIRED = ["def get_or_create_session(", "def reset_session(", "def _is_authorized(",
            "async def safe_send(", "async def _process_trigger(", "async def _watch_triggers(",
            "STATE_DIR = Path(", "from pathlib import Path", "import re\n",
            "from datetime import datetime, timedelta"]


def main(argv):
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    path = os.path.expanduser(args[0] if args else "~/.claude/telegram-bridge/bridge.py")
    src = open(path).read()

    if "def cmd_jarvis(" in src:
        print("Already patched (cmd_jarvis found). Nothing to do.")
        return 0
    taken = [n for n in TAKEN_NAMES if n in src]
    if taken:
        print("ABORTED, nothing written. bridge.py has no /jarvis command but already defines:")
        for n in taken:
            print("  -", n.strip())
        print("A hand edit or another patch uses the names this patch adds. Remove or rename those "
              "first, or apply the Jarvis blocks by hand.")
        return 1

    problems = [r for r in REQUIRED if r not in src]
    out = src
    for name, anchor, new in EDITS:
        n = out.count(anchor)
        status = "OK" if n == 1 else f"FAIL (found {n}, need exactly 1)"
        print(f"  {name:38s} {status}")
        if n != 1:
            problems.append(name)
        else:
            out = out.replace(anchor, new)
    if problems:
        print("\nABORTED, nothing written. These anchors or functions do not match this bridge.py:")
        for p in problems:
            print("  -", p)
        print("Your bridge differs from the part 1 version. Apply the same edits by hand: each one is a "
              "plain-text block in this script (CONFIG_BLOCK, PRIMER_FUNC, WATCHER_START_NEW, "
              "WATCHER_TICK_NEW, CMD_FUNC, ...).")
        return 1
    if check:
        print("\n--check: all anchors match. Run again without --check to apply.")
        return 0

    tmpdir = tempfile.mkdtemp(prefix="jarvis-patch-")
    tmp = os.path.join(tmpdir, "bridge.py")
    open(tmp, "w").write(out)
    try:
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as e:
        print("ABORTED: patched file does not compile, nothing written:\n", e)
        return 1

    backup = path + ".backup-" + datetime.now().strftime("%Y-%m-%d-%H%M%S") + "-pre-jarvis"
    shutil.copy2(path, backup)
    open(path, "w").write(out)
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\nPatched {path}\nBackup  {backup}\nNow restart the bridge (launchctl unload + load its plist).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
