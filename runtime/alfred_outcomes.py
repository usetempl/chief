"""Hold equivalent owner inputs and close receipts only after Telegram delivery or explicit quiet completion.

This reconciles transport records. It never sends a message, repeats project
work, or treats an answered blocker as a completed project.
"""
import json
import time


# Waiting and reconciliation must agree on the exact route, parent and enrollment.
ROUTED_OUTCOMES = """
    WITH outcomes AS (
      SELECT child.id, child.status, child.received, child.user_id, child.chat_id, child.label,
             parent.id AS parent_id, p.owner_id, p.turn_id, p.state, p.outcome, p.project
      FROM appserver_project_routes p
      JOIN request_context c ON c.event_id='project-outcome:' || p.route_id
        AND c.source='project_outcome' AND c.actor=p.owner_id AND c.parent_request_id=p.request_id
      JOIN requests child ON child.id=c.request_id
      JOIN requests parent ON parent.id=p.request_id
        AND parent.user_id=child.user_id AND parent.chat_id=child.chat_id AND parent.label=child.label
      JOIN users u ON u.user_id=child.user_id AND u.chat_id=child.chat_id AND u.label=child.label
        AND u.added<=child.received AND u.added<=parent.received
      WHERE p.state IN ('completed','blocked') AND p.turn_id!='' AND p.outcome!=''
        AND child.status IN ('pending','processing','done')
    )
"""


def duplicate_outcome_waits(db, *, replies_only=False):
    """Map open receipts to an equivalent receipt without changing any state.

    Before dispatch, prefer an existing reply/dispatch, then the oldest input.
    At reply time, only an existing reply can hold another reply. Never replace
    a failed or uncertain send with a fresh send under another request ID.
    All matching happens in SQL; only request IDs leave the database.
    """
    required = {"request_context", "appserver_project_routes", "appserver_dispatch", "outbox", "users", "requests"}
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= tables:
        return {}
    rows = db.execute(ROUTED_OUTCOMES + """, candidates AS (
          SELECT child.*, d.request_id AS dispatch_id, o.id AS reply_id FROM outcomes child
          LEFT JOIN appserver_dispatch d ON d.request_id=child.id
          LEFT JOIN outbox o ON o.id='result:' || child.id AND o.chat_id=child.chat_id
        )
        SELECT child.id, other.id AS representative_id
        FROM candidates child JOIN candidates other USING (user_id,chat_id,label,owner_id,turn_id,state,outcome,project)
        WHERE child.id!=other.id AND child.status IN ('pending','processing')
          AND NOT EXISTS (SELECT 1 FROM outbox own WHERE own.id='result:' || child.id)
          AND ((? AND other.reply_id IS NOT NULL) OR
            (NOT ? AND child.status='pending' AND child.dispatch_id IS NULL AND (
              other.reply_id IS NOT NULL OR other.dispatch_id IS NOT NULL OR
              (other.status='pending' AND (other.received,other.id)<(child.received,child.id)))))
        ORDER BY child.id, other.reply_id IS NULL, other.dispatch_id IS NULL, other.received, other.id
    """, (replies_only, replies_only)).fetchall()
    waits = {}
    for row in rows:
        waits.setdefault(row["id"], row["representative_id"])
    return waits


def delivered_duplicates(db, *, resolved_request_id=None):
    required = {"request_context", "appserver_project_routes", "outbox", "users", "requests"}
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= tables:
        return []
    return db.execute(ROUTED_OUTCOMES + """
        SELECT DISTINCT child.id AS request_id, child.parent_id,
               delivered.id AS delivered_request_id, COALESCE(receipt.telegram_id,0) AS telegram_id
        FROM outcomes child JOIN outcomes delivered USING (user_id,chat_id,label,owner_id,turn_id,state,outcome,project)
        JOIN outbox receipt ON receipt.id='result:' || delivered.id
          AND receipt.chat_id=child.chat_id AND ((receipt.status='sent'
          AND receipt.telegram_id>0) OR receipt.status='suppressed')
        WHERE child.id!=delivered.id AND delivered.status='done'
          AND (child.status IN ('pending','processing') OR (child.id=? AND child.status='done'))
          AND (? IS NULL OR child.id=?)
          AND NOT EXISTS (SELECT 1 FROM outbox own WHERE own.id='result:' || child.id)
        ORDER BY child.id, delivered.id
    """, (resolved_request_id, resolved_request_id, resolved_request_id)).fetchall()


