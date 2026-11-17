"""Owner request lifecycles that must not hit an unnecessary gate, plus the boundaries that remain."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
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
from alfred_local import resolve
from alfred_projects import TOOL_NAME as ROUTE_TOOL
from alfred_report import report
from alfred_tasks import due_tasks
from operator_policy import CONTACT_LABEL, OWNER_LABEL
from telegram_bridge import BridgeError, Inbox, MAX_ATTACHMENT, MAX_ATTACHMENT_STORAGE

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
            return {"turn": {"id": "owner-turn"}}
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

    async def route(self, request_id, turn, call, brief="Fix the checkout bug and return the verified result"):
        result = await self.adapter.router.handle({
            "threadId": COORDINATOR, "turnId": turn, "callId": call, "tool": ROUTE_TOOL,
            "arguments": {"request_id": request_id, "owner_id": "codex:" + PROJECT_OWNER, "brief": brief}})
        return json.loads(result["contentItems"][0]["text"])["status"]

    async def drain(self):
        while self.adapter.router.tasks:
            await asyncio.gather(*list(self.adapter.router.tasks))


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


class OwnerIntake(Base):
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
        secret = "hunter2-" + "x" * 6
        self.inbox.ingest(message(1, text="The login page breaks after reset.\npassword: " + secret))
        self.inbox.ingest(message(2, text="password: " + secret))
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
