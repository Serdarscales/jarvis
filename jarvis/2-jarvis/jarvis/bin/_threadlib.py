#!/usr/bin/python3
"""Jarvis Threads engine -- the core launcher library.

A "thread" = a row in threads.json + a Claude session UUID, run as a detached,
resumable Claude Code worker. This module owns every mechanic a resumable
thread needs beyond what jarvis-spawn does:

  - session flags branched by turn (turn 1 = --session-id; turn N>1 = --resume)
  - process group spawn (start_new_session=True) + whole-family killpg reap
  - mandatory non-blocking per-thread flock (no silent JSONL braiding on --resume)
  - liveness-derived governor slot (cap simultaneous-running; never leaks)
  - OAuth keychain inject + auth-error retry (ported from the bridge)
  - stream-json result parse after exit + status-marker scan from assistant text
  - structural resume-failure detection + SAME-uuid recovery from the committed log
  - atomic per-turn commit via _state.commit_turn (one temp+rename)
  - turn_id idempotency, activity-aware timeout, token accounting

`jarvis-thread-run` runs ONE turn in the FOREGROUND (blocking) and holds the
flock + slot for the whole turn. Detaching is the wrappers' job (they background
jarvis-thread-run via the shell, the proven jarvis-spawn pattern), so the
conductor never blocks and BUSY/QUEUED stay synchronously observable.

CLI mechanics this engine relies on:
  create  -> JSONL at ~/.claude/projects/<workspace-slug>/<uuid>.jsonl
  resume  -> result event {type:result, subtype:success, is_error:false}
  bad resume -> exit 1, result {is_error:true, subtype:error_during_execution,
                errors:["No conversation found with session ID: <uuid>"]}
"""
import os
import re
import sys
import glob
import json
import time
import errno
import signal
import fcntl
import subprocess
import threading
import getpass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _state  # noqa: E402  (shares STATE/THREADS/SLOTS + atomic commit_turn)
import _threadcore  # noqa: E402  (the shared core: pending inputs, triggers, probes)

WORKSPACE = _threadcore.WORKSPACE        # workers start here (JARVIS_WORKSPACE)
CLAUDE = _threadcore.CLAUDE_BIN          # CLAUDE_BIN in jarvis.env
OWNER = _threadcore.OWNER
PROJECTS = os.path.expanduser("~/.claude/projects")
JARVIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHARTER_TEMPLATE = os.path.join(JARVIS_DIR, "charters", "thread-charter-template.md")

MODEL = os.environ.get("JARVIS_THREAD_MODEL") or os.environ.get("JARVIS_MODEL") or "opus[1m]"
# Worker reasoning effort, fixed for every thread turn (the conductor does not
# pick it per thread). --effort accepts low|medium|high|xhigh|max.
EFFORT = os.environ.get("JARVIS_THREAD_EFFORT") or "high"
RUN_CAP = int(os.environ.get("JARVIS_RUN_CAP", "8"))          # simultaneous-running cap
IDLE_WINDOW = int(os.environ.get("JARVIS_IDLE_WINDOW", "900"))  # no-activity kill (s)
CEILING = int(os.environ.get("JARVIS_TURN_CEILING", "7200"))   # absolute kill (s); the 15-min idle window catches hangs
TOKEN_CEILING = int(os.environ.get("JARVIS_TOKEN_CEILING", "500000"))  # one-time REAL-token (input+output) advisory, never a stop

# Hard guardrail carried in --append-system-prompt so it survives charter loss.
HARD_GUARDRAIL = (
    "You are an autonomous Jarvis thread worker running with "
    "--dangerously-skip-permissions; all file operations are pre-approved, never "
    "ask for permission. DRAFT ONLY: never send a message to a human (no email "
    "send, no Telegram or DM to anyone, no posting anywhere). If an outgoing "
    "message is needed, write the exact draft into your output and end the turn "
    "NEEDS-INPUT for {OWNER} to approve. End EVERY turn with exactly one marker as "
    "the LAST non-fenced line, in the form [[THREAD-STATUS: DONE|NEEDS-INPUT|"
    "WORKING]] one-line summary. Do NOT call jarvis-thread-* or jarvis-spawn. No "
    "em dashes in anything a person outside the company will read."
).replace("{OWNER}", OWNER)

RECOVERY_PREAMBLE = (
    "RECOVERY: your previous Claude session for this thread was lost and is being "
    "re-seeded. You are the SAME worker on the SAME thread. Continue toward the "
    "goal from where the committed log below leaves off. Do NOT restart from "
    "scratch. Honest caveat: only your visible turn log survived, not your latent "
    "in-session reasoning, so re-derive any working state you need from the log."
)

