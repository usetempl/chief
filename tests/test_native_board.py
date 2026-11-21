"""Exercise the native tools on private Markdown boards, without an installed service."""

import subprocess
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
