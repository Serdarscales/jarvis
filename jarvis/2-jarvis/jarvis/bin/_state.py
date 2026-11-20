#!/usr/bin/python3
"""Tiny state helper for Jarvis: jobs.json (worker registry) + open-items.json (queue).

All mutations take an exclusive file lock so concurrent workers (many in parallel)
never clobber each other's read-modify-write.

Shared primitives (locking, atomic io, pid liveness, slots, open-item helpers,
pending inputs, triggers) live in ../cron/_threadcore.py -- ONE module
imported by BOTH this session-side engine and the launchd cron side, so there
is no second copy to drift. This module keeps its public CLI/API
surface unchanged.

Usage:
  _state.py add_job <job_id> <task>
  _state.py complete_job <job_id> <summary>
  _state.py add_open_item <kind> <ref> <summary>
  _state.py resolve_item <item_id>
  _state.py list_open
  _state.py list_jobs
  _state.py status

Threads engine:
  _state.py add_thread <id> <name> <task> <uuid> <charter_path>
  _state.py set_thread <id> '<json_fields>'
  _state.py commit_turn <id> <turn_id> <status> <status_line> <assistant_text> ['<usage_json>'] [deliverable]
  _state.py list_threads [filter|all]   (no filter = open threads only)
  _state.py slot_acquire <thread_id> <pid> <cap>
  _state.py slot_release <thread_id>
  _state.py slot_reap
"""
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cron"))
import _threadcore as _core  # noqa: E402  (THE shared thread-core, both sides)

# STATE defaults to <JARVIS_HOME>/state (resolved by _threadcore).
# JARVIS_STATE_DIR overrides it so the engine + tests can run fully isolated
# against a scratch dir without ever touching live jobs/open-items/threads.
STATE = _core.STATE
JOBS = os.path.join(STATE, "jobs.json")
ITEMS = os.path.join(STATE, "open-items.json")
# --- Threads engine state ---
THREADS = os.path.join(STATE, "threads.json")          # thread registry (single authority)
SLOTS = os.path.join(STATE, "turn-slots.json")         # liveness-derived governor slots

# shared primitives, re-exported under their historical names (public surface)
_now = _core._now
_lock = _core._lock
_load = _core._load
_save = _core._save
_save_text = _core._save_text
_pid_alive = _core._pid_alive
_live_slots = _core._live_slots


def add_job(job_id, task):
    with _lock(JOBS):
        d = _load(JOBS, {"jobs": []})
        d["jobs"].append({"id": job_id, "task": task, "status": "running",
                          "started": _now(), "finished": None, "summary": None})
        _save(JOBS, d)
    print(job_id)


def complete_job(job_id, summary):
    with _lock(JOBS):
        d = _load(JOBS, {"jobs": []})
        found = False
        for j in d["jobs"]:
            if j["id"] == job_id:
                j["status"] = "done"
                j["finished"] = _now()
                j["summary"] = summary
                found = True
        if not found:
            d["jobs"].append({"id": job_id, "task": "(unknown)", "status": "done",
                              "started": None, "finished": _now(), "summary": summary})
        _save(JOBS, d)
    print("ok")


def _add_open_item(kind, ref, summary):
    """Append an open item and RETURN its id (no print)."""
    return _core.add_open_item(kind, ref, summary, state_dir=STATE)


def add_open_item(kind, ref, summary):
    print(_add_open_item(kind, ref, summary))


def resolve_item(item_id):
    with _lock(ITEMS):
        d = _load(ITEMS, {"items": []})
        for it in d["items"]:
            if it["id"] == item_id:
                it["status"] = "resolved"
        _save(ITEMS, d)
    print("ok")


def _resolve_thread_items(thread_id):
    """Resolve any OPEN open-items pointer(s) for a thread (it is no longer
    'waiting on you')."""
    _core.resolve_thread_items(thread_id, state_dir=STATE)


def _ensure_thread_item(thread_id, name):
    """Ensure an OPEN open-items pointer exists for a thread that is handing
    work back (so a reset conductor sees it). No-op if one is already open."""
    _core.ensure_thread_item(thread_id, name, state_dir=STATE)


def list_open():
    d = _load(ITEMS, {"items": []})
    print(json.dumps([i for i in d["items"] if i.get("status") == "open"], indent=2))


