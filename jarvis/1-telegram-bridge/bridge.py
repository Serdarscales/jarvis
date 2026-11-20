#!/usr/bin/env python3
"""
Claude Code Telegram Bridge

Connects a Telegram group (with Topics enabled) to Claude Code CLI sessions.
Each topic = one Claude Code conversation with full context and tool access.

Architecture:
    Telegram Group (Topics) <-> This Bot <-> Claude Code CLI (claude -p)
    Each topic_id maps to a unique Claude Code session UUID.
    Sessions persist across bot restarts via sessions.json.
"""

import asyncio
import fcntl
import getpass
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Optional: entity-based markdown formatting (preferred over MarkdownV2 parse mode)
try:
    from telegramify_markdown import convert as tm_convert
    HAS_TELEGRAMIFY = True
except ImportError:
    HAS_TELEGRAMIFY = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS = frozenset(
    int(uid.strip()) for uid in os.environ["ALLOWED_USER_IDS"].split(",")
)
# Optional: restrict bot to a specific Telegram group (recommended)
ALLOWED_GROUP_ID = int(os.environ["TELEGRAM_GROUP_ID"]) if os.environ.get("TELEGRAM_GROUP_ID") else None
WORKING_DIR = os.environ.get("WORKING_DIR") or os.path.expanduser("~")
WHISPER_PROVIDER = os.environ.get("WHISPER_PROVIDER", "").strip().lower()
WHISPER_API_KEY = os.environ.get("WHISPER_API_KEY", "")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))  # 5 min default (triggers)
# Idle timeout: kill the claude subprocess if it produces NO output for this many
# seconds (default 15 min). Total runtime is unbounded: long agentic tasks
# (deep research, multi-step workflows) can run for hours as long as they keep
# streaming events. Only true hangs (TCC prompts, network stalls) get killed.
IDLE_TIMEOUT = int(os.environ.get("CLAUDE_IDLE_TIMEOUT", os.environ.get("USER_MSG_TIMEOUT", "900")))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")  # empty = default model


def _parse_topic_map(name: str) -> dict[str, str]:
    """Read a per-topic setting from the env var `name`.

    Format: comma-separated `topic:value` pairs, e.g. `123:opus[1m],456:sonnet`.
    The topic is the numeric Telegram topic id (send /id in the topic to see
    it), or `general` for the General topic."""
    out = {}
    for pair in (os.environ.get(name) or "").split(","):
        topic, sep, value = pair.partition(":")
        if sep and topic.strip() and value.strip():
            out[topic.strip()] = value.strip()
    return out


# Per-TOPIC model override (TOPIC_MODELS in .env). The bridge runs one Claude
# session per Telegram topic, so each topic can ride a different model. Value =
# a model alias/ID, optionally with the [1m] suffix to opt into the 1M context
# window. A topic not listed here falls back to CLAUDE_MODEL, and if that is
# empty, to the CLI default (~/.claude/settings.json).
TOPIC_MODELS = _parse_topic_map("TOPIC_MODELS")


# Fallback ladder, tried in order when a topic's model is rate-capped or out of
# usage credits. Without it the CLI's "You've reached your ... limit. Switch to
# another model..." sentence comes back as an ordinary reply and gets posted
# verbatim, so the topic looks dead for the whole cap window.
# Aliases, not pinned IDs, so the ladder follows the newest model in each tier.
# CLAUDE_MODEL_FALLBACKS (comma-separated) applies to every topic; when it is
# not set, the ladder is "sonnet[1m]" for a [1m] primary model, else "sonnet".
# TOPIC_MODEL_FALLBACKS overrides it per topic, rungs separated by "|":
# e.g. 123:sonnet[1m]|haiku.
TOPIC_MODEL_FALLBACKS = {
    k: [m.strip() for m in v.split("|") if m.strip()]
    for k, v in _parse_topic_map("TOPIC_MODEL_FALLBACKS").items()
}
CLAUDE_MODEL_FALLBACKS = [
    m.strip() for m in (os.environ.get("CLAUDE_MODEL_FALLBACKS") or "").split(",") if m.strip()
]

# Reasoning effort, passed as --effort on every spawn (user messages, triggers,
# retries, fallback rungs). Without it every topic inherits the global
# ~/.claude/settings.json value, which changes whenever you pick a different
# effort in the Claude desktop app or an IDE chat, and the whole bridge silently
# moves with it. Accepted: low|medium|high|xhigh|max (the flag also applies to
# --resume turns, and Sonnet accepts it without error). BRIDGE_EFFORT=default
# passes no flag (follow the global setting). TOPIC_EFFORTS overrides per topic.
_effort_env = (os.environ.get("BRIDGE_EFFORT") or "high").strip().lower()
BRIDGE_EFFORT = "" if _effort_env in ("default", "none", "off") else _effort_env
TOPIC_EFFORTS = _parse_topic_map("TOPIC_EFFORTS")


def _effort_for_topic(topic_key) -> str:
    """Resolve the effort for a topic: per-topic override > BRIDGE_EFFORT.
    Empty = no flag, so the CLI default from ~/.claude/settings.json applies."""
    effort = TOPIC_EFFORTS.get(str(topic_key), "") or BRIDGE_EFFORT
    return "" if effort.lower() in ("default", "none", "off") else effort


def _model_label(model: str) -> str:
    """Readable name for a model ID, for messages the user sees."""
    if not model:
        return "the default model"
    parts = [x for x in model.replace("[1m]", "").replace("claude-", "").split("-") if x]
    if not parts:
        return model
    label = parts[0].capitalize() + (f" {parts[1]}" if len(parts) > 1 else "")
    return label + (" (1M)" if "[1m]" in model else "")


def _model_chain(topic_key) -> list[str]:
    """The topic's primary model followed by its fallback ladder."""
    primary = TOPIC_MODELS.get(str(topic_key), "") or CLAUDE_MODEL
    fallbacks = TOPIC_MODEL_FALLBACKS.get(str(topic_key)) or CLAUDE_MODEL_FALLBACKS or (
        ["sonnet[1m]"] if "[1m]" in primary else ["sonnet"]
    )
    chain = [primary]
    for m in fallbacks:
        if m and m not in chain:
            chain.append(m)
    return chain


def _model_for_topic(topic_key) -> str:
    """Resolve the model for a topic: per-topic override > CLAUDE_MODEL > CLI
    default, skipping any model currently inside a known cap window, so a
    capped model costs one failed attempt total, not one per message."""
    chain = _model_chain(topic_key)
    for m in chain:
        if not _model_is_capped(m):
            return m
    return chain[0]
STREAM_EDIT_INTERVAL = 1.5  # Seconds between Telegram message edits during streaming

# Error markers Claude CLI surfaces in its streamed output when an upstream
# Anthropic API call fails. Used by retry logic in _process_prompt (user
# messages, single auth retry) and _process_trigger (cron-fired messages,
# auth retry + transient backoff retry).
_AUTH_ERR_MARKERS = (
    "authentication_error",
    "failed to authenticate",
    "api error: 401",
    "invalid bearer token",
    # Phrasings drift (3 distinct ones observed in 90 days), matched
    # case-insensitively via _is_auth_err(). Defense-in-depth: all observed
    # incidents also matched the generic "api error: 401".
    "oauth access token has expired",
    "oauth access token has been revoked",
    "re-authenticate",
)


def _is_auth_err(text: str | None) -> bool:
    """Case-insensitive auth-error classification for CLI error output."""
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _AUTH_ERR_MARKERS)


# Rate-limit / credit-exhaustion markers. The CLI gives these no distinguishable
# error class: it emits a synthetic assistant message plus
# result{is_error:true, api_error_status:429} whose text is a plain human
# sentence. Phrasing drifts by plan and model ("You're out of usage credits",
# "You've reached your <model> limit"), so match the substrings that every
# observed variant shares rather than a full sentence.
_MODEL_CAP_ERR_MARKERS = (
    "switch to another model",
    "cc_cli_limit_message",
    "out of usage credits",
)


def _is_model_cap_err(text: str | None) -> bool:
    """True when the CLI refused because the model is capped / out of credits."""
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _MODEL_CAP_ERR_MARKERS)
_TRANSIENT_API_ERR_MARKERS = (
    "API Error: 500",
    "API Error: 502",
    "API Error: 503",
    "API Error: 504",
    "API Error: 529",
    "Overloaded",
    # CLI never answered the init handshake, a slow/loaded machine, not a
    # permanent fault. Was non-retriable and posted straight to Telegram
    # (it used to post a scary "Claude failed to initialize" message).
    "failed to initialize",
)
# Backoff schedule for transient API errors on triggers (autonomous, no user).
# Total worst-case wait: 60 + 180 + 600 = 840s (~14 min) before giving up.
_TRIGGER_TRANSIENT_BACKOFF_SECONDS = (60, 180, 600)

# Tool progress indicator: show tool-call activity in the "Thinking..." message
# while Claude is executing tools (WebSearch, Read, Bash, etc.). Disable with
# BRIDGE_SHOW_PROGRESS=0 in the env/.env file if it ever misbehaves.
BRIDGE_SHOW_PROGRESS = os.environ.get("BRIDGE_SHOW_PROGRESS", "1").strip() not in ("0", "false", "no", "")

# Extended idle timeout used when an async Agent (run_in_background=true) is
# in flight. Async agents deliver their completion via a <task-notification>
# user message that can arrive many minutes after launch, during which the
# parent stdout is completely silent. Regular IDLE_TIMEOUT (15 min) would
# falsely kill legitimate async subagent work. Default: 60 min.
TASK_IDLE_TIMEOUT = int(os.environ.get("TASK_IDLE_TIMEOUT", "3600"))

# Friendly label templates for known tools. The `{x}` placeholders are filled
# from the tool's input dict. Unknown tools fall back to `_fallback_tool_label`.
_TOOL_LABELS: dict[str, callable] = {}


def _truncate(s: str, n: int = 60) -> str:
    s = str(s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _basename(path: str) -> str:
    if not path:
        return ""
    return Path(str(path)).name or str(path)


def _fallback_tool_label(name: str, _input: dict) -> str:
    return f"\u2699\ufe0f  Running {name}\u2026"


def _label_websearch(name, inp):
    q = _truncate(inp.get("query", ""), 60)
    return f"\U0001f50d Searching web: {q}" if q else "\U0001f50d Searching web\u2026"


def _label_webfetch(name, inp):
    url = _truncate(inp.get("url", ""), 60)
    return f"\U0001f310 Fetching: {url}" if url else "\U0001f310 Fetching page\u2026"


def _label_read(name, inp):
    return f"\U0001f4d6 Reading: {_basename(inp.get('file_path', ''))}"


def _label_write(name, inp):
    return f"\u270d\ufe0f  Writing: {_basename(inp.get('file_path', ''))}"


def _label_edit(name, inp):
    return f"\u270f\ufe0f  Editing: {_basename(inp.get('file_path', ''))}"


def _label_bash(name, inp):
    cmd = _truncate(inp.get("command", ""), 70)
    return f"\U0001f4bb Running: {cmd}" if cmd else "\U0001f4bb Running command\u2026"


def _label_glob(name, inp):
    p = _truncate(inp.get("pattern", ""), 60)
    return f"\U0001f50e Searching files: {p}"


def _label_grep(name, inp):
    p = _truncate(inp.get("pattern", ""), 60)
    return f"\U0001f50e Searching content: {p}"


def _label_agent(name, inp):
    desc = _truncate(inp.get("description") or inp.get("subagent_type") or "subagent", 60)
    bg = " (background)" if inp.get("run_in_background") else ""
    return f"\U0001f916 Subagent{bg}: {desc}"


def _label_todowrite(name, inp):
    todos = inp.get("todos", [])
    count = len(todos) if isinstance(todos, list) else 0
    return f"\U0001f4dd Updating task list ({count} items)"


_TOOL_LABELS.update({
    "WebSearch": _label_websearch,
    "WebFetch": _label_webfetch,
    "Read": _label_read,
    "Write": _label_write,
    "Edit": _label_edit,
    "MultiEdit": _label_edit,
    "Bash": _label_bash,
    "Glob": _label_glob,
    "Grep": _label_grep,
    "Agent": _label_agent,
    "Task": _label_agent,  # Older Claude Code versions used "Task"; alias for safety.
    "TodoWrite": _label_todowrite,
})


def _format_tool_label(name: str, inp: dict) -> str:
    fn = _TOOL_LABELS.get(name)
    if fn:
        try:
            return fn(name, inp or {})
        except Exception:
            pass
    return _fallback_tool_label(name, inp or {})


def _format_progress_suffix(running_tools: dict) -> str:
    """Render the progress line for the Telegram message.

    running_tools: dict[tool_use_id -> {"name": str, "input": dict, "async": bool}]
    Returns the most-recently-added tool's friendly label, prefixed with a
    count if >1 tool is in flight.
    """
    if not running_tools:
        return ""
    # Insertion order preserved in Python 3.7+ dicts; last entry is most recent.
    last_id = next(reversed(running_tools))
    last = running_tools[last_id]
    label = _format_tool_label(last["name"], last.get("input") or {})
    if len(running_tools) > 1:
        return f"{len(running_tools)} tools running | {label}"
    return label


# Precompiled regex for extracting tool_use_id from an async agent's
# <task-notification> envelope. Used to match background-agent completions
# back to their running_tools entry.
_TASK_NOTIFICATION_ID_RE = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")

STATE_DIR = Path(__file__).parent / "state"
SESSION_FILE = STATE_DIR / "sessions.json"
TOKEN_USAGE_FILE = STATE_DIR / "token_usage.json"
TRIGGER_DIR = STATE_DIR / "triggers"
# Per-turn holding folders for voice notes a tool parks during a turn (see _voice_last).
VOICE_DEFER_ROOT = STATE_DIR / "voice-defer"
# Notes that could not be delivered are kept here (never re-sent automatically,
# so nothing can arrive twice or out of order); the chat gets a notice instead.
VOICE_UNSENT_DIR = VOICE_DEFER_ROOT / "unsent"

def _fmt_window(cw: int) -> str:
    """Context window for the reply footer: '1M' for 1M+, '200k' below that."""
    return f"{cw / 1_000_000:g}M" if cw >= 1_000_000 else f"{cw // 1000}k"


def _get_context_window(topic_key=None) -> int:
    """Return context window size for the active model.

    Claude Code defaults every tier to 200K context. The 1M window is an
    explicit opt-in via the `[1m]` suffix on the model name (alias or full ID),
    e.g. `opus[1m]` or `claude-opus-4-7[1m]`. Without the suffix, auto-compact
    fires around 90% of 200K (~180K tokens).
    """
    model = _model_for_topic(topic_key) or "sonnet"
    if "[1m]" in model:
        return 1_000_000
    return 200_000
# Uploaded media (photos, files) land in the OS temp dir, NOT the workspace, so
# they never pile up in your project folder and macOS can sweep them too.
# Like voice notes (which are transcribed to a temp file then deleted), these are
# disposable: Telegram keeps the originals, and the local copy only needs to live
# long enough for Claude to Read it, plus a follow-up buffer. Auto-purged below.
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/private/tmp/claude-telegram-uploads")

# Downscale ONLY images too big for Claude / the stream reader to handle; every
# normal photo is left byte-for-byte untouched. The vision API already resizes
# anything past ~1568px server-side (so normal photos give Claude no extra detail
# either way), and the 32MB stream buffer holds a photo's PNG tool_result echo up
# to ~24 MP. So we only step in past a hard ceiling -- Anthropic's 8000px
# per-image dimension cap, or ~24 MP where the PNG echo nears the buffer. When we
# do resize, the target is the API's effective working size (no visible loss).
IMAGE_DOWNSCALE_TRIGGER_LONG_EDGE = int(os.environ.get("IMAGE_DOWNSCALE_TRIGGER_LONG_EDGE", "8000"))
IMAGE_DOWNSCALE_TRIGGER_PIXELS = int(os.environ.get("IMAGE_DOWNSCALE_TRIGGER_PIXELS", "24000000"))
IMAGE_TARGET_LONG_EDGE = int(os.environ.get("IMAGE_TARGET_LONG_EDGE", "1568"))
IMAGE_JPEG_QUALITY = int(os.environ.get("IMAGE_JPEG_QUALITY", "90"))

# Auto-purge uploaded media older than this. Set UPLOAD_RETENTION_HOURS=0 to disable.
UPLOAD_RETENTION_HOURS = int(os.environ.get("UPLOAD_RETENTION_HOURS", "48"))
UPLOAD_CLEANUP_INTERVAL = int(os.environ.get("UPLOAD_CLEANUP_INTERVAL", "3600"))

# Telegram limits and rate control
MAX_MSG_LEN = 3000  # Leave room for [1/N] prefix + entity overhead (limit is 4096)
TYPING_INTERVAL = 4.0  # Telegram typing indicator expires after ~5 seconds
RATE_LIMIT_INTERVAL = 1.1  # Minimum seconds between sends to same chat

# Logging: suppress httpx to prevent bot token leaking in log URLs
# RotatingFileHandler: keeps 5MB current + 4 backups (25MB total max)
_log_format = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_log_dir = Path(__file__).parent / "logs"
_log_dir.mkdir(exist_ok=True)

_file_handler = RotatingFileHandler(
    # 5MB x 5: a crash loop once filled a smaller allotment with tracebacks
    # in minutes and erased all prior history.
    _log_dir / "stderr.log", maxBytes=5_000_000, backupCount=4
)
_file_handler.setFormatter(_log_format)
_file_handler.setLevel(logging.INFO)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler])
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("claude-bridge")

