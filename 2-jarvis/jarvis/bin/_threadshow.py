#!/usr/bin/python3
"""jarvis-thread-show <thread_id> [N=3]

Render the last N committed turns of a thread (from the committed turn log, the
single authority). If the rendered text exceeds ~3500 chars, write it to
state/outputs/thread-<id>-show.md and print THAT path instead, so the conductor
sends it as a FILE rather than blowing past Telegram's ~4096-char limit.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _state          # noqa: E402
import _threadlib      # noqa: E402

SHOW_INLINE_CAP = 3500


def main():
    if len(sys.argv) < 2:
        print("usage: jarvis-thread-show <thread_id> [N]")
        return 1
    tid = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    t = _threadlib.get_thread(tid)
    if not t:
        # long-closed rows get moved out of the hot file by the nightly prune
        arch = _state._load(os.path.join(_state.STATE, "threads-archive.json"),
                            {"threads": {}}).get("threads", {}).get(tid)
        if arch:
            print(f"Thread {tid} is ARCHIVED (status {arch.get('status')}, "
                  f"closed {arch.get('done_at')}).\n"
                  f"Latest: {arch.get('last_status_line', '')}\n"
                  f"Full row: state/threads-archive.json | transcript: "
                  f"{arch.get('transcript_path')}")
            return 0
        print(f"ERROR: no such thread {tid}")
        return 2

    turns = t.get("turns", [])[-n:]
    body = (f"Thread {tid}: {t.get('name', '')}\n"
            f"Status: {t.get('status')} | turns: {t.get('turns_count')} | "
            f"tokens: {t.get('tokens_total')}\n"
            f"Task: {t.get('task', '')}\n")
    for turn in turns:
        body += (f"\n--- Turn {turn.get('n')} [{turn.get('status')}] {turn.get('ts')} ---\n"
                 f"{turn.get('text', '')}\n")
    if t.get("deliverable"):
        body += f"\nDeliverable (send as file): {t['deliverable']}\n"
    if t.get("last_status_line"):
        body += f"\nLatest: {t['last_status_line']}\n"

    if len(body) > SHOW_INLINE_CAP:
        outdir = os.path.join(_state.STATE, "outputs")
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, f"thread-{tid}-show.md")
        _state._save_text(out, body)
        print(out)              # a FILE path, never the inline wall of text
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
