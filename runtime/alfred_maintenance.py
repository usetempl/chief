#!/usr/bin/env python3
"""Bounded, zero-model observations inside the existing collector.

Only generated observations enter the coordinator. Diagnostics read counters and
file metadata, never message bodies, credentials, histories or process details.
No files are disposable by default. This module does not delete files.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from zoneinfo import ZoneInfo

DAY = 86400
HISTORY_LIMIT = 8
STATE_LIMIT = 24000
COUNT_LIMIT = 1000
SIZE_LIMIT = 2000
MIN_FREE = 2 * 1024 ** 3
GROWTH_LIMIT = 128 * 1024 ** 2
DEFAULT_CONFIG = {"enabled": False, "interval_days": 7, "timezone": None}
HEALTHY_NOTICE = ("Weekly runtime check: no findings in the selected metadata and free disk space checks. "
                  "No cleanup or repair performed. No downtime.")
FAILURE_NOTICE = ("Weekly maintenance could not finish its checks. A bounded retry is scheduled. "
                  "No cleanup or restart ran; no downtime was introduced.")
STRANDED_NOTICE = ("Maintenance review needs reconciliation. An earlier request remains open; "
                   "later observations are saved and further review requests are blocked. "
                   "Work may already have run. Nothing was replayed or marked complete by this check. "
                   "This notice grants no action authority and introduces no downtime.")
ATTRIBUTION = (
    "Scheduled maintenance observation under the installation's standing maintenance policy. "
    "This is runtime evidence, not a new human instruction or release approval. "
    "Use only existing authority for a concrete, limited repair and its focused tests. "
    "Use metadata for diagnostics. Do not read message bodies, secrets, process arguments or full histories. "
    "Do not expand project scope, rewrite policy or permissions, upgrade dependencies, or start broad research. "
    "Preserve active work, data, credentials, and project release boundaries. "
    "Before any authorized restart or update, use alfred_maintenance_notice(request_id, body) to state expected downtime. "
    "Repeat that exact notice to check delivery. Do not act until the receipt reports sent=true. "
    "The notice leaves this request open and grants no action authority. Never resend an uncertain notice. "
    "Verify recovery, then use alfred_telegram_reply(request_id, body) for the final result. "
    "If the notice tool is unavailable on this existing thread, use the local alfred_maintenance.py CLI: "
    "notice, then notice-status, with the same --request-id and owner-only --body-file; see the setup documentation. "
    "If neither route verifies sent=true, report the blocker and do not start a downtime action. "
)


def load_config(root):
    path = Path(root) / "maintenance.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return dict(DEFAULT_CONFIG)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 4096):
            raise ValueError("Invalid maintenance configuration")
        config = json.loads(stream.read(4097))
    if not isinstance(config, dict) or set(config) - DEFAULT_CONFIG.keys():
        raise ValueError("Invalid maintenance configuration")
    config = DEFAULT_CONFIG | config
    if (type(config["enabled"]) is not bool or type(config["interval_days"]) is not int
            or not 1 <= config["interval_days"] <= 365):
        raise ValueError("Invalid maintenance interval")
    zone = config["timezone"]
    if zone is not None:
        if not isinstance(zone, str) or len(zone) > 80:
            raise ValueError("Invalid maintenance timezone")
        ZoneInfo(zone)
    return config


def next_due(now, config, previous=None):
    days = config["interval_days"]
    if days != 7:
        anchor = now if previous is None else previous
        return anchor + (max(0, int((now - anchor) // (days * DAY))) + 1) * days * DAY
    zone = ZoneInfo(config["timezone"]) if config["timezone"] else None
    local = datetime.fromtimestamp(now, zone)
    due = (local + timedelta(days=(7 - local.weekday()) % 7)).replace(hour=9, minute=0, second=0, microsecond=0)
    if due.timestamp() <= now:
        due += timedelta(days=7)
    return due.timestamp()


def _directory(path):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def selected_sizes(root, workspace):
    """Stat a fixed selection, with one shared entry/time budget and no symlink traversal."""
    deadline, remaining = time.monotonic() + 1.0, SIZE_LIMIT
    sizes, incomplete = {}, []

    def size_at(parent, name, depth=0):
        nonlocal remaining
        if remaining <= 0 or time.monotonic() >= deadline or depth > 8:
            raise TimeoutError
        remaining -= 1
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return 0
        if stat.S_ISREG(info.st_mode):
            return info.st_size
        if not stat.S_ISDIR(info.st_mode):
            return 0
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            with os.scandir(fd) as entries:
                return sum(size_at(fd, entry.name, depth + 1) for entry in entries)
        finally:
            os.close(fd)

    selections = (("workspace", workspace, ("scripts", "coordinator", "runtime", "tasks")),
                  ("runtime", root, ("attachments", "inbox.sqlite", "inbox.sqlite-wal", "collector.log", "collector.log.1")))
    for label, base, names in selections:
        fd = _directory(base)
        try:
            sizes[label] = sum(size_at(fd, name) for name in names)
        except (OSError, TimeoutError):
            incomplete.append(label)
        finally:
            os.close(fd)
    return sizes, incomplete


def observe(db, root, workspace, now):
    """Queries return capped counts, with a VM/time budget even on a large queue."""
    sizes, incomplete = selected_sizes(root, workspace)
    filesystem = os.statvfs(root)
    metrics = {"free_bytes": filesystem.f_bavail * filesystem.f_frsize, "sizes": sizes}
    findings = {"size_incomplete_" + name: 1 for name in incomplete}
    if metrics["free_bytes"] < MIN_FREE:
        findings["disk_low"] = metrics["free_bytes"]
    deadline, instructions = time.monotonic() + 0.25, 0

    def budget():
        nonlocal instructions
        instructions += 1000
        return int(instructions > 200000 or time.monotonic() >= deadline)

    db.set_progress_handler(budget, 1000)
    try:
        queries = {
            "delivery_failed": ("outbox", "status='failed'", ()),
            "delivery_uncertain": ("outbox", "status='uncertain'", ()),
            "delivery_stuck": ("outbox", "status IN ('pending','sending') AND created<?", (now - DAY,)),
            "requests_stuck": ("requests", "status IN ('pending','processing') AND received<? "
                               "AND id NOT LIKE 'maintenance:%'", (now - 7 * DAY,)),
            "dispatch_faults": ("appserver_dispatch", "(state IN ('uncertain','rejected') OR error LIKE 'recovery_%') "
                                "AND request_id IN (SELECT id FROM requests WHERE status!='done') "
                                "AND request_id NOT LIKE 'maintenance:%'", ()),
            # Retain historical errors without flagging a terminal, delivered route.
            "route_faults": ("appserver_project_routes", "state IN ('uncertain','rejected') OR "
                             "(error LIKE 'recovery_%' AND NOT (state IN ('completed','blocked') AND delivered=1))", ()),
        }
        for name, (table, where, args) in queries.items():
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            # Table/where are fixed code, never external input. No body/error text is selected.
            count = db.execute("SELECT count(*) FROM (SELECT 1 FROM " + table + " WHERE " + where
                               + " LIMIT " + str(COUNT_LIMIT) + ")", args).fetchone()[0]
            metrics[name] = count
            if count:
                findings[name] = count
    finally:
        db.set_progress_handler(None, 0)
    return metrics, findings


def _stranded_delivery(db, notice):
    """Read only this diagnostic's receipt; a lost receipt never permits replay."""
    if not notice.get("queued"):
        return {"delivery_state": notice.get("delivery_state", "not_queued"), "sent": False}
    row = db.execute("SELECT status,telegram_id FROM outbox WHERE id=? AND chat_id=?",
                     (notice["notice_id"], notice["chat_id"])).fetchone()
    sent = bool(row and row[0] == "sent" and type(row[1]) is int and row[1] > 0)
    delivery = row[0] if row and row[0] in {"pending", "sending", "failed", "uncertain", "sent"} else "not_retained"
    return {"delivery_state": "sent_unverified" if delivery == "sent" and not sent else delivery, "sent": sent}