STATUS_MAP = {"DONE": "done", "NEEDS-INPUT": "needs-input", "WORKING": "working"}
_MARKER_RE = re.compile(r"\[\[THREAD-STATUS:\s*(DONE|NEEDS-INPUT|WORKING)\s*\]\]\s*(.*)", re.I)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _threads_dir():
    return os.path.join(_state.STATE, "threads")


def log(thread_id, msg):
    """Append an engine debug line to <id>.run.log (NOT the stream-json output)."""
    p = os.path.join(_threads_dir(), f"{thread_id}.run.log")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            f.write(f"{_state._now()} {msg}\n")
    except OSError:
        pass


def get_thread(thread_id):
    d = _state._load(_state.THREADS, {"threads": {}})
    return d.get("threads", {}).get(thread_id)


def resolve_jsonl(uuid):
    """Slug-safe resolution: glob across project dirs; never hand-build the slug.
    Returns the real path or None. (Verified: live sessions land in the
    slugified dir, the literal-space dir is empty.)"""
    hits = glob.glob(os.path.join(PROJECTS, "*", f"{uuid}.jsonl"))
    return hits[0] if hits else None


_CLI_VERSION = None


def _cli_version():
    """The real CLI's version string, cached per process (one process = one
    turn). Logged per turn as the drift canary's raw data."""
    global _CLI_VERSION
    if _CLI_VERSION is None:
        try:
            out = subprocess.run([CLAUDE, "--version"], capture_output=True,
                                 text=True, timeout=20)
            _CLI_VERSION = (out.stdout or "").strip() or "unknown"
        except Exception:
            _CLI_VERSION = "unknown"
    return _CLI_VERSION


AUTOMATION_TOKEN_FILE = os.path.expanduser(os.environ.get(
    "CLAUDE_AUTOMATION_TOKEN_FILE", "~/.config/claude/automation-oauth-token.txt"))


def keychain_token():
    """Read the Claude Code OAuth token from the Keychain, ONLY if its own
    expiresAt says it has >10 min left (mirrors the bridge). An injected dead token blocks the CLI's self-refresh and
    guarantees a 401. Returns None if missing/unreadable/near-expiry."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-a", getpass.getuser(), "-w"],
            capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return None
        oauth = json.loads(out.stdout.strip()).get("claudeAiOauth", {})
        token = oauth.get("accessToken")
        expires_at = oauth.get("expiresAt")
        if not token:
            return None
        # No verifiable freshness -> don't inject (an unverifiable token
        # blocks the CLI's self-refresh if it happens to be dead).
        if not isinstance(expires_at, (int, float)):
            return None
        if expires_at / 1000 - time.time() < 600:
            return None
        return token
    except Exception:
        return None


def automation_token():
    """Credential to inject for a worker attempt: long-lived setup-token file
    first (1y, immune to the 8h Keychain cycle), fresh Keychain token second,
    None (CLI self-auth) last. The auth retry in run_turn passes None
    explicitly, so a bad token never wedges a thread, it costs one attempt."""
    # Test seam (inert in production): fake workers never need a credential,
    # so the self-test reads no token file and no Keychain entry.
    if os.environ.get("JARVIS_FAKE_WORKER"):
        return None
    try:
        with open(AUTOMATION_TOKEN_FILE, encoding="utf-8") as f:
            token = f.read(4096).strip()
        if token.startswith("sk-ant-") and len(token) >= 40:
            return token
    except (OSError, UnicodeError, ValueError):
        pass
    return keychain_token()


def _jarvis_topic():
    return _threadcore.jarvis_topic()


def _build_env(token):
    """Worker env: mirror the bridge (drop CLAUDECODE), gate the worker, inject OAuth."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    # INVARIANT: never inherit an ambient token, without
    # this pop, _build_env(None) (the "token-free" auth retry) would silently
    # keep a dead inherited token and re-fail. Injection is explicit below.
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env["JARVIS_WORKER"] = "1"        # gates new/tell/done/kill/spawn (exit 3)
    # JARVIS_WORKER_DEPTH is ADVISORY ONLY: no code enforces it, the charter
    # prose does ("a helper must not spawn helpers"). The real runaway guards
    # are the family reap, the governor cap, and the 15-turn check-in.
    env["JARVIS_WORKER_DEPTH"] = "1"
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


# --------------------------------------------------------------------------- #
# stream-json parsing + marker scan
# --------------------------------------------------------------------------- #

