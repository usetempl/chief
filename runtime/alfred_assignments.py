#!/usr/bin/env python3
"""Compact recovery of every current assignment for the coordinator's intake.

Everything is derived from existing state: the request ledger, routes, direct
returns, outbox receipts, the owner registry, priorities and board cards. Open
requests and route briefs are included in full; cards, rules and deliveries are
references (path or ID plus digest) the coordinator can reread.

The adapter attaches this to each coordinator dispatch. After a resume, an
observed compaction or an operator `--cold` reset, the full capsule is sent;
otherwise only changed items or the unchanged digest. The only stored state is
what this thread last received, so a warm turn never repeats the same backlog.
Nothing is truncated, ranked or scheduled.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys

from alfred_board import STATES
from operator_policy import WORKSPACE, OWNER_LABEL, allows_project

LOCAL = Path(__file__).resolve()
CARD_FIELDS = ("Project", "Report lead", "Blocked by", "Resolver", "Reviewer", "Next")
QUESTION_COLUMNS = ("blocked", "review")
CURRENT_COLUMNS = ("doing", "blocked", "review")
LEDGER_ID = re.compile(r"(?:tg:-?\d+:\d+(?::edit:\d+)?|local:[0-9a-f]{64}|maintenance:[^\s`]+|"
                       r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def reread_command(root):
    return " ".join(shlex.quote(part) for part in (
        "env", "COS_WORKSPACE=" + str(WORKSPACE), sys.executable, str(LOCAL), "--root", str(root)))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (FileNotFoundError, IsADirectoryError):
        return None


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds") if isinstance(value, (int, float)) else None


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def card_rows(workspace):
    for state in STATES:
        folder = Path(workspace) / "tasks" / state
        if not folder.is_dir() or folder.is_symlink():
            continue
        for path in sorted(folder.rglob("*.md")):
            if path.is_symlink() or not path.is_file() or path.name.lower() == "readme.md":
                continue
            try:
                data = path.read_bytes()
                text = data.decode("utf-8")
            except (OSError, UnicodeError):
                yield state, path, None, None
                continue
            yield state, path, text, hashlib.sha256(data).hexdigest()


def card_item(workspace, state, path, text, sha):
    item = {"kind": "card", "path": str(path.relative_to(workspace)), "column": state, "sha256": sha}
    if text is None:
        item["problem"] = "unreadable card; repair the saved record"
        return item
    item["title"] = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
    for name in CARD_FIELDS:
        match = re.search(r"^" + re.escape(name) + r":[ \t]*(.*?)[ \t]*$", text, re.M)
        if match and match[1]:
            item[name.lower().replace(" ", "_")] = match[1]
    return item


def capsule(db, root, workspace=WORKSPACE):
    """Return {"digest", "items": {key: item}} for every current assignment."""
    workspace = Path(workspace)
    present = tables(db)
    items = {}
    ledger_ids = set()
    context = "request_context" in present
    dispatch = "appserver_dispatch" in present
    rows = db.execute(
        "SELECT r.*" + (",c.source,c.actor,c.parent_request_id" if context else "")
        + (",d.error AS dispatch_error" if dispatch else "")
        + " FROM requests r JOIN users u ON u.user_id=r.user_id AND u.chat_id=r.chat_id AND u.label=r.label"
        + (" LEFT JOIN request_context c ON c.request_id=r.id" if context else "")
        + (" LEFT JOIN appserver_dispatch d ON d.request_id=r.id" if dispatch else "")
        + " WHERE r.status IN ('pending','processing') AND u.added<=r.received ORDER BY r.received,r.id").fetchall()
    from alfred_local import current_sender
    revoked = set()
    for row in rows:
        keys = row.keys()
        if current_sender(db, row["id"]) is None:
            # A revoked device, sender or grant: keep the reference, never its content or authority.
            items["request:" + row["id"]] = {"kind": "request", "request_id": row["id"], "authorization": "revoked",
                                             "note": "The original sender, device or grant is no longer authorized. Do not act on it."}
            revoked.add(row["id"])
            continue
        # Stable from intake to closure: pending/processing churn is not a change to recover.
        item = {"kind": "request", "request_id": row["id"], "sender": row["label"],
                "received": stamp(row["received"]), "body": row["body"]}
        for name in ("source", "actor", "parent_request_id", "dispatch_error"):
            if name in keys and row[name] is not None:
                item[name] = row[name]
        if "reply_to_id" in keys and row["reply_to_id"]:
            item["reply_to_telegram_message_id"] = row["reply_to_id"]
        files = [dict(file) for file in db.execute("SELECT kind,status,error FROM attachments WHERE request_id=?", (row["id"],))]
        if files:
            item["attachments"] = files
        if "appserver_audio" in present:
            audio = db.execute("SELECT result FROM appserver_audio WHERE request_id=?", (row["id"],)).fetchone()
            if audio:
                item["local_audio"] = json.loads(audio["result"])  # Complete transcript, never clipped.
        reply = db.execute("SELECT status FROM outbox WHERE id=?", ("result:" + row["id"],)).fetchone()
        if reply:
            item["reply_state"] = reply["status"]
        items["request:" + row["id"]] = item
        ledger_ids.add(row["id"])
    # Deliveries that are not yet durable, including fault notices and owner answers.
    for row in db.execute("SELECT id,status,created FROM outbox WHERE id LIKE 'result:%' "
                          "AND status IN ('pending','sending','uncertain','failed') ORDER BY created,id"):
        items["delivery:" + row["id"]] = {"kind": "delivery", "outbox_id": row["id"], "status": row["status"],
                                          "created": stamp(row["created"])}
    if "appserver_project_routes" in present:
        elections = {}
        if "appserver_direct_elections" in present:
            elections = {row["route_id"]: dict(row) for row in db.execute("SELECT route_id,card,quiet FROM appserver_direct_elections")}
        for row in db.execute("""SELECT p.*, r.status AS request_status FROM appserver_project_routes p
                LEFT JOIN requests r ON r.id=p.request_id
                WHERE p.state NOT IN ('completed','blocked','rejected_unauthorized_request') OR p.delivered=0
                   OR r.status IN ('pending','processing') ORDER BY p.created,p.route_id"""):
            item = {"kind": "route", "route_id": row["route_id"], "request_id": row["request_id"], "owner_id": row["owner_id"],
                    "project": row["project"], "state": row["state"], "brief": row["brief"],
                    "outcome_saved": bool(row["outcome"]), "outcome_relayed": bool(row["delivered"])}
            if row["error"]:
                item["detail"] = row["error"]
            if row["request_id"] in revoked:
                item["authorization"] = "revoked"
            if row["route_id"] in elections:
                item["direct_return"] = {"card": elections[row["route_id"]]["card"], "quiet": bool(elections[row["route_id"]]["quiet"])}
            items["route:" + row["route_id"]] = item
            ledger_ids.update((row["route_id"], row["request_id"]))
    if "appserver_direct_returns" in present:
        for row in db.execute("""SELECT b.*, o.status AS delivery FROM appserver_direct_returns b
                LEFT JOIN outbox o ON o.id=b.outbox_id WHERE b.announced=0
                   OR COALESCE(o.status,'') IN ('pending','sending','uncertain','failed') ORDER BY b.created"""):
            receipt = json.loads(row["receipt"]) if row["receipt"] else None
            items["direct:" + row["route_id"]] = {
                "kind": "direct_return", "route_id": row["route_id"], "request_id": row["request_id"], "owner_id": row["owner_id"],
                "outbox_id": row["outbox_id"], "delivery_state": row["delivery"] or (receipt or {}).get("status", "expired"),
                "card": row["card"], "card_sha256": row["card_sha256"], "answer_sha256": row["body_sha256"],
                "quiet": bool(row["quiet"])}
            ledger_ids.update((row["route_id"], row["request_id"]))
    for state, path, text, sha in card_rows(workspace):
        mentions = sorted(ledger_ids & set(LEDGER_ID.findall(text or "")))
        if state in CURRENT_COLUMNS or mentions:
            item = card_item(workspace, state, path, text, sha)
            if mentions:
                item["ledger_ids"] = mentions
            if state in QUESTION_COLUMNS:
                item["question"] = True
            items["card:" + item["path"]] = item
    from alfred_projects import registered_owners
    owners = registered_owners(workspace / "projects.md")
    items["rules:projects.md"] = {"kind": "rules", "path": "projects.md", "sha256": file_digest(workspace / "projects.md"),
                                  "owners": {key: value["project"] for key, value in sorted(owners.items())}}
    for name in ("priorities.md", "AGENTS.md", "COORDINATOR.md", "OPERATIONS.md"):
        if (workspace / name).is_file():
            items["rules:" + name] = {"kind": "rules", "path": name, "sha256": file_digest(workspace / name)}
    for row in db.execute("SELECT DISTINCT label FROM users ORDER BY label"):
        items["grant:" + row["label"]] = {"kind": "grant", "sender": row["label"],
                                          "authority": "installation_owner" if row["label"] == OWNER_LABEL else "enrolled_external_contact",
                                          "projects": sorted({value["project"] for value in owners.values()
                                                              if allows_project(row["label"], value["project"])})}
    return {"digest": digest(items), "items": items}


def ensure_views(db):
    db.execute("""CREATE TABLE IF NOT EXISTS appserver_assignment_views (
        thread_id TEXT PRIMARY KEY, digest TEXT, items TEXT NOT NULL, cold INTEGER NOT NULL, reason TEXT)""")


def mark_cold(db, thread_id, reason):
    """The thread may have lost context: send the full capsule with its next dispatch."""
    ensure_views(db)
    db.execute("INSERT INTO appserver_assignment_views(thread_id,items,cold,reason) VALUES (?, '{}', 1, ?) "
               "ON CONFLICT(thread_id) DO UPDATE SET cold=1,reason=excluded.reason", (thread_id, reason))


def view(db, root, thread_id, workspace=WORKSPACE, *, request_id=None):
    """Return (block for this dispatch, saver). Call saver() in the dispatch transaction.

    The dispatched request itself is already the envelope body; it counts as
    seen but is not repeated here.
    """
    ensure_views(db)
    current = capsule(db, root, workspace)
    keyed = {key: digest(item) for key, item in current["items"].items()}
    shown = sorted(key for key in keyed if key != "request:" + str(request_id))
    seen = db.execute("SELECT * FROM appserver_assignment_views WHERE thread_id=?", (thread_id,)).fetchone()
    block = {"digest": current["digest"], "reread": reread_command(root)}
    if seen is None or seen["cold"]:
        block.update(mode="full", reason=seen["reason"] if seen else "first_dispatch",
                     items=[current["items"][key] for key in shown])
    elif seen["digest"] == current["digest"]:
        block["mode"] = "unchanged"
    else:
        known = json.loads(seen["items"])
        block.update(mode="changes", base_digest=seen["digest"],
                     changed=[current["items"][key] for key in shown if known.get(key) != keyed[key]],
                     closed=sorted(set(known) - set(keyed)))

    def save():
        db.execute("INSERT INTO appserver_assignment_views(thread_id,digest,items,cold,reason) VALUES (?,?,?,0,NULL) "
                   "ON CONFLICT(thread_id) DO UPDATE SET digest=excluded.digest,items=excluded.items,cold=0,reason=NULL",
                   (thread_id, current["digest"], json.dumps(keyed, sort_keys=True)))
        if "appserver_direct_returns" in tables(db):
            announced = [key.split(":", 1)[1] for key in keyed if key.startswith("direct:")]
            db.executemany("UPDATE appserver_direct_returns SET announced=1 WHERE route_id=?", [(key,) for key in announced])
    return block, save


INSTRUCTIONS = ("The assignments object is the compact recovery of every current assignment from the saved ledger and board. "
                "mode=full lists all of them; mode=changes lists only changed items and closed keys since base_digest; "
                "mode=unchanged means none changed. If base_digest or digest is not in your context, for example after compaction, "
                "run the reread command before acting. Read a referenced card or rule file only when it is relevant. ")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Existing owner-only bridge storage")
    parser.add_argument("--cold", action="store_true", help="Send the full capsule with the coordinator's next dispatch")
    args = parser.parse_args()
    from alfred_appserver import load_config
    from alfred_local import private_root
    from telegram_bridge import BridgeError, Inbox
    try:
        private_root(args.root)
        inbox = Inbox(args.root)
    except (BridgeError, OSError):
        print(json.dumps({"error": "Private bridge storage unavailable"}))
        return 1
    try:
        if args.cold:
            config = load_config(args.root)
            if not config:
                raise BridgeError("No configured coordinator")
            with inbox.db:
                mark_cold(inbox.db, config["thread_id"], "operator_reset")
        output = capsule(inbox.db, args.root)
        output["items"] = [output["items"][key] for key in sorted(output["items"])]
        print(json.dumps(output, ensure_ascii=False))
    except BridgeError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    finally:
        inbox.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