class Maintenance:
    """One latest report, eight summaries, and a fixed set of unresolved conditions."""
    def __init__(self, inbox, workspace, host_observer=None, stop_event=None):
        from alfred_local import ensure_context, private_root
        private_root(inbox.root)
        ensure_context(inbox)
        self.inbox, self.db, self.workspace = inbox, inbox.db, Path(workspace)
        self.host_observer, self.stop_event = host_observer, stop_event
        self.db.execute("CREATE TABLE IF NOT EXISTS maintenance_state (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
        self.db.commit()

    def state(self):
        row = self.db.execute("SELECT substr(value,1,?) FROM maintenance_state WHERE id=1", (STATE_LIMIT + 1,)).fetchone()
        if not row:
            return {"version": 1, "sequence": 0, "active": {}, "sizes": {}, "history": []}
        if len(row[0]) > STATE_LIMIT:
            raise ValueError("Maintenance state limit exceeded")
        return json.loads(row[0])

    def save(self, state):
        encoded = json.dumps(state, sort_keys=True)
        if len(encoded) > STATE_LIMIT:
            raise ValueError("Maintenance state limit exceeded")
        self.db.execute("INSERT OR REPLACE INTO maintenance_state VALUES (1,?)", (encoded,))
        self.inbox.set_meta("maintenance_health", state.get("health", "scheduled"))

    def _report(self, state, now, metrics, findings, failed=False):
        state["sequence"] += 1
        if findings.keys() - state["active"].keys():
            state["queue_retry"] = 0
        active = {}
        for code, value in findings.items():
            active[code] = state["active"].get(code, {
                "fingerprint": hashlib.sha256(code.encode()).hexdigest()[:16],
                "episode": state["sequence"], "notified": False}) | {"value": value}
        state["active"] = active
        state["latest"] = {"checked": now, "status": "failed" if failed else "findings" if findings else "healthy",
                           "metrics": metrics, "findings": findings}
        if findings or failed:
            if state.get("notice", {}).get("body") == HEALTHY_NOTICE:
                del state["notice"]
            state["supersede_healthy"] = state.get("notice_ids", {}).get("healthy")
        state["history"] = (state["history"] + [{"checked": now, "status": state["latest"]["status"],
                                                "finding_count": len(findings)}])[-HISTORY_LIMIT:]

    @contextmanager
    def _query_budget(self):
        deadline, instructions = time.monotonic() + 0.25, 0

        def budget():
            nonlocal instructions
            instructions += 1000
            return int(instructions > 200000 or time.monotonic() >= deadline)

        self.db.set_progress_handler(budget, 1000)
        try:
            yield
        finally:
            self.db.set_progress_handler(None, 0)

    def _outstanding(self, request_id=None):
        with self._query_budget():
            query = ("SELECT substr(r.id,1,129) AS id,r.received FROM requests r "
                     "JOIN request_context c ON c.request_id=r.id "
                     "WHERE c.source='maintenance' AND r.status!='done'")
            row = self.db.execute(query + " AND r.id=?", (request_id,)).fetchone() if request_id else None
            row = row or self.db.execute(query + " LIMIT 1").fetchone()
            if row and len(row["id"]) > 128:
                raise ValueError("Invalid maintenance request reference")
            return row

    def _review_state(self, state, now):
        """An open review blocks dispatch; a fault or age requires reconciliation."""
        row = self._outstanding(state.get("request_id"))
        state["review_status"] = "pending" if row else "clear"
        if state.get("health") in {"blocked_review_reconciliation", "review_check_failed_retry_scheduled"}:
            state["health"] = ("checks_failed_retry_scheduled" if state.get("failures")
                               else state.get("latest", {}).get("status", "scheduled"))
        if not row:
            return False
        reason = None
        with self._query_budget():
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='appserver_dispatch'").fetchone():
                fault = self.db.execute("""
                    SELECT CASE WHEN state IN ('rejected','uncertain') THEN state
                      WHEN error='recovery_required:completed_without_reply' THEN 'completed_without_reply'
                      WHEN error LIKE 'recovery_required:%' OR error LIKE 'recovery_notice_unqueued:%'
                        THEN 'dispatch_fault' END
                    FROM appserver_dispatch WHERE request_id=?
                """, (row["id"],)).fetchone()
                reason = fault[0] if fault else None
            if not reason and now - row["received"] > state.get("interval_days", 7) * DAY:
                reason = "long_outstanding"
            previous = state.get("stranded", {})
            # Once flagged, only reconciliation of the original open request can
            # release this block. A later healthy scan cannot establish recovery.
            if previous.get("request_id") == row["id"]:
                reason = reason or previous["reason"]
            if reason:
                if previous.get("request_id") != row["id"]:
                    state["stranded"] = {
                        "request_id": row["id"], "reason": reason, "queued": False,
                        "notice_id": "result:maintenance:stranded:" + hashlib.sha256(row["id"].encode()).hexdigest(),
                        "delivery_state": "not_queued", "sent": False}
                    state["queue_retry"] = 0
                state["stranded"].update(_stranded_delivery(self.db, state["stranded"]))
                state["review_status"] = "reconciliation_required"
                state["health"] = "blocked_review_reconciliation"
        return True

    def _queue_stranded(self, state, owner):
        notice = state["stranded"]
        if notice["queued"]:
            return  # Even a missing or uncertain receipt must never be recreated.
        with self._query_budget():
            row = self.db.execute("""
                SELECT 1 FROM requests r JOIN request_context c ON c.request_id=r.id
                WHERE r.id=? AND c.source='maintenance' AND r.status!='done'
                  AND r.user_id=? AND r.chat_id=? AND r.label=? AND r.received>=?
            """, (notice["request_id"], owner["user_id"], owner["chat_id"], owner["label"], owner["added"])).fetchone()
            if not row:
                raise ValueError("Original maintenance owner enrollment unavailable")
            if self.db.execute("SELECT 1 FROM outbox WHERE id=?", (notice["notice_id"],)).fetchone():
                raise ValueError("An unregistered maintenance diagnostic already exists")
            self.inbox.reply(notice["notice_id"], owner["chat_id"], STRANDED_NOTICE)
            if not self.db.execute("SELECT 1 FROM outbox WHERE id=? AND chat_id=?",
                                   (notice["notice_id"], owner["chat_id"])).fetchone():
                raise ValueError("Maintenance diagnostic was not queued")
            notice.update(queued=True, chat_id=owner["chat_id"], enrolled_at=owner["added"])
            notice.update(_stranded_delivery(self.db, notice))

    def _queue(self, state, now):
        """Generated requests and their dedup state commit together. Never claim delivery."""
        from operator_policy import OWNER_LABEL
        # The caller commits/rolls back notice, enrollment checks and dedup state
        # together. Reserve the writer before reading enrollment to prevent a
        # concurrent revocation from racing the subsequent queue insertion.
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        with self._query_budget():
            owners = self.db.execute("SELECT user_id,chat_id,label,added FROM users WHERE label=? LIMIT 2", (OWNER_LABEL,)).fetchall()
        if len(owners) != 1 or owners[0]["user_id"] != owners[0]["chat_id"]:
            raise ValueError("Maintenance owner enrollment unavailable")
        owner = owners[0]
        if state.get("review_status") == "reconciliation_required":
            self._queue_stranded(state, owner)
            return False
        notice = state.get("notice")
        if notice:
            kind = "healthy" if notice["body"] == HEALTHY_NOTICE else "failure"
            previous = state.setdefault("notice_ids", {}).get(kind)
            held = self.db.execute("SELECT id FROM outbox WHERE id=? AND chat_id=? "
                                   "AND status IN ('pending','sending','failed','uncertain')",
                                   (previous, owner["chat_id"])).fetchone()
            key = held[0] if held else "result:maintenance:notice:" + str(notice["sequence"])
            if not held:
                self.inbox.reply(key, owner["chat_id"], notice["body"])
            if not self.db.execute("SELECT 1 FROM outbox WHERE id=? AND chat_id=?", (key, owner["chat_id"])).fetchone():
                raise ValueError("Maintenance notice was not queued")
            state["notification_id"] = key
            state["notice_ids"][kind] = key
            del state["notice"]
        new = {code: value for code, value in state["active"].items() if not value["notified"]}
        if not new or self._outstanding(state.get("request_id")):
            return False
        evidence = [{"condition": code, "fingerprint": value["fingerprint"], "count_or_bytes": value["value"]}
                    for code, value in sorted(new.items())]
        event = "maintenance:" + hashlib.sha256(json.dumps(
            [(code, value["episode"]) for code, value in sorted(new.items())]).encode()).hexdigest()
        body = ("Review these bounded maintenance findings once under standing policy. "
                "Use the saved counters to identify a concrete limited fix and focused tests. "
                "Preserve original requests and delivery evidence. Do not replay uncertain sends or work. "
                "Check active ownership before any cleanup. No cleanup path is registered by these observations. "
                "No restart, update, credential change or release is authorized by this observation. "
                "If existing authorization permits an action, report expected downtime first, then verify and report its outcome. "
                "Do not repeat this review while these conditions remain unresolved.\n"
                + json.dumps({"checked": state["latest"]["checked"], "evidence": evidence}, sort_keys=True))
        if len(body) > 4000:
            raise ValueError("Maintenance evidence limit exceeded")
        identifier = -(int(event.split(":")[1][:15], 16) + 1)
        self.db.execute("INSERT INTO requests(id,update_id,user_id,chat_id,message_id,label,body,received) VALUES (?,?,?,?,?,?,?,?)",
                        (event, identifier, owner["user_id"], owner["chat_id"], identifier, owner["label"], body, now))
        self.db.execute("INSERT INTO request_context VALUES (?,?,?,?,NULL)",
                        (event, "maintenance", "collector-maintenance", event))
        for value in new.values():
            value["notified"] = True
        state["request_id"] = event
        return True

    def tick(self, now=None):
        """Return (seconds to next local check, new coordinator request). No RPC or sends."""
        now = time.time() if now is None else now
        state = self.state()
        try:
            if now >= state.get("check_retry", 0):
                config = load_config(self.inbox.root)
                if not config["enabled"]:
                    state["health"] = "disabled_by_configuration"
                    with self.db:
                        self.save(state)
                    return DAY, False
                state["interval_days"] = config["interval_days"]
                if "next_due" not in state:
                    state["next_due"] = next_due(now, config)
                if now >= max(state["next_due"], state.get("check_retry", 0)):
                    metrics, findings = observe(self.db, self.inbox.root, self.workspace, now)
                    if self.host_observer:
                        host, host_findings = self.host_observer(state.get("host_baseline"), self.stop_event)
                        if self.stop_event is not None and self.stop_event.is_set():
                            return 60, False
                        if host.get("status") != "checked":
                            # An incomplete measurement cannot establish that an
                            # unresolved host condition cleared or recurred.
                            for code, previous in state["active"].items():
                                if code.startswith("host_") and code not in host_findings:
                                    host_findings[code] = previous["value"]
                        state["host_baseline"] = host
                        metrics["host"] = host
                        findings.update(host_findings)
                    for label, size in metrics["sizes"].items():
                        baseline = state["sizes"].get(label, size)
                        if size - baseline >= GROWTH_LIMIT:
                            findings["size_growth_" + label] = size - baseline
                        else:
                            state["sizes"][label] = size
                    for label in ("workspace", "runtime"):
                        code = "size_growth_" + label
                        if label not in metrics["sizes"] and code in state["active"]:
                            findings[code] = state["active"][code]["value"]
                    self._report(state, now, metrics, findings)
                    state["failures"] = 0
                    state["check_retry"] = 0
                    state["next_due"] = next_due(now, config, state["next_due"])
                    state["health"] = state["latest"]["status"]
                    if not findings:
                        state["notice"] = {"sequence": state["sequence"], "body": HEALTHY_NOTICE}
        except Exception:
            # Error strings can contain private paths or data. Persist only fixed evidence.
            failures = min(state.get("failures", 0) + 1, 4)
            state["failures"] = failures
            state["next_due"] = min(state.get("next_due", now), now)
            state["check_retry"] = now + (300, 1800, 21600, DAY)[failures - 1]
            findings = {code: value["value"] for code, value in state["active"].items()}
            if failures >= 3:
                findings["checks_failed"] = failures
            self._report(state, now, {}, findings, failed=True)
            if failures == 1:
                state["notice"] = {"sequence": state["sequence"], "body": FAILURE_NOTICE}
            state["health"] = "checks_failed_retry_scheduled"
            logging.warning("Maintenance checks unavailable; bounded retry scheduled")
        review_ready = now >= state.get("review_retry", 0)
        if review_ready:
            try:
                outstanding = self._review_state(state, now)
                state.pop("review_retry", None)
                if outstanding and state.get("notice", {}).get("body") == HEALTHY_NOTICE:
                    del state["notice"]
            except Exception:
                # Preserve the just-observed evidence. Unknown review state cannot
                # authorize another request or a healthy result.
                review_ready = False
                state["review_status"] = "check_unavailable"
                state["health"] = "review_check_failed_retry_scheduled"
                state["review_retry"] = now + 1800
                logging.warning("Maintenance review state unavailable; bounded retry scheduled")
        else:
            state["health"] = "review_check_failed_retry_scheduled"
        # Save observations before queueing so backpressure cannot discard evidence.
        with self.db:
            superseded = state.pop("supersede_healthy", None)
            if superseded:
                # Only pending proves no transmission is in flight. Never alter an
                # uncertain/sending/sent receipt or any human/project notification.
                self.db.execute("UPDATE outbox SET status='superseded' WHERE id=? AND status='pending'", (superseded,))
                self.db.execute("DELETE FROM outbox WHERE id IN (SELECT id FROM outbox "
                                "WHERE id LIKE 'result:maintenance:notice:%' AND status='superseded' "
                                "ORDER BY created DESC LIMIT -1 OFFSET ?)", (HISTORY_LIMIT,))
            self.save(state)
        new_request = False
        stranded = state.get("review_status") == "reconciliation_required"
        needs_queue = (not state["stranded"]["queued"] if stranded else
                       bool(state.get("notice") or any(not f["notified"] for f in state["active"].values())))
        if review_ready and needs_queue and now >= state.get("queue_retry", 0):
            try:
                with self.db:
                    new_request = self._queue(state, now)
                    state["queue_retry"] = now + 21600
                    state["notification_state"] = "queued" if state.get("notification_id") else "none"
                    self.save(state)
            except Exception:
                # Discard in-memory queue changes after rollback, including notified flags.
                new_request = False
                state = self.state()
                state["queue_retry"] = now + 1800
                state["notification_state"] = "unqueued_retry_scheduled"
                if state.get("review_status") == "reconciliation_required" and not state["stranded"]["queued"]:
                    state["stranded"]["delivery_state"] = "unqueued_retry_scheduled"
                with self.db:
                    self.save(state)
                logging.warning("Maintenance output remains unqueued; bounded retry scheduled")
        due = max(state["next_due"], state.get("check_retry", 0))
        needs_queue = (not state["stranded"]["queued"] if stranded else
                       bool(state.get("notice") or any(not f["notified"] for f in state["active"].values())))
        if review_ready and needs_queue:
            due = min(due, state.get("queue_retry", now + 1800))
        if state.get("review_retry"):
            due = min(due, state["review_retry"])
        return max(1, due - now), new_request


class Service:
    """Collector-owned worker; no new process, model poll, or shared SQLite connection."""
    def __init__(self, root, workspace, signal_request=None):
        self.root, self.workspace, self.signal_request = root, workspace, signal_request
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="alfred-maintenance", daemon=True)
        self.thread.start()

    def _run(self):
        from telegram_bridge import Inbox
        while not self.stopped.is_set():
            inbox = None
            try:
                inbox = Inbox(self.root)
                inbox.db.execute("PRAGMA busy_timeout=250")
                host_observer = None
                try:
                    from alfred_host_maintenance import observe_host
                    # This optional module belongs only to the private Mac
                    # workspace. Generic packages and fixtures do not scan it.
                    from operator_policy import ROOT
                    if (Path(self.workspace).resolve() == Path(__file__).resolve().parents[1]
                            and Path(self.root).resolve() == Path(ROOT).resolve()):
                        host_observer = observe_host
                except ModuleNotFoundError as error:
                    if error.name != "alfred_host_maintenance":
                        raise
                maintenance = Maintenance(inbox, self.workspace, host_observer, self.stopped)
                while not self.stopped.is_set():
                    delay, queued = maintenance.tick()
                    try:
                        from alfred_tasks import maintenance_card
                        maintenance_card(self.workspace, maintenance.state())
                    except (OSError, ValueError, KeyError, UnicodeError):
                        logging.warning("Weekly task card needs reconciliation; observation evidence is preserved")
                    if queued and self.signal_request:
                        self.signal_request()
                    self.stopped.wait(min(delay, DAY))
            except Exception:
                # The collector's rotating log remains visible even if SQLite is unavailable.
                logging.warning("Maintenance worker unavailable; retry in five minutes")
                if inbox is not None:
                    try:
                        inbox.db.rollback()
                        with inbox.db:
                            inbox.set_meta("maintenance_health", "worker_failed_retry_scheduled")
                    except sqlite3.Error:
                        pass  # Storage itself is unavailable; the rotating log remains the evidence.
                self.stopped.wait(300)
            finally:
                if inbox is not None:
                    inbox.db.close()

    def stop(self):
        self.stopped.set()
        self.thread.join(timeout=2)


