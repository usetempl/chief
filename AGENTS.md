# Chief source

This is the public source, not a private working board. For user onboarding, read docs/start.md and create or reuse a separate private workspace. Do not put personal projects, account IDs, credentials, conversations or operational data here.

The public entry is `https://usetempl.com/chief`. Its static content, `docs/entry.md`, is generated from the five local guides by `python3 tools/build_guide.py --write`; run without `--write` to check it. Regenerate after guide or version changes. This build tool adds no service or dependency to a user's Chief. Publish the full content in the page HTML so an AI can read every path without signing in or running JavaScript. Verify the destination before switching live entry links.

Keep Chief small: instructions and Markdown records guide model judgment. Preserve existing owners and current user authorization. Optional adapters authenticate, route and deliver; they do not choose plans or rank models. Never add AI task runtime, token or answer-length budgets.

Use Python 3.12+ for the optional Telegram adapter. Install the optional adapter’s `requirements.txt` in a virtual environment, then run `python3 -m unittest discover -s tests` after setup/runtime changes. Run `python3 tools/manifest.py --write` after intentionally changing packaged sources, then `python3 tools/manifest.py` to verify. Inspect the diff before publishing. Preserve the independent workspace and its private state.

## Source and versions

Implement reusable improvements in this repository. If a fix starts in a private installation, port its generic code, instructions and relevant checks here before marking that improvement complete. Keep installation policy, identities, credentials, tasks, conversations and runtime databases private. Never copy private Git history into this repository.

Use ordinary Git for versioning. Each release updates `VERSION` and `CHANGELOG.md`, regenerates `MANIFEST.json`, and uses a matching immutable `vX.Y.Z` tag. Batch related changes into one release. Patch versions cover compatible fixes and documentation; minor versions cover new features. State breaking changes and migration steps explicitly while versions are below 1.0. Do not move published tags or add a release daemon.

Verify the changed behavior, manifest and exact public diff before pushing. Documentation-only changes need link/content checks, not a repeated runtime suite. Record the published tag/commit separately from the version actually installed by an operator. An update must preserve private state and existing authorization; pulling source is not a runtime migration or permission to restart a service.
