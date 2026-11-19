"""v0.7 architecture: reviewed checkpoints, assignment recovery at intake, elected direct owner returns and owner tab sessions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from test_owner_friction import Base, COORDINATOR, HAS_CONTACT, OWNER, CONTACT, PROJECT_OWNER, Daemon, message

import alfred_direct
import alfred_projects
from alfred_assignments import capsule
from alfred_checkpoint import confirm, prepare
from alfred_local import browser_tab, finish, handoff, match, stage_return, submit
from alfred_outcomes import retain_direct_receipts
from telegram_bridge import BridgeError, Inbox, PREFIX

SECOND_OWNER = "00000000-0000-4000-8000-0000000000a2"
CARD = "tasks/done/checkout-fix.md"
ANSWER = "The checkout bug is fixed and verified."
ROOT = Path(__file__).resolve().parents[1]
OWNER_TURN = "owner-turn"  # The turn ID the test daemon gives every owner turn/start.


class Socket:
    """The app-server websocket as the adapter reads it: raw JSON frames, then close."""
    def __init__(self, *messages):
        self.frames = [json.dumps(item) for item in messages]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)


def final(text, owner=PROJECT_OWNER, turn=OWNER_TURN, status="completed"):
    """The native notifications for an owner turn that ends with this final answer."""
    item = {"type": "agentMessage", "phase": "final_answer", "text": text}
    return [{"method": "item/completed", "params": {"threadId": owner, "turnId": turn, "item": item}},
            {"method": "turn/completed", "params": {"threadId": owner, "turn": {"id": turn, "status": status, "items": [item]}}}]


class Workspace(Base):
    def setUp(self):
        super().setUp()
        self.space = tempfile.TemporaryDirectory()
        self.workspace = Path(self.space.name)
        for state in ("backlog", "todo", "doing", "blocked", "review", "done"):
            (self.workspace / "tasks" / state).mkdir(parents=True)
        (self.workspace / "priorities.md").write_text("# Priorities\n\nShip the checkout fix.\n")
        self.index.write_text(self.index.read_text() + f"| P3 | Docs | private | `/docs` | `codex:{SECOND_OWNER}` |\n")
        self.adapter.workspace = self.adapter.router.workspace = self.workspace
        self.sends = 0

        def signal():
            self.sends += 1
        self.adapter.outbox_signal = signal
        self.addCleanup(self.space.cleanup)

    def dispatched(self):
        return [params["input"][0]["text"] for method, params in self.daemon.calls
                if method in {"turn/start", "turn/steer"} and params["threadId"] == COORDINATOR]

    def assignments(self, text):
        return json.loads(text.split("\n", 1)[1])["assignments"]

    def card(self, route_id, column="done", extra=""):
        path = self.workspace / "tasks" / column / Path(CARD).name
        path.write_text("# Checkout fix\n\nProject: Shop\nRoute: " + route_id + "\n" + extra)
        return path

    def stage(self, route_id, body=ANSWER, card=CARD, owner=PROJECT_OWNER, inbox=None):
        return stage_return(inbox or self.inbox, route_id, "codex:" + owner, body, card, workspace=self.workspace)

    async def native(self, *messages):
        """Feed frames through the adapter's real socket reader and wait for the router."""
        self.adapter.ws = Socket(*messages)
        await self.adapter.receive()
        await self.drain()

    async def elected(self, update=1, brief="DIRECT_RETURN card=" + CARD + "\nFix the checkout bug", call="route-1"):
        self.daemon.active = None
        self.inbox.ingest(message(update))
        await self.adapter.dispatch_pending()
        turn = self.daemon.active
        route = json.loads((await self.adapter.router.handle({
            "threadId": COORDINATOR, "turnId": turn, "callId": call, "tool": alfred_projects.TOOL_NAME,
            "arguments": {"request_id": "tg:43:" + str(update), "owner_id": "codex:" + PROJECT_OWNER,
                          "brief": brief}}))["contentItems"][0]["text"])
        return route, turn

    def outcomes(self):
        return self.inbox.db.execute("SELECT COUNT(*) FROM request_context WHERE source='project_outcome'").fetchone()[0]

    def answer(self, request_id):
        row = self.inbox.db.execute("SELECT body FROM outbox WHERE id=?", ("result:" + request_id,)).fetchone()
        return row[0] if row else None

    def fresh(self):
        with self.inbox.db:
            for table in ("requests", "outbox", "outbox_parts", "appserver_dispatch", "appserver_project_routes", "request_context",
                          "appserver_direct_elections", "appserver_direct_candidates", "appserver_direct_returns",
                          "appserver_project_calls", "appserver_project_turn_events", "appserver_project_turn_results"):
                self.inbox.db.execute("DELETE FROM " + table)
        for path in self.workspace.glob("tasks/*/*.md"):
            path.unlink()
        (self.workspace / "priorities.md").write_text("# Priorities\n\nShip the checkout fix.\n")


