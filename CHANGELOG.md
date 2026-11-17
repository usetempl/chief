# Changelog

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