def status(root):
    """Read saved observations and actual delivery status without running checks."""
    path = Path(root) / "inbox.sqlite"
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_state'").fetchone():
            return {"health": "not_initialized"}
        row = db.execute("SELECT substr(value,1,?) FROM maintenance_state WHERE id=1", (STATE_LIMIT + 1,)).fetchone()
        try:
            state = json.loads(row[0]) if row and len(row[0]) <= STATE_LIMIT else {"health": "state_unavailable"}
        except ValueError:
            state = {"health": "state_unavailable"}
        health = db.execute("SELECT value FROM meta WHERE key='maintenance_health'").fetchone()
        if health and health[0] == "worker_failed_retry_scheduled":
            state["health"] = health[0]
        for field, table, key in (("notification_id", "outbox", "notification_state"), ("request_id", "requests", "request_state")):
            if state.get(field) and not (field == "notification_id" and state.get("notification_state") == "unqueued_retry_scheduled"):
                record = db.execute("SELECT status FROM " + table + " WHERE id=?", (state[field],)).fetchone()
                state[key] = record[0] if record else "not_retained"
        if state.get("request_id"):
            receipt = db.execute("SELECT status FROM outbox WHERE id=?", ("result:" + state["request_id"],)).fetchone()
            state["request_reply_delivery"] = receipt[0] if receipt else "not_queued"
        if state.get("stranded"):
            state["stranded"].update(_stranded_delivery(db, state["stranded"]))
        return state
    finally:
        db.close()


