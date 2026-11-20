# Jarvis: a voice-first Claude assistant on Telegram

Send Jarvis a voice note while walking or driving. It answers with a voice note plus a text, runs bigger jobs in the background, pings you when they finish, gives you a morning brief and keeps its own memory journal. It runs on your own Mac through [Claude Code](https://claude.com/claude-code), and you install it by talking to Claude. No coding needed.

## How to install

You need a Mac with [Claude Code](https://claude.com/claude-code) installed, a Claude subscription and a Telegram account.

1. Open the Terminal app and start Claude Code: type `claude` and press Enter.
2. Paste this and press Enter:

```
Clone https://github.com/serdarscales/jarvis into ~/jarvis, then read START-HERE.md and set up Jarvis with me step by step.
```

Claude does the rest with you, one step at a time (about an hour in total). Prefer to download by hand? Click the green **Code** button above, then **Download ZIP**, unzip it, open a terminal in that folder, start `claude` and say: "Read START-HERE.md and set up Jarvis with me step by step."

## Good to know

- Jarvis runs on your own Mac, but it talks to online services: always Claude (Anthropic) and Telegram. Anything else is your choice during setup, for example ElevenLabs for a natural-sounding voice. The free options (the built-in Mac voice, free local transcription) cost nothing on top of your Claude subscription. [START-HERE.md](START-HERE.md) lists every service it uses.
- Both parts have a Removal section at the end of their README, so you can undo everything.
- Shared as it is, without support or warranty (see [LICENSE](LICENSE)). Read the "Read This First" notes in each part before you install it.
