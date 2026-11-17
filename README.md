# Chief

Your AI chief of staff, made by [Templ](https://usetempl.com).

Coordinate your existing AI leads across projects and track their work on one private board.

## 🚀 Get started

1. **Install on your main computer**

   Open your signed-in Codex and paste:

   ```text
   Read https://usetempl.com/chief. Set up Chief on this computer.
   ```

   Codex helps you choose a nickname and create your first private board.

2. **Reach Chief from your phone through Dot**

   In Dot, paste:

   ```text
   Read https://usetempl.com/chief. Connect Dot to my existing Chief. I want to operate my host computer from my phone.
   ```

   Dot needs a supported, authenticated connection. Your host must stay awake and online.

3. **Add devices and assign work**

   Open Codex on the new device and paste:

   ```text
   Read https://usetempl.com/chief. Connect this device to my existing Chief. Check its tools and ask which work it should handle.
   ```

   Then tell Chief: “Use my desktop for renders. Run the website project on my laptop.” Chief keeps each project's lead and uses verified device access.

4. **Check your board in Telegram**

   After [connecting your own bot](docs/telegram.md), send:

   ```text
   /report
   ```

   Get the saved board and recent completions without a model call or AI token use.

## ⚙️ How it works

The core uses Markdown instructions and records. Your Codex model decides where work goes. Chief adds no planning engine, vector database or model server.

| Layer | What runs |
| --- | --- |
| **Coordinator** | One Codex thread reads instructions, `projects.md` and relevant cards, then answers or chooses an existing lead. |
| **Project leads** | Each project keeps its thread, repository and tools. Chief sends a focused brief. The lead returns a result or blocker. |
| **Board** | `tasks/<column>/*.md` holds each task. **Backlog** holds ideas, **Todo** committed work, **Doing** active work, **Review** human decisions, and **Done** verified outcomes. Moves retain timestamps. |
| **Memory** | Codex handles context and compaction. Markdown keeps durable facts. Project owners keep detailed evidence. Private Git backups are optional. Chief reads relevant records. |
| **Telegram · optional** | Python 3.12+, its standard library and pinned `websockets` connect to Codex's app-server. Enrollment controls access. SQLite holds requests, the outbox and delivery receipts. |
| **Reports & replies** | The adapter's `/report` reads saved cards without model calls. Replies split at Telegram's message limit and record each part's delivery. |

![Chief by Templ: request flow, project leads, board and memory](docs/architecture.png)

[Technical details](docs/architecture.md) · [Add Telegram](docs/telegram.md) · [Connect another device](docs/device.md)

## 🔖 Versions

This repo is Chief's source of truth. Reusable improvements ship here, with `vX.Y.Z` Git tags, a matching `VERSION` and a [changelog](CHANGELOG.md). Updating source does not overwrite an existing installation.

MIT licensed. Your private board, accounts and history stay outside this repo.
