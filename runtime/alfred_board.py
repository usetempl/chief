#!/usr/bin/env python3
"""Move the existing card and keep a small, timestamped history inside it."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

STATES = ("backlog", "todo", "doing", "blocked", "review", "done")
HEADING = "## Movement history"
PENDING = re.compile(r"^<!-- chief-move: (.+) -->\n?", re.M)
BLOCKER_FIELDS = ("Blocked by", "Resolver")


def blocker(text):
    """Blocked means real missing input: the concrete missing item and who can supply it.

    A running owner, pending agent work or an unperformed extra review is not a blocker.
    """
    values = {}
    for name in BLOCKER_FIELDS:
        match = re.search(r"^" + re.escape(name) + r":[ \t]*(.*?)[ \t]*$", text, re.M)
        value = match[1] if match else ""
        if value.casefold().strip(".") in {"", "none", "n/a", "unknown", "tbd", "unavailable"}:
            raise ValueError("A blocked card needs 'Blocked by:' (the concrete missing input) and 'Resolver:' (who can supply it)")
        values[name] = value
    return values


def stamp(now=None):
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None:
        raise ValueError("Movement timestamps need a UTC offset")
    return now.isoformat(timespec="seconds")


def cell(value):
    return " ".join(str(value).split()).replace("|", "\\|")


def append_history(text, source, target, note, now=None):
    if HEADING not in text.splitlines():
        text = text.rstrip() + "\n\n" + HEADING + "\n\n| Time | From | To | Note |\n| --- | --- | --- | --- |\n"
    lines = text.splitlines()
    start = lines.index(HEADING) + 1
    while start < len(lines) and not lines[start].strip():
        start += 1
    if lines[start:start + 2] != ['| Time | From | To | Note |', '| --- | --- | --- | --- |']:
        raise ValueError('Movement history table needs reconciliation')
    end = start + 2
    while end < len(lines) and lines[end].startswith('| '):
        end += 1
    lines.insert(end, f"| {stamp(now)} | {source} | {target} | {cell(note)} |")
    return '\n'.join(lines) + '\n'


def pending_move(text):
    matches = PENDING.findall(text)
    if len(matches) > 1:
        raise ValueError("Multiple pending card moves need reconciliation")
    if not matches:
        return None
    item = json.loads(matches[0])
    if (set(item) != {"from", "to", "note"} or item["from"] not in STATES
            or item["to"] not in STATES or not isinstance(item["note"], str)):
        raise ValueError("Invalid pending card move")
    return item


def prepare_move(text, source, target, note):
    pending = pending_move(text)
    if pending:
        if pending["to"] != target or source not in {pending["from"], target}:
            raise ValueError("Finish the pending card move before another transition")
        return text
    if source == target:
        return text
    pending = {"from": source, "to": target, "note": cell(note)}
    return text.rstrip() + "\n\n<!-- chief-move: " + json.dumps(pending, ensure_ascii=False) + " -->\n"


def finish_move(text, target, now=None, recovered=False):
    pending = pending_move(text)
    if not pending:
        return text
    if pending["to"] != target:
        raise ValueError("Card has not reached the intended column")
    note = pending["note"]
    if recovered:
        note += " (interrupted move; completion observed now)"
    return append_history(PENDING.sub("", text), pending["from"], target, note, now)


def main():
    from alfred_tasks import read_card, save_move
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    move = commands.add_parser("move")
    move.add_argument("card", type=Path)
    move.add_argument("state", choices=STATES)
    move.add_argument("--reason", required=True)
    initialize = commands.add_parser("initialize")
    initialize.add_argument("tasks", type=Path)
    args = parser.parse_args()
    if args.command == "move":
        text, _ = read_card(args.card)
        print(save_move(args.card, args.state, text, expected=text, reason=args.reason))
    else:
        count = 0
        for state in STATES:
            for path in sorted((args.tasks / state).glob("*.md")):
                text, _ = read_card(path)
                if HEADING in text.splitlines():
                    continue
                updated = append_history(text, "unknown", state, "Tracking baseline; earlier move times are unknown.")
                save_move(path, state, updated, expected=text)
                count += 1
        print(f"Initialized movement history on {count} cards; no historical times inferred.")


if __name__ == "__main__":
    main()