class DirectReturn(Workspace):
    async def test_native_final_sends_the_staged_answer_once_and_staging_alone_sends_nothing(self):
        route, turn = await self.elected()
        self.assertEqual(route["status"], "accepted")
        owner_input = next(params["input"][0]["text"] for method, params in self.daemon.calls
                           if method == "turn/start" and params["threadId"] == PROJECT_OWNER)
        self.assertIn(" return --route-id " + route["route_id"], owner_input)
        self.assertIn("staging alone sends nothing", owner_input)
        self.card(route["route_id"])
        staged = self.stage(route["route_id"])
        self.assertEqual((staged["status"], staged["sent"]), ("candidate_staged", False))
        self.assertIsNone(self.answer("tg:43:1"))  # A caller with route and owner IDs cannot send.
        self.assertEqual(self.status("tg:43:1"), "processing")
        await self.native(*final(ANSWER))
        self.assertEqual(self.answer("tg:43:1"), PREFIX + ANSWER)
        self.assertEqual((self.status("tg:43:1"), self.outcomes(), self.sends), ("done", 0, 1))
        binding = self.inbox.db.execute("SELECT owner_id,turn_id,chat_id FROM appserver_direct_returns").fetchone()
        self.assertEqual(tuple(binding), ("codex:" + PROJECT_OWNER, OWNER_TURN, OWNER))
        # Replayed notifications, a late Chief reply and a different staged answer send nothing more.
        await self.native(*final(ANSWER))
        self.assertEqual(self.reply("tg:43:1", "Another answer", turn, "late"), (False, "Request was already completed with another reply"))
        self.assertEqual(self.stage(route["route_id"])["status"], "returned_directly")
        with self.assertRaisesRegex(BridgeError, "different answer"):
            self.stage(route["route_id"], body="Changed")
        self.assertEqual(self.inbox.db.execute("SELECT COUNT(*) FROM outbox WHERE id LIKE 'result:tg:43:1%'").fetchone()[0], 1)
        self.assertEqual(self.outcomes(), 0)
        self.assertEqual(sum(1 for method, params in self.daemon.calls
                             if method == "turn/start" and params["threadId"] == COORDINATOR), 1)
        # Chief learns of it at its next intake, once.
        self.daemon.active = None
        self.inbox.ingest(message(2, text="Anything else?"))
        await self.adapter.dispatch_pending()
        first = self.assignments(self.dispatched()[-1])
        self.assertTrue(any(item["kind"] == "direct_return" for item in first.get("items", first.get("changed", []))))
        self.daemon.active = None
        self.inbox.ingest(message(3, text="And now?"))
        await self.adapter.dispatch_pending()
        self.assertNotIn("direct_return", json.dumps(self.assignments(self.dispatched()[-1]).get("changed", [])))

    async def test_shared_owner_turn_returns_to_chief_for_recipient_review(self):
        route, _ = await self.elected()
        self.card(route["route_id"])
        self.stage(route["route_id"])
        await self.elected(update=2, brief="Another project question", call="second-route")
        with self.inbox.db:
            self.inbox.db.execute("UPDATE appserver_project_routes SET turn_id=(SELECT turn_id FROM appserver_project_routes "
                                  "WHERE route_id=?) WHERE request_id='tg:43:2'", (route["route_id"],))
        await self.native(*final(ANSWER))
        self.assertIsNone(self.answer("tg:43:1"))
        self.assertIsNone(self.answer("tg:43:2"))
        self.assertEqual(self.inbox.db.execute("SELECT COUNT(*) FROM appserver_direct_returns").fetchone()[0], 0)

    async def test_failed_or_mismatched_proof_goes_to_chief_once(self):
        def owners_changed(route_id):
            self.index.write_text(self.index.read_text() + "| P4 | Ops | private | `/ops` | `codex:00000000-0000-4000-8000-0000000000a3` |\n")
        cases = {
            "no staged answer": (False, lambda route_id: None, ANSWER, "completed"),
            "differs from the staged answer": (True, lambda route_id: None, ANSWER + " Also deployed.", "completed"),
            "card changed after the answer was staged": (True, lambda route_id: self.card(route_id, extra="Edited\n"), ANSWER, "completed"),
            "card is ambiguous": (True, lambda route_id: self.card(route_id, "doing"), ANSWER, "completed"),
            "operating rules changed": (True, lambda route_id: (self.workspace / "priorities.md").write_text("Hold releases\n"),
                                        ANSWER, "completed"),
            "registry or operating rules changed": (True, owners_changed, ANSWER, "completed"),
            "no longer authorized": (True, lambda route_id: self.inbox.db.execute(
                "UPDATE users SET added=? WHERE user_id=?", (time.time() + 5, OWNER)) and self.inbox.db.commit(), ANSWER, "completed"),
            "already closed": (True, lambda route_id: self.reply("tg:43:" + self.update, "Chief answered first",
                                                                 self.turn, "first-" + self.update), ANSWER, "completed"),
            "notice or uncertain send": (True, lambda route_id: (self.inbox.reply("result:tg:43:" + self.update + ":fault:1", OWNER, "Fault"),
                                                                self.inbox.db.commit()), ANSWER, "completed"),
            "did not complete": (True, lambda route_id: None, ANSWER, "failed"),
        }
        registry = self.index.read_text()
        for index, (reason, (staged, arrange, text, status)) in enumerate(cases.items()):
            with self.subTest(reason=reason):
                self.fresh()
                self.index.write_text(registry)
                with self.inbox.db:
                    self.inbox.db.execute("UPDATE users SET added=1 WHERE user_id=?", (OWNER,))
                self.update = str(100 + index)
                route, self.turn = await self.elected(100 + index, call="r" + self.update)
                self.card(route["route_id"])
                if staged:
                    self.stage(route["route_id"])
                arrange(route["route_id"])
                await self.native(*final(text, status=status))
                self.assertIsNone(self.inbox.db.execute("SELECT 1 FROM appserver_direct_returns").fetchone())
                if reason != "already closed":
                    self.assertIsNone(self.answer("tg:43:" + self.update))
                if reason == "no longer authorized":
                    # The existing relay guard holds a result for a revoked recipient; nothing is sent.
                    self.assertEqual(self.outcomes(), 0)
                    self.assertTrue(self.inbox.db.execute("SELECT error FROM appserver_project_routes").fetchone()[0]
                                    .startswith("Outcome relay held."))
                    continue
                self.assertEqual(self.outcomes(), 1)
                child = self.inbox.db.execute("SELECT body FROM requests r JOIN request_context c ON c.request_id=r.id "
                                              "WHERE c.source='project_outcome'").fetchone()[0]
                self.assertIn("Direct return not used: ", child)
                self.assertIn(reason, child)
                await self.native(*final(text, status=status))  # Redelivered: still exactly one result for Chief.
                self.assertEqual(self.outcomes(), 1)

    async def test_staging_is_bound_to_the_elected_route_card_owner_and_open_turn(self):
        route, turn = await self.elected()
        for kwargs, error in (({}, "Settle the card first: the elected card is not in tasks/done"),
                              ({"card": "tasks/done/other.md"}, "does not match"),
                              ({"body": "password: hunter2-secret"}, "credential-like"),
                              ({"owner": "00000000-0000-4000-8000-0000000000ff"}, "current owner")):
            with self.subTest(error=error), self.assertRaisesRegex(BridgeError, error):
                self.stage(route["route_id"], **kwargs)
        self.card(route["route_id"])
        second = await self.route("tg:43:1", turn, "route-2", brief="Update the docs", owner=SECOND_OWNER)
        self.assertEqual(second, "accepted")
        other = self.inbox.db.execute("SELECT route_id FROM appserver_project_routes WHERE owner_id=?",
                                      ("codex:" + SECOND_OWNER,)).fetchone()[0]
        with self.assertRaisesRegex(BridgeError, "did not elect"):
            self.stage(other, owner=SECOND_OWNER)
        self.stage(route["route_id"])
        # Another owner's or another turn's final is not proof for this route.
        await self.native(*final(ANSWER, owner=SECOND_OWNER, turn="other-turn"), *final(ANSWER, turn="another-turn"))
        self.assertEqual(self.inbox.db.execute("SELECT state FROM appserver_project_routes WHERE route_id=?",
                                               (route["route_id"],)).fetchone()[0], "accepted")
        await self.native(*final(ANSWER))
        self.assertIn("another owner is assigned", self.inbox.db.execute(
            "SELECT body FROM requests r JOIN request_context c ON c.request_id=r.id WHERE c.source='project_outcome'").fetchone()[0])
        self.assertIsNone(self.answer("tg:43:1"))
        with self.assertRaisesRegex(BridgeError, "not awaiting"):
            self.stage(route["route_id"])
        # Chief cannot elect after replying, or with a malformed marker.
        self.daemon.active = None
        self.inbox.ingest(message(2))
        await self.adapter.dispatch_pending()
        turn = self.daemon.active
        self.assertTrue(self.reply("tg:43:2", "Answered first", turn, "first")[0])
        self.assertEqual(await self.route("tg:43:2", turn, "after-reply", brief="DIRECT_RETURN card=" + CARD), "rejected_direct_return")
        self.assertEqual(await self.route("tg:43:2", turn, "bad-marker", brief="DIRECT_RETURN to the sender"), "rejected_direct_return")

    async def test_reconnect_reconciles_from_the_owner_turn_record_not_the_local_candidate(self):
        route, _ = await self.elected()
        self.card(route["route_id"])
        self.stage(route["route_id"])

        class Reconnect(Daemon):
            def __init__(self, owner_text):
                super().__init__()
                self.owner_text = owner_text

            async def __call__(self, method, params):
                if params.get("threadId") == PROJECT_OWNER and method == "thread/turns/list":
                    return {"data": [{"id": OWNER_TURN, "status": "completed"}]}
                if params.get("threadId") == PROJECT_OWNER and method == "thread/items/list":
                    return {"data": [{"turnId": OWNER_TURN, "item": {"type": "agentMessage", "phase": "final_answer",
                                                                      "text": self.owner_text}}]}
                return await super().__call__(method, params)
        self.adapter.router.rpc = Reconnect(ANSWER)
        await self.adapter.router.reconcile()
        await self.drain()
        self.assertEqual((self.answer("tg:43:1"), self.outcomes()), (PREFIX + ANSWER, 0))
        # A reconnect whose turn record ends differently goes to Chief instead.
        route, _ = await self.elected(2, call="route-2")
        self.card(route["route_id"])
        self.stage(route["route_id"])
        self.adapter.router.rpc = Reconnect("Something else entirely")
        await self.adapter.router.reconcile()
        await self.drain()
        self.assertIsNone(self.answer("tg:43:2"))
        self.assertEqual(self.outcomes(), 1)

    async def test_concurrent_settles_and_a_racing_chief_reply_never_double_send(self):
        route, turn = await self.elected()
        self.card(route["route_id"])
        self.stage(route["route_id"])
        owners = alfred_projects.registered_owners()

        def settle(_):
            inbox = Inbox(self.root)
            try:
                return alfred_direct.settle(inbox, route["route_id"], "codex:" + PROJECT_OWNER, OWNER_TURN,
                                            alfred_direct.sha(ANSWER), owners, self.workspace)[0]
            finally:
                inbox.db.close()
        with ThreadPoolExecutor(4) as pool:
            self.assertEqual(sorted(pool.map(settle, range(4))), [False, False, False, True])
        self.assertEqual(self.inbox.db.execute("SELECT COUNT(*) FROM outbox WHERE id='result:tg:43:1'").fetchone()[0], 1)
        # Chief replying while the owner's final settles: one answer wins and the other is told.
        route, turn = await self.elected(2, call="route-2")
        second_turn = "owner-turn-2"
        with self.inbox.db:
            self.inbox.db.execute("UPDATE appserver_project_routes SET turn_id=? WHERE route_id=?",
                                  (second_turn, route["route_id"]))
        self.card(route["route_id"])
        self.stage(route["route_id"])
        original = self.inbox.reply

        def owner_first(key, chat, text, reply_to=None):
            other = Inbox(self.root)
            try:
                alfred_direct.settle(other, route["route_id"], "codex:" + PROJECT_OWNER, second_turn,
                                     alfred_direct.sha(ANSWER), owners, self.workspace)
            finally:
                other.db.close()
            return original(key, chat, text, reply_to)
        with patch.object(self.inbox, "reply", owner_first):
            self.assertEqual(self.reply("tg:43:2", "Chief's own answer", turn, "race-reply"),
                             (False, "Another answer was saved for this request first; no duplicate was queued"))
        self.assertEqual(self.answer("tg:43:2"), PREFIX + ANSWER)

    async def test_quiet_election_and_retained_receipt_after_outbox_expiry(self):
        route, _ = await self.elected(brief="DIRECT_RETURN quiet card=" + CARD + "\nFix the checkout bug")
        self.card(route["route_id"])
        self.stage(route["route_id"])
        await self.native(*final(ANSWER))
        self.assertEqual(self.stage(route["route_id"])["delivery_state"], "suppressed")
        with self.inbox.db:
            self.inbox.db.execute("UPDATE outbox SET created=1 WHERE id='result:tg:43:1'")
            retain_direct_receipts(self.inbox.db, time.time())
            self.inbox.db.execute("DELETE FROM outbox WHERE id='result:tg:43:1'")
        self.assertEqual(self.stage(route["route_id"])["delivery_state"], "suppressed")
        if HAS_CONTACT:
            self.inbox.ingest(message(9, user=CONTACT))
            self.daemon.active = None
            await self.adapter.dispatch_pending()
            self.assertEqual(await self.route("tg:42:9", self.daemon.active, "contact",
                                              brief="DIRECT_RETURN quiet card=" + CARD), "rejected_direct_return")


