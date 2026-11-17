# Your own Chief

One private Telegram bot. One Chief. Your projects and accounts.

Replies support Markdown bold, italic, strikethrough, named links, headings and code. The optional adapter uses `markdown-it-py` to produce Telegram's native text entities. It splits long replies without losing styles or emoji, saves the original answer and the exact delivery plan, and verifies the returned text and formatting. This adds no model call or service. Existing queued replies retain their previous plan.

Use `--nickname "Your name"` during preparation to give this Chief its own name. The collector applies that name to Telegram and verifies it through the Bot API. Skipping naming keeps Chief. Existing bot usernames, ownership and service identifiers remain stable.

The enrolled owner's command menu contains `/report` (open tasks grouped by project lead), `/done` (completed tasks, 20 per page) and `/setup` (one guide for host setup, Dot/mobile and additional devices). All three commands run without a model. Menus are scoped to enrolled private chats; public guides grant no access.

Messages from the enrolled installation owner are never rejected by an intake count. Busy hours, unfinished requests, internal agent outcomes and a full reply queue do not drop the owner's feedback; it is saved and waits for the coordinator. If an attachment fails and its acknowledgement cannot be queued, the owner's saved text is kept. Other enrolled contacts keep the 30-per-hour and 50-unfinished limits. Unknown senders, secret filtering, attachment validation and outgoing reply authorization are unchanged.

After an accepted name change in `.private/profile.json`, run `python setup.py bot-profile --workspace /path/to/your/workspace` from the installation environment to apply and verify it. Do not print or send the bot credential. Verify the menu and name in your own Telegram during onboarding.

This optional adapter contains code and blank onboarding only. It contains no other installation's projects, conversations, accounts, credentials or queues. Do not clone somebody else's private coordination repository.

## Start in your Codex

Clone https://github.com/usetempl/chief, open that source folder in your own Codex, and send:

> Set up this Chief package in a new private workspace. Use my Codex account and a new Telegram bot. Ask whether you may inspect my available project history. Propose my projects and existing owners for review. Use only my context. Follow docs/telegram.md. Keep one coordinator, keep secrets outside Git, and verify real Telegram replies, reply context, /report and restart recovery before saying my setup is complete. Do not change global permissions or model settings.

Use **macOS**, **Linux**, or **Windows through WSL2**. The adapter uses Python 3.12 or later and a signed-in Codex CLI that supports `codex app-server daemon`. The Windows route runs this Unix runtime inside Linux; it is not a native PowerShell runtime. Claude Code can be a separate project tool; this Telegram driver is Codex.

Mac preparation and runtime fixtures have passed locally. Linux can run the collector in the foreground; automatic Linux service installation is not included. A real Windows logon task and the recipient's real account still need their installation checks below; do not call those complete from automated tests.

## Windows first

