# Your own Chief

One private Telegram bot. One Chief. Your projects and accounts.

Replies support Markdown bold, italic, strikethrough, named links, headings and code. The optional adapter uses `markdown-it-py` to produce Telegram's native text entities. It splits long replies without losing styles or emoji, saves the original answer and the exact delivery plan, and verifies the returned text and formatting. This adds no model call or service. Existing queued replies retain their previous plan.

Use `--nickname "Your name"` during preparation to give this Chief its own name. The collector applies that name to Telegram and verifies it through the Bot API. Skipping naming keeps Chief. Existing bot usernames, ownership and service identifiers remain stable.

The enrolled owner's command menu contains `/report` (open tasks grouped by project lead), `/done` (completed tasks, 20 per page) and `/setup` (one guide for host setup, Dot/mobile and additional devices). All three commands run without a model. Menus are scoped to enrolled private chats; public guides grant no access.

If a photo download fails, the collector tries another size Telegram supplied. It saves the caption even if every size fails. A connected Chief uses the text and pictures it already has and asks for a missing picture when it needs one. While Chief is offline, the collector sends one notice per chat for the same download error within five minutes.

Messages from the enrolled installation owner are never rejected by an intake count. Busy hours, unfinished requests, internal agent outcomes and a full reply queue do not drop the owner's feedback; it is saved and waits for the coordinator. The owner's replies, command answers and notices are not dropped or refused by reply counts either; Telegram's own rate limit paces delivery. If an attachment fails and its acknowledgement cannot be queued, the owner's saved text is kept. Other enrolled contacts keep the 30-per-hour, 50-unfinished and 250-queued-reply limits; failed or unconfirmed replies await reconciliation and are not counted as queued. Unknown senders, secret filtering, attachment validation and outgoing reply authorization are unchanged.

When the enrolled owner's message or bridge command is saved, the bridge sets a 👍 reaction on that exact message with Telegram's `setMessageReaction`. It is a receipt that the message was saved, not that the task is done, and it uses no model. The receipt is queued in the same transaction as the message and sent by the collector after commit. The owner's enrollment is checked again before sending; a message is acknowledged once, including redelivery and edits. Rate limits and network errors retry with bounded backoff (up to 8 attempts); other Telegram refusals stop. Receipt errors never affect saved input, replies or dispatch. Contacts, unknown senders, rejected or credential-only messages and failed attachments get no reaction. `inbox` and `status` show `owner_receipts` counts by state.

Text that looks like a password, key or one-time code is redacted before saving; the rest of the message is kept and the sender is told the secret was not stored. A message that is only a secret is not saved. An edited message is saved as new input that names the earlier version; edited commands are not run again. Photos, videos, documents and voice/audio are accepted up to Telegram's 20 MiB bot download limit. The owner's files are bounded only by a 2 GiB free-disk reserve; another contact keeps 100 MiB of their own retained files.

Each dispatched message needs its own completion. When one reply answers several messages from the same turn, the coordinator completes the others too, quietly for the owner. A message whose turn ended without a recorded reply cannot be answered from a later turn; `/report` lists it for review, and the operator can close audited ones with evidence (below).

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
Each recording is checked for its request-bound private path, ownership and size. Only one local decoder runs at a time; FFmpeg has 60 seconds and each whisper.cpp pass of up to five minutes has 180 seconds.
Any length is saved at intake. Local decoding covers up to 30 minutes in equal passes, because it runs before later messages are dispatched; a longer recording stays saved and the coordinator asks for text or shorter parts. Conversion uses a seekable private descriptor with the fd protocol only and an explicit demuxer list; subprocesses receive argument arrays, never shell text.
Silence, low-confidence output, invalid media, missing configuration and timeouts produce an unavailable result, not guessed instructions.
Interrupted decoding is not automatically repeated. Temporary conversion/decoder output is removed after the attempt; the original attachment is retained under normal policy.
Confidence checks reduce errors but do not prove exact wording. Treat unclear names, numbers or consequential instructions as requiring clarification.
When a fresh local decode succeeds for the enrolled installation owner's own voice note or audio, the bridge queues the transcript as plain text replying to that exact Telegram message, in the same transaction that saves the result and before the request is dispatched to Chief. The text is sent exactly as decoded, without Markdown rendering; a long transcript uses the normal ordered parts. It is not the answer: the request stays open and Chief's reply, or a later correction, is an ordinary reply.
Before each send the bridge checks again that the owner is still enrolled, that the message came from the owner's own private chat after enrollment, and that the target is that recording. Each Telegram message gets at most one transcript, including redelivery and edits. Rate limits and network errors retry; uncertain sends are never replayed; other refusals, such as a deleted voice note, mark only the transcript failed. Contacts, failed or interrupted decodes, native-audio input and transcripts saved before 0.6.0 get no transcript reply.

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
Missing owner enrollment remains visible and retries after thirty minutes; queued replies no longer hold the owner's notice. An unfinished review defers additional findings for six hours.
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

