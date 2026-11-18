"""Project tab bindings for owners using a tab-addressable browser connector."""

import hashlib
import re
import time
from urllib.parse import urlsplit


ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._@-]*")
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9: ._@-]*")
RESET_REASONS = {"closed", "moved", "connection-changed", "reassigned", "released"}
HELP = (
    "In the authorized Chrome profile, use a tab explicitly assigned by the operator or create your own "
    "with cua.createBrowserTab(browserId, url, {sessionName}). Use the connector's browser/profile ID for "
    "--profile and its extensionInstanceId for --instance when available. Copy the tab ID and plain origin "
    "(scheme, host and optional port) from the connector. Use a separate --slot for each working tab. "
    "Append --action bind --profile PROFILE --slot SLOT --tab-id ID --origin ORIGIN to the base command. "
    "On resume, compare the saved binding with the current tab inventory using --action check and the same "
    "observed fields. Verify the intended account, workspace and any open draft before using "
    "cua.getTab(tabId, {browser: browserId}). The tab's title or group can provide additional context. "
    "Use tab-addressed controls for typing and clicks. Share foreground access only for the individual "
    "step that requires it. Preserve human tabs and other projects' forms. If a tab closed, moved or its "
    "connection changed, reset its saved ID with --action reset --profile PROFILE --slot SLOT --tab-id OLD_ID "
    "--reason closed|moved|connection-changed|released. Rebind the authorized tab. If the connector is "
    "unavailable, preserve the binding and report the connection error. Use supported browser controls. "
    "A tab ID or signed-in page grants no project or account permission."
)


def ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS appserver_browser_tabs (
        project TEXT NOT NULL, profile TEXT COLLATE NOCASE NOT NULL, slot TEXT NOT NULL, owner_id TEXT NOT NULL,
        tab_id TEXT, origin TEXT, instance TEXT, state TEXT NOT NULL, reason TEXT, updated REAL NOT NULL,
        PRIMARY KEY(project,profile,slot))""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS appserver_browser_target
                  ON appserver_browser_tabs(profile,tab_id) WHERE state='bound'""")


def session(project, owner_id):
    suffix = hashlib.sha256(owner_id.encode()).hexdigest()[:8]
    return "🗂 " + project + " · " + suffix


def reassign(db, project, owner_id):
    with db:
        db.execute("""UPDATE appserver_browser_tabs SET state='reset',reason='reassigned',tab_id=NULL,updated=?
                      WHERE project=? AND owner_id<>? AND state='bound'""", (time.time(), project, owner_id))


def bindings(db, project, owner_id):
    return [dict(row) for row in db.execute("""
        SELECT profile,slot,tab_id,origin,instance,state,reason FROM appserver_browser_tabs
        WHERE project=? AND owner_id=? ORDER BY profile,slot""", (project, owner_id))]


def route_block(db, project, owner_id, command):
    ensure(db)
    reassign(db, project, owner_id)
    block = {"sessionName": session(project, owner_id), "show": command + " --action show",
             "rule": "For browser work, use your assigned tab by ID. Preserve other tabs and the foreground."}
    saved = bindings(db, project, owner_id)
    if saved:
        block["bindings"] = saved
    return block


def plain_origin(value):
    try:
        parts = urlsplit(value or "")
        port = parts.port
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.path not in {"", "/"} or parts.query or parts.fragment
                or any(c.isspace() or c in "\\\x00" for c in value)):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Use a plain HTTP or HTTPS origin without a path, credentials, query or fragment") from None
    host = parts.hostname.lower()
    authority = "[" + host + "]" if ":" in host else host
    if port is not None and port != {"http": 80, "https": 443}[parts.scheme]:
        authority += ":" + str(port)
    return parts.scheme + "://" + authority


def record(db, project, owner_id, action, profile=None, tab_id=None, reason=None, *, origin=None, slot="main", instance=None):
    ensure(db)
    if action == "show":
        return {"status": "bindings", "sessionName": session(project, owner_id),
                "bindings": bindings(db, project, owner_id), "usage": HELP}
    if action not in {"bind", "check", "reset"}:
        raise ValueError("Use bind, check, reset or show")
    if not isinstance(profile, str) or not PROFILE.fullmatch(profile):
        raise ValueError("Supply the browser connector's profile or browser ID")
    if not isinstance(slot, str) or not ID.fullmatch(slot):
        raise ValueError("Supply a short purpose for this tab, such as studio")
    if not isinstance(tab_id, str) or not ID.fullmatch(tab_id):
        raise ValueError("Supply the native tab ID")
    if instance is not None and (not isinstance(instance, str) or not ID.fullmatch(instance)):
        raise ValueError("Supply the connector's instance ID")
    if action == "reset":
        if reason is not None and reason not in RESET_REASONS:
            raise ValueError("Choose a reset reason from " + ", ".join(sorted(RESET_REASONS)))
    else:
        origin = plain_origin(origin)
    with db:
        db.execute("BEGIN IMMEDIATE")
        key = (project, profile, slot)
        row = db.execute("SELECT * FROM appserver_browser_tabs WHERE project=? AND profile=? AND slot=?", key).fetchone()
        bound = row is not None and row["state"] == "bound"
        same_tab = bound and (row["owner_id"], row["tab_id"]) == (owner_id, tab_id)
        if action == "reset":
            if not same_tab:
                return {"status": "stale", "detail": "The saved tab changed. Inspect the current binding."}
            db.execute("""UPDATE appserver_browser_tabs SET state='reset',reason=?,tab_id=NULL,updated=?
                          WHERE project=? AND profile=? AND slot=?""", (reason or "released", time.time(), *key))
            return {"status": "reset"}
        matches = same_tab and (row["origin"], row["instance"]) == (origin, instance)
        if action == "check":
            return {"status": "matched" if matches else "stale"}
        if bound:
            return {"status": "bound"} if matches else {
                "status": "conflict", "detail": "This slot already has a binding. Check or reset its saved tab first."}
        other = db.execute("SELECT 1 FROM appserver_browser_tabs WHERE profile=? AND tab_id=? AND state='bound'",
                           (profile, tab_id)).fetchone()
        if other:
            return {"status": "conflict", "detail": "This tab already belongs to a different project or slot."}
        db.execute("""INSERT INTO appserver_browser_tabs(project,profile,slot,owner_id,tab_id,origin,instance,state,updated)
                      VALUES (?,?,?,?,?,?,?,'bound',?) ON CONFLICT(project,profile,slot) DO UPDATE SET
                      owner_id=excluded.owner_id,tab_id=excluded.tab_id,origin=excluded.origin,instance=excluded.instance,
                      state='bound',reason=NULL,updated=excluded.updated""",
                   (*key, owner_id, tab_id, origin, instance, time.time()))
    return {"status": "bound"}
