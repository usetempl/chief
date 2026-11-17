# Changelog

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