class BrowserSessions(Workspace):
    def owner_input(self, owner):
        return json.loads(next(params["input"][0]["text"] for method, params in reversed(self.daemon.calls)
                               if method == "turn/start" and params["threadId"] == owner).split("\n", 1)[1])

    async def test_each_project_owner_gets_its_own_tab_session_and_ids_grant_nothing(self):
        self.inbox.ingest(message(1))
        await self.adapter.dispatch_pending()
        turn = self.daemon.active
        self.assertEqual(await self.route("tg:43:1", turn, "shop"), "accepted")
        self.assertEqual(await self.route("tg:43:1", turn, "docs", brief="Update docs", owner=SECOND_OWNER), "accepted")
        shop, docs = self.owner_input(PROJECT_OWNER)["browser"], self.owner_input(SECOND_OWNER)["browser"]
        self.assertNotEqual(shop["sessionName"], docs["sessionName"])
        self.assertTrue(shop["show"].endswith(" --action show"))
        shop_route, docs_route = (self.inbox.db.execute("SELECT route_id FROM appserver_project_routes WHERE owner_id=?",
                                                        ("codex:" + owner,)).fetchone()[0] for owner in (PROJECT_OWNER, SECOND_OWNER))
        def shop_tab(action, tab, **fields):
            options = {"origin": "https://shop.example", "instance": "native-1", **fields}
            return browser_tab(self.inbox, shop_route, "codex:" + PROJECT_OWNER, action,
                               "Chrome:default", tab, **options)["status"]

        def docs_tab(action, tab, **fields):
            return browser_tab(self.inbox, docs_route, "codex:" + SECOND_OWNER, action,
                               "Chrome:default", tab, origin="https://docs.example", instance="native-1", **fields)["status"]

        self.assertEqual(shop_tab("bind", "tab-11"), "bound")
        self.assertEqual(docs_tab("bind", "tab-11"), "conflict")
        self.assertEqual(docs_tab("bind", "tab-12"), "bound")
        self.assertEqual(shop_tab("check", "tab-11"), "matched")
        self.assertEqual(shop_tab("check", "tab-11", origin="https://other.example"), "stale")
        self.assertEqual(shop_tab("check", "tab-11", instance="native-2"), "stale")
        self.assertEqual(shop_tab("bind", "tab-13"), "conflict")
        self.assertEqual(shop_tab("bind", "tab-13", slot="analytics"), "bound")
        self.assertEqual(shop_tab("reset", "tab-13", reason="closed"), "stale")
        self.assertEqual(shop_tab("reset", "tab-11", reason="closed"), "reset")
        self.assertEqual(shop_tab("bind", "tab-21"), "bound")
        self.assertEqual(shop_tab("check", "tab-13", slot="analytics"), "matched")
        self.assertEqual(docs_tab("check", "tab-12"), "matched")
        with self.assertRaisesRegex(BridgeError, "open route"):
            browser_tab(self.inbox, shop_route, "codex:" + SECOND_OWNER, "show")
        # A binding grants nothing: revoking the sender closes tab recording as well as routing.
        with self.inbox.db:
            self.inbox.db.execute("UPDATE users SET added=? WHERE user_id=?", (time.time() + 5, OWNER))
        with self.assertRaisesRegex(BridgeError, "open route"):
            browser_tab(self.inbox, shop_route, "codex:" + PROJECT_OWNER, "show")
        for bad in ({"profile": "../Default"}, {"tab_id": "tab 1; rm"}):
            with self.subTest(bad=bad):
                with self.inbox.db:
                    self.inbox.db.execute("UPDATE users SET added=1 WHERE user_id=?", (OWNER,))
                with self.assertRaises(BridgeError):
                    browser_tab(self.inbox, shop_route, "codex:" + PROJECT_OWNER, "bind",
                                bad.get("profile", "Default"), bad.get("tab_id", "tab-30"), origin="https://shop.example")

    async def test_reassigned_project_resets_the_previous_owners_tab(self):
        self.inbox.ingest(message(1))
        await self.adapter.dispatch_pending()
        self.assertEqual(await self.route("tg:43:1", self.daemon.active, "shop"), "accepted")
        route_id = self.inbox.db.execute("SELECT route_id FROM appserver_project_routes").fetchone()[0]
        browser_tab(self.inbox, route_id, "codex:" + PROJECT_OWNER, "bind", "Default", "tab-11", origin="https://shop.example")
        self.index.write_text(self.index.read_text().replace(f"`codex:{PROJECT_OWNER}`", "`codex:00000000-0000-4000-8000-0000000000a9`"))
        self.daemon.active = None
        self.inbox.ingest(message(2))
        await self.adapter.dispatch_pending()
        self.assertEqual(await self.route("tg:43:2", self.daemon.active, "new-owner", owner="00000000-0000-4000-8000-0000000000a9"),
                         "accepted")
        row = self.inbox.db.execute("SELECT owner_id,tab_id,state,reason FROM appserver_browser_tabs").fetchone()
        self.assertEqual(tuple(row), ("codex:" + PROJECT_OWNER, None, "reset", "reassigned"))
        self.assertEqual(self.owner_input("00000000-0000-4000-8000-0000000000a9")["browser"].get("bindings", []), [])


