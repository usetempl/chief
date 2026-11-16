#!/usr/bin/env python3
"""Print a read-only report from saved task cards."""

import argparse
from datetime import datetime
from pathlib import Path
import re

FIELDS = ("Report name", "Report group", "Emoji", "Priority", "Project mode",
          "Updated", "Snapshot", "Last done", "Next", "Needs input")
STATES = {"doing": 0, "todo": 1, "review": 2, "backlog": 3, "done": 4}


def read_cards(root, input_field="Needs input"):
    cards = []
    for state in STATES:
        folder = root / "tasks" / state
        for path in sorted(folder.rglob("*.md")):
            if path.name.lower() == "readme.md":
                continue
            values = {}
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                content = ""
            for line in content.splitlines():
                match = re.match(r"^([^:]+):\s*(.*?)\s*$", line)
                if match and match.group(1) in (*FIELDS, input_field, "Progress estimate", "Usable version", "Reviewer"):
                    key = "Needs input" if match.group(1) == input_field else match.group(1)
                    values.setdefault(key, match.group(2))
            values["_state"] = state
            values["_path"] = str(path.relative_to(root))
            cards.append(values)
    return cards


def value(card, key):
    return card.get(key, "").strip() or "unavailable"


def progress(card):
    raw = card.get("Progress estimate", "").strip()
    if re.fullmatch(r"(?:100|[0-9]{1,2})%", raw):
        return raw
    return "unavailable"


def order(card):
    try:
        priority = int(card.get("Priority", ""))
    except ValueError:
        priority = 999999
    return priority, value(card, "Report name").casefold(), card["_path"]


def saved_order(card):
    """Sort saved checkpoints, never file modification or live observation time."""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}(?::\d{2})?))?", card.get("Updated", ""))
    try:
        stamp = datetime.fromisoformat(match[1] + "T" + (match[2] or "00:00")) if match else datetime.min
    except ValueError:
        stamp = datetime.min
    return stamp, card["_path"]


def usable_version(card, entries):
    """Use an explicit saved link, never infer readiness from a draft URL."""
    if card.get("Usable version", "").strip():
        return card["Usable version"].strip()
    linked = [entry for entry in entries if entry.get("Usable version", "").strip()]
    return max(linked, key=saved_order)["Usable version"].strip() if linked else "unavailable"