def parse_result(jsonl_path):
    """Return the final stream-json `result` event dict from a per-invocation
    log, or None. Scans all lines and keeps the last type==result object."""
    result = None
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "result":
                    result = d
    except FileNotFoundError:
        return None
    return result


def is_resume_failure(result):
    """Structural detection (NOT a stdout grep): is_error + an errors[] entry of
    the session-not-found class. Verified against the CLI result-event shape."""
    if not result or not result.get("is_error"):
        return False
    errs = result.get("errors") or []
    blob = " ".join(str(e) for e in errs).lower()
    return ("no conversation found" in blob) or ("session id" in blob and "found" in blob)


def is_auth_error(result):
    if not result or not result.get("is_error"):
        return False
    blob = (str(result.get("result") or "") + " "
            + " ".join(str(e) for e in (result.get("errors") or []))).lower()
    return any(k in blob for k in ("401", "unauthorized", "authentication",
                                   "oauth", "invalid api key", "credit balance"))


def scan_marker(assistant_text):
    """Return (status, status_line) from the LAST [[THREAD-STATUS: ...]] line
    that sits OUTSIDE a fenced code block. (None, None) if missing/ambiguous so
    the caller can mark needs-attention (never a silent WORKING). Anchored to
    the start of the stripped line."""
    if not assistant_text:
        return (None, None)
    in_fence = False
    found = None
    for ln in assistant_text.splitlines():
        s = ln.strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _MARKER_RE.match(s)
        if m:
            found = (STATUS_MAP[m.group(1).upper()], m.group(2).strip())
    return found if found else (None, None)


def _detect_deliverable(assistant_text):
    """Charter says deliverables go to disk with the path on line 1. If line 1 is
    an existing absolute path, treat it as the deliverable."""
    if not assistant_text:
        return None
    body = assistant_text.strip()
    if not body:
        return None
    first = body.splitlines()[0].strip().strip("`").strip()
    if first.startswith("/") and os.path.exists(first) and os.path.isfile(first):
        return first
    return None


# --------------------------------------------------------------------------- #
# process group spawn + activity-aware watchdog + family reap
# --------------------------------------------------------------------------- #