### Routing after a reply

The coordinator can answer a message first and then route its follow-up to an existing owner in the same turn. Routing needs the exact accepted coordinator turn, the original still-enrolled sender and that sender's project grant; one brief per owner per request stays immutable. Requests from another turn, other or unknown requests, denied or revoked senders and re-enrolled identities are refused.

### Several requests in one owner turn

Different requests routed to the same busy owner can land in one native turn. A final answer then closes only the route it identifies. Start the final answer with `ROUTE_RESULT ROUTE-ID` on its own line. If the owner already has active work, the new brief includes a command for saving another answer. Use the ID for the assignment that answer completes.

```
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_local.py finish --route-id ROUTE-ID \
  --owner-id codex:OWNER-THREAD-ID --body-file ANSWER.txt
```

A turn with one route needs no label. `finish` works only for a route whose turn carries another route, or whose turn ended without giving it the final, so it is never a progress channel.

If several assignments reached a turn, an unlabelled final needs review even when only one assignment is still open. The same applies to a final that names several or unknown assignments. The bridge saves the complete final privately and keeps every route open. Chief gets one content-free notice per set of routes, bound to the installation owner. It lists the route and request IDs, and the assignments object carries a `turn:` item with a read command:

```
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_local.py match --owner-id codex:OWNER-THREAD-ID \
  --turn-id TURN-ID [--route-id ROUTE-ID]
```

Without `--route-id` it prints the saved final and the turn's routes and sends nothing. With it, the final becomes that open route's ordinary result for its own requester. Chief decides which route it answers; repeating the match is idempotent. The owner finishes any other route, and Chief can route the notice to the owner to ask for that. After a shared turn fails, Chief gets one private notice. The assignments stay open for individual answers.

Acknowledgments are recorded in socket order, and reconnect collects every acceptance before settling a turn. A route acknowledged after its turn was already settled never reuses that final. It stays open and Chief gets a new notice.

### Direct owner return

Chief can let one routed owner answer a request without a relay turn. It opts in by starting the brief with `DIRECT_RETURN card=tasks/done/NAME.md` on its own first line, or `DIRECT_RETURN quiet card=...` where the installation owner's notification preference allows quiet completion. Only Chief's route call can opt in this way; owner text or metadata never does. The request must be an open human request with no reply or notice yet and no other owner route. The election records the original recipient and enrollment, plus a digest of the operator policy, the owner registry and grants, and `priorities.md`, `AGENTS.md` and `COORDINATOR.md`. The digest contains no credentials or recipient identifiers.

The owner's route input names the exact command. When the owner's recipient-ready answer is complete and the named card has settled, it stages the answer and then ends its turn with exactly that text as its final answer. A settled card is in `tasks/done`, records the route ID, exists in no other column and has no unfinished move.

```
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_local.py return --route-id ROUTE-ID \
  --owner-id codex:OWNER-THREAD-ID --card tasks/done/NAME.md --body-file ANSWER.txt
```

Staging never sends. A local caller that knows a route and owner ID can only store a candidate for that owner's open turn. The bridge sends only when the app-server reports that same owner's routed turn completed and its final answer is byte-identical to the candidate. The same applies when reconnect reconciliation reads the owner's completed turn record. In one transaction it then checks again:

- the original recipient and enrollment, the paired device and the project grant;
- the policy digest;
- that the card is unchanged since staging and still settled;
- that there is one owner and no other result;
- that there are no earlier, uncertain or duplicate sends, and the original dispatch was accepted.

If all of these hold, the bridge queues the answer as the request's reply in the existing outbox, with per-part receipts and the elected quiet mode. It completes the request and route, and keeps a binding that survives the 30-day outbox cleanup. Chief sees it in its next assignments capsule. Repeating the same staged answer reads that receipt; a different answer is refused.

Any other ending goes to Chief once as the route's ordinary result, with `Direct return not used:` and the reason. That includes no candidate, a differing final, a failed turn, a changed card, policy or recipient, another owner or reply, and a past notice or uncertain send. A result for a revoked recipient is held by the existing relay guard. Questions, approvals, blockers and partial results are never staged; the owner ends its turn and Chief handles them. Chief's own later reply to the same request is refused, so the answer is sent once.

`tools/replay_direct_return.py` replays the qualifying path and its main failure cases against a synthetic app-server and reports dispatched turns per assignment. In that replay the Chief relay path dispatches 3 turns (2 Chief, 1 owner) and the direct return 2 (1 Chief, 1 owner); every fallback dispatches the relay turn. These are counted dispatches, not model calls, tokens or time.

### Browser tabs for project owners

A routed owner receives a stable Chrome session name, any saved tabs for that project, and a runnable command ending in `--action show`. Run it when the task needs a browser. It returns the binding instructions. The coordinator stores tab metadata locally. Browser access still depends on the connected profile and the original project grant.

Use the supported browser connector to list the authorized profile's tabs. Resume the operator-assigned tab by its exact ID, or create a project tab with `cua.createBrowserTab(browserId, url, {sessionName})`. Record the observed provider/profile ID, tab ID, plain origin and connection instance ID when exposed. Give each working tab a purpose through `--slot`, for example `studio` and `analytics`. On return, use `--action check` with the observed metadata and verify the signed-in workspace before acting. A conflicting tab, changed connection or different origin requires identifying the intended tab again. A stale reset cannot discard a newer binding.

Use tab-targeted controls while other leads or the operator work. Preserve any draft in that tab. An OS picker, sign-in prompt or connector that requires native focus can still need a brief foreground handoff. A disconnected connector is a separate access problem. Keep the existing tabs and work available for reconnection.

### Assignment recovery at intake

Every coordinator dispatch carries an `assignments` object built from the existing ledger and board. It includes, in full:

- each open request, with complete transcripts and unanswered owner or specialist results;
- each live route with its brief;
- direct returns not yet seen and deliveries that are not yet durable;
- each sender's project grants.

It also carries references with digests to the owner registry, any shared owner turn with an open route, `priorities.md`, the instructions, Doing/Blocked/Review cards and any card naming a current request or route. Each input is checked against the current enrollment, device and grant. A revoked one keeps its ID and `authorization: revoked`, but not its content. Nothing is truncated or ranked, and no new task state is kept.

The full object is sent with the first dispatch after a resume, an observed compaction (`thread/compacted` or a `contextCompaction` item), a rejected or uncertain dispatch, or `--cold`. Later warm dispatches get only `changes` since `base_digest`, or `unchanged`; the dispatched request itself is the message body. Where the runtime does not signal compaction, the fallback is deterministic and uses no timer: each dispatch states its digest and a reread command, and the coordinator runs it when that digest is not in its context.

```
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_assignments.py --root "$PWD/.private" [--cold]
```

New workspaces get a short always-loaded `AGENTS.md` that uses this object instead of rereading the startup documents each turn. `COORDINATOR.md` is read during onboarding, and again when a rule is in question or its digest changes. Existing installations should adjust their own instructions the same way.

### After-answer checkpoint

Batch after-answer bookkeeping into one on-demand checkpoint, never a recurring job:

```
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_checkpoint.py prepare --root "$PWD/.private" \
  --request-id ID --request-id ID2 --path tasks --path projects.md
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_checkpoint.py confirm --root "$PWD/.private" --digest DIGEST
```

