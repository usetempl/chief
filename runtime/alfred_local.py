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
import re
import socket
import stat
import time

from history_safety import redact
from operator_policy import OWNER_LABEL, allows_project
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
    row = db.execute("""SELECT u.label,u.user_id,u.chat_id,u.added FROM requests r JOIN users u
        ON u.user_id=r.user_id AND u.chat_id=r.chat_id AND u.label=r.label
        WHERE r.id=? AND u.added<=r.received""", (request_id,)).fetchone()
    if row is None:
        return None
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='request_context' AND type='table'").fetchone():
        context = db.execute("SELECT event_id,source,parent_request_id FROM request_context WHERE request_id=?", (request_id,)).fetchone()
        if context and context["source"] == "project_outcome" and context["parent_request_id"]:
            # Older collectors could save a late outcome under a replacement enrollment.
            parent = db.execute("""SELECT 1 FROM requests WHERE id=? AND user_id=? AND chat_id=? AND label=?
                                AND received>=?""", (context["parent_request_id"], row["user_id"], row["chat_id"],
                                                     row["label"], row["added"])).fetchone()
            if parent is None:
                return None
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


def authorize_outcome(inbox, actor, parent_request_id, label, routed_project):
    """Bind coordinator evidence to an accepted human request, or check the existing project owner."""
    from alfred_appserver import load_config
    from alfred_projects import registered_owners
    config = load_config(inbox.root)
    if config and actor == "codex:" + config["thread_id"]:
        if parent_request_id and inbox.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='appserver_dispatch'").fetchone():
            accepted = inbox.db.execute("""SELECT 1 FROM appserver_dispatch d LEFT JOIN request_context c ON c.request_id=d.request_id
                WHERE d.request_id=? AND d.thread_id=? AND d.state IN ('accepted','reconciled')
                AND COALESCE(c.source,'native_user')='native_user'""", (parent_request_id, config["thread_id"])).fetchone()
            if accepted and routed_project is None:
                return
        raise BridgeError("A specialist result requires a human request accepted by this coordinator")
    owner = registered_owners().get(actor)
    if owner is None:
        raise BridgeError("Project outcome actor must be a currently registered owner")
    if routed_project is not None and (owner["project"] != routed_project or not allows_project(label, routed_project)):
        raise BridgeError("The original project grant or registered owner changed")


def submit(inbox, event_id, body, source="native_user", actor=None, parent_request_id=None, *, complete_route=None, routed_project=None):
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
    digest = hashlib.sha256(event_id.encode()).hexdigest()
    request_id = "local:" + digest
    identifier = -(int(digest[:15], 16) + 1)
    with inbox.db:
        # Two local callers can retry the same event while enrollment is changing.
        inbox.db.execute("BEGIN IMMEDIATE")
        if parent_request_id:
            if current_sender(inbox.db, parent_request_id, completed_schedule_reply=True) is None:
                raise BridgeError("The original request no longer has an authorized recipient")
            recipient = inbox.db.execute("SELECT u.* FROM requests r JOIN users u ON u.user_id=r.user_id WHERE r.id=?",
                                         (parent_request_id,)).fetchone()
        else:
            recipient = inbox.db.execute("SELECT * FROM users WHERE label=?", (OWNER_LABEL,)).fetchone()
        if recipient is None:
            raise BridgeError("The intended enrolled recipient is unavailable")
        if source == "project_outcome":
            authorize_outcome(inbox, actor, parent_request_id, recipient["label"], routed_project)
        existing = inbox.db.execute("""SELECT c.*,r.body,r.user_id,r.chat_id,r.label,r.received FROM request_context c
                                    LEFT JOIN requests r ON r.id=c.request_id WHERE c.event_id=?""", (event_id,)).fetchone()
        if existing:
            if (existing["body"] != clean or existing["source"] != source or existing["actor"] != actor
                    or existing["parent_request_id"] != parent_request_id or existing["user_id"] != recipient["user_id"]
                    or existing["chat_id"] != recipient["chat_id"] or existing["label"] != recipient["label"]
                    or existing["received"] < recipient["added"]):
                raise BridgeError("Local event ID conflicts with an earlier submission")
            return existing["request_id"]
        if complete_route:
            # The early result becomes the route's only result. The adapter's
            # terminal path completes only accepted routes, so it cannot repeat it.
            changed = inbox.db.execute("""UPDATE appserver_project_routes SET state='completed',outcome=?,delivered=1,updated=?
                WHERE route_id=? AND owner_id=? AND request_id=? AND state='accepted'""",
                (complete_route["outcome"], time.time(), complete_route["route_id"], actor, parent_request_id)).rowcount
            if not changed:
                raise BridgeError("Route is not awaiting this owner's result")
        inbox.db.execute("INSERT INTO requests(id,update_id,user_id,chat_id,message_id,label,body,received) VALUES (?,?,?,?,?,?,?,?)",
                         (request_id, identifier, recipient["user_id"], recipient["chat_id"], identifier, recipient["label"], clean, time.time()))
        inbox.db.execute("INSERT INTO request_context VALUES (?,?,?,?,?)", (request_id, source, actor, event_id, parent_request_id))
    return request_id