def _notice_file(path, limit=None):
    """Reject links, shared files and non-files before using operator input."""
    from telegram_bridge import BridgeError
    info = Path(path).lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or (limit is not None and info.st_size > limit)):
        raise BridgeError("Notice input and storage must be existing owner-only regular files")


def _notice_body(path):
    from history_safety import redact
    from telegram_bridge import BridgeError
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise BridgeError("Use an owner-only notice body file")
        body = stream.read().decode("utf-8")
    if not body.strip() or "\0" in body:
        raise BridgeError("Nonempty notice text without NUL characters is required")
    _, counts = redact(body, conservative=False)
    if any(name != "email" for name in counts):
        raise BridgeError("Remove credential-like content from the notice")
    return body  # No trimming or redaction: replay must match the exact notice.


def _notice_dispatch(db, request_id, thread_id):
    from telegram_bridge import BridgeError
    row = db.execute("""
        SELECT d.turn_id FROM requests r JOIN request_context c ON c.request_id=r.id
        JOIN appserver_dispatch d ON d.request_id=r.id
        WHERE r.id=? AND c.source='maintenance' AND c.actor='collector-maintenance'
          AND c.event_id=r.id AND c.parent_request_id IS NULL
          AND r.status IN ('pending','processing') AND d.thread_id=?
          AND d.state IN ('accepted','reconciled')
    """, (request_id, thread_id)).fetchone()
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise BridgeError("An original dispatched maintenance request is required")
    return row[0]