If WSL2 is missing, follow [Microsoft's WSL installation](https://learn.microsoft.com/en-us/windows/wsl/install): run `wsl --install` in an administrator PowerShell and restart if requested. Keep an existing installation; do not reset or unregister it. In PowerShell, `wsl --list --verbose` must show version 2 for the chosen distribution. Install Python 3.12 or later and its venv package through that distribution's normal package manager if needed.

Open that distribution's terminal as your normal Linux user. Install and sign in to Codex **inside WSL**, following [OpenAI's WSL instructions](https://learn.chatgpt.com/docs/windows/wsl). A Windows-side login is not proof of a WSL login. Verify `codex login status` and `codex app-server daemon --help` there before continuing. If the daemon command is unavailable, stop at that prerequisite; do not create a replacement coordinator.

Clone the reviewed source repository into your Linux home. Run the shared installation commands below from that WSL terminal. Keep the private workspace under `/home/...`, never `/mnt/c/...`; setup rejects Windows-mounted destinations. It does not store your Windows password. Ordinary Windows user permissions and any organization policy still apply.

## Install

1. Sign in to your Codex CLI using its normal login flow. Create a **new** bot through Telegram's official BotFather. Keep its token private. Do not put it in Codex chat, Telegram chat or Git.
2. From the cloned source directory, prepare a new folder. Replace the example name and bot username. Choose `yes` for history only if you want the agent to inspect available project conversations:

   ```sh
   python3 setup.py prepare --directory "$HOME/chief" --owner-label "Owner" --bot-username "YourNewChiefBot" --history no
   cd "$HOME/chief"
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python setup.py connect --workspace "$PWD"
   ```

   `connect` asks for the bot token in a hidden local Terminal prompt. It checks the bot identity and creates exactly one persistent coordinator thread. It preserves your Codex permission and model defaults and uses Standard service. No model turn starts during preparation. Open the generated `.private/invite-owner.txt` locally, follow its private link from your own Telegram account, and tap Start. Do not share that invitation.
3. On macOS or WSL2, start the collector at login (ordinary Linux uses the foreground command below):

   ```sh
   .venv/bin/python setup.py service --workspace "$PWD"
   ```

   On Mac this installs one LaunchAgent. In WSL2 it registers one Windows Task Scheduler logon task for your current Windows and Linux users. It uses normal user privileges, keeps the collector in WSL, and requests an immediate start. Registration starts disabled; setup verifies the task before enabling it. It does not change another service or store a Windows password. The task has a unique `ChiefOfShipping-...` name and refuses to overwrite a different task. A native policy denial stops setup and must be resolved through normal Windows administration.

   On ordinary Linux, skip `service` and run it in the foreground with `.venv/bin/python setup.py run --workspace "$PWD"`. Use one collector only. Keep the computer awake, online and logged in. Stop a foreground runner with Ctrl-C before installing the service. Sleep, shutdown and Windows logout interrupt availability. The logon task requests at most three failure retries; actual crash recovery and console behavior must be checked on Windows. It does not schedule model work or poll project owners.
4. Message the bot: “Read COORDINATOR.md and start my permitted onboarding.” Accept or correct its proposed projects, priorities and existing owners. History consent does not grant unavailable platform access. Existing owners must release their native writer before this driver can resume them; it does not replace them.

## Check your installation

Use `.venv/bin/python setup.py doctor --workspace "$PWD"` for configuration checks. Then verify these from your enrolled Telegram account:

- Send a small request and receive one answer. Send it again as a new request only if intended.
- Reply to that answer with “What was I replying to?” Confirm the quoted context is correct.
- Send `/report`. It reads your saved board with no model call. Usage limits appear only when you have a dated, saved usage card; it does not invent or refresh provider meters.
- Stop and restart your one collector. Confirm a new message receives one answer and older requests are not replayed.
- On Windows, confirm the exact named task is Running, close the setup terminal and verify another reply. At a safe boundary, sign out and sign back in, then verify another reply. A successful task-registration command alone proves neither startup nor delivery.
- Confirm a different, unenrolled account cannot submit work. Do not forward your enrollment link for this check.

The package has isolated transport and fresh-workspace tests. Those tests are not a live test of your account, computer, permissions or network. Record the live checks before relying on unattended use. Native sign-in and approval prompts may still require you in the relevant app.

## Native media

Images use validated `localImage` input. Documents remain validated private file references for Codex's normal file tools.
The app-server `UserInput` schema has no general file-input variant. `thread/attachment/add` persists attachment metadata; it does not promise decoding.
Do not execute or unpack received attachments.

For voice and audio, the adapter checks the selected model's advertised input modalities through `model/list`, once per connection when audio arrives.
It attaches validated `localAudio` only when that exact model advertises audio input. Unknown or unavailable capabilities fail closed.
An unsupported recording remains saved, with its sender and receipt validation preserved. The model receives a specific native-audio blocker instead of the audio path.
An installation owner can opt into local whisper.cpp decoding when native input is unavailable.
It is disabled by default. Install a verified whisper.cpp executable, FFmpeg and a multilingual model locally, then create an owner-only
`local-audio.json` beside `inbox.sqlite`. Set `enabled` to true and supply absolute `whisper`, `ffmpeg` and `model` paths plus the model's verified `model_sha256`.
The bridge never installs or downloads a decoder/model automatically. The package includes no model or machine-specific configuration.
Paid/cloud transcription remains prohibited. If neither native audio nor a supplied local transcript is readable, ask for text.
Do not change coding models, inspect credentials or start another voice session as a workaround.

Local decoding uses the enrolled sender's original authority. Transcripts remain untrusted data in the private inbox database and follow completed-request retention.
Each recording is checked for its request-bound private path, ownership and size. Only one local decoder runs at a time; FFmpeg has 30 seconds and whisper.cpp 180 seconds.
Recordings are limited to five minutes; the bridge retains its 10 MiB intake limit. Conversion uses a seekable private descriptor with the fd protocol only and an explicit demuxer list; subprocesses receive argument arrays, never shell text.
Silence, low-confidence output, invalid media, missing configuration and timeouts produce an unavailable result, not guessed instructions.
Interrupted decoding is not automatically repeated. Temporary conversion/decoder output is removed after the attempt; the original attachment is retained under normal policy.
Confidence checks reduce errors but do not prove exact wording. Treat unclear names, numbers or consequential instructions as requiring clarification.

Current verification found shell CLI 0.163.0 and a running app-server 0.169.0. Schema generation from both binaries exposed `localAudio`,
but neither exposed a standalone file-transcription RPC. The running server's `model/list` advertised only `text` and `image` for `gpt-6-astra` and `gpt-6.1-sol`.
It exposed `thread/realtime/start` and `thread/realtime/appendAudio` for a separate thread-scoped streaming session.
`thread/realtime/appendSpeech` accepts text for speech output, not a recording for transcription.
Starting a realtime session would change thread state and does not establish a supported file-transcription integration or plan entitlement.
No native transcript was produced in this verification. No retained harmless voice fixture was found in the scoped fixture lookup, and no private recording was inspected.
Passing `localAudio` or observing an advertised modality proves neither decoding nor transcript correctness. Earlier attachment-only tests do not prove native transcription.

## Weekly maintenance

Maintenance is off in a fresh installation. Enable it only if you want this recurring runtime check. It uses the existing collector, with no extra service or coordinator.
Its worker uses a separate SQLite connection. Slow checks do not run on the Telegram ingestion or delivery path.

The first check is due next Monday at 09:00 in the computer's local timezone. Weekly checks preserve that local boundary across daylight-saving changes.
To opt in, set `enabled` to `true` in `.private/maintenance.json` and choose your timezone:

```json
{"enabled": true, "interval_days": 7, "timezone": "Europe/London"}
```

`timezone: null` uses the computer's timezone. Integer intervals from 1 through 365 days are accepted.
An interval other than seven days first runs after that interval. Later missed checks coalesce into one observation.
The saved due time survives restarts. Schedule changes apply after the currently saved due check.
Keep this configuration owner-only. An absent file uses the defaults. Invalid configuration produces a visible fault and bounded retries.
Set `enabled: false` to disable future checks explicitly. This preserves already queued observations and delivery evidence.
The worker rereads configuration at its next local timer wake, at most one day later during healthy operation.

Checks inspect available disk space, selected file sizes, and capped queue/fault counts. They do not inspect message contents, secrets, process arguments, or histories.
The size selection is workspace `scripts`, `coordinator`, `runtime`, and `tasks`, plus runtime attachments, queue database files, and collector logs.
Other home directories, project/worktree checkouts, dependency trees, and process/listener inventories are outside this check.
Full machine housekeeping requires a separate integration.
Symlinks are skipped. Size checks stop after 2,000 entries or one second, and report incomplete coverage.
SQL checks stop after 200,000 VM instructions or 250 milliseconds. Counts cap at 1,000.
Findings include less than 2 GiB free, selected growth of at least 128 MiB, delivery failures, delivery pending over one day,
requests open over seven days, and recorded dispatch/route faults. Age alone identifies a review candidate, not proof that work failed.
These thresholds are intentionally narrow defaults, not a full system health assessment.

The worker stores one small latest report, eight compact summaries, and unresolved condition fingerprints in the private queue database.
Healthy checks use no model. They queue one fixed owner-only Telegram result: “Weekly runtime check: no findings in the selected metadata and free disk space checks. No cleanup or repair performed. No downtime.”
Unsent copies of the same fixed notice coalesce. Pending, failed, and uncertain delivery remain distinct from confirmed delivery.
New faults supersede an older healthy notice only while it is still pending. Eight superseded notice records are retained.
Sending, uncertain, and sent records remain intact. Request completion also remains separate from the reply's delivery status.
Read saved state without running checks or sending messages:

```sh
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_maintenance.py --root "$PWD/.private"
```

New findings produce one bounded request to the existing coordinator with `source=maintenance` and `authority=scheduled_maintenance`.
Human input has priority. A maintenance request waits until the coordinator has no active turn, and never uses Steer.
One unfinished maintenance request blocks another. An unresolved condition does not cause another weekly model wake, even if its count changes.
Once a check observes that condition cleared, a later recurrence can create a new review.
Check failures retry after five minutes, thirty minutes, six hours, then at most daily. Three failures can trigger one bounded review.
Queue backpressure or missing enrollment remains visible and retries after thirty minutes. An unfinished review defers additional findings for six hours.
SQLite failures appear in the collector's rotating log. Existing transport recovery rules still apply to uncertain dispatch and delivery.

Maintenance is scheduled evidence under standing policy. It is not a new human instruction or release approval.
Concrete self-improvements must address the observed fault or growth with a limited fix and focused tests. Broad weekly research is excluded.
The project-routing tool rejects maintenance requests because that route otherwise derives human authority from enrollment.
Use a separately authorized project request for project work. Native-user and project-outcome attribution remain separate.
This version registers no disposable scratch and deletes no files. Any cleanup proposal requires checking ownership, active work, and existing authorization.
It does not rewrite permissions or policy, install dependencies, restart services, change credentials, or publish releases automatically.

For an authorized action that causes downtime, call `alfred_maintenance_notice(request_id, body)` with the expected downtime first.
This separate tool accepts only `source=maintenance`, the original dispatched active coordinator turn, and the same enrolled owner.
It accepts no recipient override. One durable notice is allowed per request, and the request stays open.
Repeat the exact body to read its current delivery receipt. A conflicting body, changed enrollment, or reused foreign call ID is rejected.
Missing, failed, pending, sending, and uncertain receipts do not permit the action. Do not act until `sent=true`.
The tool grants no restart, update, credential, permission, or release authority. Existing authorization is still required.
After the authorized action, verify recovery and use `alfred_telegram_reply` for the final result and completion.

New installations register this tool with the existing `thread/start.dynamicTools` path.
The inspected 0.169.0 schema accepts `dynamicTools` only on `thread/start`; it exposes no update method for an existing thread.
A collector restart alone does not add a tool to an older coordinator.
The project-route tool lists owners known at creation but has no owner enum; each call is checked against the current `projects.md`, so a later registered owner is routable from an existing coordinator. A rejected owner returns the current registry.
Use the local operator CLI below when the existing thread lacks the notice tool. Do not create another coordinator or edit Codex private state.
The [official app-server guide](https://learn.chatgpt.com/docs/app-server#start-or-resume-a-thread) documents initial registration and restoration on resume.

### One-shot specialist results

When the coordinator launches a one-shot specialist for an accepted request, the launcher can return its final result once after the specialist exits:

```
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_local.py submit --root "$PWD/.private" \
  --event-id STABLE-ID --source project_outcome \
  --owner-id codex:COORDINATOR-THREAD-ID --parent-request-id ORIGINAL-REQUEST-ID --body-file RESULT.txt
```

The actor must be this installation's configured coordinator, and the parent must be an enrolled user's request that the coordinator accepted. Derived, scheduled, maintenance or unaccepted parents are rejected. The result reaches the coordinator as specialist evidence for the original recipient. It is not a user instruction, project owner or release approval. The stable event ID deduplicates; a rejected or uncertain submission is never replayed automatically.

### Shared-resource handoffs and owner receipts

A routed brief can ask an owner only to release, confirm or refuse a shared resource, such as native UI, a browser or a host. The route input gives the owner the exact command. When the answer is true, the owner writes it to an owner-only file and publishes it immediately:

```
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_local.py handoff --route-id ROUTE-ID \
  --owner-id codex:OWNER-THREAD-ID --body-file ANSWER.txt
```

The answer reaches the coordinator at once as owner evidence for the route's original recipient. The owner can keep working on unrelated tasks privately. In the same transaction, the route is marked completed and delivered, so the owner's later terminal turn is not relayed again. The command requires a currently registered owner, its own route that is still awaiting a result, and a still-enrolled original sender. Repeating the same answer returns the same request. A different answer, a route that already returned its terminal result, or another owner is rejected. Commentary and progress are never treated as a handoff, and the command grants no authority.

An owner can read only delivery metadata for its own published outcome, never another record or any body text:

```
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_entry.py outcome --request-id local:ID --owner-id codex:OWNER-THREAD-ID
```

It checks the publishing owner, the stable event ID, the still-enrolled recipient and, for routed results, the matching parent and route. It returns the request, dispatch and delivery states only.

### Existing-thread advance notice

The CLI uses the same maintenance-notice guard, outbox and durable sent receipt as the dynamic tool.
Write the exact expected-downtime notice to a small owner-only UTF-8 file. Keep credentials out of it.
From the prepared workspace, replace `maintenance:...` with the full ID of the currently dispatched maintenance request:

```sh
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_maintenance.py notice \
  --root "$PWD/.private" --request-id 'maintenance:...' --body-file "$PWD/.private/advance-notice.txt"
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_maintenance.py notice-status \
  --root "$PWD/.private" --request-id 'maintenance:...' --body-file "$PWD/.private/advance-notice.txt"
```

In a source checkout the entry point is `scripts/alfred_maintenance.py`; use that installation's runtime Python and existing private root.
Both commands require the identical body and original active maintenance turn. `notice-status` refuses a missing receipt and never enqueues a new notice.
Repeated calls reuse one notice and one CLI call record, including after CLI process restarts. The dynamic tool and CLI can read the same receipt.
Neither command accepts a recipient, source, namespace, call ID, thread or turn override.
The trusted owner-only `appserver.json` selects the coordinator. The existing queue supplies the original dispatch and enrolled owner.
Human, external, native-user, project-outcome and foreign-turn requests are rejected. Changed enrollment cannot retarget an existing notice.
The local OS account is the operator boundary; a request ID is a lookup key, not authentication for a remote caller.

The only app-server calls are `initialize`, `initialized` and one metadata-only `thread/turns/list` with `itemsView=notLoaded`.
There is no resume, Steer, model call, new thread, daemon startup or history read. The CLI does not initialize a missing queue/schema or run maintenance checks.
It queues through the existing collector without opening Telegram or reading its credential. Delivery waits for the collector's existing flush.
If configuration, schema, socket or active-turn validation fails, stop and report the blocker. No fallback send channel is provided.

Exit code **3** means the notice exists but is not verified sent. Exit **1** means rejected or unavailable; **2** means invalid arguments.
Only exit **0** from a notice command, with JSON `sent=true` and a positive `telegram_message_id`, verifies the stored delivery receipt.
Check `notice-status` again as needed within the same active turn; this command creates no polling loop.
Never replace an uncertain notice, change its body or delete its receipt to retry. The request remains open for `alfred_telegram_reply` after recovery verification.
A sent receipt grants no action authority. Existing restart, update, credential and release authorization still applies.

For an existing installation, the owner must activate the tested source at an idle boundary using the same collector.
First announce expected downtime through the authorized Telegram scope. Restart only that collector, verify ingestion and exact reply delivery,
and inspect saved maintenance state. Keep the existing enrollment, coordinator ID, queues, and release controls.
If replacing a native housekeeping job, remove only that job after verifying runtime activation. The code does not edit native automation.
Source tests and package fixtures do not prove live Telegram delivery or service recovery on the recipient's computer.

## Recovery

Your project board is `projects.md` plus `tasks/backlog`, `tasks/todo`, `tasks/doing`, `tasks/blocked`, `tasks/review` and `tasks/done`. Backlog is uncommitted; Todo is committed next work or a scheduled recurrence. Blocked names the missing input and its resolver; `alfred_board.py move` refuses a blocked card without them. Keep timestamped column moves inside the card. Review awaits a named human's decision or acceptance. `/report` starts at BACKLOG and includes every open card. Todo, Doing, Blocked and Review are grouped under a readable lead label: the card's optional `Report lead:` field, or a short name derived from `Project:` or the card name. Owner identities are never shown. Task numbers run across the whole report. `/done` shows saved completions, including completed recurring runs, newest first and 20 per page. `/done 2` and later pages are linked in the footer, and an unavailable page is answered locally. Both commands are owner-only, paginated, and make no model or live-agent calls. Keep this source and accepted context in your own private Git repository. `.private/` holds the bot credential, invitation, enrollment, one coordinator ID and the durable inbox/outbox. It is excluded from Git, as is `.venv/`. Git alone does not restore authentication or pending delivery.

Quiet completion lets the coordinator close an installation-owner request without a Telegram message, through the same `alfred_telegram_reply(request_id, body)` schema that existing threads already have. Start `body` with `DONT_NOTIFY` followed by whitespace, then the complete answer. Use it only where the owner's recorded notification preference allows: for example, routine owner outcomes or internal handoff acknowledgements when no input is needed and no usable link is ready. Never use it for a direct question. Only the coordinator's explicit tool call chooses quiet mode; incoming text never does. It is refused for other enrolled contacts. The full answer is saved as the request's result with status `suppressed`. The sender never claims it, and readers such as `alfred_entry.py result` and `outcome` report `delivery_state: suppressed`, never `sent`. Thread, turn, recipient, enrollment and parent checks are unchanged. A retry with the same body is idempotent. Changing the answer or switching between quiet and normal is a conflict. A quiet owner outcome can still close its parent, and the coordinator can send one actionable reply on that parent later. Bodies that do not start with the marker are sent normally.

For one authorized image reply through `alfred_telegram_reply`, start `body` with `![Description](https://public-host/image.png)` on its own first line. The rest is a caption with optional Markdown formatting. Captions over 1024 UTF-16 units follow the image as text messages. Only a public HTTPS URL without credentials, query or fragment is supported; no local file is opened. The same recipient guard, outbox and uncertain-delivery rules apply. Telegram receives the image URL and returns a photo receipt; ordinary replies remain text. Never publish private content just to use this route.

On macOS, the installed service prints its unique label. Restart that same service with `launchctl kickstart -k gui/$(id -u)/LABEL` only at a safe boundary. This does not restart Codex or another project's owner. An uncertain send or thread creation must be inspected before any retry; do not delete a queue or creation marker to make an error disappear. `connect` reuses a confirmed coordinator and refuses a second creation after an uncertain response.

On Windows, use the exact task name printed during setup. Inspect it with `schtasks.exe /Query /TN TASK_NAME /V /FO LIST`, from WSL or PowerShell. If installation failed, rerun the same `setup.py service` command; it checks and reuses matching records. Do not create a second task or reset WSL. Before moving or removing the installation, stop its collector at a safe boundary and verify that it exited; ending a Windows launcher alone is not proof that every Linux child stopped. Keep the private queue and sign-in state until the move is verified.

If Windows returns a task identity or action that setup cannot verify, inspect that named task; do not force a match. An already-enabled task may still run at logon. To disable it, use `schtasks.exe /Change /TN TASK_NAME /DISABLE`. If a Codex/Node upgrade changes their executable paths, first disable this task and gracefully stop its own collector at a safe boundary, verifying exit. Delete only that task with `schtasks.exe /Delete /TN TASK_NAME` and remove only `.private/windows-service.json` and `.private/windows-task.xml`. Keep bot credentials, appserver.json, creation markers and the inbox/outbox. Run the same service setup again to record the new path. A WSL hangup signal is forwarded to the collector's normal SIGTERM handler; a forced kill still requires recovery checks before retrying uncertain sends.

Do not run the same bot on two computers. For a move, stop the old collector first, preserve its private state through a secure local transfer, sign in normally on the new computer, reconcile unfinished requests, and verify delivery. A fresh installation for another person needs a new bot, new workspace and new account enrollment.

This adapter is public source; each installation’s identity and state are private. Dot access needs a separately verified transport. Telegram is the included phone entry.
