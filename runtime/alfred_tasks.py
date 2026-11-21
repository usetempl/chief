#!/usr/bin/env python3
"""Record the verified outcome of one authorized recurring card."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re

from alfred_board import pending_move, read_card, save_move


def timestamp(raw):
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        raise ValueError("Run times need an explicit UTC offset")
    return value


def schedule(fields):
    match = re.fullmatch(r"every ([1-9][0-9]{0,2}) days", fields.get("Recurrence", ""))
    if not match or int(match[1]) > 365:
        raise ValueError("Recurrence must be every 1 through 365 days")
    if not fields.get("Authority", "").strip():
        raise ValueError("Record the existing authority before enabling a schedule")
    if fields.get("Schedule driver") not in {"chief", "maintenance"}:
        raise ValueError("Choose the existing chief or maintenance driver")
    if fields["Schedule driver"] == "chief" and not re.fullmatch(r"chief|(?:codex|claude):[A-Za-z0-9_-]+", fields.get("Schedule owner", "")):
        raise ValueError("Record chief or one existing project owner as Schedule owner")
    return int(match[1]), timestamp(fields["Next run"])


def occurrence(path, fields):
    return "recurring:" + hashlib.sha256((path.name + "\n" + fields["Next run"] + "\n" + fields["Authority"] + "\n" + fields.get("Schedule owner", "")).encode()).hexdigest()


def replace_fields(text, updates):
    for name, value in updates.items():
        if "\n" in value or "\r" in value:
            raise ValueError("Card fields must fit on one line")
        pattern = re.compile(r"^" + re.escape(name) + r":.*$", re.M)
        if pattern.search(text):
            text = pattern.sub(lambda _: name + ": " + value, text, count=1)
        else:
            head, sep, rest = text.partition("\n\n")
            text = head + "\n\n" + name + ": " + value + "\n" + rest
    return text


def complete(path, outcome, now=None, *, expected_run):
    """Advance the recorded occurrence only after the operator verifies its outcome."""
    path = Path(path)
    original_path = path
    if not path.exists():
        path = path.parent.parent / "todo" / path.name
    text, fields = read_card(path)
    pending = pending_move(text)
    if pending and pending["to"] == path.parent.name:
        path = save_move(path, pending["to"], text, expected=text, now=now)
        text, fields = read_card(path)
    days, due = schedule(fields)
    if (fields.get("Last run") == expected_run and fields.get("Last occurrence") == occurrence(path, fields | {"Next run": expected_run})
            and fields.get("Last completed") and fields.get("Last done") == outcome and due > timestamp(expected_run)):
        # An old receipt cannot undo a later parking or a new occurrence's move.
        # Recover only its still-pending completion transition; a settled Todo
        # retry is already complete and never advances another slot.
        if path.parent.name != "todo" and not (pending and pending["to"] == "todo"
                                               and pending["from"] == path.parent.name):
            raise ValueError("Occurrence already completed; preserve its current column")
        return save_move(path, "todo", text, expected=text, reason="Recurring occurrence completed", now=now)
    if path != original_path:
        raise ValueError("Original card is absent; no matching completion receipt")
    if fields["Next run"] != expected_run or path.parent.name not in {"doing", "review", "done"}:
        raise ValueError("Only the exact current occurrence can be completed")
    if not outcome.strip():
        raise ValueError("A verified completion outcome is required")
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None or now < due:
        raise ValueError("Completion cannot precede its due occurrence")
    step = timedelta(days=days)
    following = due + step * (int((now - due) // step) + 1)
    stamp = now.astimezone(due.tzinfo).isoformat(timespec="seconds")
    updated = replace_fields(text, {"Next run": following.isoformat(timespec="seconds"),
                                "Last run": expected_run, "Last occurrence": occurrence(path, fields),
                                "Last completed": stamp, "Last done": outcome,
                                "Updated": stamp, "Next": "Run at the next scheduled date within the existing scope."})
    return save_move(path, "todo", updated, expected=text, reason="Recurring occurrence completed", now=now)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("complete",))
    parser.add_argument("card", type=Path)
    parser.add_argument("--expected-run", required=True)
    parser.add_argument("--outcome", required=True)
    args = parser.parse_args()
    print(complete(args.card, args.outcome, expected_run=args.expected_run))


if __name__ == "__main__":
    main()
