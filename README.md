# Chief

Chief by [Templ](https://usetempl.com) coordinates your existing project leads from one conversation in Codex or Claude Code. Each project keeps its owner. One private Markdown board records the assignments, open decisions and verified results.

## 🚀 Get started

1. **Open Chief on your main computer.** Open your existing Chief conversation in your signed-in app. For a first installation, open a conversation with file access and send this prompt.

   ```text
   Read https://github.com/usetempl/chief/blob/main/docs/start.md and set up Chief in a separate private workspace on this computer.
   ```

2. **Ask for work in that conversation.** Give Chief a goal and any release limits. Chief can send the brief to the existing lead through a supported native tool and bring the result back to you.

3. **Continue from another device.** Use a supported, authenticated connection to the host. Open the same conversation and verify that a message sent from the device gets an answer there. [Device setup](docs/device.md)

4. **Read your board.** Ask Chief for `/report` to list every open card or `/done 2` for the second page of completions. You can also read the board locally with Python. The report script makes no model or network calls. [Board commands](docs/core.md#read-the-board)

## ⚙️ How it works

| Where | What it holds |
| --- | --- |
| Chief conversation | Your request, one brief for the appropriate lead, and the result or decision you need. |
| Project owner's conversation | Implementation, tools, checks and release evidence for that project. |
| Private workspace | Project owners in `projects.md`, release and access rules in `priorities.md`, and one file per task in `tasks/`. The folder records its status. |

The signed-in app supplies the model and enforces permissions. The board works with ordinary file tools. Optional Python helpers prepare a new workspace, report on cards and record moves.

[Setup guide](docs/start.md) · [How work moves through Chief](docs/architecture.md) · [Full guide](docs/entry.md)

## 🔖 Versions

Each source release has a Git tag, a matching `VERSION` and a [changelog](CHANGELOG.md). If you use an earlier release, follow the [upgrade instructions](docs/start.md#upgrade-an-existing-chief) to keep your private board and project owners.

MIT licensed.
