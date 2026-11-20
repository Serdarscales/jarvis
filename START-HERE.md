# Jarvis: your voice-first Claude assistant on Telegram

> **Easiest path:** open a terminal in this folder on your Mac (after cloning or unzipping it), start Claude Code (`claude`), and say: **"Read START-HERE.md and set up Jarvis with me step by step."** Claude explains everything, asks you a few questions, and installs it with you one step at a time.

What you end up with: a private Telegram group on your phone where Claude Code lives. One topic in it is **Jarvis**: you send it a voice note while walking or driving, it answers with a voice note plus a text, hands bigger jobs to background agents, keeps track of everything that is open, pings you when work finishes, gives you a morning brief at 8 and writes its own memory journal every night.

The kit has two parts. Install them in this order:

| Part | Folder | What you get | Needs |
|---|---|---|---|
| 1. Telegram bridge | `1-telegram-bridge/` | Claude Code in a private Telegram group. Every topic is its own ongoing Claude Code conversation running on your Mac. Text, voice notes, photos and files all work. | A Mac, a Claude subscription (Pro or Max), Claude Code, a Telegram account. About 30 to 45 minutes. |
| 2. Jarvis | `2-jarvis/` | One topic becomes the voice-first assistant described above. | Part 1 working. Optional: an ElevenLabs account for a natural voice (the free Mac voice works too). About 30 to 45 minutes. |

## Order

1. Install **part 1** and make sure the bot answers you in Telegram. Do not start part 2 before that works.
2. Then add **part 2**. It adds a small, reversible patch to the bridge (with a backup first).

## For Claude

> You are helping the person in front of you install this kit. They may be new to the terminal: explain each step in plain words, one step at a time. Check whether part 1 is already installed (`launchctl list | grep claude-telegram`). Then open `1-telegram-bridge/README.md` and follow its steps exactly; only when the bot answers in Telegram, open `2-jarvis/README.md` and follow it. Finish and verify each step before starting the next. Never print tokens, passwords or API keys back to the screen.

## Good to know

- Everything runs on your own Mac, and the Mac has to be on (asleep is fine for short stretches; scheduled jobs catch up on wake). Data goes to Telegram and Anthropic (Claude), plus only the services you pick during setup: OpenAI or Groq if you choose cloud voice transcription instead of the free local one, and ElevenLabs or OpenAI if you choose a cloud voice for Jarvis.
- Cost: nothing extra on top of your Claude subscription with the free options (local transcription, Mac voice). Heavy use, especially Jarvis's background workers on Opus, eats into your Claude plan's usage limits. A cloud voice is its own subscription.
- The bridge runs Claude without permission prompts so it can work while you are away. Whoever can message the bot can act on your Mac, so the bot only answers your own Telegram user id. Keep the group private.
- Both parts have a Removal section at the end of their README (full undo). Remove part 2 before part 1.