def confirmed_outcome_resolution(db, request_id):
    """Return a saved resolution only while its original delivery proof matches."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='appserver_outcome_resolutions'").fetchone():
        return None
    resolution = db.execute("SELECT delivered_request_id,telegram_id FROM appserver_outcome_resolutions WHERE request_id=?",
                            (request_id,)).fetchone()
    if resolution:
        for receipt in delivered_duplicates(db, resolved_request_id=request_id):
            if (receipt["delivered_request_id"], receipt["telegram_id"]) == tuple(resolution):
                return receipt
    return None


def parent_outcome_requests(db, request_id, *, include_manual_routes=False):
    """Find evidence bound to the parent, its original enrollment and its owner or coordinator."""
    required = {"request_context", "appserver_project_routes", "outbox", "users", "requests"}
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= tables:
        return []
    children = """
        SELECT child.id, child.chat_id, child.status, c.event_id, c.actor FROM requests parent
        LEFT JOIN request_context origin ON origin.request_id=parent.id
        JOIN request_context c ON c.parent_request_id=parent.id AND c.source='project_outcome'
        JOIN requests child ON child.id=c.request_id AND child.status IN ('pending','processing','done')
          AND child.user_id=parent.user_id AND child.chat_id=parent.chat_id AND child.label=parent.label
        JOIN users u ON u.user_id=parent.user_id AND u.chat_id=parent.chat_id AND u.label=parent.label
          AND u.added<=parent.received AND u.added<=child.received
        WHERE parent.id=? AND parent.status='done'
    """
    rows = db.execute(children + """
        AND COALESCE(origin.source,'native_user')!='project_outcome'
        AND EXISTS (SELECT 1 FROM appserver_project_routes p WHERE p.request_id=parent.id AND p.owner_id=c.actor
            AND p.state IN ('accepted','completed','blocked')
            AND (? OR (p.state IN ('completed','blocked') AND c.event_id='project-outcome:'||p.route_id)))
    """, (request_id, include_manual_routes)).fetchall()
    if "appserver_dispatch" in tables:
        rows.extend(db.execute(children + """
            AND COALESCE(origin.source,'native_user')='native_user'
            AND EXISTS (SELECT 1 FROM appserver_dispatch d WHERE d.request_id=parent.id
                AND d.state IN ('accepted','reconciled') AND c.actor='codex:'||d.thread_id)
        """, (request_id,)))
    return rows


def delivered_owner_reply_ids(db, request_id, *, require_delivery=True):
    """Return one unambiguous outcome reply. A host reader can inspect a saved answer before delivery."""
    replies = set()
    children = parent_outcome_requests(db, request_id)
    for child in children:
        if child["status"] != "done":
            return []
        resolution = confirmed_outcome_resolution(db, child["id"])
        key = "result:" + (resolution["delivered_request_id"] if resolution else child["id"])
        replies.add((key, child["chat_id"]))
    # An uncertain/missing second answer still makes the parent ambiguous.
    # Collapse only duplicates with a matching, confirmed delivery proof.
    if len(replies) != 1:
        return []
    # A second owner may still owe its first outcome.
    covered = {(child["event_id"], child["actor"]) for child in children}
    routes = db.execute("""SELECT 'project-outcome:'||route_id,owner_id FROM appserver_project_routes
                        WHERE request_id=? AND state!='rejected_unauthorized_request'""", (request_id,))
    if any(tuple(route) not in covered for route in routes):
        return []
    key, chat_id = replies.pop()
    if db.execute("""SELECT 1 FROM outbox WHERE id=? AND chat_id=?
                  AND (? OR (status='sent' AND telegram_id>0) OR status='suppressed')""",
                  (key, chat_id, not require_delivery)).fetchone():
        return [key]
    return []


def reconcile_delivered_duplicates(db):
    resolved = []
    with db:
        rows = delivered_duplicates(db)
        if not rows:
            return resolved
        db.execute("""CREATE TABLE IF NOT EXISTS appserver_outcome_resolutions (
            request_id TEXT PRIMARY KEY, delivered_request_id TEXT NOT NULL,
            telegram_id INTEGER NOT NULL, resolved REAL NOT NULL)""")
        for row in rows:
            changed = db.execute("""UPDATE requests SET status='done' WHERE id=? AND status IN ('pending','processing')
                AND NOT EXISTS (SELECT 1 FROM outbox own WHERE own.id='result:' || requests.id)""",
                                 (row["request_id"],)).rowcount
            if not changed:
                continue
            db.execute("INSERT INTO appserver_outcome_resolutions VALUES (?,?,?,?)",
                       (row["request_id"], row["delivered_request_id"], row["telegram_id"], time.time()))
            db.execute("UPDATE requests SET status='done' WHERE id=? AND status IN ('pending','processing')",
                       (row["parent_id"],))
            resolved.append(row["request_id"])
    return resolved


def retain_direct_receipts(db, cutoff):
    """Copy each expiring direct-return receipt, with its parts, into its durable binding."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='appserver_direct_returns'").fetchone():
        return
    for row in db.execute("""SELECT b.route_id, o.id, o.status, o.telegram_id FROM appserver_direct_returns b
            JOIN outbox o ON o.id=b.outbox_id WHERE b.receipt IS NULL AND o.status IN ('sent','suppressed') AND o.created<?""",
                          (cutoff,)).fetchall():
        parts = [list(part) for part in db.execute(
            "SELECT part_index,status,telegram_id FROM outbox_parts WHERE outbox_id=? ORDER BY part_index", (row["id"],))]
        db.execute("UPDATE appserver_direct_returns SET receipt=? WHERE route_id=?", (json.dumps(
            {"status": row["status"], "telegram_id": row["telegram_id"], "parts": parts}), row["route_id"]))
