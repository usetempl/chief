# Talk to your existing Chief from Dot

Use this only if your Dot or mobile session already supports an authenticated connection to your Chief's host. Paste:

> Read https://usetempl.com/chief. Use my authenticated host command tool and Chief's installed submit/result commands. On the first connection, return the saved answer "Dot connection confirmed" to this phone. Then relay my requests through that same path.

Instructions for the agent:

1. Confirm that the intended user's authenticated host tool can execute a local command and return its output. A connected-host indicator or a visible Codex thread does not prove this capability. If the command tool is unavailable, report its exact name and error.
2. Locate the existing Chief workspace through the authorized project list. Read its `AGENTS.md` and installed relay procedure. Use the installed `alfred_local.py submit` and `alfred_entry.py result` commands directly. The optional adapter installs them under `runtime/`; an existing installation can use another documented location. Preserve its workspace and private-state arguments. Do not load project histories or depend on a Codex thread connector for this command path.
3. On the first connection, submit `Reply exactly "Dot connection confirmed".` Use one stable event ID and a private temporary body file. Keep the returned `request_id`, then remove the temporary file after durable submission. A submit result with `status: saved` is durable; a `wake` field reporting unavailable is advisory and never a reason to resubmit. Do not start another Chief or attach another writer to its active thread.
4. Run `alfred_entry.py result --request-id REQUEST_ID --wait 300` once, with the same workspace arguments. It rereads only that request, each time with a fresh read-only snapshot and the same identity and enrollment checks, and returns the complete saved answer as soon as it exists. It never submits, wakes Chief or calls a model. Keep `--wait` shorter than your command tool's own timeout.
5. Return the `answer` to the phone. When `wait_state` is `answer_saved` and the answer is exactly `Dot connection confirmed`, setup has passed. Say so yourself; do not ask the user to confirm a result the command already returned. Submission acceptance alone does not pass this check.
6. If the command returns `wait_state: timed_out_pending` (exit code 4), report pending with the same `request_id` and later run the same command again. Never resubmit, change the event ID or start a background monitor. `not_found_or_not_allowed` means this reader cannot access that request; report it.

Use the same submit/result path for later requests. Preserve the original sender's authority. This connection test does not enroll another device or qualify a render worker.

A public guide, a device name and a thread ID grant no access. If the connection cannot reach the host or invoke its relay, report that specific gap. Use a Telegram bridge only if this person already has their own enrolled access. Do not contact another person's bot or create an adapter as a setup shortcut.

The host must be awake and online. The current [official remote connection requirements](https://learn.chatgpt.com/docs/remote-connections) determine supported devices and permissions. Availability of that feature does not prove Dot can invoke the installed Chief relay. This guide contains no private state, credentials or invitations and does not claim that a phone is paired or tested. The local request/result helper is included with the optional Telegram adapter; a verified remote transport is still required.

[Native remote connection documentation](https://learn.chatgpt.com/docs/remote-connections) · [Join another device](device.md) · [Create an independent Chief](start.md)