async def _notice_active_turn(config):
    """Metadata-only RPCs. Never resume a writer, start a turn, or start a daemon."""
    import asyncio
    from websockets.asyncio.client import unix_connect
    from alfred_appserver import verified_socket_path
    from telegram_bridge import BridgeError

    async def rpc(ws, identifier, method, params):
        await ws.send(json.dumps({"id": identifier, "method": method, "params": params}))
        for _ in range(32):
            message = json.loads(await ws.recv())
            if not isinstance(message, dict):
                break
            if message.get("id") == identifier and "method" not in message:
                if "error" not in message and isinstance(message.get("result"), dict):
                    return message["result"]
                break
        raise BridgeError("Active coordinator metadata is unavailable")

    async with asyncio.timeout(10):
        async with unix_connect(verified_socket_path(config["socket_path"]), uri="ws://localhost",
                                max_size=256 * 1024, max_queue=8, open_timeout=3, close_timeout=1) as ws:
            await rpc(ws, "maintenance-init", "initialize", {
                "clientInfo": {"name": "alfred_maintenance_notice", "version": "1"},
                "capabilities": {"experimentalApi": True}})
            await ws.send(json.dumps({"method": "initialized", "params": {}}))
            result = await rpc(ws, "maintenance-active", "thread/turns/list", {
                "threadId": config["thread_id"], "limit": 1,
                "sortDirection": "desc", "itemsView": "notLoaded"})
            turns = result.get("data")
            if (isinstance(turns, list) and len(turns) == 1 and isinstance(turns[0], dict)
                    and turns[0].get("status") == "inProgress"
                    and isinstance(turns[0].get("id"), str) and 0 < len(turns[0]["id"]) <= 200):
                return turns[0]["id"]
    raise BridgeError("The original maintenance turn is not active")