def render(root, input_field="Needs input", excluded_names=()):
    excluded = {name.strip().casefold() for name in excluded_names}
    cards = [card for card in read_cards(root, input_field)
             if card.get("Report name", "").strip().casefold() not in excluded]
    groups = {}
    for card in cards:
        # Missing names stay visible and cannot accidentally merge unrelated cards.
        name = card.get("Report name", "").strip()
        key = name.casefold() if name else "\0" + card["_path"]
        groups.setdefault(key, []).append(card)

    sections = {name: [] for name in ("Work", "Hobby", "Review", "Backlog", "Parked", "Completed", "Needs attention", "Operations")}
    selected = []
    for entries in groups.values():
        entries.sort(key=lambda c: (STATES[c["_state"]], c["_path"]))
        current = [c for c in entries if c["_state"] != "done" and
                   c.get("Project mode", "") in ("active", "finish-then-park")]
        # Keep current-folder precedence, but do not let an alphabetically earlier
        # completed task hide a newer completed milestone for the same project.
        same_state = [card for card in entries if card["_state"] == entries[0]["_state"]]
        selected.extend((card, entries) for card in (current or [max(same_state, key=saved_order)]))

    for card, entries in selected:
        group, mode = card.get("Report group", ""), card.get("Project mode", "")
        if group == "Hidden":
            continue
        name, emoji, updated = value(card, "Report name"), value(card, "Emoji"), value(card, "Updated")
        issues = [field for field in FIELDS if not card.get(field, "").strip()]
        if group not in ("Work", "Hobby", "Operations"):
            issues.append("valid Report group")
        if mode not in ("active", "parked", "finish-then-park", "complete"):
            issues.append("valid Project mode")
        try:
            int(card.get("Priority", ""))
        except ValueError:
            if "Priority" not in issues:
                issues.append("valid Priority")
        if issues:
            name = name if name != "unavailable" else card["_path"]
        prefix = f"{emoji} {name}"
        date = f"saved {updated}"
        version = f"Latest usable: {usable_version(card, entries)}"
        if card["_state"] == "backlog":
            line = f"{prefix}: {value(card, 'Snapshot')} Not committed. Next decision: {value(card, 'Next')}. {version}"
            sections["Backlog"].append((order(card), line))
        elif card["_state"] == "review":
            if not card.get("Reviewer", "").strip():
                issues.append("Reviewer")
            line = f"{prefix}: {value(card, 'Snapshot')} Reviewer: {value(card, 'Reviewer')}. Next: {value(card, 'Next')} ({date}). {version}"
            sections["Review"].append((order(card), line))
        elif group == "Operations":
            line = f"{prefix}: {value(card, 'Snapshot')} ({date}). {version}"
            sections["Operations"].append((order(card), line))
        elif mode == "parked":
            # A done card may provide a milestone, but never use an in-progress
            # checkpoint as evidence of completed work.
            milestone = card.get("Last done", "").strip()
            if not milestone:
                done = sorted((c for c in entries if c["_state"] == "done" and c.get("Last done", "").strip()),
                              key=lambda c: (c.get("Updated", ""), c["_path"]), reverse=True)
                milestone = done[0]["Last done"].strip() if done else "unavailable"
                if done:
                    date = f"saved {value(done[0], 'Updated')}"
            sections["Parked"].append((order(card), f"🔴 {prefix}: {milestone} ({date}). {version}"))
        elif mode in ("active", "finish-then-park") and card["_state"] != "done":
            needs = card.get("Needs input", "").strip()
            required = bool(needs and needs.casefold() != "none")
            marker = "⚪" if not needs else ("🟡" if required else "🟢")
            suffix = " [finish then park]" if mode == "finish-then-park" else ""
            action = (f"{input_field}: unavailable" if not needs else
                      f"{input_field}: {needs}" if required else f"Next: {value(card, 'Next')}")
            snapshot = value(card, "Snapshot").rstrip(". ")
            line = f"{prefix} {marker}{suffix}: {snapshot}. Progress: {progress(card)}. {action} ({date}). {version}"
            sections[group if group in ("Work", "Hobby") else "Needs attention"].append((order(card), line))
        elif mode == "complete" or card["_state"] == "done":
            if group in ("Work", "Hobby") and mode == "complete":
                sections["Completed"].append((order(card), f"{prefix}: {value(card, 'Last done').rstrip('. ')}. Progress: {progress(card)} ({date}). {version}"))
        else:
            sections["Needs attention"].append((order(card), f"{prefix}: {value(card, 'Snapshot')} ({date}). {version}"))
        if issues:
            sections["Needs attention"].append((order(card), f"{card['_path']}: unavailable or invalid: {', '.join(dict.fromkeys(issues))}"))

    output = ["Saved task-board snapshot; no live checks. Progress percentages are approximate delivery-stage estimates, not measured work or time remaining."]
    for section, lines in sections.items():
        if lines:
            output.append(f"**{section}**\n\n" + "\n".join(f"- {line}" for _, line in sorted(lines)))
    if len(output) == 1:
        output.append("No saved task cards found.")
    return "\n\n".join(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Private task-board directory")
    parser.add_argument("--input-field", default="Needs input", help="Input field and report label")
    parser.add_argument("--exclude", action="append", default=[], help="Exact report name to omit; repeatable")
    args = parser.parse_args()
    print(render(args.root.resolve(), args.input_field, args.exclude))


if __name__ == "__main__":
    main()
