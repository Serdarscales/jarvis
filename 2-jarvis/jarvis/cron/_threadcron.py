#!/usr/bin/python3
"""Cron-side Jarvis Threads verbs (thin consumer of _threadcore).

All shared thread logic lives in ONE module in THIS dir, `_threadcore.py`,
imported by BOTH this file (cron side, run by the LaunchAgents) and
bin/_state.py + bin/_threadlib.py (session side). This file holds verb
ORCHESTRATION only.

Honors JARVIS_STATE_DIR / JARVIS_TURN_CEILING / JARVIS_TRANSCRIPT_CAP /
JARVIS_THREAD_ARCHIVE_DAYS / JARVIS_NEEDS_INPUT_IDLE_H for tests.

Verbs:
  _threadcron.py reconcile
  _threadcron.py watch_sweep <trigger_dir> <topic>
  _threadcron.py prune
  _threadcron.py drop_reorient <trigger_dir> <topic>
  _threadcron.py slot_reap
"""
import os
import sys
import json
import time
import gzip
import shutil
import fcntl
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _threadcore as core  # noqa: E402

STATE = core.STATE
THREADS = core.threads_path()
SLOTS = core.slots_path()
ITEMS = core.items_path()
PROJECTS = core.PROJECTS
CEILING = int(os.environ.get("JARVIS_TURN_CEILING", "7200"))
OPEN_STATES = {"spawning", "working", "needs-input", "stalled", "needs-attention"}

_now = core._now
_load = core._load
_save = core._save
_save_text = core._save_text
_age_s = core._age_s
resolve_jsonl = core.resolve_jsonl


def set_thread(tid, fields):
    core.set_thread(tid, fields, state_dir=STATE)


def add_open_item(kind, ref, summary):
    return core.add_open_item(kind, ref, summary, state_dir=STATE)


def slot_reap():
    return core.slot_reap(state_dir=STATE, ceiling=CEILING)


def _drop(trig_dir, topic, prompt, tag, sender):
    prompt = prompt.replace("{OWNER}", core.OWNER)
    core.write_trigger(trig_dir,
                       {"topic_id": int(topic), "prompt": prompt, "sender": sender},
                       "jarvis-%s-%d.json" % (tag, int(time.time())))


_PREAMBLE = ("You are Jarvis (read your persona if you lack context). This is a background "
             "trigger, NOT a message from {OWNER}, so do not repeat it back. ")

_REFIRE_WORDING = {
    "needs-input": ("is waiting on input from {OWNER} but its completion ping looks lost: %s. "
                    "Read the turn (jarvis-thread-show), then ask {OWNER} DIRECTLY in your own "
                    "voice and feed their answer back with jarvis-thread-tell."),
    "done": ("finished (DONE) but its completion ping looks lost: %s. Read the turn "
             "(jarvis-thread-show), then tell {OWNER} DIRECTLY in your own voice that it "
             "shipped and show the deliverable."),
    "needs-attention": ("needs attention but its ping looks lost: %s. Read the turn "
                        "(jarvis-thread-show) and decide: fix it yourself with "
                        "jarvis-thread-tell, or surface it to {OWNER} in your own voice."),
}