async def maintenance_notice(root, request_id, body_file, *, receipt_only=False):
    """Operator entry to the dynamic tool's guard, not a general send channel.

    The local OS account is the operator boundary. IDs are lookup keys, not
    caller authentication. Only trusted local dispatch and current turn metadata
    supply scope; callers cannot choose an actor, recipient, thread or turn.
    """
    import re
    from alfred_appserver import Adapter, MAINTENANCE_NOTICE_NAME, load_config as appserver_config
    from alfred_local import private_root
    from telegram_bridge import BridgeError, Inbox

    if not isinstance(request_id, str) or re.fullmatch(r"maintenance:[0-9a-f]{64}", request_id) is None:
        raise BridgeError("An exact maintenance request ID is required")
    root = private_root(Path(root).absolute())
    _notice_file(root / "appserver.json", 4096)
    config = appserver_config(root)
    if config is None:
        raise BridgeError("The existing coordinator must be enabled")
    path = root / "inbox.sqlite"
    _notice_file(path)
    for suffix in ("-wal", "-shm"):
        try:
            _notice_file(Path(str(path) + suffix))
        except FileNotFoundError:
            pass
    body = _notice_body(body_file)

    # Bypass the service constructors: they create/migrate tables. This command
    # needs only their existing notice guard and queue insertion, on its own DB.
    class NoticeQueue(Inbox):
        def __init__(self, db):
            self.db = db

    class NoticeGuard(Adapter):
        def __init__(self, db, active_turn):
            self.db = db
            self.inbox = NoticeQueue(db)
            self.thread_id = config["thread_id"]
            self.active_turn = active_turn
            self.outbox_signal = None  # The existing collector flushes this queue.

    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0.25)
    db.row_factory = sqlite3.Row
    try:
        required = {"requests", "request_context", "users", "appserver_dispatch",
                    "appserver_tool_calls", "appserver_maintenance_notices", "outbox"}
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables:
            raise BridgeError("The existing maintenance notice schema is required")
        original_turn = _notice_dispatch(db, request_id, config["thread_id"])
        active_turn = await _notice_active_turn(config)
        if active_turn != original_turn or appserver_config(root) != config:
            raise BridgeError("The configured coordinator or original active turn changed")
        # No lock spans network I/O. Serialize the final guard with enrollment,
        # completion and competing notice writes; fail promptly if storage is busy.
        db.execute("BEGIN IMMEDIATE")
        if _notice_dispatch(db, request_id, config["thread_id"]) != active_turn:
            raise BridgeError("The original maintenance dispatch changed")
        if receipt_only and not db.execute("""
            SELECT 1 FROM appserver_maintenance_notices n JOIN outbox o ON o.id=n.notice_id
            WHERE n.request_id=?
        """, (request_id,)).fetchone():
            raise BridgeError("No retained maintenance notice receipt; nothing was queued")
        # A single CLI call ID per request bounds audit storage and interoperates
        # with the dynamic tool's identical notice digest and durable receipt.
        call_id = "maintenance-cli:" + hashlib.sha256(request_id.encode()).hexdigest()
        return NoticeGuard(db, active_turn).tool_maintenance_notice({
            "threadId": config["thread_id"], "turnId": active_turn,
            "tool": MAINTENANCE_NOTICE_NAME, "callId": call_id,
            "arguments": {"request_id": request_id, "body": body}})
    finally:
        db.close()


