#!/bin/bash
# _env.sh -- sourced by every Jarvis shell script (bin/ and cron/).
# Resolves JARVIS_HOME from this file's location, loads jarvis.env, and fills
# defaults. Values already in the environment win over jarvis.env (tests and
# one-off overrides rely on that).

JARVIS_HOME="${JARVIS_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

if [ -f "$JARVIS_HOME/jarvis.env" ]; then
  while IFS= read -r _line || [ -n "$_line" ]; do
    _line="${_line#"${_line%%[![:space:]]*}"}"          # trim leading space
    case "$_line" in ''|'#'*) continue ;; esac
    case "$_line" in *=*) ;; *) continue ;; esac
    _k="${_line%%=*}"; _k="${_k#export }"; _k="${_k//[[:space:]]/}"
    _v="${_line#*=}"
    _v="${_v%\"}"; _v="${_v#\"}"; _v="${_v%\'}"; _v="${_v#\'}"
    [ -z "$_v" ] && continue                             # KEY= never masks a default
    if [ -z "${!_k:-}" ]; then export "$_k=$_v"; fi
  done < "$JARVIS_HOME/jarvis.env"
  unset _line _k _v
fi

BRIDGE_DIR="${BRIDGE_DIR:-$HOME/.claude/telegram-bridge}"
BRIDGE_DIR="${BRIDGE_DIR/#\~/$HOME}"
JARVIS_WORKSPACE="${JARVIS_WORKSPACE:-$HOME}"
JARVIS_WORKSPACE="${JARVIS_WORKSPACE/#\~/$HOME}"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo "$HOME/.local/bin/claude")}"
JARVIS_OWNER="${JARVIS_OWNER:-the user}"
JARVIS_MODEL="${JARVIS_MODEL:-opus[1m]}"
PY="${JARVIS_PYTHON:-/usr/bin/python3}"
JSTATE="${JARVIS_STATE_DIR:-$JARVIS_HOME/state}"
# isolated tests (JARVIS_STATE_DIR set) never write into the live bridge
if [ -n "${JARVIS_STATE_DIR:-}" ]; then TRIGGER_DIR="$JARVIS_STATE_DIR/triggers"; else TRIGGER_DIR="$BRIDGE_DIR/state/triggers"; fi
BRIDGE_ENV="$BRIDGE_DIR/.env"
# "|| true": callers run under set -eo pipefail, and a missing ffmpeg must not
# end the whole script here (the self-test reports it instead).
FFMPEG="${FFMPEG:-$(command -v ffmpeg || ls /opt/homebrew/bin/ffmpeg /usr/local/bin/ffmpeg 2>/dev/null | head -1 || true)}"

export JARVIS_HOME BRIDGE_DIR JARVIS_WORKSPACE CLAUDE_BIN JARVIS_OWNER JARVIS_MODEL JSTATE

# require_topic: every script that posts or triggers needs the Jarvis topic id.
require_topic() {
  if [ -z "${JARVIS_TOPIC_ID:-}" ]; then
    echo "jarvis: JARVIS_TOPIC_ID is not set in $JARVIS_HOME/jarvis.env" >&2
    exit 2
  fi
}

# bridge_cred <NAME>: read one value from the bridge .env without echoing it.
bridge_cred() {
  grep -E "^$1=" "$BRIDGE_ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"' "
}

ts() { date '+%Y-%m-%d %H:%M:%S'; }