`prepare` reads each request's delivery receipts once and runs the installation's own export and verification commands once, with no time limit. A failed command's complete output is kept owner-only under `.private/checkpoints`; the record holds a redacted excerpt. `prepare` then stages only the declared paths in a private temporary Git index and saves the exact diff, owner-only, beside a digest. Review that file, then `confirm` that digest. It commits exactly those bytes without touching the operator's index, other staged files or the worktree, then updates only the committed entries in the index.

Pushing is decided at review, not at confirm. With `"push": true`, `prepare` requires the remote's configured fetch and push URLs to equal the expected ones exactly, after Git's URL rewriting. It also requires the remote branch to be at the reviewed HEAD, so no unreviewed local history is published. `confirm` and crash recovery check the URLs, the saved commit's tree and parent, and the remote tip again. They then push only that commit, with no tags and no force.

The batch stops with the fact, and nothing is committed or pushed, when any of these happen:

- a delivery is not durable (sent with receipts for every part, or quiet), or a notice needs reconciliation;
- a command fails;
- added lines contain credential-like text;
- another staged version of a declared file exists;
- the declared bytes or HEAD changed after review;
- the remote URLs or branch changed.

A delivery-only batch (no paths) records the durable receipt. `confirm` resumes after an interruption and never repeats a commit or push. `status --digest` reads the saved record.

Optional owner-only `.private/checkpoint.json` refers to existing private scripts without copying them into this source: `{"export": [["/abs/script"]], "verify": [["/abs/check"]], "known_gaps": ["..."], "paths": ["tasks"], "remote": "origin", "fetch_url": "EXPECTED", "push_url": "EXPECTED", "push": false}`. A remote without both expected URLs is invalid. Commands are argument arrays run from the workspace without a shell, with `CHIEF_CHECKPOINT_REQUESTS` and `CHIEF_CHECKPOINT_PATHS`. Known gaps, unconfigured commands and unscanned binary files are reported as coverage gaps; they never hide a failed command.

### Close audited messages that ended without a reply

When a reviewed record shows that such messages were answered elsewhere or need no separate work, close them in one step. Each entry has `request_id`, `category`, `evidence` and an optional `covered_by` result ID, which must be a confirmed answer to the same recipient:

```
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_local.py resolve --root "$PWD/.private" \
  --mapping-file AUDIT.json --authority-request-id OWNER-REQUEST-ID --dry-run
```

Run it again without `--dry-run` to apply. Only messages awaiting reconciliation qualify, never undispatched or active work. A request from the currently enrolled owner authorizes it. Entries apply all together or not at all. Each request is marked done with its evidence, authority and time; its body and dispatch error are kept, and nothing is sent or repeated. The same entries are idempotent; a changed entry is refused.

### Shared-resource handoffs and owner receipts

A routed brief can ask an owner only to release, confirm or refuse a shared resource, such as native UI, a browser or a host. Chief marks it by starting the brief with `RESOURCE_HANDOFF` on its own first line, inside the existing route tool, so running coordinators need no new registration. The brief's wording is never searched. Only a marked route's input gives the owner the exact command; ordinary routes keep the browser, brief, authority and direct-return controls without it. When the answer is true, the owner writes it to an owner-only file and publishes it immediately:

```
COS_WORKSPACE="$PWD" .venv/bin/python runtime/alfred_local.py handoff --route-id ROUTE-ID \
  --owner-id codex:OWNER-THREAD-ID --body-file ANSWER.txt
```

The answer reaches the coordinator at once as owner evidence for the route's original recipient. The owner can keep working on unrelated tasks privately. In the same transaction, the route is marked completed and delivered, so the owner's later terminal turn is not relayed again. The command requires a currently registered owner, its own marked route that is still awaiting a result, and a still-enrolled original sender. Routes saved before this marker existed keep the command their input already offered. Repeating the same answer returns the same request. A different answer, a route that already returned its terminal result, or another owner is rejected. Commentary and progress are never treated as a handoff, and the command grants no authority.

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