def watch_sweep(trig_dir, topic):
    """The threads safety net:

    - STALL (flock-gated): status says a run is live (working/spawning) but NO
      turn is actually in flight (flock acquirable) and nothing has happened
      for >10 min -> mark stalled + one trigger. The old last_turn-age rule
      (which false-alarmed on healthy long turns) is GONE: a held flock means
      a live turn, never a stall.
    - LOST-PING RE-FIRE: needs-input/done/needs-attention whose last turn was
      never surfaced (last_surfaced_turn_id != last_completed_turn_id) and is
      >10 min old -> re-fire ONE trigger, then mark it surfaced.
    - 3h REMINDER: needs-input, already surfaced, idle past N hours -> one
      re-ask per turn (flag-deduped), as before.
    - STUCK PENDING: a pending-inputs entry older than 10 min -> one trigger
      telling the conductor which thread + the queued text (flag-deduped).
    - CLI CANARY: claude --version changed since .cli-version -> one trigger
      telling the conductor to run jarvis-selftest (the offline engine
      check) and report only failures.
    Then reap dead governor slots."""
    needs_idle_h = int(os.environ.get("JARVIS_NEEDS_INPUT_IDLE_H", "3"))
    grace_s = int(os.environ.get("JARVIS_STALL_GRACE_S", "600"))
    refire_s = int(os.environ.get("JARVIS_REFIRE_S", "600"))
    pending_stuck_s = int(os.environ.get("JARVIS_PENDING_STUCK_S", "600"))
    flag_path = os.path.join(STATE, ".thread-surfaced.json")
    flagged = set(_load(flag_path, []))
    threads = _load(THREADS, {"threads": {}}).get("threads", {})
    fired = []
    for tid, t in threads.items():
        st = t.get("status")
        if st in ("working", "spawning"):
            # freshest activity signal wins: turn start, last commit, creation
            ages = [core._age_s(v) for v in
                    (t.get("turn_started_at"), t.get("last_turn"), t.get("created"))
                    if v not in (None, "")]
            idle = min(ages) if ages else 0
            if idle > grace_s and core.stall_check(STATE, tid):
                key = "stalled:%s:%s" % (tid, t.get("last_completed_turn_id"))
                if key not in flagged:
                    set_thread(tid, {"status": "stalled"})
                    _drop(trig_dir, topic,
                          _PREAMBLE +
                          "Your thread %s ('%s') says it is running but has NO live turn "
                          "process (its run died or never started) and nothing has happened "
                          "for %d min; its last line was: %s. Talk to {OWNER} DIRECTLY in "
                          "your own voice: a short honest heads up, and ask whether they want "
                          "you to nudge it (jarvis-thread-tell), kill it (jarvis-thread-kill), "
                          "or leave it. No em dashes."
                          % (tid, t.get("name", ""), int(idle / 60), t.get("last_status_line", "")),
                          "thread-stalled-%s" % tid, "jarvis-watch")
                    flagged.add(key)
                    fired.append(("stalled", tid))
            continue
        if st in ("needs-input", "done", "needs-attention"):
            surfaced = t.get("last_surfaced_turn_id") == t.get("last_completed_turn_id")
            if (not surfaced and t.get("last_turn")
                    and _age_s(t["last_turn"]) > refire_s):
                _drop(trig_dir, topic,
                      _PREAMBLE + ("Your thread %s ('%s') " % (tid, t.get("name", "")))
                      + (_REFIRE_WORDING[st] % t.get("last_status_line", ""))
                      + " No em dashes.",
                      "thread-refire-%s" % tid, "jarvis-watch")
                set_thread(tid, {"last_surfaced_turn_id": t.get("last_completed_turn_id")})
                fired.append(("refire", tid))
            elif (st == "needs-input" and surfaced and t.get("last_turn")
                    and _age_s(t["last_turn"]) > needs_idle_h * 3600):
                key = "reask:%s:%s" % (tid, t.get("last_completed_turn_id"))
                if key not in flagged:
                    _drop(trig_dir, topic,
                          _PREAMBLE +
                          "Your thread %s ('%s') is still waiting on {OWNER}: %s. Talk to them "
                          "DIRECTLY in your own voice to re-surface it, then feed their answer "
                          "back with jarvis-thread-tell. No em dashes."
                          % (tid, t.get("name", ""), t.get("last_status_line", "")),
                          "thread-reask-%s" % tid, "jarvis-watch")
                    flagged.add(key)
                    fired.append(("reask", tid))
    # stuck pending inputs: queued > N min with nothing having retried them yet
    for e in core.pending_list(state_dir=STATE):
        if _age_s(e.get("ts")) > pending_stuck_s:
            key = "pending:%s" % e.get("id")
            if key not in flagged:
                _drop(trig_dir, topic,
                      _PREAMBLE +
                      "An input for your thread %s has been sitting in the pending queue "
                      "for over %d min without running (reason it queued: %s). The queued "
                      "text was: %s. Check the thread (jarvis-thread-show %s) and re-tell "
                      "it with jarvis-thread-tell if it should still run. No em dashes."
                      % (e.get("thread_id"), int(pending_stuck_s / 60), e.get("reason", "?"),
                         (e.get("input") or "")[:400], e.get("thread_id")),
                      "pending-stuck", "jarvis-watch")
                flagged.add(key)
                fired.append(("pending-stuck", e.get("thread_id")))
    # CLI drift canary: ping ONCE per version change
    vfile = os.path.join(STATE, ".cli-version")
    try:
        import subprocess
        cur = subprocess.run([core.CLAUDE_BIN, "--version"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        cur = ""
    if cur:
        try:
            prev = open(vfile).read().strip()
        except OSError:
            prev = ""
        if not prev:
            _save_text(vfile, cur + "\n")
        elif cur != prev:
            _save_text(vfile, cur + "\n")
            key = "cli:%s" % cur
            if key not in flagged:
                _drop(trig_dir, topic,
                      _PREAMBLE +
                      "The Claude CLI auto-updated from '%s' to '%s'. The threads engine "
                      "depends on its structural behavior (resume semantics, result-event "
                      "shape), so run the offline engine check now: bash \"%s\" "
                      "and report the PASS/FAIL summary to {OWNER} only if something "
                      "failed. No em dashes."
                      % (prev, cur, os.path.join(core.BIN_DIR, "jarvis-selftest")),
                      "cli-change", "jarvis-watch")
                flagged.add(key)
                fired.append(("cli-change", cur))
    try:
        _save_text(flag_path, json.dumps(sorted(flagged)))
    except Exception:
        pass
    reaped = slot_reap()
    print("threads sweep:", fired if fired else "nothing", "slots_reaped=%d" % reaped)


def reconcile():
    """single-flight, pid-gated drift repair + slot reap. Never repairs a thread
    whose per-thread flock is held (a turn is live)."""
    os.makedirs(STATE, exist_ok=True)
    rlock = open(os.path.join(STATE, "reconcile.lock"), "w")
    try:
        fcntl.flock(rlock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("reconcile: already running, skipping")
        return
    repaired = []
    d = _load(THREADS, {"threads": {}})
    for tid, t in list(d.get("threads", {}).items()):
        if t.get("status") not in OPEN_STATES:
            continue
        lp = core.thread_lock_path(tid, STATE)
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        lf = open(lp, "w")
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            lf.close()
            continue  # a turn is in flight -> leave it alone
        try:
            notes = []
            if not (t.get("charter_path") and os.path.exists(t["charter_path"])):
                notes.append("charter-missing")
            if t.get("turns_count", 0) > 0 and not resolve_jsonl(t.get("session_uuid", "")):
                notes.append("jsonl-missing(recoverable)")
            if t.get("status") in ("working", "spawning"):
                set_thread(tid, {"status": "stalled"})
                notes.append("%s->stalled(no-live-turn)" % t.get("status"))
            items = _load(ITEMS, {"items": []})
            has = any(it.get("kind") == "thread" and it.get("ref") == tid
                      and it.get("status") == "open" for it in items.get("items", []))
            if not has and t.get("status") in OPEN_STATES:
                add_open_item("thread", tid, t.get("name", tid))
                notes.append("open-item-restored")
            if notes:
                repaired.append({tid: notes})
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
            lf.close()
    reaped = slot_reap()
    print("reconcile:", repaired if repaired else "no drift", "slots_reaped=%d" % reaped)


def _archive_cut(t, cut):
    """True iff this row is closed (done/killed), not projected, and its
    done_at is older than the cut."""
    if t.get("status") not in ("done", "killed") or t.get("projected"):
        return False
    da = t.get("done_at")
    try:
        return bool(da) and datetime.strptime(da, "%Y-%m-%d %H:%M:%S") < cut
    except Exception:
        return False


def prune():
    """Archive done-thread session JSONLs (by EXACT uuid, gzipped into the state dir
    first, then removed); move long-closed rows out of the hot threads.json into
    threads-archive.json; roll up oversized
    transcripts; sweep stale logs."""
    days = int(os.environ.get("JARVIS_THREAD_ARCHIVE_DAYS", "30"))
    cap = int(os.environ.get("JARVIS_TRANSCRIPT_CAP", str(256 * 1024)))
    cut = datetime.now() - timedelta(days=days)
    arch = os.path.join(STATE, "archived-sessions")
    os.makedirs(arch, exist_ok=True)
    tdir = os.path.join(STATE, "threads")
    threads = _load(THREADS, {"threads": {}}).get("threads", {})
    archived = 0
    for tid, t in threads.items():
        if not _archive_cut(t, cut):
            continue
        u = t.get("session_uuid")
        src = resolve_jsonl(u) if u else None
        if src and os.path.isfile(src):
            try:
                with open(src, "rb") as fi, gzip.open(os.path.join(arch, u + ".jsonl.gz"), "wb") as fo:
                    shutil.copyfileobj(fi, fo)
                os.remove(src)   # the one cross-tree removal: exact uuid, archived first
                archived += 1
            except OSError as e:
                print("archive skip", tid, e)
    # hot-file diet: closed rows past the cut whose session JSONL is already
    # archived/gone move to threads-archive.json (same age knob; a row whose
    # JSONL archive failed above stays hot and retries next night). All the
    # sweeps (watch/reconcile/reorient/status) look at open states only, so an
    # archived row is invisible to them by construction.
    rows_moved = 0
    with core._lock(THREADS):
        d = _load(THREADS, {"threads": {}})
        move = [tid for tid, t in d.get("threads", {}).items()
                if _archive_cut(t, cut)
                and not (t.get("session_uuid") and resolve_jsonl(t["session_uuid"]))]
        if move:
            apath = os.path.join(STATE, "threads-archive.json")
            a = _load(apath, {"threads": {}})
            for tid in move:
                a["threads"][tid] = d["threads"].pop(tid)
            _save(apath, a)
            _save(THREADS, d)
            rows_moved = len(move)
    rolled = swept = 0
    if os.path.isdir(tdir):
        for f in os.listdir(tdir):
            p = os.path.join(tdir, f)
            try:
                if f.endswith(".transcript.md") and os.path.getsize(p) > cap:
                    with open(p, "rb") as fi, gzip.open(p + (".%d.gz" % int(time.time())), "wb") as fo:
                        shutil.copyfileobj(fi, fo)
                    with open(p) as fi:
                        tail = fi.readlines()[-200:]
                    _save_text(p, "".join(tail))
                    rolled += 1
                elif (f.endswith(".jsonl") or f.endswith(".run.log")) and os.path.isfile(p) \
                        and os.path.getmtime(p) < (time.time() - days * 86400):
                    os.remove(p)
                    swept += 1
            except OSError as e:
                print("rollup/sweep skip", f, e)
    print("threads prune: archived=%d rows_moved=%d transcripts_rolled=%d logs_swept=%d"
          % (archived, rows_moved, rolled, swept))


def drop_reorient(trig_dir, topic):
    threads = _load(THREADS, {"threads": {}}).get("threads", {})
    opent = [t for t in threads.values() if t.get("status") in OPEN_STATES]
    if not opent:
        print("no open threads; no re-orient trigger")
        return
    names = ", ".join("%s (%s)" % (t.get("name", ""), t.get("status")) for t in opent[:8])
    _drop(trig_dir, topic,
          "You are Jarvis. You just (re)started; this is a boot trigger, NOT a message from {OWNER}, so "
          "do not repeat it back. Re-orient to your in-flight threads (run jarvis-thread-list for the "
          "full picture) and pick up conducting them. Open right now: %s. If anything is waiting on "
          "{OWNER}, talk to them DIRECTLY in your own voice, one thread at a time; otherwise just keep "
          "driving the rest. No em dashes." % names, "bootreconcile", "jarvis-bootreconcile")
    print("dropped re-orient trigger for %d open thread(s)" % len(opent))


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "reconcile":
        reconcile()
    elif cmd == "watch_sweep":
        watch_sweep(argv[1], argv[2])
    elif cmd == "prune":
        prune()
    elif cmd == "drop_reorient":
        drop_reorient(argv[1], argv[2])
    elif cmd == "slot_reap":
        print("reaped", slot_reap())
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
