#!/usr/bin/env python3
"""Owner-only local submissions and a content-free Unix datagram wake signal.

This is not another coordinator or Telegram reader. Private SQLite rows carry
the content and attribution. Datagrams only wake the existing adapter.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import time

from history_safety import redact
from operator_policy import OWNER_LABEL
from telegram_bridge import BridgeError, Inbox, ROOT


def private_root(root):
    root = Path(root)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BridgeError("Local bridge storage must be an owner-only directory")
    return root


def ensure_context(inbox):
    inbox.db.executescript("""
        CREATE TABLE IF NOT EXISTS request_context (
          request_id TEXT PRIMARY KEY, source TEXT NOT NULL, actor TEXT NOT NULL,
          event_id TEXT UNIQUE NOT NULL, parent_request_id TEXT);
        CREATE TABLE IF NOT EXISTS appserver_local_actions (
          event_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending');
    """)


def current_sender(db, request_id, *, completed_schedule_reply=False):
    """Return the original, still-enrolled sender; paired entries also need a live grant."""
    row = db.execute("""SELECT r.label FROM requests r JOIN users u
        ON u.user_id=r.user_id AND u.chat_id=r.chat_id AND u.label=r.label
        WHERE r.id=? AND u.added<=r.received""", (request_id,)).fetchone()
    if row is None:
        return None
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='request_context' AND type='table'").fetchone():
        context = db.execute("SELECT event_id,source FROM request_context WHERE request_id=?", (request_id,)).fetchone()
        if context and context["source"] == "scheduled_task":
            from alfred_tasks import request_allowed
            from operator_policy import WORKSPACE
            if not request_allowed(request_id, WORKSPACE, completed_reply=completed_schedule_reply):
                return None
        if context and context["event_id"].startswith("device:"):
            try:
                from alfred_device_entry import request_allowed
            except ImportError:
                return None  # Optional device adapter is absent in a basic installation.
            if not request_allowed(db, request_id):
                return None
    return row["label"]


def submit(inbox, event_id, body, source="native_user", actor=None, parent_request_id=None):
    private_root(inbox.root)
    ensure_context(inbox)
    if (not isinstance(event_id, str) or not 1 <= len(event_id) <= 200 or not isinstance(body, str)
            or not body.strip() or "\0" in body):
        raise BridgeError("A stable event ID and nonempty text without NUL characters are required")
    if source not in {"native_user", "project_outcome"}:
        raise BridgeError("Unsupported local source")
    clean, counts = redact(body, conservative=False)
    if any(name not in {"email", "long_token"} for name in counts):
        raise BridgeError("Remove credential-like content before submitting")
    if source == "native_user":
        actor = OWNER_LABEL
        parent_request_id = None
    else:
        from alfred_projects import registered_owners
        if actor not in registered_owners():
            raise BridgeError("Project outcome actor must be a currently registered owner")
    if parent_request_id:
        recipient = inbox.db.execute("SELECT u.* FROM requests r JOIN users u ON u.user_id=r.user_id "
                                     "AND u.chat_id=r.chat_id AND u.label=r.label WHERE r.id=?", (parent_request_id,)).fetchone()
    else:
        recipient = inbox.db.execute("SELECT * FROM users WHERE label=?", (OWNER_LABEL,)).fetchone()
    if recipient is None:
        raise BridgeError("The intended enrolled recipient is unavailable")
    existing = inbox.db.execute("SELECT c.*,r.body,r.user_id FROM request_context c JOIN requests r ON r.id=c.request_id WHERE c.event_id=?",
                                (event_id,)).fetchone()
    if existing:
        if (existing["body"] != clean or existing["source"] != source or existing["actor"] != actor
                or existing["parent_request_id"] != parent_request_id or existing["user_id"] != recipient["user_id"]):
            raise BridgeError("Local event ID conflicts with an earlier submission")
        return existing["request_id"]
    digest = hashlib.sha256(event_id.encode()).hexdigest()
    request_id = "local:" + digest
    identifier = -(int(digest[:15], 16) + 1)
    with inbox.db:
        inbox.db.execute("INSERT INTO requests(id,update_id,user_id,chat_id,message_id,label,body,received) VALUES (?,?,?,?,?,?,?,?)",
                         (request_id, identifier, recipient["user_id"], recipient["chat_id"], identifier, recipient["label"], clean, time.time()))
        inbox.db.execute("INSERT INTO request_context VALUES (?,?,?,?,?)", (request_id, source, actor, event_id, parent_request_id))
    return request_id


def submit_project_outcome(inbox, event):
    body = ("Existing project owner result for " + event["project"] + " (" + event["status"] + ").\n" + event["body"])
    # Preserve the result. Concision belongs in the owner's writing, not slicing.
    suffix = "\nPrivate route ID: " + event["route_id"]
    return submit(inbox, event["event_id"], body + suffix,
                  "project_outcome", event["owner_id"], event["request_id"])


def wake(root):
    root = private_root(root)
    path = root / "wake.sock"
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BridgeError("Private adapter wake socket is unavailable")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
        sender.settimeout(1)
        sender.sendto(b"wake", str(path))


class WakeProtocol(asyncio.DatagramProtocol):
    def __init__(self, callback):
        self.callback = callback

    def datagram_received(self, data, address):
        if data == b"wake":
            self.callback()


async def start_wake(root, callback):
    root = private_root(root)
    path = root / "wake.sock"
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise BridgeError("Refusing to replace an unowned or invalid wake endpoint")
        path.unlink()  # Caller holds the adapter's exclusive process lock.
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: WakeProtocol(callback), local_addr=str(path), family=socket.AF_UNIX)
    path.chmod(0o600)
    inode = path.lstat().st_ino

    def close():
        transport.close()
        try:
            if path.lstat().st_ino == inode:
                path.unlink()
        except FileNotFoundError:
            pass
    return close


def release_owner(inbox, owner_id, event_id):
    from alfred_projects import registered_owners
    private_root(inbox.root)
    ensure_context(inbox)
    if owner_id not in registered_owners() or not event_id or len(event_id) > 200:
        raise BridgeError("A current owner and stable release event ID are required")
    with inbox.db:
        previous = inbox.db.execute("SELECT owner_id FROM appserver_local_actions WHERE event_id=?", (event_id,)).fetchone()
        if previous and previous["owner_id"] != owner_id:
            raise BridgeError("Release event ID conflicts with an earlier owner")
        inbox.db.execute("INSERT OR IGNORE INTO appserver_local_actions(event_id,owner_id) VALUES (?,?)", (event_id, owner_id))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("submit", "release-owner", "wake"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--event-id")
    parser.add_argument("--body-file", type=Path)
    parser.add_argument("--source", choices=("native_user", "project_outcome"), default="native_user")
    parser.add_argument("--owner-id")
    parser.add_argument("--parent-request-id")
    parser.add_argument("--verified-native-writer-released", action="store_true")
    args = parser.parse_args()
    inbox = Inbox(args.root)
    try:
        if args.command == "submit":
            if not args.body_file:
                raise BridgeError("A private message body file is required")
            request_id = submit(inbox, args.event_id, args.body_file.read_text(), args.source, args.owner_id, args.parent_request_id)
            result = {"status": "saved", "request_id": request_id}
        elif args.command == "release-owner":
            if not args.verified_native_writer_released:
                raise BridgeError("Confirm the native owner writer was released before retrying its pending briefs")
            release_owner(inbox, args.owner_id, args.event_id)
            result = {"status": "release_event_saved"}
        else:
            result = {"status": "wake_sent"}
        try:
            wake(inbox.root)
        except (BridgeError, OSError):
            result["wake"] = "unavailable; saved work remains durable"
            if args.command == "wake":
                result["status"] = "wake_unavailable"
        print(json.dumps(result))
    except (BridgeError, OSError):
        print(json.dumps({"error": "Local submission rejected; verify private input, identity and endpoint"}))
        return 1
    finally:
        inbox.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
