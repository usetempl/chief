#!/usr/bin/env python3
"""Read one exact local request or an owner's own outcome receipt. No submission, wake, network or model calls."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from alfred_local import private_root
from history_safety import redact
from operator_policy import OWNER_LABEL
from telegram_bridge import BridgeError, ROOT


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes arbitrary arguments, which can contain secrets.
        self.exit(2, '{"error":"Invalid arguments; use --help for usage"}\n')


def result(request_id, root=ROOT):
    """Return an allowlisted snapshot, using only an existing private database.

    The OS account is the caller boundary. An ID is a lookup key, not remote
    authentication. No fallback to another request, turn, parent or recipient.
    """
    if (not isinstance(request_id, str)
            or re.fullmatch(r"local:[0-9a-f]{64}", request_id) is None):
        raise BridgeError("An exact local request ID is required")
    root = private_root(root).absolute()
    path = root / "inbox.sqlite"
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise BridgeError("An existing owner-only database is required")

    # Inbox() creates/migrates tables. Do not use it on this read path. Keep WAL
    # visible: immutable=1 would ignore live WAL records and give stale answers.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")  # One consistent snapshot across all identity checks.
        row = db.execute("""
            SELECT r.status, r.chat_id, c.event_id FROM requests r
            JOIN request_context c ON c.request_id=r.id
            JOIN users u ON u.user_id=r.user_id AND u.chat_id=r.chat_id AND u.label=r.label
            WHERE r.id=? AND r.label=? AND c.source='native_user'
              AND c.actor=? AND c.parent_request_id IS NULL
              AND (SELECT count(*) FROM users WHERE label=?)=1
        """, (request_id, OWNER_LABEL, OWNER_LABEL, OWNER_LABEL)).fetchone()
        if (row is None or not isinstance(row["event_id"], str)
                or not 1 <= len(row["event_id"]) <= 200
                or request_id != "local:" + hashlib.sha256(row["event_id"].encode()).hexdigest()):
            return {"request_id": request_id, "status": "not_found_or_not_allowed"}

        output = {
            "request_id": request_id, "status": "found",
            "request_state": row["status"] if row["status"] in {"pending", "processing", "done", "denied"} else "unknown",
            "dispatch_state": "not_recorded", "delivery_state": "not_recorded",
            "answer_state": "unavailable", "answer": None,
            "answer_redacted": False, "answer_truncated": False,
        }
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='appserver_dispatch'").fetchone():
            dispatch = db.execute("SELECT state FROM appserver_dispatch WHERE request_id=?", (request_id,)).fetchone()
            if dispatch:
                states = {"sending", "accepted", "reconciled", "rejected", "uncertain"}
                output["dispatch_state"] = dispatch["state"] if dispatch["state"] in states else "unknown"

        answer = db.execute("""
            SELECT status, telegram_id, length(body) AS size, body,
                   instr(body,char(0)) AS nul_position
            FROM outbox WHERE id=? AND chat_id=?
        """, ("result:" + request_id, row["chat_id"])).fetchone()
        if not answer and row["status"] == "done":
            from alfred_outcomes import delivered_owner_reply_ids
            replies = delivered_owner_reply_ids(db, request_id)
            # Multiple distinct outcomes need an aggregate answer, not an arbitrary choice.
            if len(replies) == 1:
                answer = db.execute("""SELECT status,telegram_id,length(body) AS size,
                    body,instr(body,char(0)) AS nul_position
                    FROM outbox WHERE id=? AND chat_id=?""", (replies[0], row["chat_id"])).fetchone()
                output["answer_source_request_id"] = replies[0].removeprefix("result:")
        if answer:
            state = answer["status"]
            output["delivery_state"] = {"pending": "queued", "sending": "sending",
                                        "uncertain": "uncertain", "failed": "failed"}.get(state, "unknown")
            if state == "sent" and type(answer["telegram_id"]) is int and answer["telegram_id"] > 0:
                output["delivery_state"] = "sent"
            # Redact the complete body; never lose a result by clipping its tail.
            if (isinstance(answer["body"], str) and isinstance(answer["size"], int)
                    and answer["size"] > 0 and answer["nul_position"] == 0):
                clean, counts = redact(answer["body"], conservative=True)
                output.update(answer_state="saved", answer=clean,
                              answer_redacted=bool(counts), answer_truncated=False)
            else:
                output["answer_state"] = "withheld_invalid_body"
        return output


DELIVERY = {"pending": "queued", "sending": "sending", "uncertain": "uncertain", "failed": "failed"}


def outcome_receipt(request_id, owner_id, root=ROOT):
    """Delivery metadata for an owner's own published outcome. Never a body.

    The outcome must carry this owner as actor, its stable event ID, a still-enrolled
    original recipient and, when routed, the matching parent route and recipient.
    """
    from alfred_projects import registered_owners
    if (not isinstance(request_id, str) or re.fullmatch(r"local:[0-9a-f]{64}", request_id) is None
            or owner_id not in registered_owners()):
        raise BridgeError("An exact local request ID and current owner are required")
    root = private_root(root).absolute()
    path = root / "inbox.sqlite"
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BridgeError("An existing owner-only database is required")
    denied = {"request_id": request_id, "status": "not_found_or_not_allowed"}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute("""
            SELECT r.status, r.chat_id, r.user_id, r.label, c.event_id, c.parent_request_id FROM requests r
            JOIN request_context c ON c.request_id=r.id
            JOIN users u ON u.user_id=r.user_id AND u.chat_id=r.chat_id AND u.label=r.label
            WHERE r.id=? AND c.source='project_outcome' AND c.actor=? AND u.added<=r.received
        """, (request_id, owner_id)).fetchone()
        if (row is None or not isinstance(row["event_id"], str)
                or request_id != "local:" + hashlib.sha256(row["event_id"].encode()).hexdigest()):
            return denied
        routed = re.fullmatch(r"project-outcome:([0-9a-f-]{36})", row["event_id"])
        if row["parent_request_id"]:
            parent = db.execute("SELECT 1 FROM requests WHERE id=? AND user_id=? AND chat_id=? AND label=?",
                                (row["parent_request_id"], row["user_id"], row["chat_id"], row["label"])).fetchone()
            route = routed is None or db.execute(
                "SELECT 1 FROM appserver_project_routes WHERE route_id=? AND owner_id=? AND request_id=?",
                (routed[1], owner_id, row["parent_request_id"])).fetchone()
            if parent is None or not route:
                return denied
        elif routed:
            return denied
        output = {"request_id": request_id, "status": "found",
                  "request_state": row["status"] if row["status"] in {"pending", "processing", "done", "denied"} else "unknown",
                  "dispatch_state": "not_recorded", "delivery_state": "not_recorded"}
        dispatch = db.execute("SELECT state FROM appserver_dispatch WHERE request_id=?", (request_id,)).fetchone()
        if dispatch:
            output["dispatch_state"] = dispatch["state"] if dispatch["state"] in {
                "sending", "accepted", "reconciled", "rejected", "uncertain"} else "unknown"
        answer = db.execute("SELECT status,telegram_id FROM outbox WHERE id=? AND chat_id=?",
                            ("result:" + request_id, row["chat_id"])).fetchone()
        if answer:
            output["delivery_state"] = DELIVERY.get(answer["status"], "unknown")
            if answer["status"] == "sent" and type(answer["telegram_id"]) is int and answer["telegram_id"] > 0:
                output["delivery_state"] = "sent"
        return output


def main(argv=None):
    parser = SafeParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    read = commands.add_parser("result", help="Read one local owner request and its saved answer")
    read.add_argument("--request-id", required=True, help="Exact local: ID returned by alfred_local.py submit")
    read.add_argument("--root", type=Path, default=ROOT, help="Existing owner-only bridge storage")
    receipt = commands.add_parser("outcome", help="Read delivery metadata for this owner's own published outcome")
    receipt.add_argument("--request-id", required=True, help="Exact local: ID returned by alfred_local.py submit or handoff")
    receipt.add_argument("--owner-id", required=True, help="The publishing owner's registered identity")
    receipt.add_argument("--root", type=Path, default=ROOT, help="Existing owner-only bridge storage")
    args = parser.parse_args(argv)
    try:
        output = (outcome_receipt(args.request_id, args.owner_id, args.root) if args.command == "outcome"
                  else result(args.request_id, args.root))
    except (BridgeError, OSError, sqlite3.Error, ValueError):
        # Never echo paths, database diagnostics, credentials or untrusted values.
        print(json.dumps({"error": "Result unavailable; verify exact local ID, private storage and schema"}))
        return 1
    print(json.dumps(output, ensure_ascii=True))
    return 0 if output["status"] == "found" else 3


if __name__ == "__main__":
    raise SystemExit(main())