# User timezone for time injection into Claude's system prompt.
# Claude Code CLI injects today's date but not time-of-day; the bridge fills
# this gap so time-sensitive tasks ("how long since X", scheduling questions)
# work correctly. Default: follow the Mac's system timezone so it auto-tracks
# your location while traveling. Override with USER_TIMEZONE env var if a
# specific IANA zone is needed (e.g. for testing). The bridge restarts daily
# at 4 AM (see _daily_restart_scheduler), so this re-reads the Mac's TZ once
# per day, fast enough to follow timezone changes after a flight.
try:
    from zoneinfo import ZoneInfo
    _tz_override = os.environ.get("USER_TIMEZONE")
    if _tz_override:
        USER_TZ = ZoneInfo(_tz_override)
        logger.info("User timezone: %s (from USER_TIMEZONE env)", _tz_override)
    else:
        USER_TZ = datetime.now(timezone.utc).astimezone().tzinfo
        logger.info("User timezone: %s (from Mac local)", USER_TZ)
except Exception as _tz_err:
    USER_TZ = timezone.utc
    logger.warning("Could not load timezone (%s), falling back to UTC", _tz_err)

# Per-topic locks to prevent concurrent Claude processes on the same session
_topic_locks: dict[str, asyncio.Lock] = {}
# Topic -> this turn's voice-defer folder (passed to Claude as JARVIS_VOICE_DEFER_DIR)
_voice_defer_dirs: dict[str, Path] = {}
# Topics whose current turn was stopped by the user: that turn's parked voice is dropped.
_voice_discard: set[str] = set()
# Track running Claude processes so they can be stopped
_running_processes: dict[str, asyncio.subprocess.Process] = {}
# Track topics where user hit Stop (so we skip sending the response)
_stopped_topics: set[str] = set()
# Media group buffer: accumulate photos from the same Telegram album before processing
_media_group_buffer: dict[str, dict] = {}
_MEDIA_GROUP_DELAY = 1.5  # seconds to wait for remaining items in the same album
# Per-topic token usage from the latest message_start event.
# input_tokens = total context in that turn (entire conversation history included).
# Persisted to disk so /context survives bridge restarts.
def _load_token_usage() -> dict:
    if TOKEN_USAGE_FILE.exists():
        try:
            return json.loads(TOKEN_USAGE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def _save_token_usage(key: str):
    """Persist ONE topic's meter row, merged into what is on disk.

    External scripts (for example a nightly handover job) reset a topic by
    popping its row straight from the file. Dumping the whole in-memory dict
    would write those popped rows back, so a watcher reading the file would
    see the old count again and fire a second, phantom reset. Re-read the
    file, change only `key`, and adopt the file's view of every other topic.
    """
    _ensure_state_dir()
    try:
        disk = json.loads(TOKEN_USAGE_FILE.read_text()) if TOKEN_USAGE_FILE.exists() else {}
    except (json.JSONDecodeError, OSError):
        disk = dict(_topic_token_usage)
    if key in _topic_token_usage:
        disk[key] = _topic_token_usage[key]
    else:
        disk.pop(key, None)
    tmp = TOKEN_USAGE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(disk, indent=2))
    tmp.replace(TOKEN_USAGE_FILE)
    _topic_token_usage.clear()
    _topic_token_usage.update(disk)

_topic_token_usage: dict[str, dict] = _load_token_usage()


# --- Model cap state -------------------------------------------------------
# Which models are currently rate-capped and when they return. Persisted so the
# daily bridge restart neither re-burns an attempt on a model that is still
# capped nor re-announces a switch the user was already told about. resets_at
# comes from the CLI's rate_limit_event when it carries one.
MODEL_CAP_FILE = STATE_DIR / "model_caps.json"
# A cap seen without a reset timestamp is assumed to last this long, so an
# unknown 429 can never strand a topic on a fallback model permanently.
_CAP_UNKNOWN_TTL_S = int(os.environ.get("MODEL_CAP_UNKNOWN_TTL_S") or 3600)


def _load_model_caps() -> dict:
    if MODEL_CAP_FILE.exists():
        try:
            return json.loads(MODEL_CAP_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_model_caps():
    _ensure_state_dir()
    tmp = MODEL_CAP_FILE.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(_model_caps, indent=2))
    tmp.replace(MODEL_CAP_FILE)


_model_caps: dict[str, dict] = _load_model_caps()
# topic_key -> epoch reset time from the most recent rate_limit_event.
_last_rate_limit_reset: dict[str, float] = {}


def _cap_key(model: str) -> str:
    return model or "(default)"


def _cap_reset_str(model: str) -> str:
    """'Sun 21:00' for the model's cap reset, or '' when unknown."""
    resets_at = (_model_caps.get(_cap_key(model)) or {}).get("resets_at")
    if not resets_at:
        return ""
    try:
        return datetime.fromtimestamp(float(resets_at), USER_TZ).strftime("%a %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _model_is_capped(model: str) -> bool:
    """True if the model is inside a known cap window. Expired records self-clear."""
    rec = _model_caps.get(_cap_key(model))
    if not rec:
        return False
    now = time.time()
    resets_at = rec.get("resets_at") or 0
    expired = (now >= float(resets_at)) if resets_at else (
        now - rec.get("seen_at", 0) > _CAP_UNKNOWN_TTL_S
    )
    if expired:
        _model_caps.pop(_cap_key(model), None)
        try:
            _save_model_caps()
        except OSError:
            pass
        logger.info("Model cap expired, %s back in rotation", _cap_key(model))
        return False
    return True


def _record_model_cap(model: str, resets_at: float | None = None):
    """Mark a model capped. Preserves the record's notify state so one cap
    window yields exactly one 'switched model' line per topic."""
    k = _cap_key(model)
    rec = _model_caps.get(k) or {}
    rec["seen_at"] = time.time()
    if resets_at:
        rec["resets_at"] = float(resets_at)
    rec.setdefault("notified_topics", [])
    _model_caps[k] = rec
    try:
        _save_model_caps()
    except OSError as e:
        logger.warning("Could not persist model cap for %s: %s", k, e)
    logger.warning("Model capped: %s (resets %s)", k, _cap_reset_str(model) or "unknown")


def _note_rate_limit_event(topic_key, d: dict):
    """Stash the reset time the CLI reports in a rejected rate_limit_event."""
    info = d.get("rate_limit_info") or {}
    resets_at = info.get("resetsAt")
    if not resets_at:
        window = (info.get("unifiedWindows") or {}).get(info.get("rateLimitType") or "", {})
        resets_at = (window or {}).get("resetsAt")
    if resets_at:
        try:
            _last_rate_limit_reset[str(topic_key)] = float(resets_at)
        except (TypeError, ValueError):
            pass


def _fallback_notice(topic_key) -> str:
    """Heads-up line when this topic's turn ran on a fallback rung, including
    turns that skip the capped model PRE-FLIGHT and so never see an error. The
    retry paths alone are not enough: once a cap is on record, every later turn
    is silently demoted, so the very first turn of a window (the one the user
    most needs told) would get no line at all."""
    chain = _model_chain(topic_key)
    if _model_for_topic(topic_key) == chain[0]:
        return ""
    return _cap_switch_notice(topic_key, chain[0])


def _cap_switch_notice(topic_key, capped_model: str) -> str:
    """One-line heads-up that this topic switched models. Returns '' after the
    first time in a cap window, so it informs once instead of nagging."""
    rec = _model_caps.get(_cap_key(capped_model))
    if not rec:
        return ""
    seen = rec.setdefault("notified_topics", [])
    if str(topic_key) in seen:
        return ""
    seen.append(str(topic_key))
    try:
        _save_model_caps()
    except OSError:
        pass
    when = _cap_reset_str(capped_model)
    return (
        f"⚠️ {_model_label(capped_model)} is out of usage credits"
        + (f" until {when}" if when else "")
        + f", running on {_model_label(_model_for_topic(topic_key))} until then."
    )


def _get_topic_lock(topic_id) -> asyncio.Lock:
    """Get or create an asyncio.Lock for a topic. Ensures sequential processing."""
    key = str(topic_id if topic_id else "general")
    if key not in _topic_locks:
        _topic_locks[key] = asyncio.Lock()
    return _topic_locks[key]


def _topic_key(topic_id) -> str:
    return str(topic_id if topic_id else "general")


# ---------------------------------------------------------------------------
# Auth Check
# ---------------------------------------------------------------------------

def _is_authorized(update: Update) -> bool:
    """Check if the message sender is allowed AND the group is correct."""
    if not update.effective_user or update.effective_user.id not in ALLOWED_USERS:
        return False
    if ALLOWED_GROUP_ID and update.effective_chat and update.effective_chat.id != ALLOWED_GROUP_ID:
        logger.warning(
            "Rejected message from allowed user in wrong group: %s",
            update.effective_chat.id,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Session Management
# ---------------------------------------------------------------------------

def _ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TRIGGER_DIR.mkdir(exist_ok=True)


def load_sessions() -> dict:
    """Load topic -> session mapping from disk."""
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupt sessions.json, starting fresh")
            return {}
    return {}


def save_sessions(sessions: dict):
    """Persist topic -> session mapping to disk."""
    _ensure_state_dir()
    # PID-unique temp name: if an external script ever rewrites this file
    # too, a shared "sessions.tmp" would let two writers clobber each other's
    # temp or race os.replace.
    tmp = SESSION_FILE.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(sessions, indent=2))
    tmp.replace(SESSION_FILE)  # Atomic write


def get_or_create_session(topic_id) -> tuple[str, bool]:
    """Get existing session for a topic, or create a new one.

    Returns (session_uuid, is_new).
    """
    sessions = load_sessions()
    key = str(topic_id if topic_id else "general")

    if key in sessions:
        sessions[key]["last_used"] = datetime.now(timezone.utc).isoformat()
        save_sessions(sessions)
        return sessions[key]["session_id"], False

    sid = str(uuid.uuid4())
    sessions[key] = {
        "session_id": sid,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_used": datetime.now(timezone.utc).isoformat(),
    }
    save_sessions(sessions)
    return sid, True


def reset_session(topic_id) -> None:
    """Delete a topic's session so the next message starts fresh."""
    sessions = load_sessions()
    key = str(topic_id if topic_id else "general")
    sessions.pop(key, None)
    save_sessions(sessions)
    # Clear persisted token usage for this topic
    _topic_token_usage.pop(key, None)
    _save_token_usage(key)


# ---------------------------------------------------------------------------
# Message Formatting & Splitting
# ---------------------------------------------------------------------------

def split_message(text: str, max_length: int = MAX_MSG_LEN) -> list[str]:
    """Split text on newline boundaries, respecting max_length.

    Avoids splitting mid-line. Force-splits individual lines that exceed max_length.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.split("\n"):
        if len(line) > max_length:
            # Flush current buffer first
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            # Force-split oversized lines
            for i in range(0, len(line), max_length):
                chunks.append(line[i : i + max_length])
        elif len(current) + len(line) + 1 > max_length:
            # Current chunk is full, start a new one
            chunks.append(current.rstrip("\n"))
            current = line + "\n"
        else:
            current += line + "\n"

    if current.strip():
        chunks.append(current.rstrip("\n"))

    return chunks


def _tables_to_cards(text: str) -> str:
    """Convert markdown tables to mobile-friendly card format.

    Telegram doesn't render tables well, especially on mobile. This converts:
        | What          | Why separate              |
        |---------------|---------------------------|
        | Vitamin C     | Dose too low in multi     |
        | Creatine 10g  | Not in any multi          |
    Into:
        **Vitamin C**
        Why separate: Dose too low in multi

        **Creatine 10g**
        Why separate: Not in any multi
    """
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Detect a table: line starts and ends with |
        if line.startswith("|") and line.endswith("|"):
            # Collect all contiguous table lines
            table_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                table_lines.append(lines[i].strip())
                i += 1

            # Parse headers from first row
            headers = [h.strip() for h in table_lines[0].strip("|").split("|")]

            # Skip separator row(s) and collect data rows
            data_rows: list[list[str]] = []
            for tl in table_lines[1:]:
                # Skip separator rows like |---|---|
                if re.match(r"^\|[\s\-:|]+\|$", tl):
                    continue
                cells = [c.strip() for c in tl.strip("|").split("|")]
                data_rows.append(cells)

            if not data_rows:
                # Header-only table, just output headers as a list
                for h in headers:
                    if h:
                        result.append(f"- {h}")
                result.append("")
            elif len(headers) == 2:
                # Two-column table: first column becomes bold title, second becomes detail
                for row in data_rows:
                    col1 = row[0] if len(row) > 0 else ""
                    col2 = row[1] if len(row) > 1 else ""
                    if col1:
                        result.append(f"**{col1}**")
                        if col2:
                            result.append(col2)
                        result.append("")
            else:
                # Multi-column table: first column as bold title, rest as labeled fields
                for row in data_rows:
                    if not row or not row[0]:
                        continue
                    result.append(f"**{row[0]}**")
                    for j, cell in enumerate(row[1:], 1):
                        if cell and j < len(headers):
                            result.append(f"{headers[j]}: {cell}")
                    result.append("")
        else:
            result.append(lines[i])
            i += 1

    return "\n".join(result)


def _strip_markdown(text: str) -> str:
    """Remove markdown formatting for plain text fallback."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'~~(.+?)~~', r'\1', text)
    text = re.sub(r'```[\s\S]*?```', lambda m: m.group().strip('`').strip(), text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    return text


async def safe_send(message, text: str, **kwargs):
    """Send a message with entity-based formatting, falling back to plain text.

    Phase 1: Try telegramify-markdown entities (zero parse errors)
    Phase 2: Fall back to plain text with markdown stripped
    Handles RetryAfter (flood control) by sleeping and retrying once.
    """
    # Pre-process: convert markdown tables to mobile-friendly cards
    text = _tables_to_cards(text)

    # Card conversion can EXPAND a pre-split chunk past Telegram's 4096 hard
    # limit (send_long_reply splits at 3000 BEFORE this transform), which
    # makes the entity send fail, then the plain fallback fail, and the
    # reply is silently lost. Re-split and recurse
    # (cards contain no markdown tables, so the second pass is a no-op).
    if len(text) > 4096:
        result = None
        for part in split_message(text):
            result = await safe_send(message, part, **kwargs)
            await asyncio.sleep(RATE_LIMIT_INTERVAL)
        return result

    # Phase 1: Entity-based formatting (most robust)
    if HAS_TELEGRAMIFY:
        try:
            plain_text, tm_entities = tm_convert(text)
            # Convert telegramify_markdown entities to telegram.MessageEntity
            from telegram import MessageEntity
            tg_entities = [
                MessageEntity(
                    type=e.type,
                    offset=e.offset,
                    length=e.length,
                    url=e.url,
                    language=e.language,
                )
                for e in tm_entities
            ]
            return await message.reply_text(
                plain_text,
                entities=tg_entities,
                **kwargs,
            )
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                return await message.reply_text(
                    plain_text,
                    entities=tg_entities,
                    **kwargs,
                )
            except TelegramError:
                pass
        except TelegramError as exc:
            logger.warning("Entity send failed: %s", exc)
        except Exception as exc:
            logger.warning("telegramify-markdown failed: %s", exc)

    # Phase 2: Plain text fallback (strip markdown so ** doesn't show)
    clean_text = _strip_markdown(text)
    try:
        return await message.reply_text(clean_text, **kwargs)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            return await message.reply_text(clean_text, **kwargs)
        except TelegramError as exc:
            logger.error("Failed to send after retry: %s", exc)
            return None
    except BadRequest as exc:
        if "not found" in str(exc).lower():
            return None  # Message was deleted
        logger.error("BadRequest sending message: %s", exc)
        return None
    except TelegramError as exc:
        logger.error("TelegramError sending message: %s", exc)
        return None


async def send_long_reply(message, text: str):
    """Send a potentially long response, split across multiple messages."""
    chunks = split_message(text)

    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            chunk = f"[{i + 1}/{len(chunks)}]\n{chunk}"
        await safe_send(message, chunk)
        if i < len(chunks) - 1:
            await asyncio.sleep(RATE_LIMIT_INTERVAL)


# ---------------------------------------------------------------------------
# Typing Indicator
# ---------------------------------------------------------------------------

async def typing_loop(
    chat_id: int,
    bot: Bot,
    topic_id: int | None,
    stop: asyncio.Event,
):
    """Send typing indicator every TYPING_INTERVAL seconds until stopped."""
    while not stop.is_set():
        try:
            await bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.TYPING,
                message_thread_id=topic_id,
            )
        except TelegramError:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=TYPING_INTERVAL)
            break
        except asyncio.TimeoutError:
            continue


