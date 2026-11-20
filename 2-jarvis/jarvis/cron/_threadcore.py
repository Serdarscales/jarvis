#!/usr/bin/python3
"""THE single shared thread-core for the Jarvis Threads engine.

Both sides import this ONE module, so there is no second copy to drift:

  - session side: bin/_state.py + bin/_threadlib.py (they sys.path this dir)
  - cron side: _threadcron.py (same dir; verb orchestration only)

It also owns the kit's CONFIG: it loads <JARVIS_HOME>/jarvis.env into the
environment (values already set in the environment win), so every Python
entry point sees the same owner name, topic id, bridge dir and claude path.

Contents: locking, atomic load/save, pid liveness, governor-slot liveness,
session JSONL resolution, thread-row patching, open-item helpers, the
pending-inputs queue, atomic trigger writes, the
flock-gated stall predicate + live-turn probe, and small time
helpers. All functions are SILENT (no prints); the consumers own their own
CLI output conventions.

State resolution honors JARVIS_STATE_DIR exactly like _state.py/_threadcron.py
always did (isolated tests). Every state-touching function also takes an
optional state_dir override so a consumer with its own STATE spelling (the
jarvis/state symlink) stays byte-compatible.
"""
import os
import json
import time
import glob
import fcntl
import subprocess
from contextlib import contextmanager
from datetime import datetime

import shutil

# --------------------------------------------------------------------------- #
# config: <JARVIS_HOME>/jarvis.env (JARVIS_HOME = the folder above cron/)
# --------------------------------------------------------------------------- #
JARVIS_HOME = os.environ.get("JARVIS_HOME") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))