class AssignmentRecovery(Workspace):
    async def test_full_once_then_changes_only_and_full_again_after_resume_or_compaction(self):
        self.inbox.ingest(message(1, text="First open request"))
        self.inbox.ingest(message(2, text="Second open request"))
        await self.adapter.dispatch_pending()
        first, second = (self.assignments(text) for text in self.dispatched())
        self.assertEqual(first["mode"], "full")
        self.assertIn("Second open request", json.dumps(first))  # The other unanswered input, in full.
        self.assertNotIn("First open request", json.dumps(first))  # Already the envelope body.
        self.assertEqual(second["mode"], "unchanged")  # The warm turn does not repeat the backlog.
        self.assertNotIn("open request", json.dumps(second))
        self.daemon.active = None
        self.inbox.ingest(message(3, text="Third"))
        await self.adapter.dispatch_pending()
        self.assertEqual(self.assignments(self.dispatched()[-1])["changed"], [])
        self.adapter.notification({"method": "thread/compacted", "params": {"threadId": COORDINATOR, "turnId": "x"}})
        self.daemon.active = None
        self.inbox.ingest(message(4, text="Fourth"))
        await self.adapter.dispatch_pending()
        after = self.assignments(self.dispatched()[-1])
        self.assertEqual((after["mode"], after["reason"]), ("full", "compaction"))
        for body in ("First open request", "Second open request", "Third"):
            self.assertIn(body, json.dumps(after))
        self.assertIn("alfred_assignments.py", after["reread"])

    async def test_capsule_keeps_owner_results_transcripts_questions_cards_and_rule_changes(self):
        route, turn = await self.elected(brief="Fix the checkout bug")
        with self.inbox.db:
            self.inbox.db.execute("CREATE TABLE IF NOT EXISTS appserver_audio (request_id TEXT PRIMARY KEY, result TEXT NOT NULL)")
            long_text = "word " * 4000
            self.inbox.db.execute("INSERT OR REPLACE INTO appserver_audio VALUES ('tg:43:1',?)",
                                  (json.dumps({"status": "transcribed", "text": long_text}),))
        self.adapter.router.notification({"method": "turn/completed", "params": {"threadId": PROJECT_OWNER, "turn": {
            "id": "owner-turn", "status": "completed", "items": [{"type": "agentMessage", "phase": "final_answer", "text": "Shop result"}]}}})
        await self.drain()
        (self.workspace / "tasks/blocked/login.md").write_text("# Login\n\nBlocked by: staging password reset\nResolver: Owner\n")
        self.card(route["route_id"], "doing")
        items = capsule(self.inbox.db, self.root, self.workspace)["items"]
        self.assertEqual(items["request:tg:43:1"]["local_audio"]["text"], long_text)
        child = next(item for item in items.values() if item.get("source") == "project_outcome")
        self.assertEqual(child["parent_request_id"], "tg:43:1")
        self.assertIn("Shop result", child["body"])
        self.assertTrue(items["card:tasks/blocked/login.md"]["question"])
        self.assertEqual(items["card:tasks/doing/checkout-fix.md"]["ledger_ids"], [route["route_id"]])
        self.assertEqual(items["grant:Owner"]["projects"], ["Docs", "Shop"])
        self.assertEqual(items["route:" + route["route_id"]]["brief"], "Fix the checkout bug")
        before = items["rules:priorities.md"]["sha256"]
        self.daemon.active = None
        await self.adapter.dispatch_pending()
        (self.workspace / "priorities.md").write_text("# Priorities\n\nNo releases this week.\n")
        self.inbox.ingest(message(5, text="Next"))
        await self.adapter.dispatch_pending()
        changed = self.assignments(self.dispatched()[-1])["changed"]
        rules = next(item for item in changed if item.get("path") == "priorities.md")
        self.assertNotEqual(rules["sha256"], before)

    async def test_revoked_sender_or_device_inputs_keep_a_reference_but_no_content(self):
        self.inbox.ingest(message(1, text="Telegram request"))
        submit(self.inbox, "device-event", "Paired device request")
        device_id = self.inbox.db.execute("SELECT request_id FROM request_context WHERE event_id='device-event'").fetchone()[0]
        with self.inbox.db:
            self.inbox.db.execute("UPDATE request_context SET event_id=? WHERE request_id=?",
                                  ("device:00000000-0000-4000-8000-00000000d001:" + "0" * 64, device_id))
        allowed = types.ModuleType("alfred_device_entry")
        allowed.request_allowed = lambda db, request_id: True
        with patch.dict(sys.modules, {"alfred_device_entry": allowed}):
            self.assertEqual(capsule(self.inbox.db, self.root, self.workspace)["items"]["request:" + device_id]["body"],
                             "Paired device request")
        allowed.request_allowed = lambda db, request_id: False
        with patch.dict(sys.modules, {"alfred_device_entry": allowed}):
            items = capsule(self.inbox.db, self.root, self.workspace)["items"]
        self.assertEqual(items["request:" + device_id]["authorization"], "revoked")
        self.assertNotIn("Paired device request", json.dumps(items))
        self.assertEqual(items["request:tg:43:1"]["body"], "Telegram request")
        with self.inbox.db:
            self.inbox.db.execute("UPDATE users SET added=? WHERE user_id=?", (time.time() + 5, OWNER))
        items = capsule(self.inbox.db, self.root, self.workspace)["items"]
        self.assertNotIn("Telegram request", json.dumps(items))

    async def test_rejected_dispatch_resends_the_full_capsule(self):
        self.inbox.ingest(message(1))
        await self.adapter.dispatch_pending()
        self.daemon.active = None
        from alfred_appserver import RpcError
        original = self.daemon.__call__

        async def reject(method, params):
            if method == "turn/start":
                raise RpcError(-32000, "rejected")
            return await original(method, params)
        self.adapter.rpc = reject
        self.inbox.ingest(message(2))
        await self.adapter.dispatch_pending()
        self.adapter.rpc = self.daemon
        self.inbox.ingest(message(3))
        await self.adapter.dispatch_pending()
        self.assertEqual(self.assignments(self.dispatched()[-1])["mode"], "full")


