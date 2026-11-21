"""Exercise the native tools on private Markdown boards, without an installed service."""

import subprocess
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NativeBoard(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "private"
        self.run_tool("onboard.py", "--directory", self.workspace, "--driver", "codex",
                      "--history", "no", "--no-start")

    def run_tool(self, script, *args, check=True):
        return subprocess.run([sys.executable, "-E", "-s", str(ROOT / script), *map(str, args)],
                              cwd=self.workspace.parent, capture_output=True, text=True, check=check)

    def report(self, *args):
        return self.run_tool("runtime/alfred_report.py", "--root", self.workspace, *args).stdout

    def card(self, state, name, body=""):
        path = self.workspace / "tasks" / state / (name + ".md")
        path.write_text(f"# {name}\n\n{body}\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_native_move_and_report_on_a_legacy_card(self):
        card = self.card("todo", "Docs", "Project: Docs\nNext: Proofread the guide.")
        self.assertIn("1. Docs", self.report("report"))
        self.run_tool("runtime/alfred_board.py", "move", card, "doing", "--reason", "Draft ready")
        moved = self.workspace / "tasks" / "doing" / card.name
        self.assertIn("| todo | doing | Draft ready |", moved.read_text())
        self.assertFalse(card.exists())
        output = self.report()
        self.assertIn("DOING (1)", output)
        self.assertEqual(output.count("Proofread the guide."), 1)
        self.assertIn("Docs", self.run_tool("snapshot.py", "--root", self.workspace).stdout)

        blocked = self.run_tool("runtime/alfred_board.py", "move", moved, "blocked",
                                "--reason", "Input needed", check=False)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("Resolver", blocked.stderr)
        self.assertTrue(moved.exists())

    def test_done_handles_pagination_and_recurring_completions(self):
        for i in range(1, 22):
            self.card("done", f"Item {i:02}", f"Updated: 2026-01-{i:02}T18:00:00+00:00")
        due = "2026-01-22T09:00:00+00:00"
        card = self.card("doing", "Weekly", "Recurrence: every 7 days\nSchedule driver: chief\n"
                         f"Authority: Weekly review approved\nSchedule owner: chief\nNext run: {due}")
        command = ("runtime/alfred_tasks.py", "complete", card, "--expected-run", due, "--outcome", "Reviewed")
        self.run_tool(*command)
        saved = self.workspace / "tasks" / "todo" / card.name
        before = saved.read_bytes()
        self.run_tool(*command)
        self.assertEqual(saved.read_bytes(), before)
        self.assertIn("1. Weekly", self.report("done"))
        second = self.report("done", "2")
        self.assertIn("21. Item 02", second)
        self.assertIn("22. Item 01", second)
        self.assertIn("Done page unavailable", self.report("done", "3"))

    def test_missing_board_cannot_look_like_an_empty_one(self):
        result = self.run_tool("runtime/alfred_report.py", "--root", self.workspace.parent, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tasks directory", result.stderr)

    def test_numbered_open_and_done_items_keep_the_plain_card_reference_across_moves(self):
        card = self.card("backlog", "proof card", "Report name: A task\nReviewer: Fixture reviewer\nNext: Check it.")
        self.card("todo", "other", "Report name: Another task")
        def item(report, filename):
            return next(line for line in report.splitlines() if line.endswith(" · " + filename))
        self.assertTrue(item(self.report(), card.name).startswith("1. A task"))
        self.run_tool("runtime/alfred_board.py", "move", card, "review", "--reason", "Ready for review")
        self.assertTrue(item(self.report(), card.name).startswith("2. A task"))
        moved = self.workspace / "tasks" / "review" / card.name
        self.run_tool("runtime/alfred_board.py", "move", moved, "done", "--reason", "Accepted")
        self.assertTrue(item(self.report("done"), card.name).startswith("1. A task"))

    def recurring(self, state, due):
        return self.card(state, "Schedule", "Recurrence: every 7 days\nSchedule driver: chief\n"
                         f"Authority: Weekly label check\nSchedule owner: chief\nNext run: {due}")

    def test_utc_z_occurrence_and_its_saved_receipt_work_on_system_python(self):
        due = "2001-05-01T09:00:00Z"
        card = self.recurring("doing", due)
        args = ("runtime/alfred_tasks.py", "complete", card, "--expected-run", due, "--outcome", "Verified")
        self.run_tool(*args)
        saved = self.workspace / "tasks" / "todo" / card.name
        before = saved.read_bytes()
        self.assertIn("Last run: " + due, saved.read_text())
        self.run_tool(*args)
        self.assertEqual(saved.read_bytes(), before)

    def test_only_an_explicit_early_occurrence_consumes_its_slot_and_preserves_cadence(self):
        due = "2099-06-01T12:30:00Z"
        card = self.recurring("doing", due)
        args = ("runtime/alfred_tasks.py", "complete", card, "--expected-run", due, "--outcome", "Read label")
        before = card.read_bytes()
        denied = self.run_tool(*args, check=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("--early-authority REQUEST-REF", denied.stderr)
        self.assertEqual(card.read_bytes(), before)
        self.run_tool(*args, "--early-authority", "operator-request-2")
        saved = self.workspace / "tasks" / "todo" / card.name
        receipt = saved.read_text()
        self.assertIn("Last run: " + due, receipt)
        self.assertIn("Next run: 2099-06-08T12:30:00+00:00", receipt)
        self.assertIn("Recurrence: every 7 days", receipt)
        self.assertIn("Last completion authority: operator-request-2", receipt)
        self.run_tool(*args, "--early-authority", "operator-request-2")
        self.assertEqual(saved.read_text(), receipt)

    def test_due_todo_explains_the_move_without_changing_the_scheduled_occurrence(self):
        due = "2001-05-01T09:00:00+00:00"
        card = self.recurring("todo", due)
        before = card.read_bytes()
        args = ("--expected-run", due, "--outcome", "Verified")
        refused = self.run_tool("runtime/alfred_tasks.py", "complete", card, *args, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(len(refused.stderr.splitlines()), 1)
        self.assertIn("alfred_board.py move CARD doing", refused.stderr)
        self.assertEqual(card.read_bytes(), before)
        self.run_tool("runtime/alfred_board.py", "move", card, "doing", "--reason", "Authorized run started")
        active = self.workspace / "tasks" / "doing" / card.name
        self.assertIn("Next run: " + due, active.read_text())
        self.run_tool("runtime/alfred_tasks.py", "complete", active, *args)
        self.assertIn("Last run: " + due, card.read_text())

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX card permissions")
    def test_shared_writable_card_is_preserved_and_the_owner_gets_an_actionable_hint(self):
        card = self.card("todo", "shared")
        card.chmod(0o664)
        before = card.read_bytes()
        args = ("runtime/alfred_board.py", "move", card, "doing", "--reason", "Start")
        refused = self.run_tool(*args, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(len(refused.stderr.splitlines()), 1)
        self.assertIn("chmod go-w CARD", refused.stderr)
        self.assertEqual(card.stat().st_mode & 0o777, 0o664)
        self.assertEqual(card.read_bytes(), before)
        card.chmod(0o644)
        self.run_tool(*args)
        self.assertTrue((self.workspace / "tasks" / "doing" / card.name).exists())