# ---------------------------------------------------------------------------
# Claude Code Execution
# ---------------------------------------------------------------------------

# --- Automation credential resolution -------------------------------------
#
# Sources, in order:
#   1. setup-token file: long-lived (1y) subscription token minted once via
#      `claude setup-token`. Immune to the 8h Keychain token cycle and to
#      interactive-login rotation races. Circuit-broken: an auth failure
#      while it was injected quarantines it (state survives restarts).
#   2. Keychain access token: ONLY if expiresAt says it has >10 min left.
#      Injecting a dead token is worse than none: an env token makes the CLI
#      skip its own refresh flow entirely.
#   3. None: CLI self-auth (it can refresh; an env token can't).
#
# Invariant: the child env NEVER inherits CLAUDE_CODE_OAUTH_TOKEN from the
# bridge's own environment; injection is explicit per attempt.

AUTOMATION_TOKEN_FILE = os.path.expanduser(os.environ.get(
    "CLAUDE_AUTOMATION_TOKEN_FILE", "~/.config/claude/automation-oauth-token.txt"))
AUTH_HEALTH_FILE = STATE_DIR / "auth-health.json"
AUTH_QUARANTINE_S = int(os.environ.get("AUTH_QUARANTINE_S", "1800"))
KEYCHAIN_MIN_REMAINING_S = int(os.environ.get("KEYCHAIN_MIN_REMAINING_S", "600"))
AUTH_ALERT_COOLDOWN_S = int(os.environ.get("AUTH_ALERT_COOLDOWN_S", str(6 * 3600)))
REMINT_ALERT_COOLDOWN_S = int(os.environ.get("REMINT_ALERT_COOLDOWN_S", str(12 * 3600)))

# Last credential source actually injected, per topic key. Lets retry logic
# attribute an auth failure to the source that produced it (circuit breaker).
_last_auth_source: dict[str, str] = {}

# Trigger files currently deferred by not_before, for log-once bookkeeping.
_deferred_seen: set[str] = set()

# Topics with a user-path auth retry in flight. Per-topic (NOT a global flag):
# one topic's retry must never suppress another topic's legitimate retry.
_auth_retrying_topics: set[str] = set()


def _auth_health_load() -> dict:
    """Load persisted auth-health state (quarantine, failure log, alert times).

    Disk-persisted: in-memory rolling windows would reset at the daily
    restart, defeating 24h failure counting and alert dedup.
    """
    try:
        d = json.loads(AUTH_HEALTH_FILE.read_text())
        if not isinstance(d, dict):
            return {}
        # Prune expired entries on read
        now = time.time()
        fails = [t for t in d.get("setup_token_failures", []) if isinstance(t, (int, float)) and now - t < 86400]
        d["setup_token_failures"] = fails
        return d
    except (OSError, json.JSONDecodeError):
        return {}


def _auth_health_save(d: dict) -> None:
    try:
        AUTH_HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name: two writers must never collide on one .tmp path.
        tmp = AUTH_HEALTH_FILE.with_suffix(f".{time.time_ns()}.tmp")
        tmp.write_text(json.dumps(d, indent=2))
        tmp.replace(AUTH_HEALTH_FILE)
    except OSError as e:
        logger.warning("auth-health save failed: %s", e)


def _auth_health_update(mutate) -> dict:
    """Locked read-modify-write of auth-health state. Same-process async tasks
    can't interleave these sync calls, but a cross-process writer (test
    script, future second consumer) could, flock serializes them."""
    lock_path = str(AUTH_HEALTH_FILE) + ".lock"
    try:
        AUTH_HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            d = _auth_health_load()
            mutate(d)
            _auth_health_save(d)
            return d
    except OSError as e:
        logger.warning("auth-health locked update failed (%s), applying unlocked", e)
        d = _auth_health_load()
        mutate(d)
        _auth_health_save(d)
        return d


def _auth_alert_due(name: str, cooldown_s: int) -> bool:
    """True if the named operational alert is outside its cooldown window.
    Records the send time when returning True (caller must actually send)."""
    due = False

    def _mut(d):
        nonlocal due
        alerts = d.setdefault("alerts", {})
        if time.time() - alerts.get(name, 0) >= cooldown_s:
            alerts[name] = time.time()
            due = True

    _auth_health_update(_mut)
    return due


def _quarantine_setup_token(reason: str) -> None:
    """Auth failure while the setup-token was injected: bench it for a while
    so a readable-but-revoked file can never wedge the bridge. At 3 failures
    in 24h, log ONE loud re-mint error (per 12h) so the operator notices."""
    n_fails = 0

    def _mut(d):
        nonlocal n_fails
        d["setup_token_quarantine_until"] = time.time() + AUTH_QUARANTINE_S
        d.setdefault("setup_token_failures", []).append(time.time())
        n_fails = len(d["setup_token_failures"])

    _auth_health_update(_mut)
    logger.warning(
        "Setup-token quarantined for %ds (%d auth failure(s) in 24h) [reason=%s]",
        AUTH_QUARANTINE_S, n_fails, reason[:200],
    )
    if n_fails >= 3 and _auth_alert_due("setup-token-remint", REMINT_ALERT_COOLDOWN_S):
        logger.error(
            "Setup-token failed %d times in 24h, re-mint needed: run "
            "`claude setup-token` and save the token to %s",
            n_fails, AUTOMATION_TOKEN_FILE,
        )


def _read_setup_token() -> str | None:
    """Return the automation setup-token if present, plausible, and not
    quarantined. Any problem → None (fall through to the next source).
    Single open + fstat (no stat/read TOCTOU); loose permissions are
    self-healed to 600 (a TextEdit re-save can reset them to 644)."""
    d = _auth_health_load()
    if time.time() < d.get("setup_token_quarantine_until", 0):
        return None
    try:
        with open(AUTOMATION_TOKEN_FILE, "r", encoding="utf-8") as f:
            st = os.fstat(f.fileno())
            token = f.read(4096).strip()
    except FileNotFoundError:
        return None  # not set up, normal until you mint one
    except (OSError, UnicodeError, ValueError) as e:
        logger.warning("Setup-token file unreadable: %s", e)
        return None
    if not token or not token.startswith("sk-ant-") or len(token) < 40:
        logger.warning("Setup-token file present but implausible (len=%d), ignoring", len(token))
        return None
    if st.st_mode & 0o077:
        try:
            os.chmod(AUTOMATION_TOKEN_FILE, 0o600)
            logger.warning("Setup-token file permissions were %o, self-healed to 600", st.st_mode & 0o777)
        except OSError:
            logger.warning("Setup-token file permissions are %o and chmod failed, should be 600", st.st_mode & 0o777)
    age_days = (time.time() - st.st_mtime) / 86400
    if age_days > 335 and _auth_alert_due("setup-token-age", REMINT_ALERT_COOLDOWN_S):
        logger.warning("Setup-token file is %d days old, re-mint with `claude setup-token` before annual expiry", int(age_days))
    return token


async def _get_keychain_oauth_token() -> str | None:
    """Read the interactive-session OAuth token from the macOS Keychain,
    but ONLY if its own expiresAt metadata says it is still fresh.

    Returns None if missing, unreadable, or expiring within
    KEYCHAIN_MIN_REMAINING_S (the CLI self-refreshes; an injected dead
    token blocks that refresh and guarantees a 401).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "security", "find-generic-password",
            "-s", "Claude Code-credentials", "-a", getpass.getuser(), "-w",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            return None
        data = json.loads(stdout.decode("utf-8").strip())
        oauth = data.get("claudeAiOauth", {})
        token = oauth.get("accessToken")
        if not token:
            return None
        expires_at = oauth.get("expiresAt")
        if not isinstance(expires_at, (int, float)):
            # No verifiable freshness → don't inject (an unverifiable token
            # blocks the CLI's self-refresh if it happens to be dead).
            logger.info("Keychain token has no numeric expiresAt, letting CLI self-auth")
            return None
        remaining = expires_at / 1000 - time.time()
        if remaining < KEYCHAIN_MIN_REMAINING_S:
            logger.info(
                "Keychain token expired/near-expiry (%.0fs remaining), letting CLI self-refresh",
                remaining,
            )
            return None
        return token
    except Exception as e:
        logger.warning("Keychain OAuth read failed: %s", e)
        return None


async def _resolve_automation_token() -> tuple[str | None, str]:
    """Pick the credential to inject for one attempt.
    Returns (token_or_None, source_label)."""
    token = _read_setup_token()
    if token:
        return token, "setup-token"
    token = await _get_keychain_oauth_token()
    if token:
        return token, "keychain"
    return None, "cli-self-auth"


async def _auth_preflight() -> None:
    """Log the credential-resolution state at startup (incl. the 04:00 daily
    restart) so auth drift diagnoses itself from one grep of the log. Also
    prunes dead-lettered triggers older than 14 days (piggybacks on the daily
    restart cadence)."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        logger.warning(
            "Preflight: bridge environment carries CLAUDE_CODE_OAUTH_TOKEN, "
            "it is stripped from every child; injection is explicit per attempt"
        )
    else:
        logger.info("Preflight: inherited-env invariant OK (no ambient CLAUDE_CODE_OAUTH_TOKEN)")
    # Keychain freshness (logged even when the setup-token will win resolution)
    try:
        proc = await asyncio.create_subprocess_exec(
            "security", "find-generic-password",
            "-s", "Claude Code-credentials", "-a", getpass.getuser(), "-w",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        exp = json.loads(stdout.decode().strip()).get("claudeAiOauth", {}).get("expiresAt")
        if isinstance(exp, (int, float)):
            logger.info("Preflight: Keychain token expiresAt in %.0f min", (exp / 1000 - time.time()) / 60)
        else:
            logger.info("Preflight: Keychain token has no numeric expiresAt")
    except Exception as e:
        logger.info("Preflight: Keychain unreadable (%s)", e)
    # Dead-letter retention: 14 days
    try:
        dead_dir = TRIGGER_DIR / "dead"
        if dead_dir.is_dir():
            cutoff = time.time() - 14 * 86400
            pruned = 0
            for f in dead_dir.iterdir():
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    pruned += 1
            if pruned:
                logger.info("Preflight: pruned %d dead-lettered trigger(s) >14d old", pruned)
    except OSError as e:
        logger.warning("Preflight: dead-letter prune failed: %s", e)
    try:
        st = os.stat(AUTOMATION_TOKEN_FILE)
        d = _auth_health_load()
        qt = d.get("setup_token_quarantine_until", 0)
        q_note = f", QUARANTINED {qt - time.time():.0f}s more" if time.time() < qt else ""
        logger.info(
            "Preflight: setup-token file present (age %.0fd, perms %o, %d auth failure(s) in 24h%s)",
            (time.time() - st.st_mtime) / 86400, st.st_mode & 0o777,
            len(d.get("setup_token_failures", [])), q_note,
        )
    except OSError:
        logger.info(
            "Preflight: no setup-token file at %s, Keychain/CLI-self-auth in use "
            "(mint one with `claude setup-token` for long-lived automation auth)",
            AUTOMATION_TOKEN_FILE,
        )
    _, source = await _resolve_automation_token()
    logger.info("Preflight: auth source resolves to: %s", source)


async def _start_claude_streaming(prompt, session_id, is_new, topic_key, skip_keychain_token=False):
    """Start Claude CLI in streaming mode and send the prompt.

    Returns (proc, error, resume_failed):
        proc: the subprocess (or None on failure)
        error: error string (or None on success)
        resume_failed: True if the original session couldn't be resumed
            and we silently started a new session in its place. Caller
            should inform the user that prior context is lost.

    Handles the initialize handshake and sends the user message.
    """
    # Inject current time into the system prompt so Claude has time-of-day
    # awareness (it only gets today's date from the CLI's built-in context).
    # Refreshed on every subprocess spawn = every user message.
    now_str = datetime.now(USER_TZ).strftime("%A, %Y-%m-%d %H:%M %Z (%z)")

    cmd = [
        "claude",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--input-format", "stream-json",
        "--include-partial-messages",
        "--append-system-prompt",
        (
            "You are running autonomously via the Telegram bridge with full permissions "
            "(--dangerously-skip-permissions). NEVER ask the user for permission or approval "
            "before editing files, including files in ~/.claude/ or any protected directory. "
            "All file operations are pre-approved. Just do the work silently.\n\n"
            "FORMATTING: NEVER use markdown tables (pipes/dashes). Telegram breaks them on "
            "mobile. Instead use card format: **Bold Title** on its own line, details on the "
            "next line, blank line between items. For comparisons, use bold item name followed "
            "by its description. Keep responses concise and mobile-readable.\n\n"
            f"Current time (when this turn started): {now_str}."
        ),
    ]
    if not is_new:
        cmd.extend(["--resume", session_id])
    _model = _model_for_topic(topic_key)
    if _model:
        cmd.extend(["--model", _model])
    _effort = _effort_for_topic(topic_key)
    if _effort:
        cmd.extend(["--effort", _effort])

    logger.info(
        "Claude streaming [session=%s, new=%s, prompt_len=%d, effort=%s]",
        session_id[:8], is_new, len(prompt), _effort or "cli-default",
    )

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    # Voice-last: a voice tool parks its notes here; _voice_last posts them after the text.
    env.pop("JARVIS_VOICE_DEFER_DIR", None)
    if str(topic_key) in _voice_defer_dirs:
        env["JARVIS_VOICE_DEFER_DIR"] = str(_voice_defer_dirs[str(topic_key)])
    # INVARIANT: never let the child inherit an ambient token, injection is
    # explicit per attempt, so a "retry without token" is actually token-free.
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    # Inject an automation credential (setup-token preferred, fresh Keychain
    # token as fallback). If skip_keychain_token is True (auth retry), inject
    # nothing and let the CLI own auth, it can refresh; an env token can't.
    if not skip_keychain_token:
        oauth_token, source = await _resolve_automation_token()
        _last_auth_source[topic_key] = source
        if oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
        logger.info("Auth source for this attempt: %s [topic=%s]", source, topic_key)
    else:
        _last_auth_source[topic_key] = "cli-self-auth"

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKING_DIR,
            env=env,
            limit=32 * 1024 * 1024,  # 32MB line buffer. Each stream-json event is
            # one line; when Claude Reads an image (e.g. a photo you sent)
            # the CLI echoes the image's base64 tool_result on a single line, which
            # blew past the old 1MB limit -> readline() raised "Separator is not
            # found, and chunk exceed the limit". 32MB covers even large photos.
        )
    except FileNotFoundError:
        return None, "Claude CLI not found. Is it installed and in PATH?", False
    except OSError as e:
        return None, f"Failed to start Claude: {e}", False

    _running_processes[topic_key] = proc

    # Send initialize handshake
    try:
        init_req = json.dumps({
            "type": "control_request",
            "request_id": "init_1",
            "request": {"subtype": "initialize", "hooks": None},
        })
        proc.stdin.write((init_req + "\n").encode())
        await proc.stdin.drain()

        # Wait for control_response (with timeout)
        initialized = False
        init_error = None

        async def _wait_for_init():
            nonlocal initialized, init_error
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "control_response":
                    initialized = True
                    return
                if d.get("type") == "result" and d.get("is_error"):
                    init_error = d.get("result", "Unknown error")
                    return

        try:
            # 90s: under wake-from-sleep load the CLI cold start can
            # exceed 30s.
            await asyncio.wait_for(_wait_for_init(), timeout=90)
        except asyncio.TimeoutError:
            pass

        if not initialized:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            _running_processes.pop(topic_key, None)
            await proc.wait()

            # If we were resuming, retry as new session (most common cause: session not found)
            if not is_new:
                logger.warning(
                    "Session %s failed to resume, retrying as new [topic=%s, error=%s]",
                    session_id[:8], topic_key, (init_error or "init timeout")[:300],
                )
                # Force resume_failed=True on the wrapped result so caller
                # knows to warn the user that prior context is lost.
                # skip_keychain_token MUST propagate: dropping it here silently
                # re-injects a dead token on every "token-free" auth retry
                # whose resume failed (a root cause of past user-visible
                # 401 posts).
                retry_proc, retry_err, _ = await _start_claude_streaming(
                    prompt, session_id, True, topic_key,
                    skip_keychain_token=skip_keychain_token,
                )
                return retry_proc, retry_err, True

            # New session also failed
            if init_error:
                return None, f"Claude error: {init_error[:500]}", False
            return None, "Claude failed to initialize (timeout).", False

        # Prepend a bridge-metadata line to the user message content.
        # The --append-system-prompt "Current time" injection is unreliable on
        # --resume (either not re-applied, or drowned out by conversation
        # history). Embedding the time directly in the user turn puts it in
        # the highest-attention position and works on every resume.
        prompt_with_time = (
            f"[bridge metadata: current time is {now_str}]\n\n"
            f"{prompt}"
        )

        # Send user message
        user_msg = json.dumps({
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": prompt_with_time},
            "parent_tool_use_id": None,
        })
        proc.stdin.write((user_msg + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()

    except Exception as e:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        _running_processes.pop(topic_key, None)
        return None, f"Failed to send prompt: {e}", False

    return proc, None, False


# ---------------------------------------------------------------------------
# Voice Transcription
# ---------------------------------------------------------------------------

WHISPER_CONFIGS = {
    "openai": {
        "url": "https://api.openai.com/v1/audio/transcriptions",
        "model": "whisper-1",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/audio/transcriptions",
        "model": "whisper-large-v3",
    },
}

WHISPER_LOCAL_MODEL = os.environ.get("WHISPER_MODEL", "mlx-community/whisper-small-mlx")
WHISPER_IDLE_UNLOAD_MIN = int(os.environ.get("WHISPER_IDLE_UNLOAD_MIN", "15"))

# One-shot task that unloads the mlx-whisper model after idle. Reset on each transcription.
_whisper_unload_task: "asyncio.Task | None" = None


async def transcribe_voice(audio_bytes: bytes) -> str:
    """Transcribe audio using local Whisper or a remote API."""
    if WHISPER_PROVIDER == "local":
        return await _transcribe_local(audio_bytes)
    elif WHISPER_PROVIDER in WHISPER_CONFIGS:
        return await _transcribe_api(audio_bytes)
    else:
        raise ValueError(f"Unknown whisper provider: {WHISPER_PROVIDER}")


async def _transcribe_local(audio_bytes: bytes) -> str:
    """Transcribe using mlx-whisper locally on Apple Silicon."""
    import mlx_whisper

    # Save audio to temp file (mlx-whisper needs a file path)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    try:
        # Run transcription in a thread to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: mlx_whisper.transcribe(
                tmp_path,
                path_or_hf_repo=WHISPER_LOCAL_MODEL,
            ),
        )
        _schedule_whisper_unload()
        return result["text"].strip()
    finally:
        os.unlink(tmp_path)


