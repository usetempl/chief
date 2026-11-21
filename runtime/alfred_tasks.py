#!/usr/bin/env python3
"""Record the verified outcome of one authorized recurring card."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re

from alfred_board import pending_move, read_card, save_move


def timestamp(raw):
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
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


def complete(path, outcome, now=None, *, expected_run, early_authority=None):
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
    if path.parent.name == "todo" and fields["Next run"] == expected_run:
        raise ValueError("Move this authorized Todo card to Doing with alfred_board.py move CARD doing --reason REASON as work starts. Verify the result before completing it.")
    if fields["Next run"] != expected_run or path.parent.name not in {"doing", "review", "done"}:
        raise ValueError("Only the exact current occurrence can be completed")
    if not outcome.strip():
        raise ValueError("A verified completion outcome is required")
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None:
        raise ValueError("Completion time needs a UTC offset")
    if now < due and not (early_authority and early_authority.strip()):
        raise ValueError("Counting an early run requires the operator's explicit --early-authority REQUEST-REF for this occurrence.")
    step = timedelta(days=days)
    following = due + step * (max(0, (now - due) // step) + 1)
    stamp = now.astimezone(due.tzinfo).isoformat(timespec="seconds")
    authority = early_authority.strip() if now < due else fields["Authority"]
    updated = replace_fields(text, {"Next run": following.isoformat(timespec="seconds"),
                                "Last run": expected_run, "Last occurrence": occurrence(path, fields),
                                "Last completion authority": authority,
                                "Last completed": stamp, "Last done": outcome,
                                "Updated": stamp, "Next": "Run at the next scheduled date within the existing scope."})
    reason = "Recurring occurrence completed" if now >= due else "Early occurrence completed under " + authority
    return save_move(path, "todo", updated, expected=text, reason=reason, now=now)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("complete",))
    parser.add_argument("card", type=Path)
    parser.add_argument("--expected-run", required=True)
    parser.add_argument("--outcome", required=True)
    parser.add_argument("--early-authority", metavar="REQUEST-REF",
                        help="Operator's explicit request to count this run as the named future occurrence")
    args = parser.parse_args()
    print(complete(args.card, args.outcome, expected_run=args.expected_run, early_authority=args.early_authority))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(str(error))
