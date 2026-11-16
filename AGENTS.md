# Chief source

This is the public source, not a private working board. For user onboarding, read docs/start.md and create or reuse a separate private workspace. Do not put personal projects, account IDs, credentials, conversations or operational data here.

Keep Chief small: instructions and Markdown records guide model judgment. Preserve existing owners and current user authorization. Optional adapters authenticate, route and deliver; they do not choose plans or rank models. Never add AI task runtime, token or answer-length budgets.

Use Python 3.12+ for the optional Telegram adapter. Install the optional adapter’s `requirements.txt` in a virtual environment, then run `python3 -m unittest discover -s tests` after setup/runtime changes. Run `python3 tools/manifest.py --write` after intentionally changing packaged sources, then `python3 tools/manifest.py` to verify. Inspect the diff before publishing. Preserve the independent workspace and its private state.
