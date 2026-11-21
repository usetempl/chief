# Work on the Chief source

Keep reusable code and guides here. Use a separate private workspace for accounts, projects, task cards, evidence and conversation history.

For onboarding, follow [the setup guide](docs/start.md). Chief operates in the existing signed-in conversation and uses each project’s existing owner. The app controls sign-in and permissions. The source supplies instructions, plain-file board tools and an optional workspace helper. Preserve operator-specific release and access rules.

## Verify a change

Run the following commands from the source checkout. The Python tools use the standard library.

```sh
python3 tools/build_guide.py --write
python3 tools/manifest.py --write
python3 tools/build_guide.py
python3 tools/manifest.py
python3 -m unittest discover -s tests
```

The guide builder combines the README quickstart with the four guides. Commit the generated `docs/entry.md` and review the exact release files in `MANIFEST.json`. An operator publishing a separate documentation site should use the guide from the same tag for HTML, plain Markdown and navigation.

## Release

Update `VERSION` and `CHANGELOG.md`, build and verify the guide and manifest, and inspect the full public diff. Tag the verified commit with the matching immutable `vX.Y.Z` version. Document breaking changes and the steps for existing users. Keep installation settings, accounts and private Git history on the operator’s host. Record the published source version separately from any installed copy.