class Routes(Workspace):
    OWNER_ID = "codex:" + PROJECT_OWNER

    async def two_routes(self, update=1, second_route=True):
        """Two different requests in one coordinator turn, routed to the same owner."""
        self.daemon.active = None
        self.inbox.ingest(message(update, text="Fix the checkout bug"))
        self.inbox.ingest(message(update + 1, text="Rename the shipping page"))
        await self.adapter.dispatch_pending()
        self.turn = self.daemon.active
        self.assertEqual(await self.route("tg:43:" + str(update), self.turn, "checkout-" + str(update),
                                          brief="Fix the checkout bug"), "accepted")
        if second_route:
            await self.route_second(update)
        return [self.route_id("tg:43:" + str(update + index)) for index in range(1 + second_route)]

    async def route_second(self, update=1):
        self.assertEqual(await self.route("tg:43:" + str(update + 1), self.turn, "shipping-" + str(update),
                                          brief="Rename the shipping page"), "accepted")

    def route_id(self, request_id):
        return self.inbox.db.execute("SELECT route_id FROM appserver_project_routes WHERE request_id=?", (request_id,)).fetchone()[0]

    def state(self, route_id):
        return self.inbox.db.execute("SELECT state FROM appserver_project_routes WHERE route_id=?", (route_id,)).fetchone()[0]

    def children(self):
        return dict(self.inbox.db.execute("""SELECT c.parent_request_id,r.body FROM request_context c JOIN requests r ON r.id=c.request_id
                                          WHERE c.source='project_outcome' AND c.parent_request_id IS NOT NULL"""))

    def notices(self):
        return [row[0] for row in self.inbox.db.execute("""SELECT r.body FROM request_context c JOIN requests r ON r.id=c.request_id
                                                        WHERE c.event_id LIKE 'project-turn:%' ORDER BY r.received""")]

    def owner_input(self, route_id):
        return self.inbox.db.execute("SELECT input_text FROM appserver_project_routes WHERE route_id=?", (route_id,)).fetchone()[0]