def submit_project_outcome(inbox, event):
    body = ("Existing project owner result for " + event["project"] + " (" + event["status"] + ").\n" + event["body"])
    # Preserve the result. Concision belongs in the owner's writing, not slicing.
    suffix = "\nPrivate route ID: " + event["route_id"]
    return submit(inbox, event["event_id"], body + suffix,
                  "project_outcome", event["owner_id"], event["request_id"], routed_project=event["project"])


def handoff(inbox, route_id, owner_id, body):
    """Publish an owner's explicit completed resource handoff as its route's result now.

    For a brief that only asks to release or confirm a shared resource. The owner
    may continue unrelated work privately; its later terminal turn is not relayed.
    """
    from alfred_projects import registered_owners
    private_root(inbox.root)
    ensure_context(inbox)
    owner = registered_owners().get(owner_id)
    if owner is None or not isinstance(route_id, str) or not route_id:
        raise BridgeError("A current owner and its route ID are required")
    if not inbox.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='appserver_project_routes'").fetchone():
        raise BridgeError("No routed brief is recorded")
    route = inbox.db.execute("SELECT * FROM appserver_project_routes WHERE route_id=? AND owner_id=?",
                             (route_id, owner_id)).fetchone()
    if route is None or current_sender(inbox.db, route["request_id"]) is None:
        raise BridgeError("Route, owner or original sender is no longer authorized")
    if not isinstance(body, str) or not body.strip():
        raise BridgeError("A completed handoff answer is required")
    outcome, _ = redact(body, conservative=False)
    text = ("Existing project owner resource handoff for " + route["project"] + " (completed).\n" + body
            + "\nPrivate route ID: " + route_id)
    return submit(inbox, "project-outcome:" + route_id, text, "project_outcome", owner_id, route["request_id"],
                  complete_route={"route_id": route_id, "outcome": outcome}, routed_project=route["project"])


def stage_return(inbox, route_id, owner_id, body, card, workspace=None):
    """Stage an owner's candidate answer for a route Chief elected for direct return.

    This never sends. Only the app-server's report of that owner's routed turn
    completing with exactly this text can send it (see alfred_direct.settle).
    """
    import alfred_direct
    from alfred_projects import registered_owners
    from operator_policy import WORKSPACE
    private_root(inbox.root)
    ensure_context(inbox)
    if not isinstance(route_id, str) or not route_id or not inbox.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='appserver_project_routes'").fetchone():
        raise BridgeError("A current owner and its route ID are required")
    try:
        return alfred_direct.stage(inbox.db, route_id, owner_id, body, card, registered_owners(), Path(workspace or WORKSPACE))
    except ValueError as error:
        raise BridgeError(str(error)) from None


