#!/usr/bin/env python3
"""Move the existing card and keep a small, timestamped history inside it."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile

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


def read_card(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise ValueError("A bounded regular task card is required")
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError("Task card must be owned by this user and not writable by others")
    text = path.read_text(encoding="utf-8")
    fields = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z][A-Za-z ]+): (.*)$", line)
        if match:
            if match[1] in fields and match[1] in {"Recurrence", "Next run", "Schedule driver", "Schedule owner", "Authority"}:
                raise ValueError("Ambiguous schedule fields")
            fields.setdefault(match[1], match[2])
    return text, fields


def save_move(path, state, text, *, expected=None, reason="Workflow transition", now=None):
    """Rewrite one card atomically, then move it; refuse a second destination."""
    path = Path(path)
    original, _ = read_card(path)  # Missing/revoked sources must never be recreated.
    if expected is not None and original != expected:
        raise ValueError("Task changed; preserve the current card and reconcile")
    root = path.parent.parent
    if state not in STATES or path.parent.name not in STATES or root.name != "tasks":
        raise ValueError("Use a card directly inside a canonical task column")
    stamp(now)
    if state == "blocked":
        blocker(text)
    if root.is_symlink() or path.parent.is_symlink() or (root / state).is_symlink():
        raise ValueError("Task columns cannot be symlinks")
    recovered = pending_move(original) is not None
    text = prepare_move(text, path.parent.name, state, reason)
    destination = root / state / path.name
    pending = pending_move(original)
    if destination == path and pending and pending["to"] == state and pending["from"] != state:
        # Recovery may start at the destination after link succeeded but source
        # unlink failed. Remove that exact same-inode source before rewriting;
        # replacing the destination first would split one card into two copies.
        source = root / pending["from"] / path.name
        if source.exists() or source.is_symlink():
            if source.parent.is_symlink():
                raise ValueError("Task columns cannot be symlinks")
            source_text, _ = read_card(source)
            if not path.samefile(source) or source_text != original:
                raise ValueError("Conflicting original card needs reconciliation")
            source.unlink()
    if destination != path and (destination.exists() or destination.is_symlink()):
        # A crash after link but before unlink leaves the same card in both
        # columns. Finish only that move, never overwrite an independent card.
        read_card(destination)
        if path.samefile(destination) and original == text:
            path.unlink()
            return save_move(destination, state, text, expected=text, now=now)
        raise ValueError("A card with this name already exists in the destination")
    destination.parent.mkdir(exist_ok=True)
    if text != original:
        fd, temporary = tempfile.mkstemp(prefix=".task-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            if read_card(path)[0] != original:
                raise ValueError("Task changed before update; preserve it")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if destination != path:
        # Link the existing source; queueing never republishes cached text.
        os.link(path, destination)
        if read_card(destination)[0] != text:
            destination.unlink()
            raise ValueError("Task changed before move; preserve it")
        path.unlink()
    finished = finish_move(text, state, now=now, recovered=recovered)
    if finished != text:
        # The destination exists before its history claims a completed move.
        # A crash leaves a recoverable marker, never a fabricated transition.
        return save_move(destination, state, finished, expected=text, now=now)
    return destination


def main():
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
