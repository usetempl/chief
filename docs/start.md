# Set up Chief

Use your signed-in Codex or Claude Code on the computer that holds your private board. If you already have a Chief conversation, open it in that workspace and follow the upgrade instructions below.

## Create a private board

1. Obtain the [public source](https://github.com/usetempl/chief) through your existing file or Git tools. If Git is available, `git clone https://github.com/usetempl/chief.git chief-source` creates a fresh source checkout. You can also read the guides directly in the app.
2. Create a separate, private workspace. Copy the [core instructions](core.md) to `COORDINATOR.md`. In `AGENTS.md` for Codex or `CLAUDE.md` for Claude Code, tell the app to read those instructions during onboarding and return to them when a rule is in question. Create `projects.md`, `priorities.md` and six folders under `tasks/` named `backlog`, `todo`, `doing`, `blocked`, `review` and `done`.
3. Offer the user a nickname. Ask separately before inspecting project conversation history. With consent, read the minimum needed to identify projects and existing owners. Otherwise, use the context the user supplies. List owners and priorities for the user to accept or correct. Mark any gaps.
4. Open or continue one Chief conversation in that workspace. Save the accepted inventory and carry out one small, authorized request. Record the result on its card and return it to the user in the same conversation. If a native tool cannot reach an owner, identify the missing access and keep the assignment with that owner.

If Python 3.9 or later is available, this optional command prepares the files in step 2 from a source checkout.

```sh
python3 onboard.py --directory ../chief-private --driver codex --history no --no-start
```

For Claude Code, use `--driver claude`. The helper requires an unused destination. It creates the private board without reading conversation history or starting a model. Open that workspace in the app and ask Chief to read `COORDINATOR.md` to continue.

## Upgrade an existing Chief

Version 0.8.0 removes the Telegram service and its local relay commands. The same Markdown board and owner records work in the native conversation.

1. Keep a private backup of the existing workspace, including any old service records you need. Review pending actions and unconfirmed deliveries before moving a request to the new conversation. An uncertain delivery still needs evidence before a retry.
2. If the old service is running, arrange a handover with its operator. Let active work finish, then stop its collector and disable its automatic startup using the service manager that runs it. This releases its hold on the Chief conversation. Updating this source checkout alone does not stop an installed service.
3. Open the existing conversation and workspace in the signed-in app. In its private operating instructions, replace directions for the retired service with instructions to read the original cards, send briefs using native owner tools, and answer in this conversation. Preserve the workspace’s own permissions, project owners and release rules. Prior enrollment alone does not grant access through the app.
4. Ask for the open board and check a known card and its owner. For a phone or another computer, verify a round trip through the [device guide](device.md). Keep old records private for any outstanding reconciliation.

[Core instructions](core.md) · [Using a phone](dot.md)