def _schedule_whisper_unload() -> None:
    """Reset the idle-unload timer. Call after every successful local transcription."""
    global _whisper_unload_task
    if WHISPER_IDLE_UNLOAD_MIN <= 0:
        return
    if _whisper_unload_task is not None and not _whisper_unload_task.done():
        _whisper_unload_task.cancel()
    _whisper_unload_task = asyncio.create_task(_whisper_unload_after_idle())


async def _whisper_unload_after_idle() -> None:
    try:
        await asyncio.sleep(WHISPER_IDLE_UNLOAD_MIN * 60)
    except asyncio.CancelledError:
        return
    try:
        from mlx_whisper.transcribe import ModelHolder
        import mlx.core as mx
        import gc

        was_loaded = ModelHolder.model is not None
        ModelHolder.model = None
        ModelHolder.model_path = None
        try:
            mx.metal.clear_cache()
        except AttributeError:
            # Newer MLX exposes clear_cache at top level
            try:
                mx.clear_cache()
            except AttributeError:
                pass
        gc.collect()
        if was_loaded:
            logger.info(
                "Unloaded mlx-whisper model after %d min idle", WHISPER_IDLE_UNLOAD_MIN
            )
    except Exception as e:
        logger.warning("Failed to unload mlx-whisper model: %s", e)


async def _transcribe_api(audio_bytes: bytes) -> str:
    """Transcribe using a remote Whisper-compatible API (OpenAI/Groq)."""
    config = WHISPER_CONFIGS[WHISPER_PROVIDER]

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            config["url"],
            headers={"Authorization": f"Bearer {WHISPER_API_KEY}"},
            files={"file": ("voice.ogg", audio_bytes, "audio/ogg")},
            data={"model": config["model"]},
        )
        resp.raise_for_status()
        return resp.json()["text"]


# ---------------------------------------------------------------------------
# File Handling
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """Remove path components and unsafe characters from a filename."""
    name = Path(name).name
    name = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    if not name or name.startswith("."):
        name = f"file_{name}"
    return name[:200]


def get_upload_dir() -> Path:
    """Get (and create if needed) the upload directory in the OS temp dir."""
    upload_dir = Path(UPLOAD_DIR)
    if upload_dir.is_symlink():
        raise RuntimeError("Upload directory is a symlink, refusing to write")
    upload_dir.mkdir(parents=True, exist_ok=True)
    # Owner-only: uploads can be private (documents, photos) and the default
    # location is a world-readable temp dir on multi-user Macs.
    try:
        os.chmod(upload_dir, 0o700)
    except OSError:
        pass
    return upload_dir


def _downscale_image(path: Path) -> None:
    """Resize an image in place ONLY if it's too big for Claude / the bridge.

    Normal photos are left byte-for-byte untouched (returns without re-encoding).
    We only step in when the long edge exceeds Anthropic's 8000px per-image cap or
    the image is over IMAGE_DOWNSCALE_TRIGGER_PIXELS (~24 MP, where the CLI's PNG
    tool_result echo would approach the 32MB stream buffer). Triggered images are
    resized to the API's effective working size, which Claude sees no less of.
    Non-image files raise on open and are skipped by the caller. Runs in a thread
    executor -- PIL decode/resize is blocking CPU work.
    """
    from PIL import Image, ImageOps

    with Image.open(path) as src:
        fmt = src.format  # capture before any transform (transpose drops it)
        im = ImageOps.exif_transpose(src)  # bake in camera rotation
        im.load()  # force decode so we can save back to the same path

    w, h = im.size
    if max(w, h) <= IMAGE_DOWNSCALE_TRIGGER_LONG_EDGE and (w * h) <= IMAGE_DOWNSCALE_TRIGGER_PIXELS:
        return  # within what Claude handles fine -- leave the original untouched

    scale = IMAGE_TARGET_LONG_EDGE / max(w, h)
    new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
    im = im.resize(new_size, Image.LANCZOS)

    save_fmt = fmt if fmt in ("JPEG", "PNG", "WEBP") else "JPEG"
    if save_fmt == "JPEG" and im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    params = {"quality": IMAGE_JPEG_QUALITY, "optimize": True} if save_fmt == "JPEG" else {}
    im.save(path, save_fmt, **params)
    logger.info("Downscaled oversized image %s: %sx%s -> %sx%s", path.name, w, h, *new_size)


# --- Telegram media fetch with retries ---------------------------------------
# A single Bad Gateway or timeout from api.telegram.org during get_file/download
# used to kill the handler, and python-telegram-bot never retries handler errors
# (only polling), so the photo, file or voice note was silently lost. Now:
# generous timeouts, several attempts with backoff, partial files removed, and
# the caller tells you when it still fails.
MEDIA_FETCH_ATTEMPTS = int(os.environ.get("MEDIA_FETCH_ATTEMPTS") or "4")
_MEDIA_FETCH_BACKOFF = (3, 8, 15)  # seconds before attempt 2, 3, 4
_MEDIA_FETCH_TIMEOUTS = dict(connect_timeout=10.0, read_timeout=60.0, write_timeout=10.0, pool_timeout=5.0)


async def fetch_tg_media(bot, file_id: str, what: str, dest: "Path | None" = None, *, _sleep=asyncio.sleep):
    """get_file + download with retries.

    Returns the Path when dest is given (file written there), else the bytes.
    Any partial file is deleted after a failed attempt. Raises the last error
    once MEDIA_FETCH_ATTEMPTS are used up.
    """
    attempts = max(1, MEDIA_FETCH_ATTEMPTS)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            tg_file = await bot.get_file(file_id, **_MEDIA_FETCH_TIMEOUTS)
            if dest is None:
                return bytes(await tg_file.download_as_bytearray(**_MEDIA_FETCH_TIMEOUTS))
            await tg_file.download_to_drive(str(dest), **_MEDIA_FETCH_TIMEOUTS)
            return dest
        except RetryAfter as e:
            last_exc = e
            delay = int(e.retry_after) + 1
        except (NetworkError, OSError, httpx.HTTPError) as e:
            last_exc = e
            delay = _MEDIA_FETCH_BACKOFF[min(attempt - 1, len(_MEDIA_FETCH_BACKOFF) - 1)]
        if dest is not None:
            try:
                Path(dest).unlink(missing_ok=True)
            except OSError:
                pass
        if attempt < attempts:
            logger.warning(
                "%s fetch attempt %d/%d failed (%s: %s), retrying in %ds",
                what, attempt, attempts, type(last_exc).__name__, last_exc, delay,
            )
            await _sleep(delay)
    logger.error("%s fetch failed after %d attempts: %s: %s", what, attempts, type(last_exc).__name__, last_exc)
    raise last_exc


async def _notify_media_lost(message, kind: str) -> None:
    """Say so in the chat when a photo, file or voice note could not be fetched."""
    try:
        await safe_send(
            message,
            f"I couldn't fetch your {kind} from Telegram after {max(1, MEDIA_FETCH_ATTEMPTS)} tries "
            f"(network trouble between this Mac and Telegram). Please resend it.",
        )
    except Exception as e:  # network may still be down; the log line is the record
        logger.error("Could not post the lost-%s notice either: %s", kind, e)


