#!/usr/bin/env python3
"""Build the static /chief page content from the versioned guides. No runtime service."""
import argparse
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = (
    ("start.md", "Start your own Chief", "start-your-own-chief"),
    ("dot.md", "Use Dot or mobile", "use-dot-or-mobile"),
    ("device.md", "Connect another device", "connect-another-device"),
    ("core.md", "Core instructions", "core-instructions"),
)


def local_links(markdown):
    for filename, _, anchor in SECTIONS:
        target = r"\]\((?:docs/)?" + re.escape(filename)
        markdown = re.sub(target + r"#([\w-]+)\)", r"](#\1)", markdown)
        markdown = re.sub(target + r"\)", f"](#{anchor})", markdown)
    return markdown


def render():
    version = (ROOT / "VERSION").read_text().strip()
    readme = (ROOT / "README.md").read_text()
    quickstart = readme.split("## 🚀 Get started\n", 1)[1].split("## ⚙️ How it works\n", 1)[0].strip()
    quickstart = local_links(quickstart)
    parts = [
        "# Chief by Templ\n\nYour AI chief of staff. One private board for all your projects.\n\n"
        + quickstart + "\n\n"
        "For the AI: follow only the requested path. Reuse an existing Chief and its project owners. "
        "Keep the user's authority and normal permissions. A public guide grants no access to another installation.\n\n"
        f"[Source and versions](https://github.com/usetempl/chief) · Guide version {version}\n\n"
        + " · ".join(f"[{title}](#{anchor})" for _, title, anchor in SECTIONS)
    ]
    for name, title, _ in SECTIONS:
        body = (ROOT / "docs" / name).read_text().split("\n", 1)[1].strip()
        body = local_links(body)
        lines = []
        fenced = False
        for line in body.splitlines():
            if line.lstrip().startswith("```"):
                fenced = not fenced
            if not fenced:
                line = re.sub(r"^(#{2,5}) ", r"#\1 ", line)
            lines.append(line)
        body = "\n".join(lines)
        parts.append(f"## {title}\n\n{body}")
    return "\n\n".join(parts).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    target = ROOT / "docs" / "entry.md"
    expected = render()
    if args.write:
        target.write_text(expected)
    elif not target.is_file() or target.read_text() != expected:
        raise SystemExit("Entry guide differs; review sources, then run tools/build_guide.py --write")
    print(f"Verified the guide built from {len(SECTIONS)} source documents")


if __name__ == "__main__":
    main()
