# Changelog

## 0.1.5 — pending website publication

- Use one entry page at `https://usetempl.com/chief` for host setup, Dot/mobile and additional devices.
- Include the full setup, device, core and optional Telegram instructions in one static guide generated from the source files.
- Point the README prompts and optional Telegram `/setup` command at that page.
- Explain the four starting paths, including Telegram `/report` without model calls.

Existing installations keep their private state and permissions. Activate the new entry links after the complete page is verified live.

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