async def save_upload(bot, file_id: str, filename: str, what: str = "File") -> Path:
    """Download a Telegram file to the uploads directory (with retries).

    Returns the path to the saved file. Handles duplicate filenames.
    """
    upload_dir = get_upload_dir()
    safe_name = sanitize_filename(filename)
    dest = upload_dir / safe_name

    # Unique filename: reserve it atomically (O_EXCL) BEFORE any await, so two
    # uploads with the same name can never pick, or clean up, the same path.
    stem = dest.stem
    suffix = dest.suffix
    counter = 0
    while True:
        try:
            os.close(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            break
        except FileExistsError:
            counter += 1
            dest = upload_dir / f"{stem}_{counter}{suffix}"

    # Download into this request's own temp file; only a complete download
    # replaces the reservation. A failed fetch removes only our own files.
    part = dest.with_name(f".{dest.name}.part-{uuid.uuid4().hex[:8]}")
    try:
        await fetch_tg_media(bot, file_id, what, part)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        raise
    try:
        os.chmod(dest, 0o600)  # owner-only, same reasoning as the upload dir
    except OSError:
        pass

    # Verify file stays within upload directory (path traversal protection)
    try:
        dest.resolve().relative_to(upload_dir.resolve())
    except ValueError:
        dest.unlink(missing_ok=True)
        raise RuntimeError("Path traversal detected, file rejected")

    # Downscale oversized images before Claude reads them (no-op for non-images
    # and for images already within bounds). Never fatal -- fall back to original.
    try:
        await asyncio.get_event_loop().run_in_executor(None, _downscale_image, dest)
    except Exception as e:
        logger.warning("Image downscale skipped for %s: %s", dest.name, e)

    return dest


# ---------------------------------------------------------------------------
# Core Prompt Processor
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _voice_last(bot: Bot, topic_key: str):
    """Make a voice note produced during a turn the LAST message of that turn.

    A voice tool (Jarvis's jarvis-say) still does its TTS inside the turn, so no
    latency is added, but while JARVIS_VOICE_DEFER_DIR points at a live folder it
    parks the .ogg there instead of uploading. On exit (after the text is posted,
    and also on errors or [[SILENT]]) the folder is renamed away and its notes are
    sent in order. A voice tool that runs after the rename (a background worker)
    finds no folder and sends directly, so nothing is lost.
    """
    _voice_discard.discard(topic_key)  # a Stop pressed between turns must not hit this turn
    try:  # prune Stop markers older than the longest a voice tool can run
        for m in VOICE_DEFER_ROOT.glob("*.discarded"):
            if time.time() - m.stat().st_mtime > 3 * 3600:
                shutil.rmtree(m, ignore_errors=True)
    except OSError:
        pass
    d = VOICE_DEFER_ROOT / f"{topic_key}-{uuid.uuid4().hex[:8]}"
    try:
        d.mkdir(parents=True, exist_ok=True)
        _voice_defer_dirs[topic_key] = d
    except OSError as e:
        logger.warning("Voice-defer folder failed, voice goes out live [topic=%s]: %s", topic_key, e)
        d = None
    try:
        yield
    finally:
        if d is not None:
            if _voice_defer_dirs.get(topic_key) == d:
                _voice_defer_dirs.pop(topic_key, None)
            if topic_key in _voice_discard:
                # The user pressed Stop: the cancelled answer's voice must not appear.
                _voice_discard.discard(topic_key)
                try:
                    gone = d.with_name(d.name + ".discarded")
                    d.rename(gone)  # atomic: a later voice tool can no longer park here
                    for item in list(gone.iterdir()):
                        if item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink(missing_ok=True)
                    os.utime(gone)  # the marker's age counts from the Stop, not the turn start
                    # The empty "<folder>.discarded" stays as a marker: a voice tool
                    # that finishes after the Stop sees it and drops its note
                    # instead of sending it live. Old markers are pruned below.
                except OSError:
                    pass
            else:
                await _flush_deferred_voice(bot, d)


def _voice_note_topic(f: Path) -> int:
    """<time_ns>-<topic>.ogg -> topic; raises ValueError on a bad name."""
    parts = f.stem.rsplit("-", 1)
    if len(parts) != 2:
        raise ValueError(f"bad voice note name: {f.name}")
    return int(parts[1])


def _remove_empty_dirs(root: Path) -> None:
    """Remove root and its subfolders only where they are empty (never a note)."""
    for sub in sorted((p for p in root.glob("**/*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            sub.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


async def _flush_deferred_voice(bot: Bot, d: Path):
    """Post the voice notes parked in d, in order, then remove what was sent.

    Notes are named <time_ns>-<topic>.ogg, directly in d or inside one batch
    subfolder per voice-tool run (a multi-note reply is parked in one atomic
    rename). Each sent note is deleted on its own. If a note still fails after
    three tries, it and every later note of this turn are kept together in
    VOICE_UNSENT_DIR (so nothing is lost and nothing arrives out of order), and
    the chat gets a short notice. Folders are only removed once empty.
    """
    flushing = d.with_name(d.name + ".flushing")
    try:
        d.rename(flushing)  # atomic: any later voice tool sees no folder and sends live
    except OSError:
        return
    notes = sorted(list(flushing.glob("*.ogg")) + list(flushing.glob("*/*.ogg")), key=lambda p: p.name)
    failed_topic = None
    try:
        for idx, f in enumerate(notes):
            try:
                topic = _voice_note_topic(f)
            except ValueError:
                logger.warning("Deferred voice with bad name skipped: %s", f.name)
                f.unlink(missing_ok=True)
                continue
            sent = False
            for attempt in range(3):
                try:
                    with f.open("rb") as fh:
                        await bot.send_voice(
                            chat_id=ALLOWED_GROUP_ID, voice=fh, message_thread_id=topic,
                            read_timeout=60, write_timeout=60,
                        )
                    logger.info("Deferred voice sent [topic=%s, %d bytes]", topic, f.stat().st_size)
                    sent = True
                    break
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except (NetworkError, OSError) as e:
                    if attempt == 2:
                        logger.error("Deferred voice send failed [topic=%s]: %s", topic, e)
                    else:
                        await asyncio.sleep(3 * (attempt + 1))
                except TelegramError as e:
                    logger.error("Deferred voice send rejected [topic=%s]: %s", topic, e)
                    break
            if sent:
                f.unlink(missing_ok=True)
                continue
            # Keep this note and all later ones of the turn together, in order.
            keep = VOICE_UNSENT_DIR / flushing.name.replace(".flushing", "")
            try:
                keep.mkdir(parents=True, exist_ok=True)
                for rest in notes[idx:]:
                    if rest.exists():
                        rest.rename(keep / rest.name)
                logger.error("Voice note(s) not delivered, kept in %s", keep)
            except OSError as e:
                logger.error("Could not move undelivered voice to %s (left in %s): %s", keep, flushing, e)
            failed_topic = topic
            break
    finally:
        _remove_empty_dirs(flushing)  # a folder that still holds a note is never deleted
    if failed_topic is not None:
        try:
            await bot.send_message(
                chat_id=ALLOWED_GROUP_ID, message_thread_id=failed_topic,
                text="(The voice note for this reply could not be sent to Telegram. The text above is complete; ask me to say it again if you want the audio.)",
            )
        except Exception as e:
            logger.error("Could not post the undelivered-voice notice: %s", e)


async def _process_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
):
    """Central handler: send prompt to Claude Code and stream the response back.

    Uses streaming JSON mode to edit the Telegram message in real-time as
    Claude generates text. Shows thinking status and response progressively.
    """
    topic_id = update.message.message_thread_id
    key = _topic_key(topic_id)

    # Acquire per-topic lock (queues messages to same topic sequentially)
    lock = _get_topic_lock(topic_id)
    async with lock, _voice_last(context.bot, key):
        session_id, is_new = get_or_create_session(topic_id)

        # Clear any stale stop flag
        _stopped_topics.discard(key)

        # Send "Thinking..." message with Stop button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Stop", callback_data=f"stop:{key}")]
        ])
        thinking_msg = None
        try:
            thinking_msg = await update.message.reply_text(
                "Thinking...", reply_markup=keyboard,
            )
        except TelegramError as e:
            logger.warning("Failed to send thinking message: %s", e)

        # Start typing indicator
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(
            typing_loop(
                update.effective_chat.id,
                context.bot,
                topic_id,
                stop_typing,
            )
        )

        accumulated_text = ""
        last_edit_time = 0.0
        final_session_id = session_id
        error_response = None
        # tool_use_id -> {"name": str, "input": dict, "async": bool}
        # "async" is True when an Agent was invoked with run_in_background=true.
        # Sync tools are removed on tool_result; async agents are removed only on
        # the <task-notification> user message (which carries the matching tool_use_id).
        running_tools: dict[str, dict] = {}

        try:
            proc, start_err, resume_failed = await _start_claude_streaming(
                prompt, session_id, is_new, key
            )
            # Warn the user if the prior session couldn't be resumed and a new
            # one was silently started. Prior conversation context is now lost.
            # Best-effort: a failed heads-up must not block the response.
            if resume_failed and not start_err:
                try:
                    await update.message.reply_text(
                        "Couldn't resume the previous session. Starting fresh: "
                        "earlier context in this topic is not available."
                    )
                except TelegramError as e:
                    logger.warning("Failed to send resume-failed heads-up: %s", e)
                except Exception as e:
                    logger.warning("Unexpected error sending resume-failed heads-up: %s", e)
            if start_err:
                error_response = start_err
            else:
                # Read streaming events from stdout with an IDLE timeout.
                # Total runtime is unbounded -- `asyncio.wait_for` is applied to
                # each readline() call, so any output from Claude resets the
                # clock. Only kills the process if nothing arrives for
                # IDLE_TIMEOUT seconds (or TASK_IDLE_TIMEOUT when an async
                # Agent is in flight). Lets long agentic tasks run to completion
                # while still catching true hangs.
                async def _render(force: bool = False):
                    """Refresh the Telegram 'Thinking...' message.

                    Combines accumulated text with a progress suffix that
                    reflects currently running tools. Debounced via
                    STREAM_EDIT_INTERVAL unless force=True.
                    """
                    nonlocal last_edit_time
                    if not thinking_msg:
                        return
                    now = time.time()
                    if not force and (now - last_edit_time) < STREAM_EDIT_INTERVAL:
                        return

                    suffix = ""
                    if BRIDGE_SHOW_PROGRESS and running_tools:
                        suffix = _format_progress_suffix(running_tools)

                    if accumulated_text:
                        body = accumulated_text[:3900]
                        if len(accumulated_text) > 3900:
                            body += "\n\u2026"
                        display = f"{body}\n\n{suffix}" if suffix else body
                    elif suffix:
                        display = f"Thinking\u2026\n{suffix}"
                    else:
                        # Nothing to show yet; leave the original "Thinking..." alone.
                        return

                    # Telegram 4096-char hard limit; trim generously.
                    if len(display) > 4000:
                        display = display[:4000] + "\n\u2026"

                    try:
                        await thinking_msg.edit_text(
                            display, reply_markup=keyboard,
                            disable_web_page_preview=True,
                        )
                        last_edit_time = now
                    except (TelegramError, Exception):
                        # Edit failures are non-fatal; try again on next tick.
                        pass

                async def _read_stream():
                    nonlocal accumulated_text, error_response, final_session_id
                    while True:
                        # Compute effective idle timeout per readline. If any
                        # async Agent is in flight, use the extended timeout;
                        # otherwise use the regular idle timeout.
                        has_async_task = any(
                            v.get("async") for v in running_tools.values()
                        )
                        effective_timeout = TASK_IDLE_TIMEOUT if has_async_task else IDLE_TIMEOUT

                        try:
                            raw_line = await asyncio.wait_for(
                                proc.stdout.readline(),
                                timeout=effective_timeout,
                            )
                        except ValueError:
                            # A single stream-json line exceeded the StreamReader
                            # buffer limit (only way readline() raises ValueError).
                            # readline() has already dropped the oversized line from
                            # its buffer, so skip it and keep streaming the rest of
                            # the turn -- Claude's text response still gets delivered
                            # instead of a scary "chunk exceed the limit" error.
                            logger.warning(
                                "Skipped oversized stream line [topic=%s]", key
                            )
                            continue
                        if not raw_line:  # EOF -- process exited
                            return

                        # Check if user hit Stop
                        if key in _stopped_topics:
                            proc.kill()
                            return

                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        msg_type = d.get("type")

                        if msg_type == "rate_limit_event":
                            _note_rate_limit_event(key, d)
                        if msg_type == "stream_event":
                            event = d.get("event", {})
                            etype = event.get("type")

                            if etype == "content_block_start":
                                block = event.get("content_block") or {}
                                if block.get("type") == "tool_use":
                                    tool_id = block.get("id")
                                    tool_name = block.get("name", "unknown")
                                    if tool_id:
                                        # Initial entry; async flag will be set
                                        # when the assistant message with full
                                        # input arrives a moment later.
                                        running_tools[tool_id] = {
                                            "name": tool_name,
                                            "input": block.get("input") or {},
                                            "async": False,
                                        }
                                        await _render()

                            elif etype == "content_block_delta":
                                delta = event.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    accumulated_text += delta.get("text", "")
                                    await _render()

                            elif etype == "message_start":
                                # Track per-turn usage from message_start (not result,
                                # which is cumulative across all turns and inflates the count).
                                # Each overwrite keeps the LAST turn's context size.
                                msg_usage = event.get("message", {}).get("usage") or {}
                                turn_input = (
                                    msg_usage.get("input_tokens", 0)
                                    + msg_usage.get("cache_creation_input_tokens", 0)
                                    + msg_usage.get("cache_read_input_tokens", 0)
                                )
                                if turn_input:
                                    _topic_token_usage[key] = {
                                        "input_tokens": turn_input,
                                        "context_window": _get_context_window(key),
                                    }
                                    _save_token_usage(key)

                        elif msg_type == "assistant":
                            # Full assistant message: (a) extract text as backup,
                            # (b) upgrade running_tools entries with parsed input
                            #     and mark async Agents.
                            msg = d.get("message", {})
                            for block in msg.get("content", []):
                                btype = block.get("type")
                                if btype == "text" and block.get("text"):
                                    accumulated_text = block["text"]
                                elif btype == "tool_use":
                                    tid = block.get("id")
                                    if tid:
                                        inp = block.get("input") or {}
                                        name = block.get("name", "unknown")
                                        is_async = bool(
                                            name in ("Agent", "Task")
                                            and inp.get("run_in_background")
                                        )
                                        running_tools[tid] = {
                                            "name": name,
                                            "input": inp,
                                            "async": is_async,
                                        }
                                        if is_async:
                                            logger.info(
                                                "Async Agent detected [topic=%s, id=%s, desc=%s]",
                                                key, tid[:12],
                                                str(inp.get("description", ""))[:60],
                                            )
                            await _render()

                        elif msg_type == "user":
                            # The stream uses the "user" role for two distinct
                            # event kinds:
                            #   (1) sync tool_result: content is a LIST of blocks
                            #       including {"type": "tool_result", "tool_use_id": ...}
                            #   (2) async <task-notification>: content is a STRING
                            #       containing an XML envelope with <tool-use-id>
                            msg = d.get("message", {})
                            content = msg.get("content", [])

                            if isinstance(content, list):
                                # Sync tool_result path.
                                changed = False
                                for block in content:
                                    if not isinstance(block, dict):
                                        continue
                                    if block.get("type") != "tool_result":
                                        continue
                                    tid = block.get("tool_use_id")
                                    if not tid or tid not in running_tools:
                                        continue
                                    # Do NOT remove async Agents here; their
                                    # "Async agent launched successfully"
                                    # tool_result is immediate but the agent
                                    # is still running. They're removed on the
                                    # <task-notification> string message below.
                                    if running_tools[tid].get("async"):
                                        continue
                                    running_tools.pop(tid, None)
                                    changed = True
                                if changed:
                                    await _render()

                            elif isinstance(content, str) and "<task-notification>" in content:
                                # Async Agent completion path.
                                m = _TASK_NOTIFICATION_ID_RE.search(content)
                                if m:
                                    tid = m.group(1).strip()
                                    if tid in running_tools:
                                        logger.info(
                                            "Async Agent completed [topic=%s, id=%s]",
                                            key, tid[:12],
                                        )
                                        running_tools.pop(tid, None)
                                        await _render()

                        elif msg_type == "result":
                            # Stale-task-notification guard: on --resume, a leftover background-task
                            # notification can make the CLI emit an EMPTY success result BEFORE the real
                            # turn runs. Treating it as final would post an empty reply and end the run.
                            # Skip it; the real result follows.
                            if not d.get("is_error") and not d.get("result") and not accumulated_text:
                                logger.info("Skipping empty pre-turn result (stale task notification) [topic=%s]", key)
                                continue
                            final_session_id = d.get("session_id", session_id)
                            if d.get("is_error"):
                                error_response = d.get("result") or "Claude returned an error."
                            elif d.get("result"):
                                accumulated_text = d["result"]
                            return

                try:
                    await _read_stream()
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    # Determine which timeout fired (regular or task-extended)
                    # and what was running at the time, to produce a useful
                    # error message for the user and a detailed log line.
                    had_async_task = any(
                        v.get("async") for v in running_tools.values()
                    )
                    timeout_used = TASK_IDLE_TIMEOUT if had_async_task else IDLE_TIMEOUT
                    minutes = timeout_used // 60
                    if had_async_task:
                        error_response = (
                            f"Claude was idle for {minutes} min waiting for an async "
                            f"subagent to deliver its task-notification. The subagent "
                            f"likely hung or crashed. Try /clear to reset the session."
                        )
                        running_desc = ", ".join(
                            f"{v['name']}({'async' if v.get('async') else 'sync'})"
                            for v in running_tools.values()
                        )
                        logger.warning(
                            "Task-extended idle timeout [topic=%s, idle=%ds, running=%s]; killed subprocess",
                            key, timeout_used, running_desc or "none",
                        )
                    else:
                        error_response = (
                            f"Claude was idle for {minutes} min (no output). "
                            "Possibly stuck on a macOS permission prompt, network stall, "
                            "or runaway tool loop. Try /clear to reset the session."
                        )
                        logger.warning(
                            "Idle timeout [topic=%s, idle=%ds]; killed subprocess",
                            key, timeout_used,
                        )

                # Wait for process to finish
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()

                _running_processes.pop(key, None)

                # If process failed without a result (e.g. session not found)
                if not accumulated_text and not error_response and proc.returncode and proc.returncode != 0:
                    stderr_out = await proc.stderr.read()
                    err = stderr_out.decode("utf-8", errors="replace").strip()
                    if err:
                        error_response = f"Claude error:\n{err[:2000]}"
                    else:
                        error_response = f"Claude exited with code {proc.returncode}"

        except Exception as e:
            logger.exception("Streaming error [topic=%s]", key)
            error_response = f"Error: {e}"
            _running_processes.pop(key, None)

        finally:
            stop_typing.set()
            await typing_task

        # Update session mapping if Claude assigned a different session ID
        if final_session_id != session_id:
            sessions = load_sessions()
            k = str(topic_id if topic_id else "general")
            if k in sessions:
                sessions[k]["session_id"] = final_session_id
                save_sessions(sessions)

        # If user hit Stop, keep the "Stopped." message visible
        if key in _stopped_topics:
            _stopped_topics.discard(key)
            return

        # Retry up to twice on 401 auth errors: inject no token and let the CLI
        # own auth (it can refresh; an injected token can't). Keep the thinking
        # indicator visible so the user sees activity instead of a dead screen.
        # Guard is PER-TOPIC: a retry in one topic must not suppress another's.
        # Two failure classes are worth re-running the whole prompt for instead
        # of posting the CLI's raw text as if it were an answer:
        #   auth: a 401. Re-run with NO injected token (the CLI can self-refresh).
        #   cap:  the model is rate-limited / out of usage credits. Re-run on the
        #         next model in this topic's ladder and say so once.
        _retry_kind = None
        _capped_model = ""
        if _is_auth_err(error_response):
            _retry_kind = "auth"
        elif _is_model_cap_err(error_response):
            _capped_model = _model_for_topic(key)
            _record_model_cap(_capped_model, _last_rate_limit_reset.pop(key, None))
            if all(_model_is_capped(m) for m in _model_chain(key)):
                logger.error(
                    "Model cap with no fallback left [topic=%s, model=%s]", key, _capped_model,
                )
            else:
                _retry_kind = "cap"

        if _retry_kind and key not in _auth_retrying_topics:
            _auth_retrying_topics.add(key)
            try:
                # Circuit breaker: bench the setup-token if it produced this 401.
                if _retry_kind == "auth" and _last_auth_source.get(key) == "setup-token":
                    _quarantine_setup_token(error_response[:200])
                # Auth gets 2 tries; a cap gets one try per remaining rung.
                _max_recovery = 2 if _retry_kind == "auth" else max(1, len(_model_chain(key)) - 1)
                for retry_i in range(_max_recovery):
                    if retry_i and _retry_kind == "auth":
                        logger.warning("Auth retry 1 failed, retry 2 in 20s [topic=%s]", key)
                        await asyncio.sleep(20)
                    if _retry_kind == "auth":
                        logger.warning(
                            "401 auth error detected, retry %d/2 without injected token [topic=%s]",
                            retry_i + 1, key,
                        )
                    else:
                        # No backoff: a cap lasts hours, only another model helps.
                        logger.warning(
                            "Model %s is capped, re-running this turn on %s [topic=%s]",
                            _capped_model, _model_for_topic(key), key,
                        )
                    # Re-run the whole prompt through a fresh subprocess
                    session_id2, is_new2 = get_or_create_session(topic_id)
                    proc2, start_err2, resume_failed2 = await _start_claude_streaming(
                        prompt, session_id2, is_new2, key,
                        skip_keychain_token=(_retry_kind == "auth"),
                    )
                    if start_err2 or not proc2:
                        error_response = start_err2 or error_response
                        if _retry_kind == "auth" and _is_auth_err(error_response):
                            continue
                        break
                    accumulated_text = ""
                    error_response = None

                    async def _read_retry_stream():
                        nonlocal accumulated_text, error_response, final_session_id
                        async for raw_line in proc2.stdout:
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            if not line:
                                continue
                            try:
                                d = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            msg_type = d.get("type")
                            if msg_type == "rate_limit_event":
                                _note_rate_limit_event(key, d)
                            if msg_type == "stream_event":
                                event = d.get("event", {})
                                etype = event.get("type")
                                if etype == "content_block_delta":
                                    delta = event.get("delta", {})
                                    if delta.get("type") == "text_delta":
                                        accumulated_text += delta.get("text", "")
                                elif etype == "message_start":
                                    msg_usage = event.get("message", {}).get("usage") or {}
                                    turn_input = (
                                        msg_usage.get("input_tokens", 0)
                                        + msg_usage.get("cache_creation_input_tokens", 0)
                                        + msg_usage.get("cache_read_input_tokens", 0)
                                    )
                                    if turn_input:
                                        _topic_token_usage[key] = {
                                            "input_tokens": turn_input,
                                            "context_window": _get_context_window(key),
                                        }
                                        _save_token_usage(key)
                            elif msg_type == "result":
                                # Stale-task-notification guard: on --resume, a leftover background-task
                                # notification can make the CLI emit an EMPTY success result BEFORE the real
                                # turn runs. Treating it as final would post an empty reply and end the run.
                                # Skip it; the real result follows.
                                if not d.get("is_error") and not d.get("result") and not accumulated_text:
                                    logger.info("Skipping empty pre-turn result (stale task notification) [topic=%s]", key)
                                    continue
                                final_session_id = d.get("session_id", session_id2)
                                if d.get("is_error"):
                                    error_response = d.get("result") or "Auth retry also failed."
                                elif d.get("result"):
                                    accumulated_text = d["result"]
                                return

                    try:
                        try:
                            # Unlike the first attempt (idle timeout only), a
                            # retry has a total limit of IDLE_TIMEOUT.
                            await asyncio.wait_for(_read_retry_stream(), timeout=IDLE_TIMEOUT)
                        except asyncio.TimeoutError:
                            error_response = "Auth retry timed out."
                            try:
                                proc2.kill()
                            except ProcessLookupError:
                                pass
                        await proc2.wait()
                    finally:
                        # Runs even if the reader raised: a retry process must
                        # never stay registered (the daily restart waits for
                        # the registry to empty, and /stop targets it).
                        if proc2.returncode is None:
                            try:
                                proc2.kill()
                                await asyncio.wait_for(proc2.wait(), timeout=5)
                            except (ProcessLookupError, asyncio.TimeoutError):
                                pass
                        if _running_processes.get(key) is proc2:
                            _running_processes.pop(key, None)
                    # Same bookkeeping as the first attempt: a session id the
                    # CLI assigned on the retry is saved so the next message
                    # resumes it.
                    if final_session_id and final_session_id != session_id2:
                        _sessions = load_sessions()
                        if key in _sessions:
                            _sessions[key]["session_id"] = final_session_id
                            save_sessions(_sessions)
                    if _retry_kind == "auth":
                        if not _is_auth_err(error_response):
                            break
                    else:
                        if not _is_model_cap_err(error_response):
                            break
                        # Fallback is capped too: record it and try the next
                        # rung, unless the whole ladder is now capped.
                        _record_model_cap(
                            _model_for_topic(key), _last_rate_limit_reset.pop(key, None)
                        )
                        if all(_model_is_capped(m) for m in _model_chain(key)):
                            break
                # Outcome logging: show how a retry ended, not only that it
                # started.
                if error_response is None:
                    logger.info("%s retry outcome: healed [topic=%s]", _retry_kind, key)
                elif _retry_kind == "auth" and _is_auth_err(error_response):
                    logger.error(
                        "Auth retry outcome: exhausted [topic=%s, error=%r]",
                        key, error_response[:200],
                    )
                    error_response = (
                        "Auth token hiccup on my side, give it a minute and resend."
                    )
                elif _retry_kind == "cap" and _is_model_cap_err(error_response):
                    logger.error(
                        "Model-cap retry outcome: every model on the ladder is capped "
                        "[topic=%s, chain=%s]", key, _model_chain(key),
                    )
                    _when = _cap_reset_str(_capped_model)
                    error_response = (
                        "Every model I can run is out of usage credits right now"
                        + (f" ({_model_label(_capped_model)} resets {_when})" if _when else "")
                        + ". Add credits at claude.ai/settings/usage, or wait for the reset."
                    )
                else:
                    logger.warning(
                        "%s retry outcome: unrelated error [topic=%s, error=%r]",
                        _retry_kind, key, error_response[:200],
                    )
            finally:
                _auth_retrying_topics.discard(key)

        # Tell the user once per cap window that this topic is on a fallback model
        # (covers both the cap retry above and a silent pre-flight demotion).
        if not error_response:
            _notice = _fallback_notice(key)
            if _notice:
                accumulated_text = (
                    f"{_notice}\n\n{accumulated_text}" if accumulated_text else _notice
                )

        # Delete thinking indicator now that we have a final result (post-retry if applicable)
        if thinking_msg:
            try:
                await thinking_msg.delete()
            except TelegramError:
                pass

        # Defensive: [[SILENT]] only means something on trigger turns (suppression
        # lives in _process_trigger). If the model misapplies it on a direct reply,
        # strip the sentinel instead of posting literal brackets to the user.
        if not error_response and accumulated_text.rstrip().endswith("[[SILENT]]"):
            accumulated_text = accumulated_text.rstrip()[: -len("[[SILENT]]")].rstrip()

        response = error_response or accumulated_text or "(Claude returned an empty response)"
        # Append context usage footer
        usage = _topic_token_usage.get(key)
        if usage:
            pct = (usage["input_tokens"] / usage["context_window"]) * 100
            inp = usage["input_tokens"] / 1000
            response += f"\n\n_{inp:.0f}k / {_fmt_window(usage['context_window'])} ({pct:.1f}%)_"
        await send_long_reply(update.message, response)


# ---------------------------------------------------------------------------
# Trigger Processing (external cron/agent injection)
# ---------------------------------------------------------------------------

async def _send_to_topic(bot: Bot, chat_id: int, topic_id: int, text: str):
    """Send a message to a specific topic, handling splitting and formatting.

    Each chunk gets up to 3 network retries (15/60/120s backoff): a Wi-Fi/DNS
    blip at send time used to throw away an already-generated trigger response.
    """
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            chunk = f"[{i + 1}/{len(chunks)}]\n{chunk}"

        _net_backoffs = (15, 60, 120)
        for net_attempt in range(len(_net_backoffs) + 1):
            try:
                # Try entity-based formatting first, then plain text
                sent = False
                if HAS_TELEGRAMIFY:
                    try:
                        from telegram import MessageEntity
                        plain_text, tm_entities = tm_convert(chunk)
                        tg_entities = [
                            MessageEntity(
                                type=e.type, offset=e.offset, length=e.length,
                                url=e.url, language=e.language,
                            )
                            for e in tm_entities
                        ]
                        await bot.send_message(
                            chat_id=chat_id, text=plain_text,
                            entities=tg_entities, message_thread_id=topic_id,
                        )
                        sent = True
                    except NetworkError:
                        raise  # network problem, outer retry, not the plain-text fallback
                    except TelegramError as exc:
                        logger.warning("Entity send to topic failed: %s", exc)
                    except Exception as exc:
                        logger.warning("telegramify failed for trigger: %s", exc)

                if not sent:
                    clean = _strip_markdown(chunk)
                    try:
                        await bot.send_message(
                            chat_id=chat_id, text=clean,
                            message_thread_id=topic_id,
                        )
                    except RetryAfter as e:
                        await asyncio.sleep(e.retry_after + 1)
                        await bot.send_message(
                            chat_id=chat_id, text=clean,
                            message_thread_id=topic_id,
                        )
                break
            except (NetworkError, OSError) as exc:
                if net_attempt >= len(_net_backoffs):
                    raise
                wait = _net_backoffs[net_attempt]
                logger.warning(
                    "Network error sending trigger response (retry %d/%d in %ds) [topic=%s]: %s",
                    net_attempt + 1, len(_net_backoffs), wait, topic_id, exc,
                )
                await asyncio.sleep(wait)

        if i < len(chunks) - 1:
            await asyncio.sleep(RATE_LIMIT_INTERVAL)


async def _process_trigger(bot: Bot, trigger_data: dict, trigger_path: Path):
    """Process a single trigger file: run prompt through the topic's session, post response."""
    topic_id = trigger_data.get("topic_id")
    prompt = trigger_data.get("prompt", "")
    sender = trigger_data.get("sender", "unknown")

    if not topic_id or not prompt:
        logger.warning("Trigger missing topic_id or prompt: %s", trigger_path.name)
        return

    key = _topic_key(topic_id)
    logger.info("Processing trigger from '%s' for topic %s [%s]", sender, topic_id, trigger_path.name)

    # Acquire topic lock (same as regular messages)
    lock = _get_topic_lock(topic_id)
    async with lock, _voice_last(bot, key):
        # Capture the starting session_id so we can detect post-loop rotation.
        initial_session_id, initial_is_new = get_or_create_session(topic_id)

        # State shared across the attempt loop. final_session_id is updated by
        # each attempt's stream reader as Claude reports it in the result event.
        accumulated_text = ""
        final_session_id = initial_session_id

        # One attempt = one Claude subprocess + one stream read. Returns
        # (text, error). On error, the caller decides whether to retry.
        async def _run_attempt(skip_keychain_token: bool, timeout_s: float = None) -> tuple[str, str | None]:
            nonlocal final_session_id
            sess_id, is_new = get_or_create_session(topic_id)
            # The outer get_or_create_session call above already
            # created the row for a fresh session, so this second lookup
            # returns is_new=False for a session that has NO transcript yet.
            # That forced a doomed --resume on EVERY trigger that arrived
            # right after a reset. Reuse the outer call's truth.
            if sess_id == initial_session_id:
                is_new = initial_is_new
            final_session_id = sess_id
            attempt_text = ""
            attempt_err: str | None = None
            proc = None
            try:
                proc, start_err, _ = await _start_claude_streaming(
                    prompt, sess_id, is_new, key,
                    skip_keychain_token=skip_keychain_token,
                )
                if start_err:
                    return "", start_err

                async def _read_trigger_stream():
                    nonlocal attempt_text, attempt_err, final_session_id
                    async for raw_line in proc.stdout:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg_type = d.get("type")
                        if msg_type == "rate_limit_event":
                            _note_rate_limit_event(key, d)
                        if msg_type == "stream_event":
                            event = d.get("event", {})
                            etype = event.get("type")
                            if etype == "content_block_delta":
                                delta = event.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    attempt_text += delta.get("text", "")
                            elif etype == "message_start":
                                msg_usage = event.get("message", {}).get("usage") or {}
                                turn_input = (
                                    msg_usage.get("input_tokens", 0)
                                    + msg_usage.get("cache_creation_input_tokens", 0)
                                    + msg_usage.get("cache_read_input_tokens", 0)
                                )
                                if turn_input:
                                    _topic_token_usage[key] = {
                                        "input_tokens": turn_input,
                                        "context_window": _get_context_window(key),
                                    }
                                    _save_token_usage(key)
                        elif msg_type == "assistant":
                            msg = d.get("message", {})
                            for block in msg.get("content", []):
                                if block.get("type") == "text" and block.get("text"):
                                    attempt_text = block["text"]
                        elif msg_type == "result":
                            # Stale-task-notification guard: on --resume, a leftover background-task
                            # notification can make the CLI emit an EMPTY success result BEFORE the real
                            # turn runs. Treating it as final would post an empty reply and end the run.
                            # Skip it; the real result follows.
                            if not d.get("is_error") and not d.get("result") and not attempt_text:
                                logger.info("Skipping empty pre-turn result (stale task notification) [topic=%s]", key)
                                continue
                            final_session_id = d.get("session_id", sess_id)
                            if d.get("is_error"):
                                attempt_err = d.get("result") or "Claude returned an error."
                            elif d.get("result"):
                                attempt_text = d["result"]
                            return

                # Timeout: triggers are autonomous, no stop button, so enforce a hard limit.
                # Timeout is non-retriable, suggests genuinely slow Claude, not transient API blip.
                effective_timeout = timeout_s if timeout_s else CLAUDE_TIMEOUT
                try:
                    await asyncio.wait_for(_read_trigger_stream(), timeout=effective_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Trigger timed out after %ds [topic=%s, sender=%s]",
                        effective_timeout, key, sender,
                    )
                    attempt_err = f"Trigger timed out after {effective_timeout:.0f}s"
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass

                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass

                _running_processes.pop(key, None)

                if not attempt_text and not attempt_err and proc.returncode and proc.returncode != 0:
                    stderr_out = await proc.stderr.read()
                    err = stderr_out.decode("utf-8", errors="replace").strip()
                    attempt_err = (
                        f"Claude error:\n{err[:2000]}" if err
                        else f"Claude exited with code {proc.returncode}"
                    )
                return attempt_text, attempt_err

            except Exception as e:
                logger.exception("Trigger processing error [topic=%s, sender=%s]", key, sender)
                if proc and proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                _running_processes.pop(key, None)
                return attempt_text, f"Error: {e}"

        # Retry loop: budget = 1 initial + up to 2 auth retries (backoff 5s/30s)
        # + up to 3 transient retries (additive), all under one ABSOLUTE
        # deadline that caps every sleep and each attempt's subprocess timeout
        # (a deadline checked only between attempts couldn't interrupt a 600s
        # transient sleep). Timeouts are non-retriable.
        error_response: str | None = None
        auth_retries_used = 0
        max_auth = 2
        _auth_backoffs = (5, 30)
        transient_retries_used = 0
        max_transient = len(_TRIGGER_TRANSIENT_BACKOFF_SECONDS)
        attempts = 0
        # 6 = 1 initial + 2 auth + 3 transient, matching the documented
        # budget above (was 5, silently eating the last transient retry).
        max_attempts = 6
        deadline = time.monotonic() + int(os.environ.get("TRIGGER_RETRY_DEADLINE_S", "720"))

        while True:
            # Deadline checked BEFORE the attempt and with no lower floor , 
            # a between-attempts-only check could not stop a retry from
            # starting a fresh 600s subprocess past the deadline. (The ~90s
            # init handshake inside _start_claude_streaming is outside this
            # cap; worst-case overshoot is bounded by that, not by a full run.)
            remaining = deadline - time.monotonic()
            if attempts > 0 and remaining <= 10:
                logger.error(
                    "Trigger retry deadline reached (attempts=%d) [topic=%s, sender=%s, error=%r]",
                    attempts, key, sender, (error_response or "")[:200],
                )
                break
            attempts += 1
            accumulated_text, error_response = await _run_attempt(
                skip_keychain_token=(auth_retries_used > 0),
                timeout_s=min(CLAUDE_TIMEOUT, max(10.0, remaining)),
            )
            if not error_response:
                break

            is_auth = _is_auth_err(error_response)
            is_transient = any(m in error_response for m in _TRANSIENT_API_ERR_MARKERS)
            is_cap = (not is_auth) and _is_model_cap_err(error_response)
            remaining = deadline - time.monotonic()

            if attempts >= max_attempts:
                logger.error(
                    "Trigger retry attempts exhausted (attempts=%d, remaining=%.0fs) [topic=%s, sender=%s, error=%r]",
                    attempts, remaining, key, sender, error_response[:200],
                )
                break

            # Model rate-capped / out of credits: demote to the next model in
            # the ladder and re-run immediately. No backoff: a cap lasts hours,
            # so waiting is pointless and only a different model recovers the run.
            if is_cap:
                capped_now = _model_for_topic(key)
                _record_model_cap(capped_now, _last_rate_limit_reset.pop(key, None))
                next_model = _model_for_topic(key)
                # _model_for_topic falls back to chain[0] when everything is
                # capped, so test the ladder itself; comparing models would
                # burn one more attempt on an already-capped primary.
                if not all(_model_is_capped(m) for m in _model_chain(key)):
                    logger.warning(
                        "Trigger hit model cap on %s, retrying on %s [topic=%s, sender=%s]",
                        capped_now, next_model, key, sender,
                    )
                    continue
                logger.error(
                    "Trigger hit model cap with no fallback left [topic=%s, sender=%s, model=%s]",
                    key, sender, capped_now,
                )
                break

            if is_auth and auth_retries_used < max_auth:
                # Circuit breaker: if the failed attempt injected the
                # setup-token, bench it so retries (and every other topic)
                # fall through to CLI self-auth.
                if auth_retries_used == 0 and _last_auth_source.get(key) == "setup-token":
                    _quarantine_setup_token(error_response[:200])
                wait_seconds = min(_auth_backoffs[auth_retries_used], remaining - 10)
                auth_retries_used += 1
                logger.warning(
                    "Trigger auth error, retry %d/%d in %.0fs without injected token [topic=%s, sender=%s, error=%r]",
                    auth_retries_used, max_auth, wait_seconds, key, sender, error_response[:200],
                )
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                continue

            if is_transient and transient_retries_used < max_transient:
                wait_seconds = min(
                    _TRIGGER_TRANSIENT_BACKOFF_SECONDS[transient_retries_used],
                    remaining - 10,
                )
                logger.warning(
                    "Trigger transient API error, retrying in %.0fs (attempt %d/%d) [topic=%s, sender=%s, error=%r]",
                    wait_seconds, transient_retries_used + 1, max_transient,
                    key, sender, error_response[:200],
                )
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                transient_retries_used += 1
                continue

            # Non-retriable error, or retry budget exhausted, fall through to delivery.
            if is_transient and transient_retries_used >= max_transient:
                logger.error(
                    "Trigger transient API error retries exhausted (%d attempts) [topic=%s, sender=%s, error=%r]",
                    max_transient, key, sender, error_response[:200],
                )
            break

        # Update session mapping if Claude rotated the session_id mid-stream
        # (e.g., resume failure → new session). Compare against the snapshot
        # we took before the attempt loop.
        if final_session_id and final_session_id != initial_session_id:
            sessions = load_sessions()
            k = str(topic_id if topic_id else "general")
            if k in sessions:
                sessions[k]["session_id"] = final_session_id
                save_sessions(sessions)

        # Exhausted auth retries: never post the raw API error. Requeue the
        # trigger for a later delivery attempt (cap 2), then dead-letter with
        # ONE rate-limited friendly line. A scheduled message arriving 10 min
        # late beats an error message replacing it.
        if error_response and _is_auth_err(error_response):
            try:
                requeues = int(trigger_data.get("auth_requeues", 0) or 0)
            except (TypeError, ValueError):
                requeues = 0
            requeues = max(0, requeues)
            if requeues < int(os.environ.get("AUTH_REQUEUE_CAP", "2")):
                requeue_data = dict(trigger_data)
                requeue_data["auth_requeues"] = requeues + 1
                requeue_data["not_before"] = time.time() + int(os.environ.get("AUTH_REQUEUE_DELAY_S", "600"))
                # NEW filename (never the original path, the watcher's
                # finally-unlink would delete it), collision-safe via time_ns.
                new_name = f"{trigger_path.stem}-rq{requeues + 1}-{time.time_ns()}.json"
                try:
                    tmp = TRIGGER_DIR / (new_name + ".tmp")
                    tmp.write_text(json.dumps(requeue_data))
                    tmp.replace(TRIGGER_DIR / new_name)
                    logger.warning(
                        "Trigger auth-failed; requeued as %s for +%ss (requeue %d/2) [topic=%s, sender=%s]",
                        new_name, os.environ.get("AUTH_REQUEUE_DELAY_S", "600"),
                        requeues + 1, key, sender,
                    )
                    return  # silent, no post for a requeued trigger
                except OSError as e:
                    logger.error("Trigger requeue write failed: %s, falling through to notice", e)
            # Requeue cap reached (or requeue write failed), dead-letter + one notice.
            try:
                dead_dir = TRIGGER_DIR / "dead"
                dead_dir.mkdir(parents=True, exist_ok=True)
                (dead_dir / f"{trigger_path.stem}-dead-{time.time_ns()}.json").write_text(
                    json.dumps(trigger_data))
            except OSError as e:
                logger.warning("Dead-letter write failed: %s", e)
            logger.error(
                "Trigger dead-lettered after auth retries + %d requeue(s) [topic=%s, sender=%s, error=%r]",
                requeues, key, sender, error_response[:200],
            )
            notice_key = f"trigger-auth-notice-{_last_auth_source.get(key, 'unknown')}"
            if not _auth_alert_due(notice_key, AUTH_ALERT_COOLDOWN_S):
                return  # notice already sent this window for this source, stay silent
            error_response = (
                "Couldn't authenticate with the Claude API after several tries, "
                "so this scheduled message was skipped. It usually self-heals; "
                "if it keeps happening, re-mint the automation token with `claude setup-token`."
            )

        # Send response to the topic
        response = error_response or accumulated_text or "(Trigger produced empty response)"
        # [[SILENT]] sentinel: a trigger turn whose response ends with
        # [[SILENT]] judged there is nothing the user needs, so post NOTHING
        # (kills "all quiet" spam). Only trigger turns are eligible (typed
        # messages never pass through here); real errors always post.
        if not error_response and accumulated_text.rstrip().endswith("[[SILENT]]"):
            logger.info(
                "Trigger response suppressed by [[SILENT]] sentinel [topic=%s, sender=%s]",
                topic_id, sender,
            )
            return
        # Heads-up when this scheduled run is on a fallback model (once per window).
        if not error_response:
            _notice = _fallback_notice(key)
            if _notice:
                response = f"{_notice}\n\n{response}"

        chat_id = ALLOWED_GROUP_ID
        if not chat_id:
            logger.error("Cannot send trigger response: TELEGRAM_GROUP_ID not configured")
            return

        try:
            await _send_to_topic(bot, chat_id, topic_id, response)
        except (NetworkError, OSError) as send_exc:
            # All in-band send retries exhausted (network down for 3+ min).
            # Requeue the whole trigger for later instead of letting the
            # watcher's finally-unlink destroy it. Replaying
            # the trigger re-runs the Claude turn; scheduled triggers are
            # read-only + message-producing, so a replay is safe and an
            # eventual late brief beats a lost one.
            try:
                net_requeues = int(trigger_data.get("net_requeues", 0) or 0)
            except (TypeError, ValueError):
                net_requeues = 0
            net_requeues = max(0, net_requeues)
            if net_requeues < int(os.environ.get("NET_REQUEUE_CAP", "2")):
                requeue_data = dict(trigger_data)
                requeue_data["net_requeues"] = net_requeues + 1
                requeue_data["not_before"] = time.time() + int(os.environ.get("NET_REQUEUE_DELAY_S", "600"))
                new_name = f"{trigger_path.stem}-netrq{net_requeues + 1}-{time.time_ns()}.json"
                try:
                    tmp = TRIGGER_DIR / (new_name + ".tmp")
                    tmp.write_text(json.dumps(requeue_data))
                    tmp.replace(TRIGGER_DIR / new_name)
                    logger.warning(
                        "Trigger send failed on network; requeued as %s for +%ss (requeue %d/2) [topic=%s, sender=%s]: %s",
                        new_name, os.environ.get("NET_REQUEUE_DELAY_S", "600"),
                        net_requeues + 1, topic_id, sender, send_exc,
                    )
                    return
                except OSError as e:
                    logger.error("Trigger network-requeue write failed: %s, dead-lettering", e)
            # Cap reached (or requeue write failed), dead-letter so the
            # content survives on disk even though delivery failed.
            try:
                dead_dir = TRIGGER_DIR / "dead"
                dead_dir.mkdir(parents=True, exist_ok=True)
                (dead_dir / f"{trigger_path.stem}-netdead-{time.time_ns()}.json").write_text(
                    json.dumps({**trigger_data, "undelivered_response": response[:8000]}))
            except OSError as e:
                logger.warning("Network dead-letter write failed: %s", e)
            logger.error(
                "Trigger response undeliverable after %d network requeue(s), dead-lettered [topic=%s, sender=%s]",
                net_requeues, topic_id, sender,
            )
            return
        logger.info("Trigger response sent to topic %s (%d chars) [sender=%s]", topic_id, len(response), sender)


# ---------------------------------------------------------------------------
# Daily Restart Scheduler
# ---------------------------------------------------------------------------

# Hour-of-day (0-23, Mac local time) for the daily memory-recycle restart.
# Default 4 AM. Uses naive datetime.now() so it follows the Mac's system
# timezone, change timezone, restart fires at the new local hour automatically
# with no config change. Override with BRIDGE_DAILY_RESTART_HOUR in .env.
DAILY_RESTART_HOUR = int(os.environ.get("BRIDGE_DAILY_RESTART_HOUR", "4"))


async def _daily_restart_scheduler():
    """Self-exit at DAILY_RESTART_HOUR each day so launchd KeepAlive respawns
    a fresh process. Frees memory accumulated by Python heap fragmentation
    and PTB connection pool growth (~500 MB/day in observed practice).

    Defers exit while any Claude subprocess is in flight, retrying every
    10 minutes, never kills user work mid-flight.
    """
    while True:
        now = datetime.now()
        target = now.replace(hour=DAILY_RESTART_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info(
            "Daily restart scheduled for %s (%.1fh from now)",
            target.strftime("%Y-%m-%d %H:%M"), wait_seconds / 3600,
        )
        # Sleep in <=5 min slices, re-checking the wall clock each time:
        # one long asyncio.sleep() is suspended while the Mac sleeps, which
        # can drift the restart by hours. With slices, worst-case drift
        # after wake is ~5 minutes.
        while datetime.now() < target:
            remaining = (target - datetime.now()).total_seconds()
            await asyncio.sleep(min(300, max(1, remaining)))

        while _running_processes:
            in_flight = list(_running_processes.keys())
            logger.info(
                "Daily restart deferred: %d subprocess(es) running [topics=%s]; retry in 10 min",
                len(in_flight), in_flight,
            )
            await asyncio.sleep(600)

        logger.info("Daily restart: exiting cleanly, launchd will respawn")
        os._exit(0)


def _cleanup_uploads() -> tuple[int, int]:
    """Delete uploaded files older than UPLOAD_RETENTION_HOURS.

    Telegram retains the originals, so the local copies are disposable once
    Claude has read them. Returns (files_removed, bytes_freed).
    """
    if UPLOAD_RETENTION_HOURS <= 0:
        return (0, 0)
    upload_dir = Path(UPLOAD_DIR)
    if not upload_dir.is_dir():
        return (0, 0)
    cutoff = time.time() - UPLOAD_RETENTION_HOURS * 3600
    removed = freed = 0
    for f in upload_dir.iterdir():
        try:
            st = f.stat()
            if f.is_file() and st.st_mtime < cutoff:
                freed += st.st_size
                f.unlink()
                removed += 1
        except OSError:
            continue
    return (removed, freed)


async def _periodic_upload_cleanup():
    """Purge stale uploads on startup and every UPLOAD_CLEANUP_INTERVAL seconds."""
    if UPLOAD_RETENTION_HOURS <= 0:
        logger.info("Upload cleanup disabled (UPLOAD_RETENTION_HOURS=0)")
        return
    while True:
        try:
            removed, freed = await asyncio.get_event_loop().run_in_executor(
                None, _cleanup_uploads
            )
            if removed:
                logger.info(
                    "Upload cleanup: removed %d file(s) older than %dh, freed %.1f MB",
                    removed, UPLOAD_RETENTION_HOURS, freed / 1048576,
                )
        except Exception as e:
            logger.warning("Upload cleanup loop error: %s", e)
        await asyncio.sleep(UPLOAD_CLEANUP_INTERVAL)


def _dead_letter_trigger(tf: Path, tag: str) -> None:
    """Move a trigger file into triggers/dead/ (delete it if that fails)."""
    try:
        dead_dir = TRIGGER_DIR / "dead"
        dead_dir.mkdir(parents=True, exist_ok=True)
        tf.replace(dead_dir / f"{tf.stem}-{tag}-{time.time_ns()}.json")
    except OSError:
        tf.unlink(missing_ok=True)


async def _watch_triggers(bot: Bot):
    """Poll the triggers directory every 5 seconds and process any .json files found."""
    logger.info("Trigger watcher started (dir=%s)", TRIGGER_DIR)
    while True:
        try:
            await asyncio.sleep(5)

            if not TRIGGER_DIR.exists():
                continue

            trigger_files = sorted(TRIGGER_DIR.glob("*.json"))
            # Drop bookkeeping for deferred files that vanished externally
            # (manual deletion/rename) so the set can't grow forever.
            _deferred_seen.intersection_update({f.name for f in trigger_files})
            for tf in trigger_files:
                try:
                    data = json.loads(tf.read_text())
                except OSError as e:
                    logger.warning("Unreadable trigger file %s: %s -- skipping", tf.name, e)
                    continue
                except ValueError as e:
                    # JSONDecodeError and UnicodeDecodeError are both ValueErrors:
                    # a file that is not valid UTF-8 JSON must not block the queue.
                    logger.warning("Malformed trigger file %s: %s -- moved to dead/", tf.name, e)
                    _dead_letter_trigger(tf, "badjson")
                    continue
                # Valid JSON is not enough: [] or null used to crash the whole
                # scan here and block every later trigger on each poll.
                if not isinstance(data, dict) or not data.get("topic_id") or not data.get("prompt"):
                    logger.warning(
                        "Invalid trigger %s (needs a JSON object with topic_id and prompt) -- moved to dead/",
                        tf.name,
                    )
                    _dead_letter_trigger(tf, "invalid")
                    continue

                # Deferred delivery (auth requeue): skip WITHOUT unlinking
                # until not_before passes. Log only on first sight, not
                # every 5s poll.
                nb = data.get("not_before")
                if isinstance(nb, (int, float)) and nb > time.time():
                    if tf.name not in _deferred_seen:
                        _deferred_seen.add(tf.name)
                        logger.info(
                            "Trigger %s deferred until %s (auth requeue)",
                            tf.name, datetime.fromtimestamp(nb).strftime("%H:%M:%S"),
                        )
                    continue
                _deferred_seen.discard(tf.name)

                try:
                    await _process_trigger(bot, data, tf)
                except Exception as e:
                    logger.exception("Failed to process trigger %s: %s", tf.name, e)
                    # Preserve the trigger in dead/ instead of silently
                    # consuming it: an unexpected exception used to be the
                    # only copy's end.
                    try:
                        dead_dir = TRIGGER_DIR / "dead"
                        dead_dir.mkdir(parents=True, exist_ok=True)
                        tf.replace(dead_dir / f"{tf.stem}-err-{time.time_ns()}.json")
                    except OSError:
                        pass  # fall through; finally-unlink removes it
                finally:
                    tf.unlink(missing_ok=True)

        except asyncio.CancelledError:
            logger.info("Trigger watcher stopped")
            break
        except Exception as e:
            logger.exception("Trigger watcher loop error: %s", e)
            await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# Telegram Handlers
# ---------------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages."""
    if not _is_authorized(update):
        return
    if not update.message or not update.message.text:
        return

    await _process_prompt(update, context, update.message.text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages: download, transcribe via Whisper, then process."""
    if not _is_authorized(update):
        return
    if not update.message:
        return

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    if not WHISPER_PROVIDER:
        await safe_send(
            update.message,
            "Voice messages not configured. Set WHISPER_PROVIDER in .env.",
        )
        return

    if WHISPER_PROVIDER != "local" and not WHISPER_API_KEY:
        await safe_send(
            update.message,
            f"Voice API key not configured. Set WHISPER_API_KEY for {WHISPER_PROVIDER}.",
        )
        return

    if voice.file_size and voice.file_size > 25 * 1024 * 1024:
        await safe_send(update.message, "Voice message too large (max 25MB).")
        return

    # Download (with retries) and transcribe
    logger.info(
        "Voice received [topic=%s, msg=%s, %ss, %s bytes]",
        update.message.message_thread_id, update.message.message_id,
        getattr(voice, "duration", "?"), voice.file_size,
    )
    try:
        audio_bytes = await fetch_tg_media(context.bot, voice.file_id, "Voice")
    except Exception as e:
        logger.error(
            "Voice dropped [topic=%s, msg=%s]: %s",
            update.message.message_thread_id, update.message.message_id, e,
        )
        await _notify_media_lost(update.message, "voice message")
        return
    try:
        text = await transcribe_voice(audio_bytes)
    except httpx.HTTPStatusError as e:
        await safe_send(update.message, f"Transcription API error: {e.response.status_code}")
        return
    except Exception as e:
        await safe_send(update.message, f"Transcription failed: {e}")
        return

    # Check for empty transcription
    if not text or not text.strip():
        await safe_send(update.message, "Could not transcribe voice message (empty result).")
        return

    # Show what was transcribed
    await safe_send(update.message, f"Transcribed: {text}")

    # Process the transcribed text as a prompt
    await _process_prompt(update, context, text)


async def _flush_media_group(media_group_id: str):
    """Wait briefly, then process all buffered photos from the same album as one prompt."""
    await asyncio.sleep(_MEDIA_GROUP_DELAY)
    buf = _media_group_buffer.pop(media_group_id, None)
    if not buf:
        return
    paths = buf["paths"]
    caption = buf["caption"] or "Please take a look at these images."
    update = buf["update"]
    context = buf["context"]
    if len(paths) == 1:
        prompt = f"I've uploaded an image to {paths[0]}. {caption}"
    else:
        paths_str = "\n".join(f"- {p}" for p in paths)
        prompt = f"I've uploaded {len(paths)} images:\n{paths_str}\n\n{caption}"
    await _process_prompt(update, context, prompt)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo uploads: save to project dir, tell Claude the path.

    When multiple photos are sent as an album (media group), buffers them and
    processes the whole group as a single prompt after a short delay.
    """
    if not _is_authorized(update):
        return
    if not update.message or not update.message.photo:
        return

    # Get highest resolution version
    photo = update.message.photo[-1]
    topic_id = update.message.message_thread_id
    logger.info(
        "Photo received [topic=%s, msg=%s, %s bytes]",
        topic_id, update.message.message_id, photo.file_size,
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"photo_{timestamp}_{photo.file_unique_id[:8]}.jpg"

    try:
        dest = await save_upload(context.bot, photo.file_id, filename, "Photo")
    except Exception as e:
        logger.error("Photo dropped [topic=%s, msg=%s]: %s", topic_id, update.message.message_id, e)
        await _notify_media_lost(update.message, "photo")
        return
    logger.info("Photo saved %s [topic=%s, %d bytes]", dest.name, topic_id, dest.stat().st_size)

    # Use absolute path so Claude doesn't misinterpret a leading ".claude-uploads/"
    # as being under $HOME (past bug: Claude tried ~/.claude-uploads/ and failed).
    caption = update.message.caption  # only the first item in an album has a caption

    media_group_id = update.message.media_group_id
    if media_group_id:
        # This photo is part of an album, buffer it and wait for the rest
        if media_group_id not in _media_group_buffer:
            _media_group_buffer[media_group_id] = {
                "paths": [],
                "caption": None,
                "update": update,
                "context": context,
                "task": None,
            }
        buf = _media_group_buffer[media_group_id]
        buf["paths"].append(str(dest))
        if caption:
            buf["caption"] = caption
        # Cancel the previous flush task and schedule a fresh one
        if buf["task"] and not buf["task"].done():
            buf["task"].cancel()
        buf["task"] = asyncio.create_task(_flush_media_group(media_group_id))
    else:
        # Single photo, process immediately
        prompt = f"I've uploaded an image to {dest}. {caption or 'Please take a look at this image.'}"
        await _process_prompt(update, context, prompt)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document/file uploads: save to project dir, tell Claude the path."""
    if not _is_authorized(update):
        return
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    if doc.file_size and doc.file_size > 50 * 1024 * 1024:
        await safe_send(update.message, "File too large (max 50MB).")
        return

    topic_id = update.message.message_thread_id
    filename = doc.file_name or f"file_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    logger.info(
        "Document received [topic=%s, msg=%s, %s, %s bytes]",
        topic_id, update.message.message_id, filename, doc.file_size,
    )

    try:
        dest = await save_upload(context.bot, doc.file_id, filename, "Document")
    except Exception as e:
        logger.error("Document dropped [topic=%s, msg=%s]: %s", topic_id, update.message.message_id, e)
        await _notify_media_lost(update.message, "file")
        return
    logger.info("Document saved %s [topic=%s, %d bytes]", dest.name, topic_id, dest.stat().st_size)

    # Use absolute path so Claude doesn't misinterpret a leading ".claude-uploads/"
    # as being under $HOME (past bug: Claude tried ~/.claude-uploads/ and failed).
    safe_name = dest.name  # Use the sanitized filename, not the original
    caption = update.message.caption or "Please take a look at this file."
    prompt = f"I've uploaded {safe_name} to {dest}. {caption}"

    await _process_prompt(update, context, prompt)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show token context usage for this topic."""
    if not _is_authorized(update):
        return

    topic_id = update.message.message_thread_id
    key = _topic_key(topic_id)
    # Read the file, not memory: an overnight or threshold reset pops the row there.
    usage = _load_token_usage().get(key)

    if not usage:
        await safe_send(update.message, "No context data yet. Send a message first.")
        return

    input_tokens = usage["input_tokens"]
    context_window = usage["context_window"]
    pct = (input_tokens / context_window) * 100

    await safe_send(
        update.message,
        f"Context: {input_tokens:,} / {context_window:,} tokens ({pct:.1f}%)",
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset the current topic's Claude session (start fresh)."""
    if not _is_authorized(update):
        return

    topic_id = update.message.message_thread_id
    reset_session(topic_id)
    await safe_send(
        update.message,
        "Session cleared. Next message starts fresh.",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current session info for this topic."""
    if not _is_authorized(update):
        return

    topic_id = update.message.message_thread_id
    sessions = load_sessions()
    key = str(topic_id if topic_id else "general")

    if key in sessions:
        s = sessions[key]
        await safe_send(
            update.message,
            f"Session: {s['session_id'][:8]}...\n"
            f"Created: {s.get('created_at', 'unknown')}\n"
            f"Last used: {s.get('last_used', 'unknown')}\n"
            f"Working dir: {WORKING_DIR}",
        )
    else:
        await safe_send(update.message, "No session for this topic yet. Send a message to start one.")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the chat/group ID (for .env configuration)."""
    if not update.effective_user or update.effective_user.id not in ALLOWED_USERS:
        return

    chat = update.effective_chat
    await safe_send(
        update.message,
        f"Chat ID: {chat.id}\n"
        f"Chat type: {chat.type}\n"
        f"Chat title: {chat.title or 'N/A'}\n"
        f"Your user ID: {update.effective_user.id}\n"
        f"Topic ID: {update.message.message_thread_id or 'N/A'}",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available commands."""
    if not _is_authorized(update):
        return

    await safe_send(
        update.message,
        "Claude Code Telegram Bridge\n\n"
        "Just type a message or send a voice note to talk to Claude Code.\n"
        "Each topic = separate conversation.\n\n"
        "Commands:\n"
        "/clear - Start fresh session\n"
        "/stop - Stop current response\n"
        "/topic <name> - Create a new topic\n"
        "/context - Token context usage\n"
        "/status - Session info\n"
        "/id - Show chat/group ID\n"
        "/help - Show commands",
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop the currently running Claude process in this topic."""
    if not _is_authorized(update):
        return

    topic_id = update.message.message_thread_id
    key = _topic_key(topic_id)
    proc = _running_processes.get(key)

    if proc and proc.returncode is None:
        _stopped_topics.add(key)
        _voice_discard.add(key)  # drop the stopped turn's parked voice
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await safe_send(update.message, "Stopped.")
    else:
        await safe_send(update.message, "Nothing running to stop.")


async def handle_stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the inline Stop button press."""
    query = update.callback_query

    # Auth check
    if not query.from_user or query.from_user.id not in ALLOWED_USERS:
        await query.answer("Not authorized.")
        return

    await query.answer()  # Acknowledge button press (removes loading state)

    data = query.data  # "stop:<topic_key>"
    key = data.split(":", 1)[1] if ":" in data else None
    if not key:
        return

    proc = _running_processes.get(key)
    if proc and proc.returncode is None:
        _stopped_topics.add(key)
        _voice_discard.add(key)  # drop the stopped turn's parked voice
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await query.edit_message_text("Stopped.")
        except TelegramError:
            pass
    else:
        try:
            await query.edit_message_text("Already finished.")
        except TelegramError:
            pass


async def cmd_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a new forum topic and set up a session for it."""
    if not _is_authorized(update):
        return

    if not context.args:
        await safe_send(update.message, "Usage: /topic <name>\nExample: /topic SEO Strategy")
        return

    topic_name = " ".join(context.args)

    try:
        forum_topic = await context.bot.create_forum_topic(
            chat_id=update.effective_chat.id,
            name=topic_name,
        )
    except TelegramError as e:
        await safe_send(update.message, f"Failed to create topic: {e}")
        return

    # Set up a session for the new topic
    get_or_create_session(forum_topic.message_thread_id)

    await safe_send(
        update.message,
        f"Topic \"{topic_name}\" created. Go send your first message there.",
    )


# ---------------------------------------------------------------------------
# Post-Init: Register Bot Command Menu
# ---------------------------------------------------------------------------

async def post_init(application: Application):
    """Register bot commands and start the trigger watcher."""
    await application.bot.set_my_commands([
        BotCommand("clear", "Reset current session"),
        BotCommand("stop", "Stop current response"),
        BotCommand("topic", "Create a new topic"),
        BotCommand("status", "Session info"),
        BotCommand("help", "Show commands"),
        BotCommand("context", "Token context usage"),
        BotCommand("id", "Show chat/group/topic IDs"),
    ])
    logger.info("Bot command menu registered")

    # Auth preflight: log how credentials resolve RIGHT NOW so future 401
    # incidents are diagnosable from one grep. Runs at every start, which
    # includes the 04:00 daily restart. Failures here are per-source: a bad
    # setup-token file just means the next source is used (never fatal).
    try:
        await _auth_preflight()
    except Exception as e:
        logger.warning("Auth preflight failed (non-fatal): %s", e)

    # Leftover holding folders from a crash or restart: move each aside into the
    # unsent folder in one rename (its notes are kept, never deleted), so an
    # orphaned voice tool finds no folder and sends its note live. A voice
    # tool's own batch folder (".jarvis-say-batch-*") belongs to that running
    # tool and is left alone; it removes it itself.
    if VOICE_DEFER_ROOT.is_dir():
        for leftover in list(VOICE_DEFER_ROOT.iterdir()):
            if leftover == VOICE_UNSENT_DIR or not leftover.is_dir() or leftover.name.startswith(".jarvis-say-batch"):
                continue
            if leftover.name.endswith(".discarded"):
                # Stop marker: keep it while a voice tool from that turn may still run.
                try:
                    if time.time() - leftover.stat().st_mtime > 3 * 3600:
                        shutil.rmtree(leftover, ignore_errors=True)
                except OSError:
                    pass
                continue
            _remove_empty_dirs(leftover)
            if leftover.exists():
                try:
                    VOICE_UNSENT_DIR.mkdir(parents=True, exist_ok=True)
                    leftover.rename(VOICE_UNSENT_DIR / leftover.name.replace(".flushing", ""))
                    logger.warning("Voice notes from before the restart kept in %s", VOICE_UNSENT_DIR)
                except OSError as e:
                    logger.error("Could not move leftover voice folder %s: %s", leftover, e)

    # Start the trigger watcher as a background task
    asyncio.create_task(_watch_triggers(application.bot))

    # Daily memory-recycle restart (defers if subprocess in-flight)
    asyncio.create_task(_daily_restart_scheduler())

    # Periodic cleanup of stale uploaded media (Telegram keeps the originals)
    asyncio.create_task(_periodic_upload_cleanup())


# ---------------------------------------------------------------------------
# Error Handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler. Shuts down on duplicate instance, logs everything else."""
    from telegram.error import Conflict

    if isinstance(context.error, Conflict):
        logger.critical(
            "Another bot instance is running with the same token. "
            "Exiting with code 1 so launchd will restart us."
        )
        # Exit non-zero so launchd KeepAlive restarts us.
        # ThrottleInterval (10s) prevents a tight restart loop.
        os._exit(1)

    if isinstance(context.error, NetworkError):
        if isinstance(update, Update):
            # Raised inside a handler: python-telegram-bot does NOT retry these,
            # the incoming message is gone (media handlers retry themselves).
            logger.error(
                "Network error while handling update %s (NOT retried, message dropped): %s",
                getattr(update, "update_id", "?"), context.error,
            )
        else:
            logger.warning("Network error in polling (will retry automatically): %s", context.error)
        return

    logger.error("Unhandled error: %s", context.error, exc_info=context.error)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    _ensure_state_dir()

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("topic", cmd_topic))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("context", cmd_context))
    app.add_handler(CommandHandler("id", cmd_id))

    # Inline button handler (Stop button)
    app.add_handler(CallbackQueryHandler(handle_stop_callback, pattern=r"^stop:"))

    # Message handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Global error handler
    app.add_error_handler(error_handler)

    logger.info("Claude Code Telegram Bridge starting...")
    logger.info("Working directory: %s", WORKING_DIR)
    logger.info("Allowed users: %s", ALLOWED_USERS)
    logger.info("Whisper provider: %s", WHISPER_PROVIDER or "disabled")
    logger.info("Claude timeout: %ds", CLAUDE_TIMEOUT)
    logger.info("Claude model: %s (per-topic overrides: %s)", CLAUDE_MODEL or "default", TOPIC_MODELS or "none")
    logger.info("Claude effort: %s (per-topic overrides: %s)", BRIDGE_EFFORT or "cli-default", TOPIC_EFFORTS or "none")
    logger.info("Model fallback ladder (General topic): %s", " -> ".join(m or "default" for m in _model_chain("general")))

    # bootstrap_retries=-1: retry Telegram bootstrap forever with backoff.
    # The default (0) makes a transient DNS blip a hard exit -> launchd
    # crash-loop that can rotate away the entire log history.
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
