# Changelog

## 0.8.0 — 2026-11-20

- **Breaking change.** Chief now runs directly in the existing Codex or Claude Code conversation. This version removes the Telegram bot, enrollment and delivery service, attachment and voice handling, service installers and relay commands.
- Read all open cards or page through completed work from the native conversation or with the standalone Python report command. Existing Markdown cards, movement histories and recurring completion records are supported.
- The [upgrade guide](docs/start.md#upgrade-an-existing-chief) covers private backups, outstanding deliveries and releasing the old service before opening the same conversation directly.

## 0.7.3 — 2026-11-19

- When the largest Telegram photo fails to download, Chief tries the next available size. If Telegram offers a single usable file, Chief retries a failed read once. A Telegram rate limit stops further attempts for that attachment.
- Chief keeps each caption and records which files arrived. While connected, it asks for a missing picture only if the task needs it. While offline, the collector sends one notice per chat for the same download error within five minutes. File size warnings appear when a size check fails.

Restart the Telegram collector to load this update.

## 0.7.2 — 2026-11-18

- **One owner turn, several requests.** When different requests reach the same existing owner in one native turn, a final closes only the route it names with a `ROUTE_RESULT ROUTE-ID` line. A one-route final needs no label.
  - The owner finishes other routes with the new `alfred_local.py finish`. It works only for a route that shares its turn, or whose turn ended without giving it the final.
  - An unlabelled or ambiguous final reaches no requester. The complete text is saved privately and every route stays open. Chief gets one content-free notice and an assignments `turn:` item. `alfred_local.py match` reads the final, or with `--route-id` makes it that route's ordinary result.
  - Owner acknowledgments are recorded in socket order. Reconnect collects every acceptance before settling a turn. A restart recovers the original assignment and answer. A late acknowledgment cannot reuse that answer. A failed shared turn leaves the assignments open. Direct returns still refuse shared turns.
- **Explicit shared-resource handoffs.** The `handoff` command and its instructions now appear only on routes whose brief starts with `RESOURCE_HANDOFF` on its own line. The brief's wording is never searched. The marker works through the existing route tool, and coordinator dispatches describe it, so running sessions can use it.
  - `handoff` refuses unmarked new routes. Routes saved earlier keep the command their input already offered.
  - Ordinary routes keep the browser, brief, authority and direct-return controls.

Migration: no breaking change, configuration or tool registration. Restart the one collector at a safe boundary to load the change; it adds one table and one column to the private database. Saved route inputs are not rewritten. An installation's own instructions that described `handoff` for every route should mention `RESOURCE_HANDOFF` instead.

## 0.7.1 — 2026-11-18

- Browser origins reject the URL user-information delimiter directly. This preserves the tab binding checks and lets the source pass the after-answer credential scan.

## 0.7.0 — 2026-11-18

- **After-answer checkpoint.** New `alfred_checkpoint.py prepare`/`confirm`/`status` turns after-answer bookkeeping into one on-demand, reviewed checkpoint per batch.
  - It reads delivery receipts once and runs the installation's own export and verification commands once, with no time limit. A failed command's complete output is kept owner-only.
  - It saves the exact diff of the declared paths, staged in a private temporary index, under a digest.
  - `confirm` commits exactly those bytes without touching the operator's index, other staged files or worktree.
  - Pushing is fixed at review and pinned to the configured expected fetch and push URLs. Those URLs, the saved commit and the remote tip are checked again at confirm and in crash recovery. Only that commit is pushed, with no tags and no force, and only when the remote is at the reviewed HEAD.
  - Non-durable deliveries, failed commands, fresh credential-like lines, conflicting staged work, changed bytes, a moved HEAD and changed remote URLs or branches stop the batch with the fact.
  - Private scripts are referenced from owner-only `.private/checkpoint.json`, never copied. Known gaps are reported as evidence. No schedule.
- **Assignment recovery at intake.** Each coordinator dispatch carries an `assignments` object derived from the ledger and board.
  - Open requests, transcripts and unanswered owner results are included in full; live routes include their briefs. Non-durable deliveries, grants and digested references to the registry, priorities, instructions and relevant cards are also included.
  - Inputs are checked against the current enrollment, device and grant; revoked ones keep only their reference.
  - The full object follows a resume, an observed compaction, a failed dispatch or `alfred_assignments.py --cold`. Warm dispatches carry only changes or `unchanged`, plus a reread command as the deterministic fallback when compaction is not signaled. No truncation, ranking or new task state.
- **Reduced initial context.** New workspaces get a short always-loaded `AGENTS.md`/`CLAUDE.md`. `COORDINATOR.md` is read during onboarding, then only when a rule is in question or its digest changes. The core and setup guides say the same.
- **Direct owner return.** Chief can elect one routed owner to answer directly by starting the brief with `DIRECT_RETURN card=tasks/done/NAME.md` (optionally `quiet`).
  - The owner stages its answer with `alfred_local.py return`, which never sends.
  - The bridge sends only when the app-server reports that owner's routed turn completed with a final byte-identical to the candidate, including on reconnect reconciliation.
  - It then checks again the recipient and enrollment, device and grant, a policy digest, the unchanged settled card, one owner, and no earlier, uncertain or duplicate send. The policy digest covers operator policy, registry and grants, and operating rules, with no credentials or recipient IDs.
  - The answer goes through the existing outbox with per-part receipts; its binding survives outbox cleanup.
  - Anything else reaches Chief once as the ordinary route result with the reason.
- Each project owner gets a separate browser session name and can retain multiple tab IDs in the authorized profile. A tab binding records the origin and provider instance when available. Conflicts, changed origins, stale resets and owner reassignment are checked. Foreground coordination is needed only for actions that use native focus.
- `alfred_telegram_reply` now reports a conflict instead of "queued" when another answer for the same request was saved first.
- `tools/replay_direct_return.py` replays the direct-return path and its main failure cases against a synthetic app-server. It counts dispatched turns: Chief relay 3 (2 Chief, 1 owner), direct return 2 (1 Chief, 1 owner). These are dispatches, not model calls.

Migration: no breaking change, configuration or new tool registration. `DIRECT_RETURN` works inside the existing frozen route schema.
- Restart the one collector at a safe boundary to start sending the assignments object and tab sessions; the first dispatch after restart carries the full object.
- Existing installations should shorten their always-loaded instructions to use the object instead of rereading the startup documents each turn (see docs/telegram.md).
- To use checkpoints, optionally add `.private/checkpoint.json` pointing to existing private export and verification scripts; a remote needs `fetch_url` and `push_url`.

## 0.6.0 — 2026-11-18

- When the optional on-device decoder transcribes the enrolled installation owner's voice note or audio, the bridge immediately queues the transcript as plain text replying to that exact Telegram message, before Chief's model work. Text is sent as decoded: same language and punctuation, literal Markdown, ordered parts for long transcripts.
- The transcript is not the answer and never completes the request; Chief's answer and any correction are normal replies. A failed or refused transcript send does not hold other replies.
- Enrollment, same sender, owner's own private message and target are rechecked before every send. One transcript per Telegram message, including redelivery and edits. Rate limits and connection failures retry; uncertain sends are not replayed.
- Contacts, failed or interrupted decodes and native-audio input get no transcript reply. Transcripts saved earlier are not sent.

Migration: none. Restart the one collector at a safe boundary to start sending transcript replies; no configuration, model or decoder change.

## 0.5.2 — 2026-11-18

- Concurrent host submissions with the same event ID and content return one request ID. A new enrollment cannot reuse an earlier enrollment's event or read its answer.
- The original host request ID returns a single matching owner or specialist answer as soon as Chief saves it. The reader reports queued, uncertain and quiet delivery separately. Multiple distinct outcomes need a combined answer from Chief.
- Quiet answers can contain local Markdown image references. Telegram photo validation applies when Chief requests a send.
- A retired owner or changed project grant can hold one finished result. Chief records the reason and continues with other requests. Reconnect uses the saved route and checks the original enrollment. Equivalent outcomes share a receipt only within the same enrollment and project.
- New coordinators look up owners in the current project registry. Direct requests still work when the registry file is missing.
- After a server interruption, a later authenticated turn from the same enrolled sender can complete the original saved request without replaying its work. Other turns and approval or tool failures remain blocked.

## 0.5.1 — 2026-11-17

- The bridge sets a 👍 reaction on each saved message or bridge command from the enrolled installation owner, using Telegram's `setMessageReaction`. It is a receipt, not completion, and uses no model.
- Receipts are queued as metadata in the intake transaction, sent by the existing collector after commit, rechecked against the owner's enrollment, and deduplicated per message, including redeliveries and edits. Rate limits and network errors retry with bounded backoff for up to 8 attempts; other refusals stop. Saved input, replies and dispatch are never affected.
- Contacts, unknown senders, rejected or credential-only messages and failed attachments get no reaction.
- `inbox`/`status` report `owner_receipts` counts. Sent and failed receipts follow the 30-day cleanup.

Migration: restart the one collector at a safe boundary. Messages saved earlier get no retroactive reaction.

## 0.5.0 — 2026-11-17

- Route a follow-up after answering first. A request answered earlier in the same coordinator turn can still be routed to an existing owner; the exact turn, still-enrolled original sender, project grant and one immutable brief per owner are still required. Other turns, other or unknown requests, denied, revoked and re-enrolled senders stay refused.
- The installation owner's replies, command answers and notices are no longer dropped or refused by reply counts. Other contacts keep their limits, and failed or unconfirmed replies no longer count as queued for anyone, so old delivery faults cannot block new answers.
- Credential-like text is redacted before saving instead of discarding the whole message; the sender is told the secret was not stored. A message that is only a secret is still not saved.
- Edited Telegram messages are saved as new input that names the earlier version. Edited commands are not run again.
- Videos, video notes and animations are saved as private opaque files with their caption. Attachments are accepted up to Telegram's 20 MiB bot download limit (was 10 MiB). The owner's files are bounded by a 2 GiB free-disk reserve instead of a shared 100 MiB quota; each other contact keeps a 100 MiB quota of their own files.
- Voice and audio of any length are saved. Optional local decoding transcribes up to 30 minutes in equal passes of at most five minutes (was a five-minute intake rejection).
- Recurring tasks are no longer held back while 50 requests are open; one receipt per occurrence already bounds them.
- New `alfred_local.py resolve`: close audited messages whose turn ended without a reply, with an evidence reference, the owner's authorizing request and an optional confirmed covering reply. All or nothing, idempotent, dry-run first; bodies and dispatch errors are kept and nothing is sent or replayed.
- `/report` explains open delivery or reconciliation items instead of always printing counters, and coordinators are told to complete each message answered by a combined reply.

Migration: restart the one collector at a safe boundary; it then also receives edited messages. Existing coordinators need no new tool; the combined-reply instruction applies to newly dispatched messages. Review any previously stranded messages, then apply an audited mapping with `resolve --dry-run` before `resolve`.

## 0.4.2 — 2026-11-17

- Never reject the enrolled installation owner's Telegram messages by an intake count. The 30-per-hour and 50-unfinished gates, which also counted internal agent outcomes, and the 250-queued-reply gate no longer apply to the owner. A failed attachment acknowledgement cannot roll back the owner's saved text.
- Other enrolled contacts keep their limits. Unknown senders, secret filtering, attachment validation and outgoing reply authorization are unchanged.

Migration: restart the one collector at a safe boundary. Messages rejected earlier were never saved and cannot be recovered by this update; ask the sender to resend.

## 0.4.1 — 2026-11-17

- Keep BACKLOG first even when `/report` spans several Telegram messages. Preserve formatting offsets, complete parts and delivery receipts. Other replies retain their part counters.
- Make authenticated host command execution the default Dot onboarding path. Verify one saved request/answer round trip instead of depending on a Codex thread connector.
- Add `alfred_entry.py result --wait SECONDS` (1–900). It rereads one exact request with a fresh read-only snapshot and the same identity and enrollment checks on each read. It returns the complete saved answer automatically (`wait_state: answer_saved`). On timeout it returns the true pending state and the same `request_id` (`timed_out_pending`, exit code 4). It never submits, wakes Chief, calls a model or runs in the background. Single-snapshot `result` and `outcome` are unchanged.
- Dot onboarding uses the waiting read-back and treats an unavailable wake as advisory after durable submission. Dot recognizes `Dot connection confirmed` itself instead of asking the user to confirm.

## 0.4.0 — 2026-11-17

- `/report` starts at BACKLOG, with no introductory text, and lists every open card. Todo, Doing, Blocked and Review are grouped under a readable lead label: an optional `Report lead:` card field, or a short label derived from `Project:` or the card name. Owner identities are never shown. Task numbering stays global, and multipart delivery is unchanged.
- Completions move out of `/report` to a new owner-only `/done`. It lists saved completions, including completed recurring runs, newest first and 20 per page (`/done 2`, …). The footer links to the other pages, and an unavailable page is answered locally. No model calls.
- The owner's Telegram menu adds `/done`. Other contacts and `/setup` are unchanged.

Migration: restart the one collector at a safe boundary. Its menu check then adds `/done`. Optionally add `Report lead:` to cards for clearer group names. Saved report numbers still resolve against each delivered report.

## 0.3.0 — 2026-11-17

- Add explicit quiet completion through the existing `alfred_telegram_reply(request_id, body)` schema. A body that starts with `DONT_NOTIFY` and whitespace saves the complete answer as the request's result with status `suppressed`. Telegram receives nothing. It is available only for installation-owner requests and owner evidence addressed to that owner, and only by the coordinator's explicit choice.
- Local readers report `delivery_state: suppressed`. Queue limits and 30-day cleanup treat suppressed results like completed ones. Quiet owner outcomes close their parent and resolve equivalent duplicates without a send.
- Ordinary replies are unchanged.

Migration: restart the one collector at a safe boundary before relying on quiet completion. An older running collector would send the marker text as an ordinary message. Existing coordinators need no new tool.

## 0.2.2 — 2026-11-17

- Add `alfred_local.py handoff` so an owner can publish a completed shared-resource answer (release, confirm or refuse) immediately as its route's only result, while it continues unrelated work. The command binds to the route, owner, parent and still-enrolled recipient. It is idempotent, and the later terminal turn is not relayed again. Route briefs include the exact command.
- Add `alfred_entry.py outcome`, which returns delivery metadata, never bodies, for an owner's own published outcome.

Existing coordinators need no new tool. Both commands are local CLI paths. Routes created before the collector loads 0.2.2 do not include the command in their brief.

## 0.2.1 — 2026-11-17

- Keep reproducible failures of the requested main flow within the narrow delivery holds.
- Clarify that a genuinely required external input or shared-resource release can use Blocked with its concrete missing item and resolver. Running work and extra reviews remain outside that column.

## 0.2.0 — 2026-11-17

- Add a `tasks/blocked/` column for real missing input. `alfred_board.py move` requires `Blocked by:` (the concrete missing item) and `Resolver:` (who can supply it). `/report` and the snapshot list blocked cards between Doing and Review. A running owner or an unperformed review is not a blocker.
- Accept a one-shot specialist's result through `alfred_local.py submit --source project_outcome` when the actor is the configured coordinator and the parent is an enrolled user's request that coordinator accepted. It arrives as labeled specialist evidence with no user, owner or release authority. Its reply can close a still-open parent.
- Stop freezing project owners into the route tool's enum at thread creation. Calls are checked against the current `projects.md`. A rejected owner returns the current registry.
- Route briefs and core guidance limit delivery holds to user authority, sign-ins, cost limits, data preservation and privacy. They exclude unrequested reviews, synthetic suites, counters and reporting loops. Completed output moves to Review or Done when delivered. Canceled work leaves Doing.

Migration: `tasks/blocked/` is created on first use. Restart the one collector at a safe boundary to load the new report and attribution code. Existing coordinators keep their creation-time tool schema; owner checks already use the live registry. Saved report numbers still resolve against the delivered report.

## 0.1.5 — 2026-11-17

- Use one entry page at `https://usetempl.com/chief` for host setup, Dot/mobile and additional devices.
- Include the full setup, device, core and optional Telegram instructions in one static guide generated from the source files.
- Point the README prompts and optional Telegram `/setup` command at that page.
- Explain the four starting paths, including Telegram `/report` without model calls.

The complete entry page was verified live before publication. Existing installations keep their private state and permissions.

## 0.1.4 — 2026-11-17

- Load the optional Markdown parser only when rendering a Telegram reply. Local submission and result readers still work with the host's standard Python.

## 0.1.3 — 2026-11-17

- Render Markdown as Telegram bold, italic, strikethrough, named links and code.
- Preserve Unicode offsets and styles across numbered message parts and photo captions.
- Save the exact delivery plan before sending and verify text plus formatting in Telegram receipts.
- Keep existing queued replies, private settings and delivery history unchanged.

The optional Telegram adapter adds markdown-it-py for CommonMark parsing. No model calls or new services.

## 0.1.2 — 2026-11-16

- Follow the GitHub organization rename to `usetempl/chief`.
- Update clone commands, onboarding/device guides and the optional Telegram `/setup` links.

Existing release tags and private installation state are preserved.

## 0.1.1 — 2026-11-16

- Put the Templ-branded architecture diagram in the README.
- Explain the coordinator, project leads, board, memory and optional Telegram adapter below setup.
- Tighten the quick start and describe board columns in context.
- Establish this repository as the upstream for reusable improvements, with Git tags, `VERSION` and this changelog.

Documentation and release metadata only. No runtime or private workspace migration.

## 0.1.0 — 2026-11-16

- Initial MIT-licensed Chief by Templ release from a clean source extraction.
- Codex onboarding, optional nickname, consent-based project discovery and a private five-column task board.
- Optional Telegram adapter with enrolled senders, existing-owner routing, durable requests, report pagination and delivery receipts.
- One architecture diagram and setup, device and technical guides.

23 local setup/transport checks and fresh-clone blank-workspace preparation passed. Actual account enrollment and Windows/macOS/Linux device qualification remain installation-specific.