def _killpg(pgid, thread_id=None):
    """SIGTERM the whole group, wait briefly, then SIGKILL. Reaps grandchildren."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):
        time.sleep(0.25)
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            if thread_id:
                log(thread_id, f"killpg {pgid}: terminated cleanly")
            return
    try:
        os.killpg(pgid, signal.SIGKILL)
        if thread_id:
            log(thread_id, f"killpg {pgid}: SIGKILLed")
    except ProcessLookupError:
        pass


def _group_pids(pgid):
    try:
        out = subprocess.run(["ps", "-g", str(pgid), "-o", "pid="],
                             capture_output=True, text=True, timeout=5)
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def _has_live_descendants(pgid, leader_pid):
    return any(p != leader_pid for p in _group_pids(pgid))


def spawn_and_wait(thread_id, argv, env, out_path):
    """Spawn argv detached in its own process group, watchdog it (activity-aware
    + absolute ceiling), wait, and return (rc, result_dict). Reaps the whole
    family on timeout via os.killpg. Reusable by tests with a synthetic argv."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    outf = open(out_path, "wb")
    try:
        proc = subprocess.Popen(argv, stdout=outf, stderr=outf,
                                stdin=subprocess.DEVNULL, cwd=WORKSPACE, env=env,
                                start_new_session=True)
    finally:
        outf.close()
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = proc.pid
    _state.set_thread(thread_id, {"worker_pid": pgid})
    log(thread_id, f"spawned pid={proc.pid} pgid={pgid}: {os.path.basename(argv[0])}")

    stop = threading.Event()
    timed_out = {"v": False}
    # poll often enough to honor small windows (tests) without busy-spinning in prod
    poll = max(1, min(15, CEILING // 2, IDLE_WINDOW // 2))

    def watchdog():
        start = time.time()
        last_size, last_change = -1, time.time()
        while not stop.wait(poll):
            now = time.time()
            if now - start > CEILING:
                log(thread_id, "watchdog: absolute ceiling exceeded -> killpg")
                timed_out["v"] = True
                _killpg(pgid, thread_id)
                return
            try:
                size = os.path.getsize(out_path)
            except OSError:
                size = last_size
            if size != last_size:
                last_size, last_change = size, now
            if (now - last_change) > IDLE_WINDOW and not _has_live_descendants(pgid, proc.pid):
                log(thread_id, "watchdog: idle window, no live descendants -> killpg")
                timed_out["v"] = True
                _killpg(pgid, thread_id)
                return

    wd = threading.Thread(target=watchdog, daemon=True)
    wd.start()
    rc = proc.wait()
    stop.set()
    wd.join(timeout=2)
    # belt: if we killed but stragglers remain in the group, reap again
    if timed_out["v"]:
        _killpg(pgid, thread_id)
    log(thread_id, f"worker exit rc={rc} timed_out={timed_out['v']}")
    return rc, parse_result(out_path)


# --------------------------------------------------------------------------- #
# prompt building
# --------------------------------------------------------------------------- #

def _committed_tail(t, max_turns=6):
    parts = []
    for turn in t.get("turns", [])[-max_turns:]:
        parts.append(f"[Turn {turn.get('n')} :: {turn.get('status')}]\n{turn.get('text', '')}")
    return "\n\n".join(parts)


def _build_prompt(t, input_text, first, recovery):
    charter = ""
    try:
        with open(t["charter_path"]) as f:
            charter = f.read()
    except OSError:
        charter = (f"You are a focused Jarvis thread worker. Goal:\n{t.get('task', '')}\n"
                   "End every turn with a [[THREAD-STATUS: DONE|NEEDS-INPUT|WORKING]] marker.")
    if recovery:
        seed = charter + "\n\n--- " + RECOVERY_PREAMBLE + " ---\n\n"
        seed += "Committed progress so far:\n" + (_committed_tail(t) or "(none)") + "\n\n"
        if input_text:
            seed += "Latest instruction from %s (continue the work):\n" % OWNER + input_text
        else:
            seed += "Continue toward the goal."
        return seed
    if first:
        # charter already embeds the task via the {{TASK}} fill
        return charter
    return input_text or "Continue toward the goal."


def _build_argv(uuid, prompt, create):
    # Test seam (inert in production): a synthetic worker so the mechanic tests
    # (reap/flock/governor/commit) run deterministically without a real claude
    # call. The fake cmd is expected to emit a stream-json result line itself.
    fake = os.environ.get("JARVIS_FAKE_WORKER")
    if fake:
        return ["/bin/bash", "-c", fake]
    argv = [CLAUDE, "-p", prompt,
            "--model", MODEL,
            "--effort", EFFORT,
            "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions",
            "--setting-sources", "project",
            "--append-system-prompt", HARD_GUARDRAIL]
    argv += (["--session-id", uuid] if create else ["--resume", uuid])
    return argv


# --------------------------------------------------------------------------- #
# completion ping (each finished turn queues one ping; the bridge coalescer
# merges a burst into one trigger). In test mode triggers go to a scratch dir, never the live bridge.
# --------------------------------------------------------------------------- #

def _trigger_dir():
    return _threadcore.trigger_dir()


def fire_completion(thread_id, status, status_line, turn_id=None):
    """Queue ONE completion ping for this finished turn (idempotent: called
    once per committed turn). the engine no longer writes conductor
    triggers directly -- it APPENDS to pending-pings.json and the BRIDGE's 5s
    loop coalesces a settled burst (~8s quiet) into ONE Jarvis-topic trigger via
    _threadcore.flush_pending_pings, which sets last_surfaced_turn_id at the
    moment the trigger actually exists. A failed append is logged, never
    swallowed: the watch sweep's lost-ping re-fire (keyed on
    last_surfaced_turn_id != last_completed_turn_id) is the net."""
    t = get_thread(thread_id) or {}
    try:
        _threadcore.pending_ping_add(thread_id, t.get("name", ""),
                                     turn_id or t.get("last_completed_turn_id"),
                                     status, status_line, state_dir=_state.STATE)
    except OSError as e:
        log(thread_id, f"pending-ping append FAILED ({e}); sweep will re-fire")


# --------------------------------------------------------------------------- #
# the turn (foreground-blocking; holds flock + slot for the whole turn)
# --------------------------------------------------------------------------- #

def run_turn(thread_id, input_text, first=False, auto=False):
    """Run ONE turn for a thread. Returns one of:
        BUSY     -- a turn is already in flight (flock held)
        QUEUED   -- the simultaneous-running cap is full
        NOTHREAD -- no such thread
        DUP      -- this turn_id is already committed (idempotent no-op)
        <status> -- the committed status: done|needs-input|working|needs-attention
    auto=True marks an engine-fired autocontinue turn: a GENERIC continue that
    hits BUSY is dropped instead of queued (the live turn's own tail re-checks
    autocontinue, so nothing is lost), while a real popped input re-queues.
    """
    t = get_thread(thread_id)
    if not t:
        return "NOTHREAD"

    lockpath = os.path.join(_threads_dir(), f"{thread_id}.lock")
    os.makedirs(_threads_dir(), exist_ok=True)
    lockfd = open(lockpath, "w")
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as e:
        if getattr(e, "errno", None) in (errno.EAGAIN, errno.EACCES) or isinstance(e, BlockingIOError):
            log(thread_id, "flock busy -> BUSY (a turn is already in flight)")
            if input_text and not (auto and input_text == GENERIC_CONTINUE):
                _threadcore.pending_add(thread_id, input_text, "busy",
                                        state_dir=_state.STATE)
                log(thread_id, "input queued to pending-inputs (busy); it retries when the live turn ends")
            lockfd.close()
            return "BUSY"
        lockfd.close()
        raise

    slot_held = False
    try:
        # re-read the row UNDER the flock
        t = get_thread(thread_id)
        if not t:
            return "NOTHREAD"

        # an explicit NEW turn on a human-closed thread is an intentional
        # reopen (the auto paths -- pending retry, autocontinue -- never
        # resume a closed thread, so only a deliberate tell/run lands here)
        if t.get("closed_by_human"):
            log(thread_id, "explicit turn on a closed thread -> reopening (clearing closed_by_human)")
            _state.set_thread(thread_id, {"closed_by_human": False})
            t["closed_by_human"] = False

        # governor: liveness-derived cap. Slot keyed on THIS run process's pid,
        # which is alive for the whole foreground turn.
        with _state._lock(_state.SLOTS):
            d = _state._load(_state.SLOTS, {"slots": []})
            live = [s for s in _state._live_slots(d.get("slots", []), CEILING)
                    if s.get("thread_id") != thread_id]
            if len(live) >= RUN_CAP:
                d["slots"] = live
                _state._save(_state.SLOTS, d)
                log(thread_id, f"governor FULL ({len(live)}/{RUN_CAP}) -> QUEUED")
                if input_text:
                    _threadcore.pending_add(thread_id, input_text, "queued",
                                            state_dir=_state.STATE)
                    log(thread_id, "input queued to pending-inputs (queued); it retries when a slot frees")
                return "QUEUED"
            live.append({"thread_id": thread_id, "pid": os.getpid(), "started": time.time()})
            d["slots"] = live
            _state._save(_state.SLOTS, d)
            slot_held = True

        turns_count = t.get("turns_count", 0)
        turn_id = f"{thread_id}:{turns_count + 1}"
        if t.get("last_completed_turn_id") == turn_id:
            log(thread_id, f"turn {turn_id} already committed -> DUP")
            return "DUP"

        uuid = t["session_uuid"]
        _state.set_thread(thread_id, {"status": "working",
                                      "turn_started_at": time.time()})
        if not os.environ.get("JARVIS_FAKE_WORKER"):
            log(thread_id, f"cli {_cli_version()}")

        # ---- attempt 1: normal create-or-resume --------------------------- #
        # cur_prompt/cur_create always describe the LAST attempt made, so the
        # auth retry re-runs exactly that attempt.
        create = bool(first)
        cur_prompt = _build_prompt(t, input_text, first=first, recovery=False)
        cur_create = create
        env = _build_env(automation_token())
        out_path = os.path.join(_threads_dir(), f"{thread_id}.t{turns_count + 1}.a1.jsonl")
        log(thread_id, f"turn {turn_id} attempt 1 ({'create' if create else 'resume'}, effort={EFFORT})")
        rc, result = spawn_and_wait(thread_id, _build_argv(uuid, cur_prompt, cur_create), env, out_path)

        # ---- resume-failure -> SAME-uuid recovery (retry once) ------------ #
        if (not create) and is_resume_failure(result):
            log(thread_id, "resume failed structurally -> same-uuid recovery")
            cur_prompt = _build_prompt(t, input_text, first=False, recovery=True)
            cur_create = True   # re-create the SAME uuid (its JSONL is gone)
            renv = _build_env(automation_token())
            rout = os.path.join(_threads_dir(), f"{thread_id}.t{turns_count + 1}.recovery.jsonl")
            rc, result = spawn_and_wait(thread_id, _build_argv(uuid, cur_prompt, cur_create), renv, rout)

        # ---- auth-error -> retry once letting the CLI self-auth ----------- #
        if is_auth_error(result):
            log(thread_id, "auth error -> retry once without injected token "
                           "(same prompt + mode as the attempt that failed)")
            renv = _build_env(None)
            aout = os.path.join(_threads_dir(), f"{thread_id}.t{turns_count + 1}.authretry.jsonl")
            rc, result = spawn_and_wait(thread_id, _build_argv(uuid, cur_prompt, cur_create), renv, aout)

        # ---- interpret result --------------------------------------------- #
        assistant_text = (result or {}).get("result") or ""
        usage = (result or {}).get("usage") or {}
        status, status_line = scan_marker(assistant_text)
        if status is None:
            status = "needs-attention"
            if assistant_text.strip():
                status_line = assistant_text.strip().splitlines()[-1][:200]
            else:
                status_line = f"worker produced no parseable status (rc={rc})"
            log(thread_id, f"no clean marker -> needs-attention: {status_line[:120]}")
        deliverable = _detect_deliverable(assistant_text)

        # Test hook (inert in production): simulate a hard crash at the most
        # dangerous instant -- after the worker did its work, right before the
        # commit -- to prove commit_turn is all-or-nothing (Test 5).
        if os.environ.get("JARVIS_TEST_CRASH_BEFORE_COMMIT") == "1":
            log(thread_id, "TEST: crashing before commit")
            os._exit(137)

        # ---- the single atomic commit ------------------------------------- #
        outcome = _state.commit_turn(thread_id, turn_id, status, status_line,
                                     assistant_text, usage, deliverable)
        if outcome == "dup":
            # a parallel commit won the race for this turn_id: no state change
            # belongs to THIS run, so no ping and no status claim
            log(thread_id, f"turn {turn_id} commit refused as dup -> no ping")
            return "DUP"
        if outcome == "no-thread":
            log(thread_id, f"turn {turn_id} commit found no thread -> no ping")
            return "NOTHREAD"
        if outcome == "committed-closed":
            # human closed/killed the thread mid-turn: keep the record, never
            # re-open or ping 
            log(thread_id, f"turn {turn_id} committed on a closed thread -> no ping, status stays")
            return status

        # token advisory: one-time heads-up on REAL tokens (input+output only;
        # cache reads are billing noise under Max). Never overrides the marker.
        t2 = get_thread(thread_id) or {}
        if t2.get("tokens_real", 0) > TOKEN_CEILING and not t2.get("token_warned"):
            log(thread_id, f"real-token advisory crossed ({t2.get('tokens_real')}); one-time heads-up")
            _state.set_thread(thread_id, {"token_warned": True})
            status_line = (f"{status_line} (heads up: this thread has burned "
                           f"{t2.get('tokens_real', 0) // 1000}k real tokens so far)")

        # wake routing: a WORKING commit that will auto-continue
        # does not wake the conductor at all; the driver loop handles it and
        # the committed log keeps the record. Only real events wake: needs-
        # input, done, needs-attention, and the check-in valve.
        wake = True
        if status == "working":
            decision = _autocontinue_decision(thread_id)
            if decision == "continue":
                wake = False
                log(thread_id, "working + autocontinue -> no conductor wake (next turn self-fires)")
            elif decision == "checkin":
                wake = False
                _fire_checkin(thread_id, turn_id, status_line)
                log(thread_id, f"turn {turn_id}: check-in valve fired (paused as needs-input)")
        if wake:
            fire_completion(thread_id, status, status_line, turn_id=turn_id)
        log(thread_id, f"turn {turn_id} committed status={status}")
        return status
    finally:
        if slot_held:
            try:
                _state.slot_release(thread_id)
            except Exception:
                pass
        try:
            fcntl.flock(lockfd, fcntl.LOCK_UN)
        except Exception:
            pass
        lockfd.close()


BIN_DIR = os.path.dirname(os.path.abspath(__file__))

# The autonomy driver's generic next-turn input (matches _build_prompt's
# fallback wording). A worker that ends WORKING is re-invoked with this.
GENERIC_CONTINUE = "Continue toward the goal."


def _autocontinue_decision(thread_id):
    """off | continue | checkin -- evaluated on a FRESH row read so a kill or
    close that landed during the turn is always respected.

    continue: status=working, not human-closed, JARVIS_AUTOCONTINUE != 0, and
              turns_count is NOT a multiple of JARVIS_CHECKIN_TURNS.
    checkin:  same but turns_count IS a multiple -> the runaway valve fires
              (a heads-up through Jarvis) and the thread pauses until told.
    off:      anything else (killed/closed/needs-input/env-disabled)."""
    if os.environ.get("JARVIS_AUTOCONTINUE", "1") == "0":
        return "off"
    t = get_thread(thread_id) or {}
    if t.get("status") != "working" or t.get("closed_by_human"):
        return "off"
    try:
        checkin = int(os.environ.get("JARVIS_CHECKIN_TURNS", "15") or 15)
    except ValueError:
        checkin = 15
    n = t.get("turns_count", 0)
    if checkin > 0 and n > 0 and n % checkin == 0:
        return "checkin"
    return "continue"


def _fire_checkin(thread_id, turn_id, status_line):
    """The N-turn valve: the thread has driven itself JARVIS_CHECKIN_TURNS
    turns since the last human word. Flip it to needs-input (so the queue
    pointer, the 3h reminder, and the stall detector all see the truth: it is
    waiting on a human), then wake the conductor ONCE to ask keep-pushing-or-
    wrap. The owner's answer via jarvis-thread-tell resumes the loop."""
    t = get_thread(thread_id) or {}
    n = t.get("turns_count", 0)
    line = (f"check-in after {n} self-driven turns: still going; "
            f"last: {status_line} -- keep pushing or wrap?")
    _state.set_thread(thread_id, {"status": "needs-input", "last_status_line": line})
    _state._ensure_thread_item(thread_id, t.get("name", thread_id))
    # the check-in rides the same coalesced pending-pings path as
    # every other conductor wake (status "check-in"; the coalesced prompt tells
    # Jarvis a check-in wants their keep-pushing-or-wrap call). The sweep's
    # needs-input re-fire is the net if the append or the flush is lost.
    try:
        _threadcore.pending_ping_add(thread_id, t.get("name", ""), turn_id,
                                     "check-in", line, state_dir=_state.STATE)
    except OSError as e:
        log(thread_id, f"check-in ping append FAILED ({e}); sweep will re-fire")


def kill_thread(thread_id, note=""):
    """'Kill it' actually kills: mark the thread killed +
    closed_by_human FIRST (so the dying turn's late commit sees the close and
    cannot re-open anything), then killpg the live worker family, then resolve
    the open-item pointers. A later explicit tell re-opens the thread."""
    t = get_thread(thread_id)
    if not t:
        print(f"ERROR: no such thread {thread_id}")
        return 2
    _state.set_thread(thread_id, {
        "status": "killed", "closed_by_human": True,
        "done_at": _state._now(),
        "last_status_line": note or ("killed by %s" % OWNER)})
    pgid = t.get("worker_pid")
    live = _threadcore.flock_held(thread_id, state_dir=_state.STATE)
    reaped = False
    if live and pgid:
        log(thread_id, f"kill: live turn detected -> killpg {pgid}")
        try:
            _killpg(int(pgid), thread_id)
            reaped = True
        except (ValueError, TypeError, OSError) as e:
            log(thread_id, f"kill: killpg {pgid} failed: {e}")
    _state._resolve_thread_items(thread_id)
    log(thread_id, f"killed by human ({'worker family reaped' if reaped else 'no live worker'})")
    print(f"killed {thread_id}" + (" (live worker family reaped)" if reaped else ""))
    return 0


def _relaunch_detached(thread_id, input_text, auto=False):
    """Fire the next turn for a thread as a DETACHED process (the wrappers'
    proven nohup pattern, engine-side). Never blocks the caller; output goes
    to the thread's run log like every other detached turn."""
    os.makedirs(_threads_dir(), exist_ok=True)
    runlog = os.path.join(_threads_dir(), f"{thread_id}.run.log")
    argv = ["/bin/bash", os.path.join(BIN_DIR, "jarvis-thread-run"), thread_id]
    if auto:
        argv.append("--auto")
    argv.append(input_text)
    with open(runlog, "a") as lf:
        subprocess.Popen(argv, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                         cwd=WORKSPACE, env=os.environ.copy(),
                         start_new_session=True)


def _post_turn(thread_id, res):
    """Runs in main() AFTER run_turn returns (flock + governor slot released).
    Retries pending inputs: first this thread's own queued input,
    else the oldest global pending whose thread is idle while the governor has
    room. At most ONE relaunch per finishing turn -- the chain continues as
    each subsequent turn ends. Closed/killed threads are never auto-resumed
    (their stuck entries surface via the watch sweep instead)."""
    if res in ("BUSY", "QUEUED"):
        return  # nothing ended here; the live turn's own tail retries
    # The autonomy driver: a turn that committed WORKING continues
    # itself. Prefer a queued human input over the generic continue. The
    # decision re-reads the row fresh, so a kill/close/check-in wins.
    if res == "working" and _autocontinue_decision(thread_id) == "continue":
        ent = _threadcore.pending_pop(thread_id=thread_id, state_dir=_state.STATE)
        nxt = ent.get("input") if (ent and ent.get("input")) else GENERIC_CONTINUE
        log(thread_id, f"autocontinue -> next turn ({'queued input ' + str(ent.get('id')) if ent else 'generic continue'})")
        _relaunch_detached(thread_id, nxt, auto=True)
        return
    t = get_thread(thread_id)
    if t and not t.get("closed_by_human") and t.get("status") != "killed":
        ent = _threadcore.pending_pop(thread_id=thread_id, state_dir=_state.STATE)
        if ent:
            log(thread_id, f"pending retry: delivering queued input {ent.get('id')} to this thread")
            _relaunch_detached(thread_id, ent.get("input") or "")
            return
    try:
        d = _state._load(_state.SLOTS, {"slots": []})
        room = len(_state._live_slots(d.get("slots", []), CEILING)) < RUN_CAP
    except Exception:
        room = False
    if not room:
        return
    for e in _threadcore.pending_list(state_dir=_state.STATE):
        tid2 = e.get("thread_id")
        if tid2 == thread_id:
            continue  # this thread was already handled (or is closed) above
        ot = get_thread(tid2)
        if not ot or ot.get("closed_by_human") or ot.get("status") == "killed":
            continue
        if _threadcore.flock_held(tid2, state_dir=_state.STATE):
            continue
        got = _threadcore.pending_pop(entry_id=e.get("id"), state_dir=_state.STATE)
        if got:
            log(tid2, f"pending retry: capacity freed, delivering queued input {got.get('id')}")
            _relaunch_detached(tid2, got.get("input") or "")
        return


def create_thread(name, task):
    """Allocate a thread id + session uuid, fill the charter from the template,
    register the row + open-item pointer. Returns the thread id. Does NOT run a
    turn (the caller fires turn 1 detached)."""
    import re as _re
    import uuid as _uuid
    import io as _io
    import contextlib as _ctx
    from datetime import datetime as _dt
    slug = (_re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24]) or "thread"
    tid = f"th-{slug}-{_dt.now().strftime('%Y%m%d-%H%M%S')}"
    u = str(_uuid.uuid4())
    try:
        tmpl = open(CHARTER_TEMPLATE).read()
    except OSError:
        tmpl = "# {{THREAD_NAME}}\n\n{{TASK}}\n"
    charter = (tmpl.replace("{{THREAD_NAME}}", name).replace("{{TASK}}", task)
               .replace("{{OWNER}}", OWNER)
               .replace("{{OUTPUTS_DIR}}", os.path.join(_state.STATE, "outputs") + "/"))
    cdir = os.path.join(_state.STATE, "charters")
    os.makedirs(cdir, exist_ok=True)
    cpath = os.path.join(cdir, tid + ".md")
    with open(cpath, "w") as f:
        f.write(charter)
    with _ctx.redirect_stdout(_io.StringIO()):   # swallow add_thread's id print
        _state.add_thread(tid, name, task, u, cpath)
    return tid


def main(argv):
    """CLI:
      _threadlib.py run <thread_id> [--first] "<input>"   -> run ONE turn
      _threadlib.py new "<name>" "<task>"                 -> create + print "<id>\\t<state_dir>"
      _threadlib.py kill <thread_id> ["note"]             -> kill the live worker family + sticky close
    """
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "new":
        if len(argv) < 3:
            print("usage: _threadlib.py new <name> <task>")
            return 1
        tid = create_thread(argv[1], argv[2])
        sys.stdout.write(f"{tid}\t{_state.STATE}\n")
        return 0
    if cmd == "kill":
        if len(argv) < 2:
            print("usage: _threadlib.py kill <thread_id> [note]")
            return 1
        return kill_thread(argv[1], argv[2] if len(argv) > 2 else "")
    if cmd == "run":
        args = argv[1:]
        first = "--first" in args
        auto = "--auto" in args
        args = [a for a in args if a not in ("--first", "--auto")]
        if not args:
            print("usage: _threadlib.py run <thread_id> [--first] [--auto] <input>")
            return 1
        thread_id = args[0]
        input_text = args[1] if len(args) > 1 else ""
        res = run_turn(thread_id, input_text, first=first, auto=auto)
        print(res)
        try:
            _post_turn(thread_id, res)
        except Exception as e:
            log(thread_id, f"post-turn hook error (pending retry skipped): {e}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