class SharedOwnerTurns(Routes):
    """Different requests that reach one owner in one native turn (the test daemon gives every owner turn/start OWNER_TURN)."""
    async def test_one_job_final_stays_simple_and_cannot_use_the_sibling_command(self):
        self.inbox.ingest(message(1))
        await self.adapter.dispatch_pending()
        self.assertEqual(await self.route("tg:43:1", self.daemon.active, "only"), "accepted")
        route_id = self.route_id("tg:43:1")
        envelope = json.loads(self.owner_input(route_id).split("\n", 1)[1])
        self.assertNotIn("finish", envelope)
        self.assertIn("ROUTE_RESULT <route_id>", self.owner_input(route_id))
        with self.assertRaisesRegex(BridgeError, "shares its owner turn"):
            finish(self.inbox, route_id, self.OWNER_ID, "Progress: halfway")
        await self.native(*final("Checkout fixed."))
        self.assertEqual(self.state(route_id), "completed")
        self.assertIn("Checkout fixed.", self.children()["tg:43:1"])
        self.assertEqual(self.notices(), [])

    async def test_labelled_final_closes_only_its_route_and_the_sibling_is_finished_later(self):
        first, second = await self.two_routes()
        await self.native(*final("ROUTE_RESULT " + second + "\nShipping page renamed."))
        self.assertEqual((self.state(first), self.state(second)), ("accepted", "completed"))
        self.assertEqual(list(self.children()), ["tg:43:2"])
        self.assertIn("Shipping page renamed.", self.children()["tg:43:2"])
        notices = self.notices()
        self.assertEqual(len(notices), 1)
        self.assertIn(first, notices[0])
        self.assertNotIn("Shipping page renamed", notices[0])
        await self.native(*final("ROUTE_RESULT " + second + "\nShipping page renamed."))  # Redelivered: nothing new.
        self.assertEqual((len(self.children()), len(self.notices())), (1, 1))
        finish(self.inbox, first, self.OWNER_ID, "Checkout fixed and verified.")
        self.assertEqual(finish(self.inbox, first, self.OWNER_ID, "Checkout fixed and verified."),
                         self.inbox.db.execute("SELECT request_id FROM request_context WHERE parent_request_id='tg:43:1'").fetchone()[0])
        children = self.children()
        self.assertIn("Checkout fixed and verified.", children["tg:43:1"])
        self.assertNotIn("Shipping", children["tg:43:1"])
        self.assertNotIn("Checkout", children["tg:43:2"])

    async def test_unlabelled_final_waits_privately_for_chief_and_keeps_every_route_open(self):
        first, second = await self.two_routes()
        raw = ("Checkout fixed; the shipping page is renamed in build " + "0123456789abcdef" * 4 + ".\n"
               + "Verified detail. " * 600)
        text = alfred_projects.safe_text(raw)  # The existing outcome redaction; nothing is shortened.
        self.assertIn("[REDACTED]", text)
        await self.native(*final(raw))
        self.assertEqual((self.state(first), self.state(second), self.children()), ("accepted", "accepted", {}))
        await self.native(*final(raw))
        notices = self.notices()
        self.assertEqual(len(notices), 1)
        self.assertNotIn("Checkout fixed", notices[0])
        self.assertIn("assignments item turn:" + self.OWNER_ID + ":" + OWNER_TURN, notices[0])
        self.assertNotIn("[REDACTED]", notices[0])
        items = capsule(self.inbox.db, self.root, self.workspace)["items"]
        pending = items["turn:" + self.OWNER_ID + ":" + OWNER_TURN]
        self.assertEqual((pending["state"], pending["open_routes"]), ("ambiguous", [first, second]))
        self.assertTrue(pending["read"].endswith(" match --owner-id " + self.OWNER_ID + " --turn-id " + OWNER_TURN))
        shown = subprocess.run([*shlex.split(pending["read"]), "--root", str(self.root)], capture_output=True, text=True)
        self.assertEqual(json.loads(shown.stdout)["final"], text, shown.stderr)
        self.assertNotIn("Checkout fixed", json.dumps(items["turn:" + self.OWNER_ID + ":" + OWNER_TURN]))
        saved = match(self.inbox, self.OWNER_ID, OWNER_TURN)
        self.assertEqual((saved["status"], saved["final"]), ("ambiguous", text))  # The complete original text.
        matched = match(self.inbox, self.OWNER_ID, OWNER_TURN, first)
        self.assertEqual(match(self.inbox, self.OWNER_ID, OWNER_TURN, first), matched)
        self.assertIn(text, self.children()["tg:43:1"])
        self.assertEqual((self.state(first), self.state(second)), ("completed", "accepted"))
        with self.assertRaises(BridgeError):
            finish(self.inbox, first, self.OWNER_ID, "Another answer")
        finish(self.inbox, second, self.OWNER_ID, "Shipping page renamed.")
        self.assertNotIn("Verified detail", self.children()["tg:43:2"])
        self.assertNotIn("turn:" + self.OWNER_ID + ":" + OWNER_TURN, capsule(self.inbox.db, self.root, self.workspace)["items"])
        # Chief's answer to one matched result closes only that request.
        self.daemon.active = None
        await self.adapter.dispatch_pending()
        child = matched["request_id"]
        self.assertTrue(self.reply(child, "Checkout and shipping are done.", self.daemon.active, "answer-1")[0])
        self.assertEqual((self.status("tg:43:1"), self.status("tg:43:2")), ("done", "processing"))

    async def test_restart_and_late_acknowledgments_never_reuse_a_settled_final(self):
        first, second = await self.two_routes()
        client = self.inbox.db.execute("SELECT client_id FROM appserver_project_routes WHERE route_id=?", (second,)).fetchone()[0]
        with self.inbox.db:  # The collector stopped before the second acknowledgment was saved.
            self.inbox.db.execute("UPDATE appserver_project_routes SET state='sending',turn_id=NULL WHERE route_id=?", (second,))

        class Restart(Daemon):
            async def __call__(self, method, params):
                if params.get("threadId") == PROJECT_OWNER and method == "thread/turns/list":
                    return {"data": [{"id": OWNER_TURN, "status": "completed"}]}
                if params.get("threadId") == PROJECT_OWNER and method == "thread/items/list":
                    return {"data": [{"turnId": OWNER_TURN, "item": {"type": "userMessage", "clientId": client, "content": []}},
                                     {"turnId": OWNER_TURN, "item": {"type": "agentMessage", "phase": "final_answer",
                                                                      "text": "Both are done."}}]}
                return await super().__call__(method, params)
        self.adapter.router.rpc = Restart()
        await self.adapter.router.reconcile()
        await self.drain()
        self.assertEqual((self.state(first), self.state(second), self.children()), ("accepted", "accepted", {}))
        self.assertEqual(len(self.notices()), 1)
        await self.adapter.router.reconcile()
        self.assertEqual(len(self.notices()), 1)

        # The socket reader records an owner acknowledgment before that turn's completion is read.
        self.fresh()
        self.adapter.router.rpc = self.daemon
        first, second = await self.two_routes(11)
        client = self.inbox.db.execute("SELECT client_id FROM appserver_project_routes WHERE route_id=?", (second,)).fetchone()[0]
        with self.inbox.db:
            self.inbox.db.execute("UPDATE appserver_project_routes SET state='sending',turn_id=NULL WHERE route_id=?", (second,))
        future = asyncio.get_running_loop().create_future()
        self.adapter.pending["ack"] = future
        self.adapter.rpc_context["ack"] = ("turn/start", {"threadId": PROJECT_OWNER, "clientUserMessageId": client})
        await self.native({"id": "ack", "result": {"turn": {"id": OWNER_TURN}}}, *final("Checkout fixed."))
        self.assertEqual((self.state(first), self.state(second), self.children()), ("accepted", "accepted", {}))

        # An acknowledgment that arrives after its turn settled for another route stays open for its own result.
        self.fresh()
        first, = await self.two_routes(21, second_route=False)
        await self.native(*final("Checkout fixed."))
        await self.route_second(21)
        await self.drain()
        second = self.route_id("tg:43:22")
        self.assertEqual(self.state(second), "accepted")
        self.assertEqual(list(self.children()), ["tg:43:21"])
        self.assertIn("attributed to route " + first, self.notices()[0])
        finish(self.inbox, second, self.OWNER_ID, "Shipping page renamed.")
        self.assertNotIn("Checkout", self.children()["tg:43:22"])

    async def test_failed_shared_turn_keeps_each_assignment_open(self):
        first, second = await self.two_routes()
        await self.native(*final("Partial checkout notes", status="failed"))
        self.assertEqual((self.state(first), self.state(second)), ("accepted", "accepted"))
        self.assertEqual(self.children(), {})
        self.assertEqual(len(self.notices()), 1)
        self.assertNotIn("--route-id", self.notices()[0])
        with self.assertRaisesRegex(BridgeError, "without an individual answer"):
            match(self.inbox, self.OWNER_ID, OWNER_TURN, first)


class ResourceHandoff(Routes):
    async def test_only_an_explicit_marker_gets_the_handoff_command(self):
        self.inbox.ingest(message(1, text="Release the shared browser"))
        self.inbox.ingest(message(2, text="Release the shared browser"))
        await self.adapter.dispatch_pending()
        turn = self.daemon.active
        self.assertIn("'RESOURCE_HANDOFF' on its own first line", self.dispatched()[0])
        # Task prose that mentions releasing a resource is ordinary work.
        self.assertEqual(await self.route("tg:43:1", turn, "plain", brief="Release the shared browser when you are done"), "accepted")
        plain = self.route_id("tg:43:1")
        text = self.owner_input(plain)
        envelope = json.loads(text.split("\n", 1)[1])
        self.assertNotIn(" handoff --route-id", text)
        self.assertNotIn("resource_handoff", envelope)
        self.assertEqual(envelope["brief"], "Release the shared browser when you are done")
        self.assertEqual(envelope["source"]["authority"], "installation_owner")
        self.assertIn("sessionName", envelope["browser"])
        with self.assertRaisesRegex(BridgeError, "did not mark"):
            handoff(self.inbox, plain, self.OWNER_ID, "Browser released")
        self.assertEqual(await self.route("tg:43:2", turn, "bad", brief="RESOURCE_HANDOFF: release it", owner=SECOND_OWNER),
                         "rejected_resource_handoff")
        self.assertEqual(await self.route("tg:43:2", turn, "marked", brief="RESOURCE_HANDOFF\nRelease the shared browser",
                                          owner=SECOND_OWNER), "accepted")
        marked = self.route_id("tg:43:2")
        text = self.owner_input(marked)
        self.assertIn(" handoff --route-id " + marked, text)
        self.assertTrue(json.loads(text.split("\n", 1)[1])["resource_handoff"])
        handoff(self.inbox, marked, "codex:" + SECOND_OWNER, "Browser released")
        self.assertEqual(self.state(marked), "completed")
        self.assertIn("Browser released", self.children()["tg:43:2"])
        # A route saved before the marker existed keeps the command its input already offered.
        with self.inbox.db:
            self.inbox.db.execute("UPDATE appserver_project_routes SET resource_handoff=NULL WHERE route_id=?", (plain,))
        handoff(self.inbox, plain, self.OWNER_ID, "Browser released")
        self.assertEqual(self.state(plain), "completed")


def run(workspace, *args, **kwargs):
    return subprocess.run(["git", "-C", str(workspace), *args], check=True, capture_output=True, text=True, **kwargs).stdout.strip()


