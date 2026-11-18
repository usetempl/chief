#!/usr/bin/env python3
"""Optional synthetic replay of one comparable assignment: Chief relay versus direct owner return.

Runs the real adapter, router, staging command, native completion path and
outbox against a fake app-server in a throwaway workspace with generic identities. It counts the
turns each path dispatches (turn/start and turn/steer per thread). These are
observed dispatches at that boundary, not model API calls, tokens or time.
No network, Telegram, Codex or private state is used.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

COORDINATOR = "00000000-0000-4000-8000-0000000000c0"
OWNER_THREAD = "00000000-0000-4000-8000-0000000000a1"
SECOND_THREAD = "00000000-0000-4000-8000-0000000000a2"
OWNER = 43
CARD = "tasks/done/checkout-fix.md"
ANSWER = "The checkout bug is fixed and verified on the release build."


def environment():
    if "COS_WORKSPACE" not in os.environ:
        workspace = Path(tempfile.mkdtemp())
        (workspace / ".private").mkdir(mode=0o700)
        profile = workspace / ".private/profile.json"
        profile.write_text(json.dumps({"bot_username": "ExampleChiefBot", "owner_label": "Owner"}))
        profile.chmod(0o600)
        os.environ["COS_WORKSPACE"] = str(workspace)
    code = Path(__file__).resolve().parents[1] / "runtime"
    if str(code) not in sys.path:
        sys.path.insert(0, str(code))


class Daemon:
    def __init__(self):
        self.active, self.turns = None, {}

    async def __call__(self, method, params):
        thread = params.get("threadId")
        if method == "thread/turns/list":
            return {"data": [{"id": self.active, "status": "inProgress"}] if self.active and thread == COORDINATOR else []}
        if method == "thread/resume":
            return {"thread": {"id": thread, "canAcceptDirectInput": True}}
        if method in {"turn/start", "turn/steer"}:
            self.turns[thread] = self.turns.get(thread, 0) + 1
            turn = thread[-2:] + "-turn-" + str(self.turns[thread])
            if thread == COORDINATOR:
                self.active = turn
            return {"turn": {"id": turn}}
        if method == "model/list":
            return {"data": []}
        raise AssertionError("Unexpected RPC " + method)


class Scenario:
    def __init__(self, base):
        import alfred_projects
        from alfred_appserver import Adapter
        from operator_policy import OWNER_LABEL
        from telegram_bridge import Inbox
        self.base = Path(base)
        self.root, self.workspace = self.base / "private", self.base / "workspace"
        self.root.mkdir(mode=0o700)
        for state in ("doing", "done"):
            (self.workspace / "tasks" / state).mkdir(parents=True)
        (self.workspace / "priorities.md").write_text("# Priorities\n\nShip the checkout fix.\n")
        self.index = self.workspace / "projects.md"
        self.index.write_text("| Priority | Project | GitHub repository | Local checkout | Existing lead |\n"
                              f"| P2 | Shop | private | `/shop` | `codex:{OWNER_THREAD}` |\n"
                              f"| P3 | Docs | private | `/docs` | `codex:{SECOND_THREAD}` |\n")
        self.original = alfred_projects.registered_owners
        alfred_projects.registered_owners = lambda path=None: self.original(self.index)
        self.inbox = Inbox(self.root)
        with self.inbox.db:
            self.inbox.db.execute("INSERT INTO users VALUES (?,?,?,1)", (OWNER, OWNER, OWNER_LABEL))
        self.adapter = Adapter(self.inbox, {"thread_id": COORDINATOR, "socket_path": "/unused"})
        self.adapter.workspace = self.adapter.router.workspace = self.workspace
        self.daemon = Daemon()
        self.adapter.rpc = self.adapter.router.rpc = self.daemon
        self.adapter.router.projects_path = self.index
        self.relays = 0

    def close(self):
        import alfred_projects
        alfred_projects.registered_owners = self.original
        self.inbox.db.close()

    async def intake(self, update_id=1, text="Please fix the checkout bug"):
        import time
        self.inbox.ingest({"update_id": update_id, "message": {
            "message_id": update_id, "date": int(time.time()) + 1, "from": {"id": OWNER, "is_bot": False},
            "chat": {"id": OWNER, "type": "private"}, "text": text}})
        await self.adapter.dispatch_pending()
        return "tg:43:" + str(update_id), self.daemon.active

    async def route(self, request_id, turn, brief, owner=OWNER_THREAD, call="route-1"):
        from alfred_projects import TOOL_NAME
        result = await self.adapter.router.handle({
            "threadId": COORDINATOR, "turnId": turn, "callId": call, "tool": TOOL_NAME,
            "arguments": {"request_id": request_id, "owner_id": "codex:" + owner, "brief": brief}})
        return json.loads(result["contentItems"][0]["text"])

    def card(self, route_id, column="done"):
        (self.workspace / "tasks" / column / Path(CARD).name).write_text(
            "# Checkout fix\n\nProject: Shop\nRoute: " + route_id + "\n\n## Movement history\n")

    async def owner_finishes(self, owner=OWNER_THREAD, text=ANSWER):
        self.daemon.active = None
        self.adapter.router.notification({"method": "turn/completed", "params": {"threadId": owner, "turn": {
            "id": owner[-2:] + "-turn-1", "status": "completed",
            "items": [{"type": "agentMessage", "phase": "final_answer", "text": text}]}}})
        while self.adapter.router.tasks:
            await asyncio.gather(*list(self.adapter.router.tasks))

    async def chief_relays(self):
        """Chief answers each pending owner outcome once: one more coordinator turn per relay."""
        child = self.inbox.db.execute("SELECT r.id FROM requests r JOIN request_context c ON c.request_id=r.id "
                                      "WHERE c.source='project_outcome' AND r.status='pending'").fetchone()
        if child is None:
            return None
        await self.adapter.dispatch_pending()
        self.relays += 1
        result = self.adapter.tool_reply({"threadId": COORDINATOR, "turnId": self.daemon.active, "callId": "relay-" + child["id"],
                                          "tool": "alfred_telegram_reply", "arguments": {"request_id": child["id"], "body": ANSWER}})
        return result["success"]

    def measure(self, path, request_id, outcome):
        answers = self.inbox.db.execute("SELECT COUNT(*) FROM outbox WHERE chat_id=? AND id LIKE 'result:%' "
                                        "AND id NOT LIKE '%:fault:%'", (OWNER,)).fetchone()[0]
        return {"path": path, "outcome": outcome, "request_state": self.inbox.db.execute(
                    "SELECT status FROM requests WHERE id=?", (request_id,)).fetchone()[0],
                "coordinator_turns": self.daemon.turns.get(COORDINATOR, 0),
                "owner_turns": sum(count for thread, count in self.daemon.turns.items() if thread != COORDINATOR),
                "dispatched_turns": sum(self.daemon.turns.values()), "chief_relays": self.relays,
                "answers_queued_to_sender": answers}


async def chief_relay(base):
    s = Scenario(base)
    try:
        request_id, turn = await s.intake()
        await s.route(request_id, turn, "Fix the checkout bug and return the verified result")
        await s.owner_finishes()
        await s.chief_relays()
        return s.measure("chief_relay", request_id, "answered_by_chief")
    finally:
        s.close()


async def direct(base, *, break_with=None):
    """Chief elects; the owner stages and ends its turn; only that native final can send."""
    from alfred_local import stage_return
    s = Scenario(base)
    try:
        request_id, turn = await s.intake()
        route = await s.route(request_id, turn, "DIRECT_RETURN card=" + CARD + "\nFix the checkout bug and return the verified result")
        route_id = route["route_id"]
        if break_with != "missing_card":
            s.card(route_id)
        if break_with == "ambiguous_card":
            s.card(route_id, "doing")
        if break_with == "terminal_first":
            await s.owner_finishes()
        staged = "not staged"
        try:
            staged = stage_return(s.inbox, route_id, "codex:" + OWNER_THREAD, ANSWER, CARD, workspace=s.workspace)["status"]
        except Exception as error:  # A refused stage is part of the measured outcome.
            staged = "staging refused: " + str(error)
        if break_with == "second_owner":
            await s.route(request_id, turn, "Update the docs for the checkout fix", owner=SECOND_THREAD, call="route-2")
        if break_with == "priorities_changed":
            (s.workspace / "priorities.md").write_text("# Priorities\n\nHold all releases.\n")
        if break_with != "terminal_first":
            await s.owner_finishes(text=ANSWER + " Also deployed." if break_with == "final_differs" else ANSWER)
        sent = s.inbox.db.execute("SELECT 1 FROM appserver_direct_returns").fetchone() is not None
        reason = s.inbox.db.execute("SELECT r.body FROM requests r JOIN request_context c ON c.request_id=r.id "
                                    "WHERE c.source='project_outcome'").fetchone()
        await s.chief_relays()
        outcome = "returned_directly" if sent else staged + "; " + (
            reason[0].split("Direct return not used: ", 1)[-1].split("\n", 1)[0] if reason else "no result")
        return s.measure("direct_return" + ("/" + break_with if break_with else ""), request_id, outcome)
    finally:
        s.close()


def run():
    environment()
    results = []
    cases = [("chief_relay", chief_relay, {}), ("direct", direct, {})] + [
        (case, direct, {"break_with": case}) for case in
        ("missing_card", "ambiguous_card", "second_owner", "priorities_changed", "final_differs", "terminal_first")]
    for _, function, options in cases:
        with tempfile.TemporaryDirectory() as base:
            results.append(asyncio.run(function(base, **options)))
    return {"measurement": "dispatched turns at a synthetic app-server boundary for one comparable assignment; "
                           "not model API calls, tokens, time or weekly savings", "results": results}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