def browser_tab(inbox, route_id, owner_id, action, profile=None, tab_id=None, reason=None, *, origin=None, slot="main", instance=None):
    """Record this owner's tab for the project of its own open route. Grants no permission."""
    import alfred_browser
    from alfred_projects import registered_owners
    private_root(inbox.root)
    owner = registered_owners().get(owner_id)
    if owner is None or not isinstance(route_id, str) or not inbox.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='appserver_project_routes'").fetchone():
        raise BridgeError("A current owner and its route ID are required")
    route = inbox.db.execute("SELECT * FROM appserver_project_routes WHERE route_id=? AND owner_id=? AND state='accepted'",
                             (route_id, owner_id)).fetchone()
    sender = current_sender(inbox.db, route["request_id"]) if route else None
    if route is None or sender is None or owner["project"] != route["project"] or not allows_project(sender, route["project"]):
        raise BridgeError("Only the owner's open route for its current project can record a tab")
    try:
        return alfred_browser.record(inbox.db, route["project"], owner_id, action, profile, tab_id, reason,
                                     origin=origin, slot=slot, instance=instance)
    except ValueError as error:
        raise BridgeError(str(error)) from None


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


STRANDED = ("(d.error LIKE 'recovery_required:%' OR d.state IN ('rejected','uncertain')) "
            "AND r.status IN ('pending','processing')")


