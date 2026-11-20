# Claude Code skills

Free, ready-to-install setups for [Claude Code](https://claude.com/claude-code). Each folder is one kit: you hand it to Claude Code and it installs it with you, step by step, asking you a few questions along the way. No coding needed.

| Kit | What you get |
|---|---|
| [`jarvis/`](jarvis/) | **Jarvis:** a voice-first assistant on Telegram. Send it a voice note while walking or driving, it answers with a voice note plus a text, runs bigger jobs in the background, pings you when they finish, gives you a morning brief and keeps its own memory journal. Runs on your own Mac. |

## How to install a kit

You need a Mac with [Claude Code](https://claude.com/claude-code) installed and a Claude subscription.

1. Open the Terminal app and start Claude Code: type `claude` and press Enter.
2. Paste this and press Enter:

```
Clone https://github.com/serdar-goku/claude-skills into ~/claude-skills, then read jarvis/START-HERE.md and set up Jarvis with me step by step.
```

Claude does the rest with you. Prefer to download by hand? Click the green **Code** button above, then **Download ZIP**, unzip it, open a terminal in the `jarvis` folder, start `claude` and say: "Read START-HERE.md and set up Jarvis with me step by step."

## Good to know

- The kits run on your own Mac, but they talk to online services: always Claude (Anthropic) and, for Jarvis, Telegram. Anything else is your choice during setup, for example ElevenLabs for a natural-sounding voice. The free options (the built-in Mac voice, free local transcription) cost nothing on top of your Claude subscription. Each kit's README lists every service it uses.
- Every kit has a Removal section at the end of its README, so you can undo it completely.
- These kits are shared as they are, without support or warranty (see [LICENSE](LICENSE)). Read a kit's "Read This First" notes before you install it.
