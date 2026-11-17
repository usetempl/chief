#!/usr/bin/env python3
"""Dated recurring cards, using the existing collector and request queue.

The Markdown folder is the task state. A queue receipt only deduplicates a due
occurrence; it never proves completion. No command, model or network runs here.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from alfred_board import STATES, blocker, prepare_move, finish_move, pending_move, stamp

ATTRIBUTION = (
    "A previously authorized recurring task is due. This is a scheduler notice, "
    "not a new human instruction or release approval. Read the named private task card, "
    "confirm its standing authority and use its existing project owner. Preserve all "
    "access and release boundaries. Do not expand scope or repeat uncertain work. "
    "Keep failed or blocked work open. Only after the actual outcome is verified, "
    "return the same recurring card to todo with its next future run and completion evidence. "
)


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


def request_scope(request_id, workspace, *, completed_reply=False):
    """Removal, rescheduling or changed standing authority revokes queued work."""
    for column in (STATES if completed_reply else ("todo", "doing")):
        folder = Path(workspace) / "tasks" / column
        if folder.is_symlink():
            return None
        for path in folder.glob("*.md"):
            try:
                text, fields = read_card(path)
                pending = pending_move(text)
                if pending and column not in {pending["from"], pending["to"]}:
                    continue
                effective_column = pending["to"] if pending else column
                if fields.get("Schedule driver") != "chief":
                    continue
                schedule(fields)
                scope = {"card": path.name, "owner": fields["Schedule owner"], "occurrence": fields["Next run"]}
                if effective_column in {"todo", "doing"} and occurrence(path, fields) == request_id:
                    return scope
                if (completed_reply and fields.get("Last occurrence") == request_id
                        and fields.get("Last completed") and fields.get("Last run")):
                    timestamp(fields["Last completed"])
                    if occurrence(path, fields | {"Next run": fields["Last run"]}) == request_id:
                        return scope | {"occurrence": fields["Last run"]}
            except (OSError, ValueError, KeyError, UnicodeError):
                continue
    return None


def request_allowed(request_id, workspace, *, completed_reply=False):
    return request_scope(request_id, workspace, completed_reply=completed_reply) is not None


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


def complete(path, outcome, now=None, *, expected_run):
    """Explicit operator completion. A Telegram reply alone never calls this."""
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


def due_tasks(inbox, workspace, now=None):
    """Called by the collector. Queue each due occurrence once, never replay it."""
    from alfred_local import ensure_context, private_root
    from operator_policy import OWNER_LABEL
    private_root(inbox.root)
    ensure_context(inbox)
    now = datetime.now(timezone.utc) if now is None else now
    queued, errors = False, 0
    # A crash can leave a future-dated completion in Todo, or an admitted
    # occurrence in Doing. Finish only durable recurring-card move markers
    # before looking for due work. This does not run or reschedule a task.
    for column in STATES:
        folder = Path(workspace) / "tasks" / column
        if folder.is_symlink():
            raise ValueError("Task directory cannot be a symlink")
        for path in sorted(folder.glob("*.md"))[:500]:
            try:
                text, fields = read_card(path)
                if fields.get("Schedule driver") != "chief":
                    continue
                pending = pending_move(text)
                if pending:
                    save_move(path, pending["to"], text, expected=text, now=now)
                    queued = True  # Wake the existing dispatcher to reconcile its receipt.
            except (OSError, ValueError, KeyError, UnicodeError):
                errors += 1
    folder = Path(workspace) / "tasks" / "todo"
    if folder.is_symlink():
        raise ValueError("Task directory cannot be a symlink")
    for path in sorted(folder.glob("*.md"))[:500]:
        try:
            text, fields = read_card(path)
            if pending_move(text):
                raise ValueError("Finish the card move before scheduling work")
            if "Recurrence" not in fields or fields.get("Schedule driver") == "maintenance":
                continue
            _, due = schedule(fields)
            if due > now:
                continue
            request_id = occurrence(path, fields)
            digest = request_id.split(":", 1)[1]
            with inbox.db:
                inbox.db.execute("BEGIN IMMEDIATE")
                owners = inbox.db.execute("SELECT * FROM users WHERE label=?", (OWNER_LABEL,)).fetchall()
                if len(owners) != 1 or owners[0]["user_id"] != owners[0]["chat_id"]:
                    raise ValueError("The original owner must be enrolled")
                owner = owners[0]
                previous = inbox.db.execute("SELECT user_id,chat_id,label,received FROM requests WHERE id=?", (request_id,)).fetchone()
                if previous is None:
                    if inbox.db.execute("SELECT 1 FROM request_context WHERE event_id=?", (request_id,)).fetchone():
                        raise ValueError("Earlier occurrence receipt retained; reconcile without replay")
                    # One receipt per card occurrence bounds scheduled work; dispatch
                    # already runs it after human requests without steering them.
                    body = ("Recurring task due. Read the private task card " + json.dumps(path.name)
                            + " in tasks/doing (or tasks/todo if its committed queue receipt preceded the move). "
                            "Reconcile the same occurrence before work; do not create a second task.\nScheduled occurrence: " + fields["Next run"])
                    identifier = -(int(digest[:15], 16) + 1)
                    inbox.db.execute("INSERT INTO requests(id,update_id,user_id,chat_id,message_id,label,body,received) VALUES (?,?,?,?,?,?,?,?)",
                                     (request_id, identifier, owner["user_id"], owner["chat_id"], identifier, owner["label"], body, now.timestamp()))
                    inbox.db.execute("INSERT INTO request_context VALUES (?,?,?,?,NULL)",
                                     (request_id, "scheduled_task", "collector-schedule", request_id))
                elif (previous["user_id"], previous["chat_id"], previous["label"]) != (owner["user_id"], owner["chat_id"], owner["label"]) or previous["received"] < owner["added"]:
                    raise ValueError("Original occurrence enrollment changed; reconcile without replay")
                # Receipt commits before the file move. A crash retries the move,
                # not the request. The collector wakes the adapter only afterward.
            save_move(path, "doing", text, expected=text, reason="Recurring occurrence due", now=now)
            queued = True
        except (OSError, ValueError, KeyError, UnicodeError):
            errors += 1  # Fixed metadata only; no private task content in diagnostics.
    health = "ready" if errors == 0 else f"{errors}_cards_need_reconciliation"
    if inbox.meta("recurring_tasks_health") != health:
        with inbox.db:
            inbox.set_meta("recurring_tasks_health", health)
    return queued


def maintenance_card(workspace, state):
    """Reflect the existing weekly driver's saved receipt; never schedule it twice."""
    paths = [Path(workspace) / "tasks" / column / "weekly-housekeeping.md" for column in ("todo", "doing")]
    present = [path for path in paths if path.exists()]
    if not present or not state.get("latest", {}).get("checked") or not state.get("next_due"):
        return
    successful = state["latest"].get("status") in {"healthy", "findings"}
    target = "todo" if successful else "doing"
    if len(present) == 2:
        for path in present:
            read_card(path)
        if not present[0].samefile(present[1]):
            raise ValueError("Conflicting maintenance cards need reconciliation")
        unchanged, _ = read_card(present[0])
        pending = pending_move(unchanged)
        recovery_target = pending["to"] if pending else target
        path = next(path for path in present if path.parent.name != recovery_target)
        path = save_move(path, recovery_target, unchanged, expected=unchanged)
    else:
        path = present[0]
    text, fields = read_card(path)
    if pending_move(text):
        path = save_move(path, pending_move(text)["to"], text, expected=text)
        text, fields = read_card(path)
    if fields.get("Schedule driver") != "maintenance":
        return
    _, old_due = schedule(fields)
    stamp = datetime.fromtimestamp(state["latest"]["checked"], old_due.tzinfo).isoformat(timespec="seconds")
    if not successful:
        if fields.get("Last attempt") == stamp:
            save_move(path, "doing", text, expected=text)
            return
        retry = state.get("check_retry")
        updated = replace_fields(text, {"Last attempt": stamp, "Attempt result": "Failed; completion is not established.",
            "Retry after": datetime.fromtimestamp(retry, old_due.tzinfo).isoformat(timespec="seconds") if retry else "Not recorded",
            "Next": "Existing maintenance driver retries its failed check; preserve the last successful completion."})
        save_move(path, "doing", updated, expected=text)
        return
    if fields.get("Last completed") == stamp:
        save_move(path, "todo", text, expected=text)
        return
    due = datetime.fromtimestamp(state["next_due"], old_due.tzinfo).isoformat(timespec="seconds")
    updated = replace_fields(text, {"Next run": due, "Last completed": stamp, "Updated": stamp, "Last attempt": stamp,
        "Attempt result": "Observation saved.", "Retry after": "None",
        "Last done": "Scheduled metadata observation saved. Any findings keep their separate review and action boundaries.",
        "Next": "Run the next bounded observation at the saved date; unresolved findings do not trigger repeat model reviews."})
    save_move(path, "todo", updated, expected=text)


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