def resolve(inbox, entries, authority_request_id, evidence_ref, *, dry_run=False):
    """Close audited requests whose turn stopped without a reply, with evidence.

    Only requests awaiting reconciliation qualify, never undispatched or active
    work. The installation owner's own request authorizes the audit. Each closure
    keeps its request, dispatch error and evidence; nothing is sent or replayed.
    All entries are applied together or not at all.
    """
    private_root(inbox.root)
    db = inbox.db
    db.execute("""CREATE TABLE IF NOT EXISTS request_resolutions (
        request_id TEXT PRIMARY KEY, category TEXT NOT NULL, evidence TEXT NOT NULL,
        covered_by TEXT, authority_request_id TEXT NOT NULL, resolved REAL NOT NULL)""")
    authority = db.execute("""SELECT 1 FROM requests r JOIN users u ON u.user_id=r.user_id AND u.chat_id=r.chat_id
        AND u.label=r.label AND u.user_id=u.chat_id AND u.added<=r.received
        LEFT JOIN request_context c ON c.request_id=r.id
        WHERE r.id=? AND r.label=? AND COALESCE(c.source,'native_user')='native_user'""",
        (authority_request_id, OWNER_LABEL)).fetchone()
    if authority is None:
        raise BridgeError("A request from the currently enrolled installation owner must authorize the resolution")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip() or not isinstance(entries, list) or not entries:
        raise BridgeError("An evidence reference and at least one audited entry are required")
    results, problems = [], []
    for entry in entries:
        request_id = entry.get("request_id") if isinstance(entry, dict) else None
        category, detail = (entry.get("category"), entry.get("evidence")) if isinstance(entry, dict) else (None, None)
        covered = entry.get("covered_by") if isinstance(entry, dict) else None
        if (not isinstance(request_id, str) or not isinstance(category, str) or not re.fullmatch(r"[a-z_]{1,64}", category)
                or not isinstance(detail, str) or not detail.strip()):
            problems.append({"request_id": request_id, "problem": "request_id, category and evidence are required"})
            continue
        evidence = evidence_ref.strip() + "#" + request_id + ": " + redact(detail.strip(), conservative=False)[0]
        previous = db.execute("SELECT * FROM request_resolutions WHERE request_id=?", (request_id,)).fetchone()
        if previous:
            same = (previous["category"], previous["evidence"], previous["covered_by"]) == (category, evidence, covered)
            (results if same else problems).append({"request_id": request_id, "status" if same else "problem":
                                                    "already_resolved" if same else "conflicts with an earlier resolution"})
            continue
        row = db.execute("SELECT r.chat_id FROM requests r JOIN appserver_dispatch d ON d.request_id=r.id WHERE r.id=? AND "
                         + STRANDED + " AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.id='result:' || r.id)",
                         (request_id,)).fetchone()
        if row is None:
            problems.append({"request_id": request_id, "problem": "not awaiting reconciliation"})
            continue
        if covered is not None and not db.execute("""SELECT 1 FROM outbox WHERE id=? AND chat_id=?
                AND ((status='sent' AND telegram_id>0) OR status='suppressed')""", (covered, row["chat_id"])).fetchone():
            problems.append({"request_id": request_id, "problem": "covering reply is not a confirmed answer to the same recipient"})
            continue
        results.append({"request_id": request_id, "status": "resolvable" if dry_run else "resolved",
                        "category": category, "evidence": evidence, "covered_by": covered})
    if problems or dry_run:
        return {"applied": False, "results": results, "problems": problems}
    with db:
        for item in results:
            if item["status"] != "resolved":
                continue
            db.execute("INSERT INTO request_resolutions VALUES (?,?,?,?,?,?)", (
                item["request_id"], item["category"], item["evidence"], item["covered_by"], authority_request_id, time.time()))
            db.execute("UPDATE requests SET status='done' WHERE id=? AND status IN ('pending','processing')", (item["request_id"],))
    return {"applied": True, "results": results, "problems": []}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("submit", "handoff", "return", "browser-tab", "release-owner", "wake", "resolve"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--event-id")
    parser.add_argument("--body-file", type=Path)
    parser.add_argument("--source", choices=("native_user", "project_outcome"), default="native_user")
    parser.add_argument("--owner-id")
    parser.add_argument("--parent-request-id")
    parser.add_argument("--route-id")
    parser.add_argument("--card", help="The elected tasks/done card for a direct-return candidate")
    parser.add_argument("--action", choices=("bind", "check", "reset", "show"), help="browser-tab: record, reset or read the owned tab")
    parser.add_argument("--origin", help="Plain origin from the observed tab")
    parser.add_argument("--slot", default="main", help="Project-local tab purpose")
    parser.add_argument("--instance", help="Browser connection instance ID when exposed")
    parser.add_argument("--profile", help="browser-tab: the browser profile name in use")
    parser.add_argument("--tab-id", help="browser-tab: the native tab ID to bind")
    parser.add_argument("--reason", help="browser-tab: why a tab is reset")
    parser.add_argument("--verified-native-writer-released", action="store_true")
    parser.add_argument("--mapping-file", type=Path, help="JSON list, or object with entries: request_id, category, evidence[, covered_by]")
    parser.add_argument("--authority-request-id")
    parser.add_argument("--evidence-ref")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    inbox = Inbox(args.root)
    try:
        if args.command == "resolve":
            if not args.mapping_file:
                raise BridgeError("An audited mapping file is required")
            mapping = json.loads(args.mapping_file.read_text())
            result = resolve(inbox, mapping.get("entries") if isinstance(mapping, dict) else mapping,
                             args.authority_request_id, args.evidence_ref or str(args.mapping_file), dry_run=args.dry_run)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["applied"] or (args.dry_run and not result["problems"]) else 1
        if args.command == "submit":
            if not args.body_file:
                raise BridgeError("A private message body file is required")
            request_id = submit(inbox, args.event_id, args.body_file.read_text(), args.source, args.owner_id, args.parent_request_id)
            result = {"status": "saved", "request_id": request_id}
        elif args.command == "handoff":
            if not args.body_file:
                raise BridgeError("A private handoff body file is required")
            request_id = handoff(inbox, args.route_id, args.owner_id, args.body_file.read_text())
            result = {"status": "saved", "request_id": request_id}
        elif args.command == "return":
            if not args.body_file:
                raise BridgeError("A private final-answer file is required")
            result = stage_return(inbox, args.route_id, args.owner_id, args.body_file.read_text(), args.card)
        elif args.command == "browser-tab":
            print(json.dumps(browser_tab(inbox, args.route_id, args.owner_id, args.action, args.profile, args.tab_id, args.reason,
                                         origin=args.origin, slot=args.slot, instance=args.instance)))
            return 0
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
    except (BridgeError, OSError, ValueError) as error:
        detail = str(error) if isinstance(error, BridgeError) and args.command in {"resolve", "return", "browser-tab"} else None
        print(json.dumps({"error": detail or "Local submission rejected; verify private input, identity and endpoint"}))
        return 1
    finally:
        inbox.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
