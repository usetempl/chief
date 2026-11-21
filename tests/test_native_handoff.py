"""Exercise card recovery through the shipped CLI. Native messages and turns are fixtures."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def run(script, *args, body=None, check=True):
    return subprocess.run([sys.executable, "-B", str(ROOT / script), *map(str, args)],
                          input=body, text=True, capture_output=True, check=check)


class NativeHandoff(unittest.TestCase):
    def setUp(self):
        self.space = tempfile.TemporaryDirectory()
        self.addCleanup(self.space.cleanup)

    def board(self, driver):
        workspace = Path(self.space.name) / driver
        run("onboard.py", "--directory", workspace, "--driver", driver, "--history", "no", "--no-start")
        card = workspace / "tasks" / "doing" / "sample.md"
        card.write_text("# Sample\n\nNext: Read the fixture.\n", encoding="utf-8")
        card.chmod(0o600)
        return card

    def call(self, card, action, key="assignment-a", *, owner="fixture-owner", attempt=None,
             origin=None, receipt=None, body=None, fails=False):
        args = [action, card, key]
        for option, value in (("owner", None if action == "status" else owner),
                              ("attempt", attempt), ("origin", origin), ("receipt", receipt)):
            if value is not None:
                args.extend(["--" + option, str(value)])
        result = run("runtime/alfred_handoff.py", *args, body=body, check=not fails)
        if fails:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Handoff stopped", result.stderr)
            return
        return json.loads(result.stdout)

    def test_refused_native_send_and_one_recovery_across_reopens_and_card_move(self):
        for driver in ("codex", "claude"):
            with self.subTest(driver=driver):
                card = self.board(driver)
                kwargs = {"origin": "original-ask-in-" + driver, "body": "Read the label.\nβ\u2028Blue."}
                first = self.call(card, "offer", **kwargs)
                self.assertTrue(first["send_once"])
                self.assertFalse(self.call(card, "offer", **kwargs)["send_once"])
                self.assertFalse(self.call(card, "retry", attempt=1)["send_once"])
                self.assertEqual(self.call(card, "refused", attempt=1, receipt="writer-refusal")["status"], "refused")
                report = run("runtime/alfred_report.py", "--root", card.parents[2], "report").stdout
                self.assertIn("assignment-a refused · sample.md", report)
                self.assertNotIn(kwargs["body"], report)
                run("runtime/alfred_board.py", "move", card, "review", "--reason", "Review the fixture")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    retries = list(pool.map(lambda _: self.call(card, "retry", attempt=1), range(2)))
                self.assertEqual(sorted(x["send_once"] for x in retries), [False, True])
                self.assertTrue(all(x["attempt"] == 2 for x in retries))
                self.call(card, "accept", attempt=1, receipt="old-turn", fails=True)
                self.call(card, "accept", attempt=2, receipt="owner-turn")
                self.call(card, "return", attempt=2, receipt="another-turn", body=first["return_label"] + "\nBlue.", fails=True)
                self.call(card, "return", attempt=2, receipt="owner-turn", body="Blue.", fails=True)
                answer = first["return_label"] + "\nBlue.\nβ\n"
                self.call(card, "return", attempt=2, receipt="owner-turn", body=answer)
                moved = card.parent.parent / "review" / card.name
                before = moved.read_bytes()
                self.assertEqual(self.call(card, "return", attempt=2, receipt="owner-turn", body=answer)["status"], "returned")
                self.call(card, "return", attempt=2, receipt="owner-turn", body=answer + "Changed", fails=True)
                self.call(card, "offer", origin="another-request", body=kwargs["body"], fails=True)
                self.assertEqual(moved.read_bytes(), before)
                saved = self.call(card, "status")["assignment"]
                self.assertEqual(saved["origin"], kwargs["origin"])
                self.assertEqual(saved["brief"], kwargs["body"])
                self.assertEqual(saved["result"], "Blue.\nβ\n")
                self.assertEqual([x["status"] for x in saved["attempts"]], ["refused", "returned"])

    def test_owner_can_take_refused_card_on_next_visit_and_shared_turn_labels_stay_separate(self):
        card = self.board("claude")
        offers = {}
        for key in ("assignment-a", "assignment-b"):
            offers[key] = self.call(card, "offer", key, origin=key, body="Read " + key)
            self.call(card, "refused", key, attempt=1, receipt="not-accepted")
            self.call(card, "accept", key, owner="other-owner", attempt=1, receipt="shared-turn", fails=True)
            self.call(card, "accept", key, attempt=1, receipt="shared-turn")
            self.assertFalse(self.call(card, "retry", key, attempt=1)["send_once"])
        b = offers["assignment-b"]["return_label"] + "\nReady."
        self.call(card, "return", "assignment-a", attempt=1, receipt="shared-turn", body=b, fails=True)
        self.call(card, "return", "assignment-b", attempt=1, receipt="shared-turn", body=b)
        self.assertEqual(self.call(card, "status", "assignment-a")["status"], "accepted")
        returned = self.call(card, "status", "assignment-b")
        self.assertEqual(returned["status"], "returned")
        evidence = returned["assignment"]["attempts"][0]
        self.assertEqual(evidence["refusal"], "not-accepted")
        self.assertTrue(all(evidence[field] for field in ("refused_at", "accepted_at", "returned_at")))

    def test_competing_first_offers_reserve_one_send_and_conflicting_brief_is_preserved(self):
        card = self.board("codex")
        with ThreadPoolExecutor(max_workers=2) as pool:
            offered = list(pool.map(lambda _: self.call(card, "offer", origin="same-request", body="First"), range(2)))
        self.assertEqual(sorted(x["send_once"] for x in offered), [False, True])
        self.call(card, "offer", origin="same-request", body="Other", fails=True)
        self.assertEqual(self.call(card, "status")["assignment"]["brief"], "First")
