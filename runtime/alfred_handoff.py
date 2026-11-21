#!/usr/bin/env python3
"""Record one native assignment and its return on the originating private card."""
import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import re
import sys

from alfred_board import STATES, board_lock, pending_move, read_card, stamp, write_card


PREFIX = "Chief handoff: "


def find_card(path):
    tasks = path.parent.parent
    if tasks.name != "tasks" or path.parent.name not in STATES or path.suffix != ".md":
        raise ValueError("Supply the original Markdown card in a task column")
    matches = []
    for state in STATES:
        folder = tasks / state
        if tasks.is_symlink() or folder.is_symlink():
            raise ValueError("Task columns must be real directories")
        candidate = folder / path.name
        if candidate.exists() or candidate.is_symlink():
            matches.append(candidate)
    if len(matches) != 1:
        raise ValueError("Locate the unique original card before continuing the handoff")
    return matches[0]


def find_record(text, key):
    record = None
    original_line = None
    for line in text.split("\n"):
        if line.startswith(PREFIX):
            saved = json.loads(line[len(PREFIX):])
            if saved["key"] == key:
                if record is not None:
                    raise ValueError("Multiple handoffs use this key on the card")
                record, original_line = saved, line
    return record, original_line


def transition(record, action, attempt, receipt, body):
    """Return true only to the caller that reserved a new native send."""
    attempts = record["attempts"]
    if not isinstance(attempt, int) or not 1 <= attempt <= len(attempts):
        raise ValueError("Read the current attempt on the originating card")
    item = attempts[attempt - 1]
    if action == "retry":
        if attempt == len(attempts) and item["status"] == "refused":
            attempts.append({"status": "unconfirmed", "started": stamp()})
            return True
        return False
    # An exact repeated acknowledgement is harmless, including after a later transition.
    if action == "refused" and item.get("refusal") == receipt:
        return False
    if action == "accept" and item.get("turn") == receipt:
        return False
    if attempt != len(attempts):
        raise ValueError("This evidence belongs to an earlier attempt")
    if action == "refused" and item["status"] == "unconfirmed":
        item.update(status="refused", refusal=receipt, refused_at=stamp())
    elif action == "accept" and item["status"] in {"unconfirmed", "refused"}:
        item.update(status="accepted", turn=receipt, accepted_at=stamp())
    elif action == "return" and item.get("turn") == receipt and item["status"] in {"accepted", "returned"}:
        if item["status"] == "returned":
            if record["result"] != body:
                raise ValueError("A different result is already saved for this assignment")
            return False
        item.update(status="returned", returned_at=stamp())
        record["result"] = body
    else:
        raise ValueError("The saved status or owner turn conflicts with this evidence")
    return False


def handoff(card, key, action, *, owner=None, origin=None, attempt=None, receipt=None, body=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", key):
        raise ValueError("Use a stable assignment key containing letters, digits, dots, dashes or underscores")
    if action not in {"offer", "status", "retry", "refused", "accept", "return"}:
        raise ValueError("Unknown handoff action")
    if action != "status" and not (owner and owner.strip()):
        raise ValueError("Identify the existing project owner")
    if action in {"offer", "return"} and not (body and body.strip()):
        raise ValueError("Supply the brief or complete outcome on standard input")
    if action in {"accept", "refused", "return"} and not (receipt and receipt.strip()):
        raise ValueError("Supply the native turn or refusal reference")
    card = Path(card).absolute()
    lock = nullcontext() if action == "status" else board_lock(card.parent.parent)
    with lock:
        path = find_card(card)
        text, _ = read_card(path)
        if pending_move(text):
            raise ValueError("Finish the recorded card move before using this handoff")
        record, line = find_record(text, key)
        return_label = f"HANDOFF_RESULT {path.name}#{key}"
        if action == "return":
            label, sep, outcome = body.partition("\n")
            if label != return_label or not sep or not outcome.strip():
                raise ValueError("The owner result must start with this assignment's return label on its own line")
            body = outcome
        send_once = False
        if action == "offer":
            if not origin or not origin.strip():
                raise ValueError("Identify the original request and its reply destination")
            identity = {"key": key, "origin": origin, "owner": owner, "brief": body}
            if record is None:
                record = dict(identity, attempts=[{"status": "unconfirmed", "started": stamp()}])
                send_once = True
            elif any(record[field] != value for field, value in identity.items()):
                raise ValueError("This key already belongs to a different request, owner or brief")
        elif record is None:
            raise ValueError("No handoff with that key exists on this card")
        elif action != "status":
            if owner != record["owner"]:
                raise ValueError("Use the owner recorded on this assignment")
            send_once = transition(record, action, attempt, receipt, body)
        if action != "status":
            # Keep one physical card line across Markdown editors and board moves.
            new_line = PREFIX + json.dumps(record, ensure_ascii=True)
            if line is None:
                updated = text + ("" if text.endswith("\n") else "\n") + "\n" + new_line + "\n"
            else:
                updated = re.sub("^" + re.escape(line) + "$", lambda _: new_line, text, count=1, flags=re.M)
            write_card(path, text, updated)
        current = record["attempts"][-1]
        reply = {"card": str(path), "key": key, "status": current["status"], "attempt": len(record["attempts"]),
                 "return_label": return_label, "send_once": send_once}
        if action == "status":
            reply["assignment"] = record
        return reply


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("offer", "status", "retry", "refused", "accept", "return"):
        cmd = actions.add_parser(name)
        cmd.add_argument("card", type=Path)
        cmd.add_argument("key")
        if name != "status":
            cmd.add_argument("--owner", required=True)
        if name == "offer":
            cmd.add_argument("--origin", required=True)
        if name not in ("offer", "status"):
            cmd.add_argument("--attempt", required=True, type=int)
        if name in ("refused", "accept", "return"):
            cmd.add_argument("--receipt", required=True)
    args = vars(parser.parse_args())
    if args["action"] in {"offer", "return"}:
        args["body"] = sys.stdin.read()
    try:
        print(json.dumps(handoff(**args), ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Handoff stopped: {error}\n")


if __name__ == "__main__":
    main()