class Checkpoint(Workspace):
    def setUp(self):
        super().setUp()
        run(self.workspace, "init", "-q", "-b", "main")
        run(self.workspace, "config", "user.name", "Example Operator")
        run(self.workspace, "config", "user.email", "operator@example.invalid")
        (self.workspace / ".gitignore").write_text(".private/\n")
        (self.workspace / "tasks/doing/checkout.md").write_text("# Checkout\n\nProject: Shop\n")
        (self.workspace / "notes.md").write_text("Shared notes\n")
        run(self.workspace, "add", "-A")
        run(self.workspace, "commit", "-q", "-m", "Baseline")
        self.sent("tg:43:1")

    def sent(self, request_id):
        update = int(request_id.rsplit(":", 1)[1])
        self.inbox.ingest(message(update))
        with self.inbox.db:
            self.inbox.reply("result:" + request_id, OWNER, "Done")
            self.inbox.db.execute("UPDATE outbox SET status='sent',telegram_id=? WHERE id=?", (900 + update, "result:" + request_id))
            self.inbox.db.execute("UPDATE requests SET status='done' WHERE id=?", (request_id,))

    def test_receipt_only_and_pending_delivery_do_not_run_export(self):
        marker = self.root / "unexpected-export"
        config = self.root / "checkpoint.json"
        config.write_text(json.dumps({"export": [[sys.executable, "-c", "from pathlib import Path; Path(" +
                                                  repr(str(marker)) + ").touch()"]]}))
        config.chmod(0o600)
        receipt = prepare(self.inbox, ["tg:43:1"], [], workspace=self.workspace)
        self.assertEqual(receipt["kind"], "delivery_receipt")
        self.assertEqual(receipt["hooks"], [])
        with self.inbox.db:
            self.inbox.db.execute("UPDATE outbox SET status='uncertain' WHERE id='result:tg:43:1'")
        held = prepare(self.inbox, ["tg:43:1"], ["tasks"], workspace=self.workspace)
        self.assertEqual(held["state"], "blocked")
        self.assertFalse(marker.exists())

    def move_card(self):
        (self.workspace / "tasks/doing/checkout.md").rename(self.workspace / "tasks/done/checkout.md")

    def test_delivery_only_batch_needs_its_durable_receipt(self):
        ready = prepare(self.inbox, ["tg:43:1"], [], workspace=self.workspace)
        self.assertEqual((ready["kind"], ready["state"]), ("delivery_receipt", "prepared"))
        self.assertEqual(ready["coverage_gaps"], [])
        self.assertEqual(confirm(self.inbox, ready["digest"], workspace=self.workspace)["state"], "receipted")
        self.inbox.ingest(message(2))
        with self.inbox.db:
            self.inbox.reply("result:tg:43:2", OWNER, "Maybe")
            self.inbox.db.execute("UPDATE outbox SET status='uncertain' WHERE id='result:tg:43:2'")
        blocked = prepare(self.inbox, ["tg:43:2"], [], workspace=self.workspace)
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["findings"], [{"problem": "delivery is not durable: uncertain", "request_id": "tg:43:2"}])

    def test_reviewed_diff_commits_only_declared_paths_and_keeps_others_staged_work(self):
        (self.workspace / "notes.md").write_text("Someone else's staged edit\n")
        run(self.workspace, "add", "notes.md")
        (self.workspace / "scratch.txt").write_text("unrelated\n")
        self.move_card()
        ready = prepare(self.inbox, ["tg:43:1"], ["tasks"], workspace=self.workspace)
        self.assertEqual(ready["state"], "prepared", ready["findings"])
        diff = Path(ready["diff_file"]).read_text()
        self.assertIn("tasks/done/checkout.md", diff)
        self.assertNotIn("notes.md", diff)
        self.assertEqual(sorted(path for _, path in ready["changes"]), ["tasks/doing/checkout.md", "tasks/done/checkout.md"])
        done = confirm(self.inbox, ready["digest"], workspace=self.workspace)
        self.assertEqual(done["state"], "committed")
        self.assertEqual(run(self.workspace, "rev-parse", "HEAD"), done["commit"])
        self.assertEqual(run(self.workspace, "show", "--name-only", "--no-renames", "--format=", "HEAD").splitlines(),
                         ["tasks/doing/checkout.md", "tasks/done/checkout.md"])
        self.assertEqual(run(self.workspace, "status", "--porcelain"), "M  notes.md\n?? scratch.txt".strip())
        # Resume is idempotent; reconciling an interrupted ref update adopts the same commit.
        self.assertEqual(confirm(self.inbox, ready["digest"], workspace=self.workspace)["commit"], done["commit"])
        with self.inbox.db:
            self.inbox.db.execute("UPDATE checkpoints SET state='committing' WHERE digest=?", (ready["digest"],))
        self.assertEqual(confirm(self.inbox, ready["digest"], workspace=self.workspace)["state"], "committed")
        self.assertEqual(run(self.workspace, "rev-list", "--count", "HEAD"), "2")

    def test_changed_bytes_secrets_hook_failures_and_staged_conflicts_surface(self):
        self.move_card()
        ready = prepare(self.inbox, ["tg:43:1"], ["tasks"], workspace=self.workspace)
        (self.workspace / "tasks/done/checkout.md").write_text("# Checkout\n\nChanged after review\n")
        changed = confirm(self.inbox, ready["digest"], workspace=self.workspace)
        self.assertEqual(changed["findings"], [{"problem": "declared files changed since review; prepare again"}])
        self.assertEqual(run(self.workspace, "rev-list", "--count", "HEAD"), "1")
        (self.workspace / "tasks/done/checkout.md").write_text("# Checkout\n\npassword: hunter2-secret-value\n")
        secret = prepare(self.inbox, ["tg:43:1"], ["tasks"], workspace=self.workspace)
        self.assertEqual(secret["state"], "blocked")
        self.assertEqual(secret["findings"][0]["patterns"], ["credential_assignment"])
        self.assertNotIn("hunter2", json.dumps(secret))
        (self.workspace / "tasks/done/checkout.md").write_text("# Checkout\n\nDone\n")
        hooks = tempfile.TemporaryDirectory()
        self.addCleanup(hooks.cleanup)
        failing = Path(hooks.name) / "verify.sh"
        failing.write_text("#!/bin/sh\nseq 1 500\nsleep 1\necho 'export check failed: 2 cards unreadable' >&2\nexit 2\n")
        failing.chmod(0o700)
        config = self.root / "checkpoint.json"
        config.write_text(json.dumps({"verify": [[str(failing)]], "known_gaps": ["history export covers only text"]}))
        config.chmod(0o600)
        failed = prepare(self.inbox, ["tg:43:1"], ["tasks"], workspace=self.workspace)
        self.assertEqual(failed["state"], "blocked")
        self.assertIn({"problem": "verify command failed", "command": "verify.sh"}, failed["findings"])
        self.assertIn({"gap": "history export covers only text", "source": "known_gaps"}, failed["coverage_gaps"])
        hook = next(hook for hook in failed["hooks"] if hook["kind"] == "verify")
        self.assertIn("2 cards unreadable", hook["output_tail"])
        evidence = Path(hook["evidence_file"])
        self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
        self.assertTrue(evidence.is_relative_to(self.root))
        self.assertIn(b"\n1\n2\n", evidence.read_bytes())  # Complete output, not just the excerpt.
        self.assertIn(b"500\n", evidence.read_bytes())
        with self.assertRaisesRegex(BridgeError, "findings"):
            confirm(self.inbox, failed["digest"], workspace=self.workspace)
        config.unlink()
        run(self.workspace, "add", "tasks/done/checkout.md")
        (self.workspace / "tasks/done/checkout.md").write_text("# Checkout\n\nDone and reviewed\n")
        staged = prepare(self.inbox, ["tg:43:1"], ["tasks"], workspace=self.workspace)
        self.assertIn({"problem": "another staged version exists in the operator's index", "path": "tasks/done/checkout.md"},
                      staged["findings"])

    def remote(self, push=True):
        remote = tempfile.TemporaryDirectory()
        self.addCleanup(remote.cleanup)
        self.bare = Path(remote.name) / "private.git"
        self.other = Path(remote.name) / "other.git"
        for path in (self.bare, self.other):
            subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
        run(self.workspace, "remote", "add", "origin", str(self.bare))
        run(self.workspace, "push", "-q", "origin", "main")
        config = self.root / "checkpoint.json"
        config.write_text(json.dumps({"remote": "origin", "fetch_url": str(self.bare), "push_url": str(self.bare), "push": push}))
        config.chmod(0o600)
        return Path(remote.name)

    def tip(self, bare):
        return subprocess.run(["git", "-C", str(bare), "rev-parse", "--verify", "-q", "main"],
                              capture_output=True, text=True).stdout.strip()

    def test_push_goes_only_to_the_pinned_target_and_resumes(self):
        base = self.remote()
        self.move_card()
        ready = prepare(self.inbox, ["tg:43:1"], ["tasks"], workspace=self.workspace)
        self.assertEqual((ready["push"], ready["push_url"]), (True, str(self.bare)))
        pushed = confirm(self.inbox, ready["digest"], workspace=self.workspace)
        self.assertEqual(pushed["state"], "pushed")
        self.assertEqual(self.tip(self.bare), pushed["commit"])
        with self.inbox.db:
            self.inbox.db.execute("UPDATE checkpoints SET state='pushing' WHERE digest=?", (ready["digest"],))
        self.assertEqual(confirm(self.inbox, ready["digest"], workspace=self.workspace)["state"], "pushed")
        # Someone else pushes first: the next checkpoint reports it instead of overwriting.
        clone = base / "clone"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(clone)], check=True)
        run(clone, "-c", "user.name=Other", "-c", "user.email=other@example.invalid", "commit", "-q", "--allow-empty", "-m", "Other")
        run(clone, "push", "-q", "origin", "main")
        (self.workspace / "tasks/done/checkout.md").write_text("# Checkout\n\nVerified\n")
        diverged = prepare(self.inbox, ["tg:43:1"], ["tasks"], workspace=self.workspace)
        self.assertIn({"problem": "the remote branch is not at the reviewed HEAD; reconcile it before checkpointing"},
                      diverged["findings"])

    def test_changed_remote_target_or_unreviewed_history_is_never_pushed(self):
        self.remote()
        self.move_card()
        for change, problem in ((("remote", "set-url", "--push", "origin", "OTHER"), "push URL"),
                                (("remote", "set-url", "origin", "OTHER"), "fetch URL"),
                                (("config", "url.OTHER.insteadOf", "BARE"), "fetch URL")):
            with self.subTest(change=change):
                ready = prepare(self.inbox, ["tg:43:1"], ["tasks"], workspace=self.workspace)
                self.assertEqual(ready["state"], "prepared", ready["findings"])
                run(self.workspace, *[part.replace("OTHER", str(self.other)).replace("BARE", str(self.bare)) for part in change])
                result = confirm(self.inbox, ready["digest"], workspace=self.workspace)
                self.assertIn(problem, result["findings"][0]["problem"])
                self.assertEqual(run(self.workspace, "rev-list", "--count", "HEAD"), "1")
                again = prepare(self.inbox, ["tg:43:1"], ["tasks"], workspace=self.workspace)
                self.assertIn(problem, again["findings"][0]["problem"])
                subprocess.run(["git", "-C", str(self.workspace), "config", "--unset-all",
                                "url." + str(self.other) + ".insteadOf"], capture_output=True)
                run(self.workspace, "remote", "set-url", "origin", str(self.bare))
                run(self.workspace, "remote", "set-url", "--push", "origin", str(self.bare))
        self.assertEqual(self.tip(self.other), "")
        # Crash recovery rechecks the target before pushing the saved commit.
        ready = prepare(self.inbox, ["tg:43:1"], ["tasks"], workspace=self.workspace)
        self.assertEqual(ready["state"], "prepared", ready["findings"])
        with patch("alfred_checkpoint.remote_tip", side_effect=[ready["remote_tip"], RuntimeError("crash before push")]):
            with self.assertRaises(RuntimeError):
                confirm(self.inbox, ready["digest"], workspace=self.workspace)
        self.assertEqual(self.inbox.db.execute("SELECT state FROM checkpoints WHERE digest=?", (ready["digest"],)).fetchone()[0],
                         "committed")
        run(self.workspace, "remote", "set-url", "--push", "origin", str(self.other))
        stopped = confirm(self.inbox, ready["digest"], workspace=self.workspace)
        self.assertIn("push URL", stopped["findings"][0]["problem"])
        self.assertEqual(self.tip(self.other), "")
        run(self.workspace, "remote", "set-url", "--push", "origin", str(self.bare))
        self.assertEqual(confirm(self.inbox, ready["digest"], workspace=self.workspace)["state"], "pushed")
        # A local commit that was never reviewed or pushed blocks the next pushed checkpoint.
        run(self.workspace, "commit", "-q", "--allow-empty", "-m", "Unreviewed local work")
        (self.workspace / "tasks/done/checkout.md").write_text("# Checkout\n\nAgain\n")
        local = prepare(self.inbox, ["tg:43:1"], ["tasks"], workspace=self.workspace)
        self.assertIn({"problem": "the remote branch is not at the reviewed HEAD; reconcile it before checkpointing"}, local["findings"])
        # A configured remote without its expected URLs is refused outright.
        (self.root / "checkpoint.json").write_text(json.dumps({"remote": "origin", "push": True}))
        with self.assertRaisesRegex(BridgeError, "Invalid checkpoint.json"):
            prepare(self.inbox, ["tg:43:1"], ["tasks"], workspace=self.workspace)


class SyntheticReplay(unittest.TestCase):
    def test_synthetic_replay_counts_one_fewer_coordinator_turn_only_on_the_qualifying_path(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import replay_direct_return
        results = {item["path"]: item for item in replay_direct_return.run()["results"]}
        self.assertEqual((results["chief_relay"]["coordinator_turns"], results["chief_relay"]["dispatched_turns"]), (2, 3))
        self.assertEqual((results["direct_return"]["coordinator_turns"], results["direct_return"]["dispatched_turns"]), (1, 2))
        for path, item in results.items():
            self.assertEqual((item["request_state"], item["answers_queued_to_sender"]), ("done", 1), path)
            if path.startswith("direct_return/"):
                self.assertEqual(item["coordinator_turns"], 2, path)


if __name__ == "__main__":
    unittest.main()