def list_jobs():
    print(json.dumps(_load(JOBS, {"jobs": []}), indent=2))


def status():
    """Human-readable fleet status: running workers (with elapsed), live +
    waiting THREADS, and open items."""
    jobs = _load(JOBS, {"jobs": []}).get("jobs", [])
    items = _load(ITEMS, {"items": []}).get("items", [])
    threads = _load(THREADS, {"threads": {}}).get("threads", {})
    running = [j for j in jobs if j.get("status") == "running"]
    open_items = [i for i in items if i.get("status") == "open"]
    open_threads = [t for t in threads.values()
                    if t.get("status") not in ("done", "killed")]
    now = datetime.now()
    out = [f"RUNNING ({len(running)}):"]
    for j in running:
        el = ""
        if j.get("started"):
            try:
                secs = int((now - datetime.strptime(j["started"], "%Y-%m-%d %H:%M:%S")).total_seconds())
                el = f" [{secs // 60}m{secs % 60}s]"
            except ValueError:
                pass
        out.append(f"  - {j['id']}{el}: {(j.get('task') or '')[:140]}")
    if not running:
        out.append("  (nothing running)")
    out.append(f"THREADS ({len(open_threads)}):")
    for t in sorted(open_threads, key=lambda x: x.get("created", "")):
        live = ""
        if _core.flock_held(t.get("id"), state_dir=STATE):
            secs = int(_core._age_s(t.get("turn_started_at")))
            live = f" [RUNNING {secs // 60}m]" if secs else " [RUNNING]"
        out.append(f"  - {t.get('id')} ({t.get('status')}) {t.get('name', '')},"
                   f" turns {t.get('turns_count', 0)}{live}")
        if t.get("last_status_line"):
            out.append(f"      {(t.get('last_status_line') or '')[:160]}")
    if not open_threads:
        out.append("  (no open threads)")
    out.append(f"OPEN / waiting on you ({len(open_items)}):")
    for i in open_items:
        out.append(f"  - {i['id']} ({i.get('kind')}): {(i.get('summary') or '')[:160]}")
    if not open_items:
        out.append("  (nothing open)")
    print("\n".join(out))


# ---------------------------------------------------------------------------
# Threads engine: thread registry + liveness-derived governor.
# threads.json is the SINGLE authority for thread state; the per-thread
# transcript file is a DERIVED render of the committed turn log (never primary).
# ---------------------------------------------------------------------------

def _thread_transcript_path(thread_id):
    return os.path.join(STATE, "threads", f"{thread_id}.transcript.md")


def _transcript_turn_block(turn):
    return (f"## Turn {turn.get('n')} ({turn.get('turn_id')}) "
            f"[{turn.get('status')}] {turn.get('ts')}\n\n{turn.get('text', '')}\n")


def _render_transcript(t):
    """Render the human transcript from the committed turn log. Since the
    turn-text cap rows hold CAPPED turn text, so this backfill
    render is the degraded path (used only when the transcript file is missing);
    the normal path APPENDS each turn's FULL text at commit time."""
    lines = [f"# Thread {t.get('id')}: {t.get('name', '')}", "",
             f"Task: {t.get('task', '')}", ""]
    return "\n".join(lines) + "".join(
        "\n" + _transcript_turn_block(turn) for turn in t.get("turns", []))


def _cap_turn_text(text, cap):
    """threads.json diet: every op is a full read-modify-rewrite of
    threads.json, so monster turn texts make the hot file balloon. Cap what the
    ROW stores (head + tail with an explicit trim note); the transcript file
    keeps the full text. JARVIS_TURN_TEXT_CAP=0 disables."""
    if not text or not cap or len(text) <= cap:
        return text
    head, tail = text[:int(cap * 0.75)], text[-int(cap * 0.2):]
    return (head + f"\n\n[... trimmed {len(text) - len(head) - len(tail)} chars "
            f"(row cap {cap}); full text lives in the transcript file ...]\n\n" + tail)