def main(argv=None):
    import asyncio
    from operator_policy import ROOT

    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            self.exit(2, '{"error":"Invalid arguments; use --help for usage"}\n')

    parser = SafeParser(description="Read saved maintenance state or use the scoped advance-notice queue", allow_abbrev=False)
    parser.add_argument("command", nargs="?", choices=("status", "notice", "notice-status"), default="status")
    parser.add_argument("--root", type=Path, default=ROOT, help="Existing owner-only bridge storage")
    parser.add_argument("--request-id", help="Exact maintenance: ID of the currently dispatched request")
    parser.add_argument("--body-file", type=Path, help="Owner-only UTF-8 file; use the identical body for receipt checks")
    args = parser.parse_args(argv)
    if ((args.command == "status" and (args.request_id is not None or args.body_file is not None))
            or (args.command != "status" and (not args.request_id or args.body_file is None))):
        parser.error("Invalid command arguments")
    try:
        if args.command == "status":
            output = status(args.root.absolute())
        else:
            result = asyncio.run(maintenance_notice(args.root, args.request_id, args.body_file,
                                                   receipt_only=args.command == "notice-status"))
            if not result["success"]:
                print(json.dumps({"error": result["contentItems"][0]["text"], "sent": False}))
                return 1
            output = json.loads(result["contentItems"][0]["text"])
    except Exception:
        # Do not print raw socket, database, path, or input errors. A committed
        # notice can still be pending; inspect the same receipt, never a new body.
        print(json.dumps({"error": "Notice/state unavailable; verify private input, existing schema and original active dispatch",
                          "sent": False}))
        return 1
    print(json.dumps(output, ensure_ascii=True))
    # A successful enqueue is not a sent receipt. Shell callers must not use &&
    # to start downtime merely because the queue accepted the notice.
    return 0 if args.command == "status" or output["sent"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
