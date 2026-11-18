"""Elected single-owner direct return: stage locally, send only on the owner's native final.

Chief elects a route in its brief. The owner stages its recipient-ready answer
and settled card with `alfred_local.py return`, which never sends. The adapter
sends only when the app-server reports that same owner's routed turn completed
with a final answer byte-identical to the staged candidate, and every binding
still holds. Anything else goes to Chief once as the route's ordinary result.
"""
import hashlib
import json
from pathlib import Path
import stat
import time

from history_safety import redact
import operator_policy
from operator_policy import OWNER_LABEL, ROUTE_AUTHORITY, allows_project

ALLOWED = {"email", "long_token"}


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def ensure(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS appserver_direct_elections (
          route_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, owner_id TEXT NOT NULL, card TEXT NOT NULL,
          quiet INTEGER NOT NULL, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, label TEXT NOT NULL,
          enrolled_at REAL NOT NULL, policy_sha256 TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS appserver_direct_candidates (
          route_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, turn_id TEXT NOT NULL, body TEXT NOT NULL,
          body_sha256 TEXT NOT NULL, card_sha256 TEXT NOT NULL, staged REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS appserver_direct_returns (
          route_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, owner_id TEXT NOT NULL, turn_id TEXT NOT NULL,
          user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, label TEXT NOT NULL, enrolled_at REAL NOT NULL,
          outbox_id TEXT NOT NULL, body_sha256 TEXT NOT NULL, card TEXT NOT NULL, card_sha256 TEXT NOT NULL,
          policy_sha256 TEXT NOT NULL, quiet INTEGER NOT NULL, created REAL NOT NULL,
          announced INTEGER NOT NULL DEFAULT 0, receipt TEXT);
    """)


def file_sha(path):
    try:
        return sha(Path(path).read_bytes())
    except (FileNotFoundError, IsADirectoryError):
        return None


def policy_evidence(sender, owners, workspace):
    """Digest of the permissions and rules a direct return relies on. No credentials or recipient IDs."""
    evidence = {
        "operator_policy": file_sha(operator_policy.__file__), "owner_label": OWNER_LABEL,
        "route_authority": sha(ROUTE_AUTHORITY), "sender": sender,
        "registry": {owner: value["project"] for owner, value in sorted(owners.items())},
        "grants": sorted({value["project"] for value in owners.values() if allows_project(sender, value["project"])}),
        "rules": {name: file_sha(Path(workspace) / name) for name in ("priorities.md", "AGENTS.md", "COORDINATOR.md", "OPERATIONS.md")},
    }
    return sha(json.dumps(evidence, sort_keys=True))


def card_evidence(workspace, card, route_id):
    """(sha256, None) when the elected card is settled in tasks/done, else (None, reason)."""
    from alfred_board import PENDING, STATES
    tasks = Path(workspace) / "tasks"
    path = tasks / "done" / Path(card).name
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or (tasks / "done").is_symlink() or "tasks/done/" + path.name != card:
            return None, "the elected card is not a regular file in tasks/done"
        data = path.read_bytes()
        text = data.decode("utf-8")
    except FileNotFoundError:
        return None, "the elected card is not in tasks/done"
    except (OSError, UnicodeError):
        return None, "the elected card is unreadable"
    others = [state for state in STATES if state != "done" and (tasks / state / path.name).exists()]
    if others:
        return None, "the card is ambiguous; a copy is also in " + ", ".join(others)
    if route_id not in text:
        return None, "the card does not record this route ID"
    if PENDING.search(text):
        return None, "the card has an unfinished move"
    return sha(data), None


def binding_status(db, binding):
    receipt = db.execute("SELECT status,telegram_id FROM outbox WHERE id=?", (binding["outbox_id"],)).fetchone()
    if receipt:
        parts = [list(part) for part in db.execute(
            "SELECT part_index,status,telegram_id FROM outbox_parts WHERE outbox_id=? ORDER BY part_index", (binding["outbox_id"],))]
        state = {"status": receipt["status"], "telegram_id": receipt["telegram_id"], "parts": parts}
    else:
        state = json.loads(binding["receipt"]) if binding["receipt"] else {"status": "expired_without_receipt"}
    sent = state["status"] == "sent" and type(state.get("telegram_id")) is int and state["telegram_id"] > 0
    return {"status": "returned_directly", "request_id": binding["request_id"], "outbox_id": binding["outbox_id"],
            "delivery_state": "sent" if sent else state["status"], "telegram_message_id": state.get("telegram_id") if sent else None,
            "parts": state.get("parts", []), "card": binding["card"], "card_sha256": binding["card_sha256"]}


def clean(body):
    if not isinstance(body, str) or not body.strip() or "\0" in body:
        return None, "a recipient-ready final answer is required"
    _, counts = redact(body, conservative=False)
    if any(name not in ALLOWED for name in counts):
        return None, "credential-like content"
    return body, None


def stage(db, route_id, owner_id, body, card, owners, workspace):
    """Record the owner's candidate for its open routed turn. Never sends or completes anything."""
    from alfred_local import current_sender
    ensure(db)
    if owner_id not in owners:
        raise ValueError("A current owner and its route ID are required")
    body, problem = clean(body)
    if problem:
        raise ValueError("Remove credential-like content; nothing was staged" if problem == "credential-like content" else problem)
    with db:
        db.execute("BEGIN IMMEDIATE")
        route = db.execute("""SELECT p.*, e.card AS elected FROM appserver_project_routes p
            JOIN appserver_direct_elections e ON e.route_id=p.route_id AND e.owner_id=p.owner_id
            WHERE p.route_id=? AND p.owner_id=?""", (route_id, owner_id)).fetchone()
        if route is None:
            raise ValueError("Chief did not elect a direct return for this owner's route")
        bound = db.execute("SELECT * FROM appserver_direct_returns WHERE route_id=?", (route_id,)).fetchone()
        if bound:
            if bound["body_sha256"] != sha(body):
                raise ValueError("A different answer was already returned for this route")
            return binding_status(db, bound)
        if card != route["elected"]:
            raise ValueError("The card does not match this assignment")
        if route["state"] != "accepted" or not route["turn_id"]:
            raise ValueError("Route is not awaiting this owner's result")
        if current_sender(db, route["request_id"]) is None:
            raise ValueError("Route, owner or original sender is no longer authorized")
        card_sha, reason = card_evidence(workspace, card, route_id)
        if reason:
            raise ValueError("Settle the card first: " + reason)
        db.execute("INSERT INTO appserver_direct_candidates VALUES (?,?,?,?,?,?,?) ON CONFLICT(route_id) DO UPDATE SET "
                   "turn_id=excluded.turn_id,body=excluded.body,body_sha256=excluded.body_sha256,"
                   "card_sha256=excluded.card_sha256,staged=excluded.staged",
                   (route_id, owner_id, route["turn_id"], body, sha(body), card_sha, time.time()))
    return {"status": "candidate_staged", "route_id": route_id, "sent": False,
            "next": "End this turn with exactly the staged text as your final answer. "
                    "Only that native final can send it; anything else goes to Chief."}


def settle(inbox, route_id, owner_id, turn_id, final_sha, owners, workspace):
    """Called only from the app-server's completed-turn path. Returns (sent, reason)."""
    from alfred_local import current_sender
    from telegram_bridge import BridgeError, PREFIX, reply_parts
    db = inbox.db
    ensure(db)
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT p.*, e.card, e.quiet, e.user_id AS e_user, e.chat_id AS e_chat, e.label AS e_label,
                   e.enrolled_at AS e_enrolled, e.policy_sha256, c.body, c.body_sha256, c.card_sha256, c.turn_id AS c_turn
            FROM appserver_project_routes p JOIN appserver_direct_elections e ON e.route_id=p.route_id
            LEFT JOIN appserver_direct_candidates c ON c.route_id=p.route_id AND c.owner_id=p.owner_id
            WHERE p.route_id=? AND p.owner_id=? AND p.turn_id=? AND p.state='accepted'""",
                         (route_id, owner_id, turn_id)).fetchone()
        if row is None:
            return False, "the route is not this owner's open turn"
        request = db.execute("""SELECT r.*, u.added AS enrolled_at, c.source, d.state AS dispatch_state FROM requests r
            JOIN users u ON u.user_id=r.user_id AND u.chat_id=r.chat_id AND u.label=r.label
            LEFT JOIN request_context c ON c.request_id=r.id LEFT JOIN appserver_dispatch d ON d.request_id=r.id
            WHERE r.id=?""", (row["request_id"],)).fetchone()
        sender = current_sender(db, row["request_id"])
        owner = owners.get(owner_id)
        card_sha, card_problem = card_evidence(workspace, row["card"], route_id) if row["body"] is not None else (None, None)
        checks = (
            (row["body"] is None, "no staged answer"),
            (row["c_turn"] != turn_id, "the staged answer belongs to another turn"),
            (final_sha != row["body_sha256"], "the native final answer differs from the staged answer"),
            (clean(row["body"] or "")[1] is not None, "the answer has credential-like content"),
            (sender is None or request is None, "the original sender or device is no longer authorized"),
            (request is not None and (request["user_id"], request["chat_id"], request["label"], request["enrolled_at"])
             != (row["e_user"], row["e_chat"], row["e_label"], row["e_enrolled"]), "the recipient or enrollment changed"),
            (owner is None or owner["project"] != row["project"] or not allows_project(sender, row["project"]),
             "the owner registration or project grant changed"),
            (sender is not None and policy_evidence(sender, owners, workspace) != row["policy_sha256"],
             "permissions, registry or operating rules changed since the election"),
            (request is not None and request["status"] not in {"pending", "processing"}, "the request is already closed"),
            (request is not None and (request["source"] or "native_user") != "native_user", "only a human request can be answered directly"),
            (request is not None and request["dispatch_state"] not in {"accepted", "reconciled"}, "the original dispatch is uncertain"),
            (db.execute("SELECT 1 FROM outbox WHERE id=? OR id LIKE ?", ("result:" + row["request_id"],
                        "result:" + row["request_id"] + ":%")).fetchone() is not None,
             "a reply, notice or uncertain send already exists for this request"),
            (db.execute("SELECT COUNT(*) FROM appserver_project_routes WHERE request_id=?", (row["request_id"],)).fetchone()[0] != 1,
             "another owner is assigned to this request"),
            (db.execute("SELECT 1 FROM appserver_project_routes WHERE owner_id=? AND turn_id=? AND route_id<>?",
                        (owner_id, turn_id, route_id)).fetchone() is not None,
             "the owner turn handled more than one assignment"),
            (db.execute("SELECT 1 FROM request_context WHERE parent_request_id=?", (row["request_id"],)).fetchone() is not None,
             "another owner or specialist result exists for this request"),
            ((row["error"] or "").startswith("recovery_required"), "the owner turn needs recovery"),
            (bool(row["quiet"]) and sender != OWNER_LABEL, "quiet completion is only for the installation owner"),
            (card_problem is not None, card_problem),
            (card_problem is None and card_sha != row["card_sha256"], "the card changed after the answer was staged"),
        )
        reason = next((why for failed, why in checks if failed), None)
        if reason:
            return False, reason
        key = "result:" + request["id"]
        try:
            reply_parts(key, PREFIX + row["body"])
            if row["quiet"]:
                inbox.suppress(key, request["chat_id"], row["body"])
            else:
                inbox.reply(key, request["chat_id"], row["body"])
        except BridgeError as error:
            return False, "the answer could not be queued: " + str(error)
        saved = db.execute("SELECT body FROM outbox WHERE id=? AND chat_id=?", (key, request["chat_id"])).fetchone()
        if not saved or saved["body"] != PREFIX + row["body"]:
            raise BridgeError("The answer was not queued as staged")
        outcome, _ = redact(row["body"], conservative=False)
        db.execute("UPDATE appserver_project_routes SET state='completed',outcome=?,delivered=1,updated=? WHERE route_id=?",
                   (outcome, time.time(), route_id))
        db.execute("UPDATE requests SET status='done' WHERE id=?", (request["id"],))
        db.execute("INSERT INTO appserver_direct_returns(route_id,request_id,owner_id,turn_id,user_id,chat_id,label,enrolled_at,"
                   "outbox_id,body_sha256,card,card_sha256,policy_sha256,quiet,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            route_id, request["id"], owner_id, turn_id, request["user_id"], request["chat_id"], request["label"],
            request["enrolled_at"], key, row["body_sha256"], row["card"], card_sha, row["policy_sha256"], row["quiet"], time.time()))
    return True, None