def add_thread(thread_id, name, task, uuid, charter_path):
    """Create a new thread row (status=spawning) and, synchronously, an
    open-items pointer so the boot-check sees it the instant it exists."""
    with _lock(THREADS):
        d = _load(THREADS, {"threads": {}})
        d["threads"][thread_id] = {
            "id": thread_id, "name": name, "task": task,
            "session_uuid": uuid, "status": "spawning",
            "created": _now(), "last_turn": None, "last_status_line": None,
            "turns_count": 0, "last_completed_turn_id": None,
            "last_surfaced_turn_id": None, "charter_path": charter_path,
            "transcript_path": _thread_transcript_path(thread_id),
            "projected": False, "topic_id": None, "deliverable": None,
            "worker_pid": None, "tokens_total": 0, "done_at": None,
            "turns": [],
        }
        _save(THREADS, d)
    # Mirror jarvis-notify's working pattern (its line 17): pointer in open-items.
    _add_open_item("thread", thread_id, name)
    print(thread_id)


def set_thread(thread_id, fields):
    """Patch arbitrary fields on a thread under lock. `fields` is a dict."""
    if _core.set_thread(thread_id, fields, state_dir=STATE):
        print("ok")
    else:
        print("no-thread")


def commit_turn(thread_id, turn_id, status, status_line, assistant_text,
                usage=None, deliverable=None):
    """THE single atomic per-turn commit point.

    One locked read-modify-write of threads.json updates status, last_status_line,
    turns_count, last_completed_turn_id, tokens_total, optional deliverable, and
    appends the turn (with its text) to the committed turn log -- all in one
    temp+rename. The transcript file is then re-rendered from that committed log
    (a derived view; if it lags or is torn it can always be regenerated, so the
    authority never tears). Idempotent: a re-fired turn whose turn_id is already
    the last committed exits without side effects.

    Returns (and prints) the outcome so run_turn can act on it:
      "committed"        -- normal commit, status flipped, queue pointer synced
      "committed-closed" -- thread was closed by a human (kill/done): the turn is
                            appended for the record and tokens counted, but the
                            status does NOT flip and no queue pointer re-opens
      "dup"              -- this turn_id was already committed (no-op)
      "no-thread"        -- unknown thread id
    """
    usage = usage or {}
    tokens = (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
              + usage.get("cache_creation_input_tokens", 0)
              + usage.get("cache_read_input_tokens", 0))
    tokens_real = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    with _lock(THREADS):
        d = _load(THREADS, {"threads": {}})
        t = d["threads"].get(thread_id)
        if not t:
            print("no-thread")
            return "no-thread"
        if t.get("last_completed_turn_id") == turn_id:
            print("dup")
            return "dup"
        t.setdefault("turns", [])
        # belt-and-suspenders: never double-append the same turn_id
        if any(tt.get("turn_id") == turn_id for tt in t["turns"]):
            print("dup")
            return "dup"
        closed = bool(t.get("closed_by_human"))
        cap = int(os.environ.get("JARVIS_TURN_TEXT_CAP", "12000"))
        t["turns_count"] = t.get("turns_count", 0) + 1
        t["turns"].append({"n": t["turns_count"], "turn_id": turn_id,
                           "status": status, "ts": _now(),
                           "text": _cap_turn_text(assistant_text, cap)})
        t["last_completed_turn_id"] = turn_id
        t["tokens_total"] = t.get("tokens_total", 0) + tokens
        t["tokens_real"] = t.get("tokens_real", 0) + tokens_real
        t["last_turn"] = _now()
        if deliverable:
            t["deliverable"] = deliverable
        if not closed:
            t["status"] = status
            t["last_status_line"] = status_line
            if status == "done":
                t["done_at"] = _now()
        tname = t.get("name", thread_id)
        _save(THREADS, d)                     # <-- the single commit point
        # Transcript (derived, non-authoritative): APPEND this turn's FULL text
        # (the row may hold a capped copy). If the file is missing, backfill
        # from the rows (degraded to whatever text they still hold).
        tpath = t.get("transcript_path") or _thread_transcript_path(thread_id)
        try:
            full_block = _transcript_turn_block(dict(t["turns"][-1], text=assistant_text))
            if os.path.exists(tpath):
                with open(tpath, "a") as f:
                    f.write("\n" + full_block)
            else:
                backfill = dict(t, turns=t["turns"][:-1])
                _save_text(tpath, _render_transcript(backfill) + "\n" + full_block)
        except OSError:
            pass
    if closed:
        # a human closed/killed this thread while the turn was in flight: the
        # record is kept but nothing re-opens 
        print("committed-closed")
        return "committed-closed"
    # Keep the open-items queue accurate (done outside the THREADS lock to avoid
    # nested locks): a done thread is no longer "waiting on you"; a thread handing
    # work back gets a pointer so a reset conductor still sees it.
    if status == "done":
        _resolve_thread_items(thread_id)
    elif status in ("needs-input", "needs-attention", "stalled"):
        _ensure_thread_item(thread_id, tname)
    print("committed")
    return "committed"