def _load_env_file(path):
    """KEY=VALUE lines into os.environ. Existing non-empty env values win;
    empty values in the file are skipped (so `KEY=` never masks a default)."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if k.startswith("export "):
                    k = k[7:].strip()
                if v and not os.environ.get(k):
                    os.environ[k] = v
    except OSError:
        pass


_load_env_file(os.path.join(JARVIS_HOME, "jarvis.env"))

OWNER = os.environ.get("JARVIS_OWNER") or "the user"
STATE = os.environ.get("JARVIS_STATE_DIR") or os.path.join(JARVIS_HOME, "state")
PROJECTS = os.path.expanduser("~/.claude/projects")
BRIDGE_DIR = os.path.expanduser(os.environ.get("BRIDGE_DIR") or "~/.claude/telegram-bridge")
CLAUDE_BIN = (os.environ.get("CLAUDE_BIN") or shutil.which("claude")
              or os.path.expanduser("~/.local/bin/claude"))
WORKSPACE = os.path.expanduser(os.environ.get("JARVIS_WORKSPACE") or "~")
BIN_DIR = os.path.join(JARVIS_HOME, "bin")


def trigger_dir():
    """Where the bridge watches for trigger files. Isolated tests (a
    JARVIS_STATE_DIR override) get a scratch dir, never the live bridge."""
    if os.environ.get("JARVIS_STATE_DIR"):
        return os.path.join(os.environ["JARVIS_STATE_DIR"], "triggers")
    return os.path.join(BRIDGE_DIR, "state", "triggers")


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _age_s(ts):
    """Age in seconds of a '%Y-%m-%d %H:%M:%S' string OR an epoch number.
    Unparseable/missing -> 0 (treated as 'just now', the safe direction)."""
    if ts is None or ts == "":
        return 0
    if isinstance(ts, (int, float)):
        return max(0, time.time() - float(ts))
    try:
        return (datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# locking + atomic io
# --------------------------------------------------------------------------- #

@contextmanager
def _lock(path):
    """Exclusive lock for a read-modify-write on `path` (via a sidecar .lock file)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lf = open(path + ".lock", "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass
        lf.close()


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _save_text(path, text):
    """Atomic text write (temp + rename), used for the rendered transcript."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# state file paths (state_dir override -> the caller's own STATE spelling)
# --------------------------------------------------------------------------- #

def threads_path(state_dir=None):
    return os.path.join(state_dir or STATE, "threads.json")


def slots_path(state_dir=None):
    return os.path.join(state_dir or STATE, "turn-slots.json")


def items_path(state_dir=None):
    return os.path.join(state_dir or STATE, "open-items.json")


def pending_path(state_dir=None):
    return os.path.join(state_dir or STATE, "pending-inputs.json")


def thread_lock_path(tid, state_dir=None):
    return os.path.join(state_dir or STATE, "threads", "%s.lock" % tid)


# --------------------------------------------------------------------------- #
# pid + governor-slot liveness
# --------------------------------------------------------------------------- #

def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    # The pid exists, but a zombie (killed, not yet reaped by its parent) still
    # passes os.kill(pid,0) -- treat a defunct/zombie process as dead so a
    # kill -9'd worker's slot is reclaimable.
    try:
        out = subprocess.run(["ps", "-o", "state=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=3)
        if out.stdout.strip().upper().startswith("Z"):
            return False
    except Exception:
        pass
    return True


def _live_slots(slots, ceiling):
    now = time.time()
    live = []
    for s in slots:
        if not _pid_alive(s.get("pid")):
            continue
        try:
            if (now - float(s.get("started", now))) > 2 * ceiling:
                continue
        except (TypeError, ValueError):
            pass
        live.append(s)
    return live


def slot_reap(state_dir=None, ceiling=None):
    """Drop slots whose pid is dead or older than 2x the ceiling. Returns the
    number reaped."""
    if ceiling is None:
        ceiling = float(os.environ.get("JARVIS_TURN_CEILING", "7200"))
    p = slots_path(state_dir)
    with _lock(p):
        d = _load(p, {"slots": []})
        before = len(d.get("slots", []))
        d["slots"] = _live_slots(d.get("slots", []), ceiling)
        _save(p, d)
        return before - len(d["slots"])


# --------------------------------------------------------------------------- #
# session JSONL resolution
# --------------------------------------------------------------------------- #

def resolve_jsonl(uuid):
    """Slug-safe resolution: glob across project dirs; never hand-build the slug.
    Returns the real path or None."""
    hits = glob.glob(os.path.join(PROJECTS, "*", "%s.jsonl" % uuid))
    return hits[0] if hits else None


# --------------------------------------------------------------------------- #
# thread-row patching + open-item helpers
# --------------------------------------------------------------------------- #

def set_thread(tid, fields, state_dir=None):
    """Patch arbitrary fields on a thread under lock. Returns True if the
    thread exists, False otherwise. Silent (callers own the CLI prints)."""
    p = threads_path(state_dir)
    with _lock(p):
        d = _load(p, {"threads": {}})
        t = d.get("threads", {}).get(tid)
        if not t:
            return False
        t.update(fields)
        _save(p, d)
        return True


def get_thread(tid, state_dir=None):
    d = _load(threads_path(state_dir), {"threads": {}})
    return d.get("threads", {}).get(tid)


def add_open_item(kind, ref, summary, state_dir=None):
    """Append an open item and return its id."""
    p = items_path(state_dir)
    with _lock(p):
        d = _load(p, {"items": []})
        item_id = "item-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        d["items"].append({"id": item_id, "kind": kind, "ref": ref,
                           "summary": summary, "status": "open", "ts": _now()})
        _save(p, d)
    return item_id


def ensure_thread_item(tid, name, state_dir=None):
    """Ensure an OPEN open-items pointer exists for a thread that is handing
    work back (so a reset conductor sees it). No-op if one is already open.
    Takes only the ITEMS lock -- never call while holding the THREADS lock."""
    p = items_path(state_dir)
    with _lock(p):
        d = _load(p, {"items": []})
        if any(it.get("kind") == "thread" and it.get("ref") == tid
               and it.get("status") == "open" for it in d.get("items", [])):
            return
        item_id = "item-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        d.setdefault("items", []).append(
            {"id": item_id, "kind": "thread", "ref": tid,
             "summary": name, "status": "open", "ts": _now()})
        _save(p, d)


def resolve_thread_items(tid, state_dir=None):
    """Resolve any OPEN open-items pointer(s) for a thread (it is no longer
    'waiting on you'). Takes only the ITEMS lock."""
    p = items_path(state_dir)
    with _lock(p):
        d = _load(p, {"items": []})
        changed = False
        for it in d.get("items", []):
            if (it.get("kind") == "thread" and it.get("ref") == tid
                    and it.get("status") == "open"):
                it["status"] = "resolved"
                changed = True
        if changed:
            _save(p, d)


# --------------------------------------------------------------------------- #
# pending-inputs queue
# --------------------------------------------------------------------------- #

def pending_add(thread_id, input_text, reason, state_dir=None):
    """Enqueue an input that could not run right now (flock BUSY, governor
    QUEUED, or a tell during spawning). Retried by the engine at the tail of
    every finishing turn; the watch sweep pings the conductor about entries
    stuck > ~10 min. Returns the entry id."""
    p = pending_path(state_dir)
    with _lock(p):
        d = _load(p, {"pending": []})
        eid = "pi-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        d.setdefault("pending", []).append(
            {"id": eid, "thread_id": thread_id, "input": input_text or "",
             "reason": reason, "ts": _now()})
        _save(p, d)
    return eid


def pending_list(state_dir=None):
    """All pending entries, oldest first (append order)."""
    return list(_load(pending_path(state_dir), {"pending": []}).get("pending", []))


def pending_pop(thread_id=None, entry_id=None, state_dir=None):
    """Pop and return one entry: by exact entry_id, or the OLDEST entry for
    thread_id. Returns None if nothing matches."""
    p = pending_path(state_dir)
    with _lock(p):
        d = _load(p, {"pending": []})
        for i, e in enumerate(d.get("pending", [])):
            if ((entry_id and e.get("id") == entry_id)
                    or (entry_id is None and thread_id and e.get("thread_id") == thread_id)):
                d["pending"].pop(i)
                _save(p, d)
                return e
    return None


# --------------------------------------------------------------------------- #
# pending-pings coalescer: workers only APPEND here on
# turn commit; the BRIDGE owns the flush (its 5s trigger loop calls
# flush_pending_pings), draining quiet batches into ONE Jarvis-topic trigger. Burst of
# finishes -> one conductor wake instead of a ping storm.
# --------------------------------------------------------------------------- #

def pending_pings_path(state_dir=None):
    return os.path.join(state_dir or STATE, "pending-pings.json")


def jarvis_topic(state_dir=None):
    """The Jarvis topic id from jarvis.env (JARVIS_TOPIC_ID). 0 if unset: the
    bridge rejects a trigger without a topic, so a missing id fails loudly in
    the bridge log instead of posting into the wrong topic."""
    try:
        return int(os.environ.get("JARVIS_TOPIC_ID") or 0)
    except ValueError:
        return 0


def pending_ping_add(thread_id, name, turn_id, status, status_line, state_dir=None):
    """Append one completion ping for the bridge to coalesce. Returns the id."""
    p = pending_pings_path(state_dir)
    with _lock(p):
        d = _load(p, {"pings": []})
        pid = "pp-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        d.setdefault("pings", []).append(
            {"id": pid, "thread_id": thread_id, "name": name or "",
             "turn_id": turn_id, "status": status,
             "status_line": (status_line or "")[:600], "ts": _now()})
        _save(p, d)
    return pid


_PING_PREAMBLE = (
    "You are Jarvis (read your persona at " + os.path.join(JARVIS_HOME, "jarvis-prompt.md")
    + " if you lack context). This is a background trigger, NOT "
    "a message from {OWNER}, so do not repeat it back. ")

_PING_CLOSING = (
    "Talk to {OWNER} DIRECTLY in your own voice (second person, casual, lead with "
    "the point, no em dashes): if a thread needs input, ask them the question and "
    "feed their answer back with jarvis-thread-tell (more than one waiting -> ask ONE "
    "AT A TIME, never fan one vague answer across threads); if it is done, tell them "
    "it shipped and show the deliverable; a check-in wants their keep-pushing-or-wrap "
    "call; if they seem mid-flow on something else, park it in the open-items queue. "
    "If after orienting NOTHING here needs them, end your reply with [[SILENT]].")


def coalesced_ping_prompt(entries):
    """One conductor prompt covering every drained ping (1..N threads)."""
    show = "bash \"%s\"" % os.path.join(BIN_DIR, "jarvis-thread-show")
    if len(entries) == 1:
        e = entries[0]
        return (_PING_PREAMBLE.replace("{OWNER}", OWNER)
                + "One of your threads, '%s' (%s), just hit %s: %s. "
                % (e.get("name", ""), e.get("thread_id"),
                   (e.get("status") or "").upper(), e.get("status_line", ""))
                + "Read its latest turn first: %s %s . " % (show, e.get("thread_id"))
                + _PING_CLOSING.replace("{OWNER}", OWNER))
    lines = "".join(
        "\n- '%s' (%s) hit %s: %s" % (e.get("name", ""), e.get("thread_id"),
                                      (e.get("status") or "").upper(),
                                      e.get("status_line", ""))
        for e in entries)
    return (_PING_PREAMBLE.replace("{OWNER}", OWNER)
            + ("%d of your threads just finished turns:" % len(entries)) + lines
            + "\n\nHandle ALL of them in THIS one turn. Read each latest turn first "
            + "(%s <id>). " % show + _PING_CLOSING.replace("{OWNER}", OWNER))


def flush_pending_pings(trig_dir, topic=None, state_dir=None, debounce_s=None):
    """The bridge-side flush: if the NEWEST pending ping is older than the
    debounce window (default 8s, env JARVIS_PING_DEBOUNCE_S), drain ALL entries
    into ONE trigger, mark each thread surfaced, and clear the drained entries.
    Crash-safe ordering: the trigger is written BEFORE entries are removed, so
    a failed write leaves everything queued for the next 5s tick. Entries whose
    turn the sweep already surfaced (or duplicate turn_ids) are dropped without
    re-firing. Returns the number of threads included in the fired trigger."""
    if debounce_s is None:
        debounce_s = int(os.environ.get("JARVIS_PING_DEBOUNCE_S", "8"))
    p = pending_pings_path(state_dir)
    entries = _load(p, {"pings": []}).get("pings", [])
    if not entries:
        return 0
    if min(_age_s(e.get("ts")) for e in entries) < debounce_s:
        return 0  # a burst is still settling; coalesce on a later tick
    seen, fire, drop_ids = set(), [], []
    for e in entries:
        key = (e.get("thread_id"), e.get("turn_id"))
        if key in seen:
            drop_ids.append(e.get("id"))
            continue
        seen.add(key)
        t = get_thread(e.get("thread_id"), state_dir) or {}
        if e.get("turn_id") and t.get("last_surfaced_turn_id") == e.get("turn_id"):
            drop_ids.append(e.get("id"))   # sweep already re-fired this one
            continue
        fire.append(e)
    if fire:
        write_trigger(trig_dir,
                      {"topic_id": int(topic) if topic else jarvis_topic(state_dir),
                       "prompt": coalesced_ping_prompt(fire),
                       "sender": "jarvis-worker"},
                      "jarvis-threads-coalesced-%d" % int(time.time()))
    handled = drop_ids + [e.get("id") for e in fire]
    with _lock(p):
        d = _load(p, {"pings": []})
        d["pings"] = [e for e in d.get("pings", []) if e.get("id") not in handled]
        _save(p, d)
    for e in fire:
        if e.get("turn_id"):
            set_thread(e["thread_id"], {"last_surfaced_turn_id": e["turn_id"]},
                       state_dir=state_dir)
    return len(fire)


# --------------------------------------------------------------------------- #
# atomic trigger writes
# --------------------------------------------------------------------------- #

def write_trigger(trig_dir, payload, name):
    """Write one trigger file atomically (temp + os.replace). The temp name
    ends '.json.tmp', which the bridge's *.json glob never matches. Returns
    the final path. Raises OSError on failure (callers decide how to log)."""
    os.makedirs(trig_dir, exist_ok=True)
    if not name.endswith(".json"):
        name += ".json"
    path = os.path.join(trig_dir, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------- #
# live-turn probe + flock-gated stall predicate
# --------------------------------------------------------------------------- #

def flock_held(tid, state_dir=None):
    """True iff the per-thread turn flock is currently held (a turn is live)."""
    lp = thread_lock_path(tid, state_dir)
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    lf = open(lp, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lf, fcntl.LOCK_UN)
        return False
    except (BlockingIOError, OSError):
        return True
    finally:
        lf.close()


def stall_check(state_dir, tid):
    """True iff the thread's status claims a run (working/spawning) but NO
    turn is actually live (flock acquirable). This is the reconciler's gate,
    now THE one shared stall condition (the last_turn-age heuristic that
    false-alarmed on healthy long turns is gone)."""
    t = get_thread(tid, state_dir)
    if not t or t.get("status") not in ("working", "spawning"):
        return False
    return not flock_held(tid, state_dir)


def live_turns(state_dir=None):
    """Ids of all threads whose turn flock is currently held (live turns)."""
    d = _load(threads_path(state_dir), {"threads": {}})
    return [tid for tid in d.get("threads", {}) if flock_held(tid, state_dir)]
