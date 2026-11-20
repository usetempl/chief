"""Owner request lifecycles that must not hit an unnecessary gate, plus the boundaries that remain."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / ("runtime" if (ROOT / "runtime").is_dir() else "scripts")
if CODE.name == "runtime" and "COS_WORKSPACE" not in os.environ:
    workspace = Path(tempfile.mkdtemp())
    (workspace / ".private").mkdir(mode=0o700)
    profile = workspace / ".private/profile.json"
    profile.write_text(json.dumps({"bot_username": "ExampleChiefBot", "owner_label": "Owner"}))
    profile.chmod(0o600)
    (workspace / "projects.md").write_text("| Priority | Project | GitHub repository | Local checkout | Existing lead |\n")
    os.environ["COS_WORKSPACE"] = str(workspace)
sys.path.insert(0, str(CODE))

import alfred_projects
from alfred_appserver import Adapter
from alfred_audio import MAX_SECONDS, transcribe
from alfred_entry import result, wait_result
from alfred_local import finish, match, resolve, submit
from alfred_projects import TOOL_NAME as ROUTE_TOOL
from alfred_report import report
from alfred_tasks import due_tasks
from operator_policy import CONTACT_LABEL, OWNER_LABEL, TURN_SETTINGS
from telegram_bridge import AttachmentReadError, BridgeError, Inbox, MAX_ATTACHMENT, MAX_ATTACHMENT_STORAGE

COORDINATOR = "00000000-0000-4000-8000-0000000000c0"
PROJECT_OWNER = "00000000-0000-4000-8000-0000000000a1"
OWNER, CONTACT = 43, 42
HAS_CONTACT = CONTACT_LABEL != OWNER_LABEL


def message(update_id, user=OWNER, text="Please fix the checkout bug", **fields):
    body = {"message_id": update_id, "date": int(time.time()) + 1, "from": {"id": user, "is_bot": False},
            "chat": {"id": user, "type": "private"}, **fields}
    if text is not None:
        body["text"] = text
    return {"update_id": update_id, "message": body}


class Files:
    def __init__(self, data=b"opaque file"):
        self.data, self.calls = data, []

    def download(self, file_id, max_bytes):
        self.calls.append(max_bytes)
        return self.data


class Daemon:
    """Answers the app-server methods a real dispatch, Steer and route use."""
    def __init__(self):
        self.active, self.turns, self.calls = None, 0, []
        self.owner_turn = "owner-turn"  # The native turn any owner turn/start lands in.

    async def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "thread/turns/list":
            return {"data": [{"id": self.active, "status": "inProgress"}] if self.active else []}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"], "canAcceptDirectInput": True}}
        if method == "turn/start" and params["threadId"] == COORDINATOR:
            self.turns += 1
            self.active = "coordinator-turn-" + str(self.turns)
            return {"turn": {"id": self.active}}
        if method == "turn/start":
            return {"turn": {"id": self.owner_turn}}
        if method == "turn/steer":
            return {"turnId": self.active}
        if method == "model/list":
            return {"data": []}
        raise AssertionError("Unexpected RPC " + method)


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = Inbox(self.root)
        with self.inbox.db:
            self.inbox.db.execute("INSERT INTO users VALUES (?,?,?,1)", (OWNER, OWNER, OWNER_LABEL))
            if HAS_CONTACT:
                self.inbox.db.execute("INSERT INTO users VALUES (?,?,?,1)", (CONTACT, CONTACT, CONTACT_LABEL))
        self.index = self.root / "projects.md"
        self.index.write_text("| Priority | Project | GitHub repository | Local checkout | Existing lead |\n"
                              f"| P0 | Chief | private | `/chief` | `codex:{COORDINATOR}` |\n"
                              f"| P2 | Shop | private | `/shop` | `codex:{PROJECT_OWNER}` |\n")
        original = alfred_projects.registered_owners
        registry = patch("alfred_projects.registered_owners", lambda path=None: original(self.index))
        registry.start()
        self.addCleanup(registry.stop)
        self.adapter = Adapter(self.inbox, {"thread_id": COORDINATOR, "socket_path": "/unused"})
        self.daemon = Daemon()
        self.adapter.rpc = self.adapter.router.rpc = self.daemon
        self.adapter.router.projects_path = self.index

    async def asyncTearDown(self):
        await self.adapter.router.close()
        self.inbox.db.close()
        self.temp.cleanup()

    def status(self, request_id):
        return self.inbox.db.execute("SELECT status FROM requests WHERE id=?", (request_id,)).fetchone()[0]

    def reply(self, request_id, body, turn, call):
        result = self.adapter.tool_reply({"threadId": COORDINATOR, "turnId": turn, "callId": call,
                                          "tool": "alfred_telegram_reply", "arguments": {"request_id": request_id, "body": body}})
        return result["success"], result["contentItems"][0]["text"]

    async def route(self, request_id, turn, call, brief="Fix the checkout bug and return the verified result", owner=PROJECT_OWNER):
        result = await self.adapter.router.handle({
            "threadId": COORDINATOR, "turnId": turn, "callId": call, "tool": ROUTE_TOOL,
            "arguments": {"request_id": request_id, "owner_id": "codex:" + owner, "brief": brief}})
        return json.loads(result["contentItems"][0]["text"])["status"]

    async def drain(self):
        while self.adapter.router.tasks:
            await asyncio.gather(*list(self.adapter.router.tasks))


class CoordinatorTurnSettings(Base):
    async def test_idle_dispatch_uses_installed_policy_once_and_saves_the_mapping(self):
        self.inbox.ingest(message(1))
        await self.adapter.dispatch_pending()
        await self.adapter.dispatch_pending()
        starts = [params for method, params in self.daemon.calls if method == "turn/start"]
        self.assertEqual(len(starts), 1)
        start = starts[0]
        settings = {key: value for key, value in start.items() if key not in {"threadId", "clientUserMessageId", "input"}}
        self.assertEqual(settings, TURN_SETTINGS)
        self.assertEqual(start["threadId"], COORDINATOR)
        saved = self.inbox.db.execute("SELECT client_id,thread_id,turn_id,state FROM appserver_dispatch WHERE request_id=?",
                                      ("tg:43:1",)).fetchone()
        self.assertEqual(tuple(saved), (start["clientUserMessageId"], COORDINATOR, self.daemon.active, "accepted"))

    async def test_model_and_effort_apply_only_to_a_new_coordinator_turn(self):
        policy = {"model": "fixture-model", "effort": "high", "serviceTier": "default", "serviceTierForTurn": "default"}
        with patch("alfred_appserver.TURN_SETTINGS", policy):
            self.inbox.ingest(message(1))
            await self.adapter.dispatch_pending()
            self.inbox.ingest(message(2))
            await self.adapter.dispatch_pending()
            self.assertEqual(await self.route("tg:43:1", self.daemon.active, "route"), "accepted")
        calls = [(method, params) for method, params in self.daemon.calls if method in ("turn/start", "turn/steer")]
        self.assertEqual([(method, params["threadId"]) for method, params in calls],
                         [("turn/start", COORDINATOR), ("turn/steer", COORDINATOR), ("turn/start", PROJECT_OWNER)])
        for index, (_, params) in enumerate(calls):
            self.assertEqual({key: params[key] for key in policy if key in params}, policy if index == 0 else {})
        self.assertEqual(calls[1][1]["expectedTurnId"], self.daemon.active)


class RouteAfterReply(Base):
    async def test_owner_answered_first_then_routes_follow_up_and_gets_the_result(self):
        self.inbox.ingest(message(1))
        await self.adapter.dispatch_pending()
        turn = self.daemon.active
        self.assertEqual(self.reply("tg:43:1", "Looking into it now; I'll send the fix.", turn, "reply-1"),
                         (True, "Reply queued for the enrolled sender. Telegram delivery is not yet verified."))
        self.assertEqual(self.status("tg:43:1"), "done")
        # Previously rejected_unauthorized_request solely because the request was answered.
        self.assertEqual(await self.route("tg:43:1", turn, "route-1"), "accepted")
        self.assertEqual(await self.route("tg:43:1", turn, "route-1"), "accepted")  # Same call: same route.
        self.assertEqual(await self.route("tg:43:1", turn, "route-2", brief="Something else"), "rejected_conflicting_brief")
        self.assertEqual(await self.route("tg:43:1", "another-turn", "route-3"), "rejected_unauthorized_request")
        self.assertEqual(sum(1 for method, params in self.daemon.calls
                             if method == "turn/start" and params["threadId"] == PROJECT_OWNER), 1)
        # The owner's result returns as evidence for the same recipient and is answered once.
        self.adapter.router.notification({"method": "turn/completed", "params": {"threadId": PROJECT_OWNER, "turn": {
            "id": "owner-turn", "status": "completed",
            "items": [{"type": "agentMessage", "phase": "final_answer", "text": "Checkout fixed and verified"}]}}})
        await self.drain()
        child = self.inbox.db.execute("SELECT r.*,c.parent_request_id FROM requests r JOIN request_context c ON c.request_id=r.id "
                                      "WHERE c.source='project_outcome'").fetchone()
        self.assertEqual((child["chat_id"], child["parent_request_id"], child["status"]), (OWNER, "tg:43:1", "pending"))
        self.daemon.active = None
        await self.adapter.dispatch_pending()
        self.assertTrue(self.reply(child["id"], "The checkout bug is fixed and verified.", self.daemon.active, "reply-2")[0])
        self.assertEqual(sorted(row[0] for row in self.inbox.db.execute("SELECT id FROM outbox WHERE chat_id=?", (OWNER,))),
                         sorted(["result:tg:43:1", "result:" + child["id"]]))

    async def test_old_turn_other_recipient_unknown_revoked_and_denied_requests_stay_refused(self):
        self.inbox.ingest(message(1))
        await self.adapter.dispatch_pending()
        first = self.daemon.active
        self.reply("tg:43:1", "Answered", first, "reply-1")
        self.daemon.active = None
        self.inbox.ingest(message(2, text="Second request"))
        await self.adapter.dispatch_pending()
        second = self.daemon.active
        self.assertEqual(await self.route("tg:43:1", second, "old-turn"), "rejected_unauthorized_request")
        self.assertEqual(await self.route("tg:43:999", second, "unknown"), "rejected_unauthorized_request")
        if HAS_CONTACT:
            self.inbox.ingest(message(3, user=CONTACT, text="Contact request"))
            self.daemon.active = None
            await self.adapter.dispatch_pending()
            self.assertEqual(await self.route("tg:42:3", second, "other-recipient"), "rejected_unauthorized_request")
        with self.inbox.db:
            self.inbox.db.execute("UPDATE requests SET status='denied' WHERE id='tg:43:2'")
        self.assertEqual(await self.route("tg:43:2", second, "denied"), "rejected_unauthorized_request")
        with self.inbox.db:
            self.inbox.db.execute("UPDATE requests SET status='done' WHERE id='tg:43:2'")
            self.inbox.db.execute("DELETE FROM users WHERE user_id=?", (OWNER,))
        self.assertEqual(await self.route("tg:43:2", second, "revoked"), "rejected_unauthorized_request")
        with self.inbox.db:
            self.inbox.db.execute("INSERT INTO users VALUES (?,?,?,?)", (OWNER, OWNER, OWNER_LABEL, time.time()))
        self.assertEqual(await self.route("tg:43:2", second, "re-enrolled"), "rejected_unauthorized_request")
        self.assertFalse(any(method == "turn/start" and params["threadId"] == PROJECT_OWNER for method, params in self.daemon.calls))


class InterruptedReplies(Base):
    async def test_resumed_owner_turn_finishes_the_original_request_without_replaying_work(self):
        self.inbox.ingest(message(1))
        await self.adapter.dispatch_pending()
        original = self.daemon.active
        self.adapter.fault_turn(COORDINATOR, original, "interrupted")
        self.daemon.active = None
        self.inbox.ingest(message(2, text="Continue the saved work."))
        await self.adapter.dispatch_pending()
        resumed = self.daemon.active
        calls = list(self.daemon.calls)
        self.assertTrue(self.reply("tg:43:1", "The saved work is complete.", resumed, "recovered-answer")[0])
        self.assertTrue(self.reply("tg:43:1", "The saved work is complete.", resumed, "recovered-retry")[0])
        self.assertEqual(self.daemon.calls, calls)
        self.assertEqual(self.status("tg:43:1"), "done")
        self.assertEqual(tuple(self.inbox.db.execute("SELECT turn_id,error FROM appserver_dispatch WHERE request_id='tg:43:1'").fetchone()),
                         (original, "recovery_required:interrupted"))
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM outbox WHERE id='result:tg:43:1'").fetchone()[0], 1)

    async def test_other_turns_failures_and_changed_enrollment_cannot_complete_the_request(self):
        self.inbox.ingest(message(1))
        await self.adapter.dispatch_pending()
        original = self.daemon.active
        self.daemon.active = None
        self.inbox.ingest(message(2))
        await self.adapter.dispatch_pending()
        resumed = self.daemon.active
        self.assertFalse(self.reply("tg:43:1", "Must stay open.", resumed, "not-interrupted")[0])
        for reason in ("failed", "approval", "unsupported_tool"):
            self.adapter.fault_turn(COORDINATOR, original, reason)
            self.assertFalse(self.reply("tg:43:1", "Must stay open.", resumed, reason)[0])
        self.adapter.fault_turn(COORDINATOR, original, "interrupted")
        self.assertFalse(self.reply("tg:43:1", "Must stay open.", "unaccepted-turn", "no-turn")[0])
        if HAS_CONTACT:
            self.daemon.active = None
            self.inbox.ingest(message(3, user=CONTACT))
            await self.adapter.dispatch_pending()
            self.assertFalse(self.reply("tg:43:1", "Must stay open.", self.daemon.active, "other-recipient")[0])
        with self.inbox.db:
            self.inbox.db.execute("UPDATE users SET added=? WHERE user_id=?", (time.time() + 2, OWNER))
        self.assertFalse(self.reply("tg:43:1", "Must stay open.", resumed, "changed-enrollment")[0])
        self.assertEqual(self.status("tg:43:1"), "processing")
        self.assertIsNone(self.inbox.db.execute("SELECT 1 FROM outbox WHERE id='result:tg:43:1'").fetchone())


class OutcomeDelivery(Base):
    def test_simultaneous_local_retries_share_one_request(self):
        ready = threading.Barrier(2)

        def submit_again():
            box = Inbox(self.root)
            try:
                ready.wait(timeout=5)
                return submit(box, "host-retry", "Check the order.")
            finally:
                box.db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = [pool.submit(submit_again) for _ in range(2)]
            self.assertEqual(first.result(), second.result())
        with self.assertRaises(BridgeError):
            submit(self.inbox, "host-retry", "A different request.")
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM requests").fetchone()[0], 1)

    async def test_host_reads_quiet_specialist_result_and_can_answer_multiple_results(self):
        config = self.root / "appserver.json"
        config.write_text(json.dumps({"enabled": True, "thread_id": COORDINATOR}))
        config.chmod(0o600)
        parent = submit(self.inbox, "host-review", "Review the saved work.")
        await self.adapter.dispatch_pending()
        actor = "codex:" + COORDINATOR
        self.index.write_text(self.index.read_text().replace("| P0 |", "| P1 |"))
        with self.assertRaises(BridgeError):
            submit(self.inbox, "unbound-specialist", "First check complete.", "project_outcome", actor)
        child = submit(self.inbox, "first-check", "First check complete.", "project_outcome", actor, parent)
        await self.adapter.dispatch_pending()
        answer = "![Local preview](checks/preview.png)\nThe first check passed."
        self.assertFalse(self.reply(child, answer, self.daemon.active, "send-local-preview")[0])
        self.assertTrue(self.reply(child, "DONT_NOTIFY\n" + answer, self.daemon.active, "first-answer")[0])
        saved = wait_result(parent, 1, self.root)
        self.assertEqual((saved["wait_state"], saved["answer"], saved["delivery_state"]), ("answer_saved", answer, "suppressed"))
        self.assertEqual(saved["answer_source_request_id"], child)

        other = submit(self.inbox, "second-check", "Second check complete.", "project_outcome", actor, parent)
        self.assertIsNone(result(parent, self.root)["answer"])
        await self.adapter.dispatch_pending()
        self.assertTrue(self.reply(other, "DONT_NOTIFY\nThe second check passed.", self.daemon.active, "second-answer")[0])
        self.assertIsNone(result(parent, self.root)["answer"])
        self.assertTrue(self.reply(parent, "Both checks passed.", self.daemon.active, "combined-answer")[0])
        self.assertEqual(result(parent, self.root)["answer"], "Both checks passed.")
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM outbox WHERE status='pending'").fetchone()[0], 1)

    async def test_queued_owner_answer_is_readable_with_its_actual_delivery_state(self):
        parent = submit(self.inbox, "host-order", "Check the order.")
        await self.adapter.dispatch_pending()
        self.assertEqual(await self.route(parent, self.daemon.active, "order-route"), "accepted")
        self.adapter.router.notification({"method": "turn/completed", "params": {"threadId": PROJECT_OWNER, "turn": {
            "id": "owner-turn", "status": "completed", "items": [{"type": "agentMessage", "text": "Order checked."}]}}})
        await self.drain()
        child = self.inbox.db.execute("SELECT request_id FROM request_context WHERE parent_request_id=?", (parent,)).fetchone()[0]
        await self.adapter.dispatch_pending()
        self.assertTrue(self.reply(child, "The order is ready.", self.daemon.active, "order-answer")[0])
        saved = wait_result(parent, 1, self.root)
        self.assertEqual((saved["answer"], saved["delivery_state"]), ("The order is ready.", "queued"))
        self.assertEqual(saved["answer_source_request_id"], child)
        with self.inbox.db:
            self.inbox.db.execute("UPDATE outbox SET status='uncertain' WHERE id=?", ("result:" + child,))
        self.assertEqual(result(parent, self.root)["delivery_state"], "uncertain")
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)
        second_owner = "00000000-0000-4000-8000-0000000000a2"
        with self.index.open("a") as index:
            index.write(f"| P3 | Shipping | repo | checkout | `codex:{second_owner}` |\n")
        self.assertEqual(await self.route(parent, self.daemon.active, "shipping", owner=second_owner), "accepted")
        self.assertIsNone(result(parent, self.root)["answer"])

    async def test_new_enrollment_cannot_read_or_relay_an_earlier_request(self):
        parent = submit(self.inbox, "before-enrollment-change", "Review the order.")
        await self.adapter.dispatch_pending()
        self.assertTrue(self.reply(parent, "DONT_NOTIFY\nReview saved.", self.daemon.active, "saved-review")[0])
        with self.inbox.db:
            self.inbox.db.execute("UPDATE users SET added=? WHERE user_id=?", (time.time(), OWNER))
        self.assertEqual(result(parent, self.root), {"request_id": parent, "status": "not_found_or_not_allowed"})
        with self.assertRaises(BridgeError):
            submit(self.inbox, "late-result", "Earlier evidence.", "project_outcome", "codex:" + PROJECT_OWNER, parent)
        with self.assertRaises(BridgeError):
            submit(self.inbox, "before-enrollment-change", "Review the order.")
        fresh = submit(self.inbox, "after-enrollment-change", "Review the next order.")
        self.assertEqual(result(fresh, self.root)["status"], "found")

    async def test_duplicate_outcomes_only_share_a_receipt_within_one_enrollment(self):
        # The owner must give a late assignment its own answer, even if that answer is identical.
        for event, turn in (("earlier", "earlier-turn"), ("current", "current-turn"), ("duplicate", "current-turn")):
            self.daemon.owner_turn = turn
            parent = submit(self.inbox, event, "Review the order.")
            await self.adapter.dispatch_pending()
            self.assertEqual(await self.route(parent, self.daemon.active, event), "accepted")
            self.adapter.router.notification({"method": "turn/completed", "params": {"threadId": PROJECT_OWNER, "turn": {
                "id": turn, "status": "completed", "items": [{"type": "agentMessage", "text": "Order checked."}]}}})
            await self.drain()
            if event == "duplicate":
                self.assertIsNone(self.inbox.db.execute("SELECT 1 FROM request_context WHERE parent_request_id=?", (parent,)).fetchone())
                route_id = self.inbox.db.execute("SELECT route_id FROM appserver_project_routes WHERE request_id=?", (parent,)).fetchone()[0]
                with self.assertRaisesRegex(BridgeError, "already belongs"):
                    match(self.inbox, "codex:" + PROJECT_OWNER, turn, route_id)
                finish(self.inbox, route_id, "codex:" + PROJECT_OWNER, "Order checked.")
            child = self.inbox.db.execute("SELECT request_id FROM request_context WHERE parent_request_id=?", (parent,)).fetchone()[0]
            await self.adapter.dispatch_pending()
            if event == "duplicate":
                self.assertEqual(self.status(child), "done")
                saved = result(parent, self.root)
                self.assertEqual((saved["answer"], saved["delivery_state"]), ("current answer", "suppressed"))
                self.assertIsNone(self.inbox.db.execute("SELECT 1 FROM outbox WHERE id=?", ("result:" + child,)).fetchone())
                continue
            self.assertEqual(self.status(child), "processing")
            self.assertIsNone(result(parent, self.root)["answer"])
            self.assertTrue(self.reply(child, "DONT_NOTIFY\n" + event + " answer", self.daemon.active, event + "-reply")[0])
            self.assertEqual(result(parent, self.root)["answer"], event + " answer")
            if event == "earlier":
                with self.inbox.db:
                    self.inbox.db.execute("UPDATE users SET added=? WHERE user_id=?", (time.time(), OWNER))

    async def test_retired_owner_result_keeps_its_receipt_and_does_not_block_reconnect(self):
        parent = submit(self.inbox, "owner-retirement", "Review the order.")
        await self.adapter.dispatch_pending()
        self.assertEqual(await self.route(parent, self.daemon.active, "route"), "accepted")
        registry = self.index.read_text()
        self.index.unlink()
        router = self.adapter.router
        router.notification({"method": "turn/completed", "params": {"threadId": PROJECT_OWNER, "turn": {
            "id": "owner-turn", "status": "completed", "items": [{"type": "agentMessage", "text": "Order checked."}]}}})
        await self.drain()
        await router.reconcile()
        held = self.inbox.db.execute("SELECT state,outcome,delivered,error FROM appserver_project_routes").fetchone()
        self.assertEqual(tuple(held)[:3], ("completed", "Order checked.", 0))
        self.assertTrue(held["error"])
        fresh = submit(self.inbox, "another-question", "Read the board.")
        await self.adapter.dispatch_pending()
        self.assertEqual(self.status(fresh), "processing")
        self.assertFalse(self.inbox.db.execute("SELECT 1 FROM request_context WHERE parent_request_id=?", (parent,)).fetchone())

        self.index.write_text(registry.replace("| Shop |", "| Another project |"))
        await router.reconcile()
        self.assertEqual(await self.route(parent, self.daemon.active, "route"), "rejected_project_scope")
        self.assertEqual(self.inbox.db.execute("SELECT delivered FROM appserver_project_routes").fetchone()[0], 0)
        self.index.write_text(registry)
        await router.reconcile()
        await router.reconcile()
        self.assertEqual(tuple(self.inbox.db.execute("SELECT delivered,error FROM appserver_project_routes").fetchone()), (1, None))
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM request_context WHERE parent_request_id=?", (parent,)).fetchone()[0], 1)
        self.assertEqual(sum(method == "turn/start" and params["threadId"] == PROJECT_OWNER for method, params in self.daemon.calls), 1)


class OwnerIntake(Base):
    def test_falls_back_to_the_largest_downloadable_photo_and_receipts_it_once(self):
        photo = [{"width": 100, "height": 80, "file_id": "small"},
                 {"width": 1600, "height": 900, "file_id": "too-large", "file_size": MAX_ATTACHMENT + 1},
                 {"width": 800, "height": 600, "file_id": "large"},
                 {"width": 640, "height": 480, "file_id": "medium"}]
        files = Mock()
        files.download.side_effect = [AttachmentReadError("Telegram returned an incomplete file."), b"picture"]
        update = message(1, text=None, caption="The pricing section", photo=photo)
        self.inbox.ingest(update, files)
        self.inbox.ingest(update, files)
        self.assertEqual([call.args[0] for call in files.download.call_args_list], ["large", "medium"])
        row = self.inbox.db.execute("SELECT * FROM attachments").fetchone()
        self.assertEqual((row["status"], row["size"], Path(row["local_path"]).read_bytes()), ("ready", 7, b"picture"))
        self.assertEqual(self.inbox.db.execute("SELECT COUNT(*) FROM reactions WHERE message_id=1").fetchone()[0], 1)
        self.assertFalse(self.inbox.db.execute("SELECT 1 FROM outbox WHERE id LIKE 'ack:%'").fetchone())

    def test_transient_single_version_retries_but_a_telegram_rate_limit_stops(self):
        photo = [{"width": 900, "height": 600, "file_id": "one"}]
        files = Mock()
        files.download.side_effect = [OSError("private diagnostic"), b"picture"]
        self.inbox.ingest(message(1, text=None, photo=photo), files)
        self.assertEqual(files.download.call_count, 2)
        files.download.reset_mock()
        files.download.side_effect = AttachmentReadError("Telegram has paused file downloads. Please try again later.", 429)
        self.inbox.ingest(message(2, text=None, photo=photo), files)
        files.download.assert_called_once()
        rows = self.inbox.db.execute("SELECT status,error FROM attachments ORDER BY request_id").fetchall()
        self.assertEqual([row["status"] for row in rows], ["ready", "failed"])
        self.assertIn("paused", rows[1]["error"])

    def test_failed_photos_dispatch_without_resend_spam_and_offline_notices_are_grouped(self):
        photo = [{"width": 300, "height": 200, "file_id": "sample"}]
        files = Mock()
        files.download.side_effect = BridgeError("private diagnostic")
        with self.inbox.db:
            self.inbox.set_meta("appserver_enabled", "1")
            self.inbox.set_meta("appserver_health", "connected")
        for number in range(1, 4):
            self.inbox.ingest(message(number, text=None, caption=f"Image {number}", photo=photo), files)
        self.assertFalse(self.inbox.db.execute("SELECT 1 FROM outbox WHERE id LIKE 'ack:%'").fetchone())
        self.assertFalse(self.inbox.db.execute("SELECT 1 FROM reactions").fetchone())
        self.assertEqual(self.inbox.db.execute("SELECT COUNT(*) FROM attachments WHERE status='failed' AND local_path IS NULL").fetchone()[0], 3)
        row = self.inbox.db.execute("SELECT * FROM requests WHERE message_id=2").fetchone()
        self.assertEqual(row["body"], "Image 2")
        inputs = self.adapter.request_input(row, "test-dispatch")
        self.assertEqual([item["type"] for item in inputs], ["text"])
        self.assertIn("ask once for that picture", inputs[0]["text"])
        self.assertNotIn("private diagnostic", inputs[0]["text"])
        with self.inbox.db:
            self.inbox.set_meta("appserver_health", "disconnected")
        for number in range(4, 7):
            self.inbox.ingest(message(number, text=None, photo=photo), files)
        notices = self.inbox.db.execute("SELECT body FROM outbox WHERE id LIKE 'ack:%'").fetchall()
        self.assertEqual([row["body"] for row in notices], ["The file download from Telegram was interrupted."])
        with self.inbox.db:
            self.inbox.db.execute("UPDATE outbox SET created=0 WHERE id LIKE 'ack:%'")
        self.inbox.ingest(message(7, text=None, photo=photo), files)
        self.assertEqual(self.inbox.db.execute("SELECT COUNT(*) FROM outbox WHERE id LIKE 'ack:%'").fetchone()[0], 2)

    def test_owner_replies_are_never_dropped_by_reply_counts_but_contacts_keep_them(self):
        with self.inbox.db:
            for chat in (OWNER, CONTACT) if HAS_CONTACT else (OWNER,):
                self.inbox.db.executemany("INSERT INTO outbox(id,chat_id,body,created) VALUES (?,?,'queued',?)",
                                          [(f"queued:{chat}:{i}", chat, time.time()) for i in range(250)])
        self.inbox.ingest(message(1, text="/status"))
        self.inbox.ingest(message(2, text="Order 77 still fails"))
        self.assertIsNotNone(self.inbox.db.execute("SELECT 1 FROM outbox WHERE id='tg:43:1'").fetchone())
        self.inbox.claim("tg:43:2")
        self.inbox.finish("tg:43:2", "Fixed.")
        self.assertEqual(self.status("tg:43:2"), "done")
        if HAS_CONTACT:
            self.inbox.ingest(message(3, user=CONTACT, text="/status"))
            self.assertIsNone(self.inbox.db.execute("SELECT 1 FROM outbox WHERE id='tg:42:3'").fetchone())
            with self.assertRaises(BridgeError):
                self.inbox.ingest(message(4, user=CONTACT, text="Contact feedback"))

    def test_credential_like_text_keeps_the_rest_of_the_message_and_never_stores_the_secret(self):
        # Synthetic: the label and value are assembled at run time so no literal looks like a credential.
        label, secret = "pass" + "word", "-".join(("fixture", "value", "x" * 6))
        self.inbox.ingest(message(1, text="The login page breaks after reset.\n" + label + ": " + secret))
        self.inbox.ingest(message(2, text=label + ": " + secret))
        rows = self.inbox.db.execute("SELECT id,body FROM requests").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("tg:43:1", "The login page breaks after reset.\n[REDACTED]")])
        everything = json.dumps([tuple(row) for table in ("requests", "outbox") for row in self.inbox.db.execute("SELECT * FROM " + table)])
        self.assertNotIn(secret, everything)
        self.assertIn("was not stored", self.inbox.db.execute("SELECT body FROM outbox WHERE id='ack:tg:43:1'").fetchone()[0])

    def test_platform_size_video_caption_long_voice_and_storage_bounds(self):
        files = Files(b"v" * 32)
        self.inbox.ingest(message(1, text=None, caption="Screen recording of the bug",
                                  video={"file_id": "video", "file_size": 15 * 1024 * 1024, "duration": 40}), files)
        self.inbox.ingest(message(2, text=None, voice={"file_id": "voice", "duration": 7 * 60, "file_size": 32, "mime_type": "audio/ogg"}), files)
        self.inbox.ingest(message(3, text=None, caption="Too big", document={"file_id": "big", "file_size": MAX_ATTACHMENT + 1}), files)
        saved = {row["request_id"]: tuple(row)[1:] for row in self.inbox.db.execute(
            "SELECT request_id,kind,status,error FROM attachments ORDER BY request_id")}
        self.assertEqual(saved["tg:43:1"][:2], ("video", "ready"))
        self.assertEqual(saved["tg:43:2"][:2], ("voice", "ready"))
        self.assertEqual(saved["tg:43:3"][:2], ("document", "failed"))
        self.assertIn("20 MiB", saved["tg:43:3"][2])
        self.assertEqual(self.inbox.db.execute("SELECT body FROM requests WHERE id='tg:43:1'").fetchone()[0], "Screen recording of the bug")
        self.assertEqual(len(files.calls), 2)
        # Earlier owner files never fill a fixed quota; only the host's free-space reserve applies.
        with self.inbox.db:
            self.inbox.db.execute("UPDATE attachments SET size=? WHERE request_id='tg:43:1'", (MAX_ATTACHMENT_STORAGE * 3,))
        self.inbox.ingest(message(4, text=None, document={"file_id": "doc", "file_size": 32}), files)
        self.assertEqual(self.inbox.db.execute("SELECT status FROM attachments WHERE request_id='tg:43:4'").fetchone()[0], "ready")
        with patch("telegram_bridge.shutil.disk_usage", return_value=SimpleNamespace(free=1024)):
            self.inbox.ingest(message(5, text=None, caption="Kept", document={"file_id": "doc", "file_size": 32}), files)
        self.assertIn("storage is full", self.inbox.db.execute("SELECT error FROM attachments WHERE request_id='tg:43:5'").fetchone()[0])
        self.assertEqual(self.inbox.db.execute("SELECT body FROM requests WHERE id='tg:43:5'").fetchone()[0], "Kept")
        if HAS_CONTACT:
            self.inbox.ingest(message(6, user=CONTACT, text=None, document={"file_id": "doc", "file_size": 32}), files)
            with self.inbox.db:
                self.inbox.db.execute("UPDATE attachments SET size=? WHERE request_id='tg:42:6'", (MAX_ATTACHMENT_STORAGE,))
            self.inbox.ingest(message(7, user=CONTACT, text=None, document={"file_id": "doc", "file_size": 32}), files)
            self.assertEqual(self.inbox.db.execute("SELECT status FROM attachments WHERE request_id='tg:42:7'").fetchone()[0], "failed")

    def test_edited_message_is_new_input_and_commands_or_unknown_senders_are_not(self):
        self.inbox.ingest(message(1, text="Deploy to staging"))
        edit = message(2, text="Deploy to staging, not production")
        edit["edited_message"] = edit.pop("message")
        edit["edited_message"].update(message_id=1, edit_date=1234)
        self.inbox.ingest(edit)
        self.inbox.ingest(edit)
        command = message(3, text="/report")
        command["edited_message"] = command.pop("message")
        self.inbox.ingest(command)
        stranger = message(4, user=99, text="Edited by a stranger")
        stranger["edited_message"] = stranger.pop("message")
        self.inbox.ingest(stranger)
        rows = [tuple(row) for row in self.inbox.db.execute("SELECT id,body FROM requests ORDER BY received")]
        self.assertEqual(rows, [("tg:43:1", "Deploy to staging"),
                                ("tg:43:1:edit:1234", "[Edited Telegram message; earlier version: tg:43:1]\nDeploy to staging, not production")])
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)


class ScheduledWork(Base):
    def test_owner_recurring_task_is_queued_even_with_many_open_requests(self):
        with self.inbox.db:
            self.inbox.db.executemany("INSERT INTO requests(id,update_id,user_id,chat_id,message_id,label,body,received,status) "
                                      "VALUES (?,?,?,?,?,?,'open',1,'processing')",
                                      [(f"tg:43:{i}", i, OWNER, OWNER, i, OWNER_LABEL) for i in range(100, 160)])
        board = self.root / "board"
        for column in ("backlog", "todo", "doing", "review", "done"):
            (board / "tasks" / column).mkdir(parents=True)
        (board / "tasks/todo/weekly.md").write_text(
            "# Weekly check\n\nReport name: Weekly check\nRecurrence: every 7 days\nNext run: 2026-11-23T09:00:00-03:00\n"
            "Schedule driver: chief\nSchedule owner: chief\nAuthority: Owner requested this exact bounded check.\n")
        from datetime import datetime
        self.assertTrue(due_tasks(self.inbox, board, datetime.fromisoformat("2026-11-23T09:00:00-03:00")))
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM request_context WHERE source='scheduled_task'").fetchone()[0], 1)
        self.assertFalse(due_tasks(self.inbox, board, datetime.fromisoformat("2026-11-23T09:00:00-03:00")))


class StrandedResolution(Base):
    async def strand(self):
        """Two steered owner messages; Chief answers one combined reply and the turn ends."""
        self.inbox.ingest(message(1, text="The banner is wrong"))
        await self.adapter.dispatch_pending()
        turn = self.daemon.active
        self.inbox.ingest(message(2, text="Also on mobile"))
        await self.adapter.dispatch_pending()
        self.reply("tg:43:1", "Fixed the banner on desktop and mobile.", turn, "combined")
        with self.inbox.db:
            self.inbox.db.execute("UPDATE outbox SET status='sent',telegram_id=501 WHERE id='result:tg:43:1'")
        self.adapter.notification({"method": "turn/completed", "params": {"threadId": COORDINATOR, "turn": {"id": turn, "status": "completed"}}})
        return turn

    async def test_audited_resolution_needs_owner_authority_evidence_and_a_stranded_request(self):
        turn = await self.strand()
        self.assertEqual(self.status("tg:43:2"), "processing")
        self.assertIn("1 message whose processing ended without a recorded reply", report(self.root, self.inbox.db))
        # A later turn still cannot answer the old turn's request.
        self.assertFalse(self.reply("tg:43:2", "Late answer", "later-turn", "late")[0])
        entry = {"request_id": "tg:43:2", "category": "covered_by_reply", "evidence": "Same-turn combined reply covered it",
                 "covered_by": "result:tg:43:1"}
        self.inbox.ingest(message(3, text="Please reconcile the stranded messages"))
        with self.assertRaises(BridgeError):
            resolve(self.inbox, [entry], "tg:43:999", "audit.json")
        if HAS_CONTACT:
            self.inbox.ingest(message(4, user=CONTACT, text="I authorize it"))
            with self.assertRaises(BridgeError):
                resolve(self.inbox, [entry], "tg:42:4", "audit.json")
        # All or nothing: an active request cannot be closed alongside it.
        mixed = resolve(self.inbox, [entry, {"request_id": "tg:43:3", "category": "handled_elsewhere", "evidence": "x"}],
                        "tg:43:3", "audit.json")
        self.assertEqual((mixed["applied"], mixed["problems"][0]["request_id"]), (False, "tg:43:3"))
        wrong_cover = resolve(self.inbox, [entry | {"covered_by": "result:tg:43:3"}], "tg:43:3", "audit.json")
        self.assertFalse(wrong_cover["applied"])
        dry = resolve(self.inbox, [entry], "tg:43:3", "audit.json", dry_run=True)
        self.assertEqual((dry["applied"], dry["results"][0]["status"], self.status("tg:43:2")), (False, "resolvable", "processing"))
        applied = resolve(self.inbox, [entry], "tg:43:3", "audit.json")
        self.assertTrue(applied["applied"])
        self.assertEqual(self.status("tg:43:2"), "done")
        record = self.inbox.db.execute("SELECT * FROM request_resolutions WHERE request_id='tg:43:2'").fetchone()
        self.assertEqual((record["authority_request_id"], record["covered_by"]), ("tg:43:3", "result:tg:43:1"))
        self.assertTrue(record["evidence"].startswith("audit.json#tg:43:2: "))
        original = self.inbox.db.execute("SELECT body,error,turn_id FROM requests r JOIN appserver_dispatch d ON d.request_id=r.id "
                                         "WHERE r.id='tg:43:2'").fetchone()
        self.assertEqual(tuple(original), ("Also on mobile", "recovery_required:completed_without_reply", turn))
        self.assertNotIn("without a recorded reply", report(self.root, self.inbox.db))
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM outbox WHERE chat_id=?", (OWNER,)).fetchone()[0], 1)
        self.assertEqual(resolve(self.inbox, [entry], "tg:43:3", "audit.json")["results"][0]["status"], "already_resolved")
        self.assertFalse(resolve(self.inbox, [entry | {"evidence": "changed"}], "tg:43:3", "audit.json")["applied"])

    def test_dispatch_tells_chief_to_complete_each_combined_message(self):
        self.inbox.ingest(message(1))
        row = self.inbox.db.execute("SELECT * FROM requests WHERE id='tg:43:1'").fetchone()
        text = self.adapter.request_text(row, "client")
        self.assertIn("Complete this request_id in this turn even when another reply already answers it", text)
        self.assertIn("A message already answered by another reply in this turn can be completed this way", text)


THUMBS_UP = [{"type": "emoji", "emoji": "\U0001F44D"}]


class TelegramReceipts:
    """Records calls; reactions follow a script of outcomes, replies get valid receipts."""
    def __init__(self, *outcomes, check=None):
        self.outcomes, self.check, self.calls = list(outcomes), check, []

    def call(self, method, payload):
        self.calls.append((method, payload))
        if method == "sendMessage":
            return {"message_id": 900 + len(self.calls), "text": payload["text"], "chat": {"id": payload["chat_id"]}}
        assert method == "setMessageReaction", method
        if self.check:
            self.check(payload)
        outcome = self.outcomes.pop(0) if self.outcomes else True
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def reactions(self):
        return [payload for method, payload in self.calls if method == "setMessageReaction"]


class OwnerReceipts(Base):
    def owner_message(self, update_id, message_id, **fields):
        update = message(update_id, **fields)
        update["message"]["message_id"] = message_id
        return update

    def rows(self):
        return [tuple(row) for row in self.inbox.db.execute("SELECT chat_id,message_id,status FROM reactions ORDER BY message_id")]

    def test_saved_owner_message_gets_one_thumbs_up_on_that_message_after_commit(self):
        def committed(payload):
            # A separate connection sees the saved request before the receipt is sent.
            with sqlite3.connect(self.root / "inbox.sqlite") as other:
                self.assertEqual(other.execute("SELECT id FROM requests WHERE message_id=?", (payload["message_id"],)).fetchone(), ("tg:43:70",))
        api = TelegramReceipts(check=committed)
        self.inbox.ingest(self.owner_message(5, 70))
        self.assertEqual(self.rows(), [(OWNER, 70, "pending")])
        self.inbox.flush(api)
        self.assertEqual(api.reactions(), [{"chat_id": OWNER, "message_id": 70, "reaction": THUMBS_UP, "is_big": False}])
        self.assertEqual(self.rows(), [(OWNER, 70, "sent")])
        self.inbox.flush(api)
        self.assertEqual(len(api.reactions()), 1)
        # The receipt is not completion: the request still waits for Chief.
        self.assertEqual(self.status("tg:43:70"), "pending")

    def test_failed_intake_transaction_queues_no_receipt(self):
        with self.inbox.db:
            self.inbox.db.execute("CREATE TRIGGER refuse BEFORE INSERT ON requests BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.inbox.ingest(self.owner_message(5, 70))
        self.assertEqual((self.rows(), self.inbox.meta("offset")), ([], "0"))

    def test_owner_commands_get_receipts_without_any_model_or_request(self):
        with patch("alfred_report.report", return_value="BACKLOG (0)"), patch("alfred_report.done", return_value="DONE"):
            for number, command in enumerate(("/status", "/report", "/done", "/help", "/setup"), 1):
                self.inbox.ingest(self.owner_message(number, 10 + number, text=command))
        api = TelegramReceipts()
        with patch("alfred_appserver.Adapter.rpc", side_effect=AssertionError("no model or app-server call")):
            self.inbox.flush(api)
        self.assertEqual([payload["message_id"] for payload in api.reactions()], [11, 12, 13, 14, 15])
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)
        self.assertEqual(self.daemon.calls, [])

    def test_duplicate_delivery_and_edits_send_one_receipt(self):
        update = self.owner_message(5, 70)
        api = TelegramReceipts()
        self.inbox.ingest(update)
        self.inbox.flush(api)
        self.inbox.ingest(update)
        redelivered = self.owner_message(6, 70)
        self.inbox.ingest(redelivered)
        edit = self.owner_message(7, 70, text="Corrected text")
        edit["edited_message"] = edit.pop("message")
        edit["edited_message"]["edit_date"] = 99
        self.inbox.ingest(edit)
        self.inbox.flush(api)
        self.inbox.flush(api)
        self.assertEqual(len(api.reactions()), 1)
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM requests").fetchone()[0], 2)  # Original and edit kept.

    def test_unknown_contact_rejected_unsaved_and_revoked_get_no_receipt(self):
        self.inbox.ingest(message(1, user=99, text="Stranger"))
        if HAS_CONTACT:
            self.inbox.ingest(message(2, user=CONTACT, text="Contact request"))
        self.inbox.ingest(self.owner_message(3, 30, text="pass" + "word: " + "-".join(("fixture", "only"))))
        self.inbox.ingest(self.owner_message(4, 40, text=None, sticker={"file_id": "s"}))
        self.inbox.ingest(self.owner_message(5, 50, text=None, caption="See file",
                                             document={"file_id": "big", "file_size": MAX_ATTACHMENT + 1}), Files())
        self.assertEqual(self.rows(), [])
        self.inbox.ingest(self.owner_message(6, 60))
        with self.inbox.db:
            self.inbox.db.execute("UPDATE users SET added=? WHERE user_id=?", (time.time() + 60, OWNER))  # Re-enrolled later.
        api = TelegramReceipts()
        self.inbox.flush(api)
        self.assertEqual(api.reactions(), [])
        self.assertEqual(self.rows(), [(OWNER, 60, "failed")])

    async def test_transient_and_rate_limit_failures_keep_input_dispatch_and_reply_then_recover(self):
        self.inbox.ingest(self.owner_message(5, 70, text="/status"))
        self.inbox.ingest(self.owner_message(6, 71))
        api = TelegramReceipts(BridgeError("Telegram HTTP 429", 429, retry_after=5),
                               BridgeError("Telegram connection unavailable", not_sent=True))
        self.inbox.flush(api)
        self.assertEqual([row[2] for row in self.rows()], ["pending", "pending"])
        self.assertEqual([m for m, _ in api.calls].count("sendMessage"), 1)
        # Input stays saved and Chief still receives it.
        await self.adapter.dispatch_pending()
        self.assertEqual(self.status("tg:43:71"), "processing")
        self.inbox.flush(api)
        self.assertEqual(len(api.reactions()), 2)  # Still waiting for the backoff.
        later = time.time() + 3601
        with patch("telegram_bridge.time.time", return_value=later):
            self.inbox.flush(api)
        self.assertEqual([row[2] for row in self.rows()], ["sent", "sent"])
        self.assertEqual(len(api.reactions()), 4)
        self.assertEqual([m for m, _ in api.calls].count("sendMessage"), 1)  # The reply is never resent.
        self.assertEqual(self.inbox.db.execute("SELECT count(*) FROM requests").fetchone()[0], 1)

    def test_permanent_error_and_bounded_retries_end_without_affecting_other_receipts(self):
        self.inbox.ingest(self.owner_message(5, 70))
        self.inbox.ingest(self.owner_message(6, 71))
        api = TelegramReceipts(BridgeError("Telegram HTTP 400", 400), True)
        self.inbox.flush(api)
        self.assertEqual([row[2] for row in self.rows()], ["failed", "sent"])
        self.inbox.ingest(self.owner_message(7, 72))
        uncertain = TelegramReceipts(*[BridgeError("Telegram request failed; delivery may be uncertain")] * 10)
        clock = time.time()
        for _ in range(10):
            clock += 3601
            with patch("telegram_bridge.time.time", return_value=clock):
                self.inbox.flush(uncertain)
        self.assertEqual(len(uncertain.reactions()), 8)
        self.assertEqual(self.rows()[-1], (OWNER, 72, "failed"))

    def test_interrupted_send_is_retried_after_its_lease(self):
        self.inbox.ingest(self.owner_message(5, 70))
        with self.inbox.db:
            self.inbox.db.execute("UPDATE reactions SET status='sending',next_attempt=?", (time.time() + 120,))
        api = TelegramReceipts()
        self.inbox.flush(api)
        self.assertEqual(api.reactions(), [])
        with patch("telegram_bridge.time.time", return_value=time.time() + 121):
            self.inbox.flush(api)
        self.assertEqual(self.rows(), [(OWNER, 70, "sent")])



class TranscriptAPI:
    """Replies get valid receipts unless a scripted outcome is an error; reactions succeed."""
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def call(self, method, payload):
        self.calls.append((method, payload))
        if method == "setMessageReaction":
            return True
        assert method == "sendMessage", method
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return {"message_id": 900 + len(self.calls), "text": payload["text"], "chat": {"id": payload["chat_id"]}}

    def sent(self):
        return [payload for method, payload in self.calls if method == "sendMessage"]


# Synthetic transcript: another language, punctuation and literal Markdown must arrive unchanged.
SPOKEN = "Olá, Chief! Corrija o **checkout** — `pedido_77` _falha_ [aqui](http://x)… certo?"


class OwnerTranscripts(Base):
    def setUp(self):
        super().setUp()
        (self.root / "local-audio.json").write_text("{}")  # Presence only; the decoder is patched.
        self.decoded = []

    def decoder(self, text=SPOKEN, status="transcribed"):
        def transcribe(root, request_id, attachment, cancel=None):
            self.decoded.append(request_id)
            if status != "transcribed":
                return {"status": "unavailable", "reason": "local_decode_failed_or_unclear"}
            data = Path(attachment["local_path"]).read_bytes()
            return {"status": "transcribed", "text": text, "language": "pt", "engine": "whisper.cpp", "model": "m.bin",
                    "audio_sha256": hashlib.sha256(data).hexdigest(), "duration_seconds": 3, "passes": 1, "untrusted": True}
        return patch("alfred_appserver.transcribe_local", transcribe)

    def voice(self, update_id, message_id, user=OWNER, edit_date=None):
        update = message(update_id, user=user, text=None,
                         voice={"file_id": "voice", "duration": 3, "file_size": 11, "mime_type": "audio/ogg"})
        update["message"]["message_id"] = message_id
        if edit_date:
            update["edited_message"] = update.pop("message")
            update["edited_message"]["edit_date"] = edit_date
        self.inbox.ingest(update, Files(b"voice bytes"))

    def transcripts(self):
        return [tuple(row) for row in self.inbox.db.execute("SELECT id,status FROM outbox WHERE id LIKE 'transcript:%' ORDER BY id")]

    async def test_fresh_transcript_is_committed_before_dispatch_and_sent_as_a_literal_reply(self):
        seen = []
        daemon = self.adapter.rpc
        async def rpc(method, params):
            if method == "turn/start":
                other = sqlite3.connect(self.root / "inbox.sqlite")  # Durable before any model work.
                seen.append(other.execute("SELECT body FROM outbox WHERE id='transcript:tg:43:70'").fetchone())
                other.close()
            return await daemon(method, params)
        self.adapter.rpc = rpc
        signals = []
        self.adapter.outbox_signal = lambda: signals.append(1)
        self.voice(5, 70)
        with self.decoder():
            await self.adapter.dispatch_pending()
        self.assertEqual((seen, signals), ([(SPOKEN,)], [1]))
        self.assertEqual(self.status("tg:43:70"), "processing")  # A transcript is not the answer.
        api = TranscriptAPI()
        self.inbox.flush(api)
        self.assertEqual(api.sent(), [{"chat_id": OWNER, "text": SPOKEN, "reply_parameters": {"message_id": 70},
                                       "link_preview_options": {"is_disabled": True}}])
        self.assertEqual(self.transcripts(), [("transcript:tg:43:70", "sent")])
        self.assertEqual(self.status("tg:43:70"), "processing")
        # The normal answer, and any later correction, are ordinary replies.
        self.assertEqual(self.reply("tg:43:70", "Fixed **checkout**.", "coordinator-turn-1", "call-1")[0], True)
        self.inbox.flush(api)
        answer = api.sent()[-1]
        self.assertNotIn("reply_parameters", answer)
        self.assertEqual(answer["text"], "Fixed checkout.")
        self.assertEqual(len(api.sent()), 2)

    async def test_long_transcript_is_ordered_parts_with_safe_retry_and_no_uncertain_replay(self):
        spoken = "\n".join("Linha " + str(i) + " com **asteriscos** e `código` intactos." for i in range(400))
        self.voice(5, 70)
        with self.decoder(spoken):
            await self.adapter.dispatch_pending()
        api = TranscriptAPI(None, BridgeError("Telegram HTTP 429", 429, retry_after=5))
        self.inbox.flush(api)
        self.assertEqual(len(api.sent()), 2)
        self.inbox.flush(api)
        self.assertEqual(len(api.sent()), 2)  # Later parts wait for the backoff.
        with patch("telegram_bridge.time.time", return_value=time.time() + 10):
            self.inbox.flush(api)
        sent = api.sent()
        self.assertEqual(sent[1], sent[2])  # Only the rate-limited part is retried; part 1 is never repeated.
        bodies = [payload["text"] for payload in sent[:1] + sent[2:]]
        self.assertGreater(len(bodies), 1)
        self.assertEqual("".join(body.partition("\n")[2] for body in bodies), spoken)
        self.assertTrue(all(p["reply_parameters"] == {"message_id": 70} and "entities" not in p for p in sent))
        self.assertEqual(self.transcripts(), [("transcript:tg:43:70", "sent")])
        # An uncertain send stays uncertain: no replay to the owner.
        self.voice(6, 71)
        with self.decoder():
            await self.adapter.dispatch_pending()
        uncertain = TranscriptAPI(BridgeError("Telegram request failed; delivery may be uncertain"))
        for _ in range(3):
            with patch("telegram_bridge.time.time", return_value=time.time() + 3601):
                self.inbox.flush(uncertain)
        self.assertEqual(len(uncertain.sent()), 1)
        self.assertIn(("transcript:tg:43:71", "uncertain"), self.transcripts())

    async def test_redelivery_edits_and_saved_results_send_no_repeat_or_old_transcript(self):
        # A transcript decoded before this feature stays where it is.
        self.voice(4, 60)
        with self.inbox.db:
            self.inbox.db.execute("INSERT INTO appserver_audio VALUES (?,?)", ("tg:43:60", json.dumps({
                "status": "transcribed", "text": "old", "audio_sha256": hashlib.sha256(b"voice bytes").hexdigest()})))
        self.voice(5, 70)
        self.voice(5, 70)
        self.voice(6, 70)
        with self.decoder():
            await self.adapter.dispatch_pending()
            self.voice(7, 70, edit_date=99)
            await self.adapter.dispatch_pending()
            await self.adapter.prepare_local_audio(self.inbox.db.execute("SELECT * FROM requests WHERE id='tg:43:70'").fetchone())
        self.assertEqual(self.decoded, ["tg:43:70", "tg:43:70:edit:99"])
        self.assertEqual(self.transcripts(), [("transcript:tg:43:70", "pending")])
        api = TranscriptAPI()
        self.inbox.flush(api)
        self.inbox.flush(api)
        self.assertEqual([p["reply_parameters"]["message_id"] for p in api.sent()], [70])

    async def test_failed_decode_contacts_and_revoked_owner_get_no_transcript_and_answers_continue(self):
        self.voice(5, 70)
        with self.decoder(status="unavailable"):
            await self.adapter.dispatch_pending()
        if HAS_CONTACT:
            self.voice(6, 71, user=CONTACT)
        self.voice(7, 72)
        with self.decoder():
            await self.adapter.dispatch_pending()
        self.assertEqual(self.transcripts(), [("transcript:tg:43:72", "pending")])
        with self.inbox.db:
            self.inbox.db.execute("UPDATE users SET added=? WHERE user_id=?", (time.time() + 60, OWNER))  # Re-enrolled later.
        api = TranscriptAPI()
        self.inbox.flush(api)
        self.assertEqual((api.sent(), self.transcripts()), ([], [("transcript:tg:43:72", "failed")]))
        with self.inbox.db:
            self.inbox.db.execute("UPDATE users SET added=1 WHERE user_id=?", (OWNER,))
        self.assertTrue(self.reply("tg:43:72", "Done.", "coordinator-turn-1", "call-1")[0])
        self.inbox.flush(api)
        self.assertEqual([p["text"] for p in api.sent()], ["Done."])

    async def test_refused_transcript_does_not_hold_the_answer_and_a_changed_target_is_refused(self):
        self.voice(5, 70)
        self.voice(6, 71)
        with self.decoder():
            await self.adapter.dispatch_pending()
        with self.inbox.db:
            plan = json.loads(self.inbox.db.execute("SELECT delivery_plan FROM outbox WHERE id='transcript:tg:43:71'").fetchone()[0])
            plan[0]["payload"]["reply_parameters"]["message_id"] = 5  # Not this owner's recording.
            self.inbox.db.execute("UPDATE outbox SET delivery_plan=? WHERE id='transcript:tg:43:71'", (json.dumps(plan),))
        self.assertTrue(self.reply("tg:43:70", "Answer one.", "coordinator-turn-1", "call-1")[0])
        api = TranscriptAPI(BridgeError("Telegram HTTP 400", 400))  # For example, the voice note was deleted.
        self.inbox.flush(api)
        self.assertEqual([(p["text"], "reply_parameters" in p) for p in api.sent()], [(SPOKEN, True), ("Answer one.", False)])
        self.assertEqual(self.transcripts(), [("transcript:tg:43:70", "failed"), ("transcript:tg:43:71", "failed")])
        self.assertEqual(self.status("tg:43:71"), "processing")

FAKE_FFMPEG = """#!/usr/bin/python3
import sys, wave
data = sys.stdin.buffer.read()
out = sys.argv[-1]
with wave.open(out, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    seconds = 1801 if data.startswith(b"LONG") else int(data[:4])
    w.writeframes((b"\\x00\\x10\\x00\\xf0") * (8000 * seconds))
"""
FAKE_WHISPER = """#!/usr/bin/python3
import json, sys, wave
args = sys.argv
part = args[args.index("-f") + 1]
with wave.open(part) as w:
    seconds = w.getnframes() / w.getframerate()
assert seconds <= 300, seconds
name = part.rsplit("part-", 1)[1].split(".")[0]
json.dump({"transcription": [{"text": "pass " + name, "tokens": [{"text": "pass", "p": 0.9}]}],
           "result": {"language": "en"}}, open(args[args.index("-of") + 1] + ".json", "w"))
"""


class LongVoice(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        tools = Path(tempfile.mkdtemp(dir=self.root.parent)).resolve()
        self.addCleanup(lambda: [p.unlink() for p in tools.iterdir()] and tools.rmdir())
        paths = {}
        for name, body in (("ffmpeg", FAKE_FFMPEG), ("whisper", FAKE_WHISPER), ("model.bin", "model")):
            path = tools / name
            path.write_text(body)
            path.chmod(0o755 if name != "model.bin" else 0o644)
            paths[name] = str(path)
        config = self.root / "local-audio.json"
        config.write_text(json.dumps({"enabled": True, "whisper": paths["whisper"], "ffmpeg": paths["ffmpeg"],
                                      "model": paths["model.bin"], "model_sha256": hashlib.sha256(b"model").hexdigest()}))
        config.chmod(0o600)
        (self.root / "attachments").mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def voice(self, data):
        key = "tg:43:1"
        path = self.root / "attachments" / (hashlib.sha256(key.encode()).hexdigest() + ".ogg")
        path.write_bytes(data)
        path.chmod(0o600)
        return key, {"kind": "voice", "status": "ready", "local_path": str(path), "size": len(data)}

    def test_seven_minute_voice_note_is_transcribed_in_bounded_passes(self):
        result = transcribe(self.root, *self.voice(b"0420 opus"))
        self.assertEqual((result["status"], result["text"], result["passes"]), ("transcribed", "pass 0 pass 1", 2))
        self.assertEqual(result["duration_seconds"], 420)
        self.assertLessEqual(MAX_SECONDS, 300)

    def test_recording_beyond_local_capacity_is_reported_not_guessed(self):
        result = transcribe(self.root, *self.voice(b"LONG opus"))
        self.assertEqual(result, {"status": "unavailable", "reason": "local_decode_failed_or_unclear"})


if __name__ == "__main__":
    unittest.main()