OPEN_STATES = ("spawning", "working", "needs-input", "stalled", "needs-attention")


def list_threads(flt=None):
    """Print thread rows as JSON (turn text stripped for brevity).
    No filter -> OPEN threads only (the conductor's default working set).
    'all' -> every row still in the hot file (incl. done/killed).
    Any other filter matches status, name substring, or exact id across ALL
    rows, so by-name/id resolution of a done thread still works."""
    d = _load(THREADS, {"threads": {}})
    threads = list(d.get("threads", {}).values())
    if not flt:
        threads = [t for t in threads if t.get("status") in OPEN_STATES]
    elif flt.lower() != "all":
        f = flt.lower()
        threads = [t for t in threads
                   if f in (t.get("status") or "").lower()
                   or f in (t.get("name") or "").lower()
                   or f == (t.get("id") or "").lower()]
    out = []
    for t in sorted(threads, key=lambda x: x.get("created", "")):
        out.append({k: v for k, v in t.items() if k != "turns"})
    print(json.dumps(out, indent=2))


# --- liveness-derived governor -------------

def slot_acquire(thread_id, pid, cap):
    """Admit a running turn iff live-slot count < cap. Returns OK or FULL.
    Dead/stale slots are dropped first, so a kill -9'd worker can't block."""
    cap = int(cap)
    ceiling = float(os.environ.get("JARVIS_TURN_CEILING", "7200"))
    with _lock(SLOTS):
        d = _load(SLOTS, {"slots": []})
        # keep live slots, minus any prior slot for THIS thread (idempotent re-acquire)
        slots = [s for s in _live_slots(d.get("slots", []), ceiling)
                 if s.get("thread_id") != thread_id]
        if len(slots) < cap:
            slots.append({"thread_id": thread_id, "pid": int(pid),
                          "started": time.time()})
            d["slots"] = slots
            _save(SLOTS, d)
            print("OK")
        else:
            d["slots"] = slots
            _save(SLOTS, d)
            print("FULL")


def slot_release(thread_id):
    with _lock(SLOTS):
        d = _load(SLOTS, {"slots": []})
        d["slots"] = [s for s in d.get("slots", []) if s.get("thread_id") != thread_id]
        _save(SLOTS, d)
    print("ok")


def slot_reap():
    """Drop slots whose pid is dead or older than 2x the ceiling (self-heal)."""
    ceiling = float(os.environ.get("JARVIS_TURN_CEILING", "7200"))
    reaped = _core.slot_reap(state_dir=STATE, ceiling=ceiling)
    print(f"reaped {reaped}")


# --- thin CLI adapters for verbs that take structured args -------------------

def _cli_set_thread(thread_id, fields_json):
    set_thread(thread_id, json.loads(fields_json))


def _cli_commit_turn(thread_id, turn_id, status, status_line, assistant_text,
                     usage_json="{}", deliverable=None):
    commit_turn(thread_id, turn_id, status, status_line, assistant_text,
                json.loads(usage_json), deliverable)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    args = sys.argv[2:]
    fns = {"add_job": add_job, "complete_job": complete_job,
           "add_open_item": add_open_item, "resolve_item": resolve_item,
           "list_open": list_open, "list_jobs": list_jobs, "status": status,
           # threads engine
           "add_thread": add_thread, "set_thread": _cli_set_thread,
           "commit_turn": _cli_commit_turn, "list_threads": list_threads,
           "slot_acquire": slot_acquire, "slot_release": slot_release,
           "slot_reap": slot_reap}
    fn = fns.get(cmd)
    if not fn:
        print(f"unknown command: {cmd}\n{__doc__}")
        sys.exit(1)
    fn(*args)


if __name__ == "__main__":
    main()
