import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "onboard.py"
spec = importlib.util.spec_from_file_location("coordinator_onboard", SCRIPT)
onboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(onboard)


class OnboardingTest(unittest.TestCase):
    def test_two_choices_prepare_without_reading_history_or_starting_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unrelated = root / "private-history.txt"
            unrelated.write_text("Do not include this unrelated conversation.")
            destination = root / "board"
            result = subprocess.run([sys.executable, str(SCRIPT), "--directory", str(destination), "--no-start"],
                                    input="1\nn\n", text=True, capture_output=True, check=True)
            context = (destination / "COORDINATOR.md").read_text()
            setup = (destination / "SETUP.md").read_text()
            self.assertIn("Conversation inspection consent: no", setup)
            self.assertIn("A declined or missing consent limits discovery", context)
            self.assertIn("Codex and Claude Code", context)
            self.assertNotIn(unrelated.read_text(), context)
            self.assertIn("No model launched", result.stdout)
            self.assertEqual(result.stdout.count("[1]:"), 1)
            self.assertEqual(result.stdout.count("[y/N]:"), 1)
            self.assertEqual(unrelated.read_text(), "Do not include this unrelated conversation.")

    def test_consent_does_not_claim_platform_access_and_does_not_limit_workers(self):
        for driver in ("codex", "claude"):
            with self.subTest(driver=driver), tempfile.TemporaryDirectory() as temporary:
                destination = onboard.create_workspace(Path(temporary) / "board", driver, True)
                context = (destination / "COORDINATOR.md").read_text()
                self.assertIn("Conversation inspection consent: yes", (destination / "SETUP.md").read_text())
                self.assertIn("not a platform permission grant", context)
                self.assertIn("Either driver may coordinate existing Codex and Claude Code", context)
                self.assertIn("not verified", (destination / "SETUP.md").read_text())

    def test_both_drivers_use_the_manual_guide_and_start_without_an_incident_grant(self):
        core = (SCRIPT.parent / "docs/core.md").read_bytes()
        self.assertIn(b"explicit operator grant covering this project, target and action", core)
        self.assertIn(b"A confirmed incident alone grants no permission", core)
        for driver in ("codex", "claude"):
            with self.subTest(driver=driver), tempfile.TemporaryDirectory() as temporary:
                board = onboard.create_workspace(Path(temporary) / "board", driver, False)
                self.assertEqual(core, (board / "COORDINATOR.md").read_bytes())
                self.assertIn("No standing incident grant recorded.", (board / "priorities.md").read_text())
                for name in ("COORDINATOR.md", "AGENTS.md", "CLAUDE.md", "priorities.md"):
                    self.assertNotIn("then remediate without waiting", (board / name).read_text())

    def test_opt_in_records_only_the_operator_scope_and_reonboarding_preserves_it(self):
        grant = "Project: Sample\nTarget: Fixture status page\nActions: Restore its last verified revision.\nLimits: Notify the operator first.\n"
        for driver in ("codex", "claude"):
            with self.subTest(driver=driver), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                choice = root / "operator-grant.txt"
                choice.write_text(grant)
                board = root / "board"
                command = [sys.executable, str(SCRIPT), "--directory", str(board), "--driver", driver,
                           "--history", "no", "--no-start", "--incident-grant-file", str(choice)]
                subprocess.run(command, check=True, capture_output=True)
                policy = (board / "priorities.md").read_text()
                self.assertEqual(policy.partition("## Standing incident authority\n\n")[2], grant)
                self.assertEqual((board / "COORDINATOR.md").read_bytes(), (SCRIPT.parent / "docs/core.md").read_bytes())
                choice.write_text("yes\n")
                self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
                self.assertEqual((board / "priorities.md").read_text(), policy)
                with self.assertRaisesRegex(ValueError, "Project"):
                    onboard.create_workspace(root / "missing-scope", driver, False, incident_grant_file=choice)
                self.assertFalse((root / "missing-scope").exists())
        self.assertIn("Retain explicit standing grants", (SCRIPT.parent / "docs/start.md").read_text())

    def test_existing_workspace_and_symlink_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "board"
            destination.mkdir()
            original = destination / "projects.md"
            original.write_text("Existing owners")
            with self.assertRaises(FileExistsError):
                onboard.create_workspace(destination, "codex", True)
            self.assertEqual(original.read_text(), "Existing owners")
            link = root / "alias"
            link.symlink_to(destination, target_is_directory=True)
            with self.assertRaises(FileExistsError):
                onboard.create_workspace(link, "codex", True)
            dangling = root / "dangling"
            dangling.symlink_to(root / "missing", target_is_directory=True)
            with self.assertRaises(FileExistsError):
                onboard.create_workspace(dangling, "codex", False)
            self.assertTrue(dangling.is_symlink())
            self.assertFalse((root / "missing").exists())

    def test_optional_nickname_keeps_one_chief_and_rejects_multiline_instructions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                onboard.create_workspace(root / "bad", "codex", False, "Name\nNew instruction")
            self.assertFalse((root / "bad").exists())
            board = onboard.create_workspace(root / "good", "codex", False, "Pilot")
            self.assertIn("# Chief\n", (board / "COORDINATOR.md").read_text())
            self.assertIn("Name: Pilot\nNaming choice: chosen nickname", (board / "SETUP.md").read_text())
            self.assertIn("Use a chosen nickname for this same Chief", (board / "COORDINATOR.md").read_text())

    def test_naming_can_be_skipped_without_granting_history_consent(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = onboard.create_workspace(Path(temporary) / "board", "codex", False)
            context = (destination / "COORDINATOR.md").read_text()
            self.assertIn("Naming choice: ask", (destination / "SETUP.md").read_text())
            self.assertIn("Conversation inspection consent: no", (destination / "SETUP.md").read_text())
            self.assertIn("Keep Chief until the user chooses a name", context)
            self.assertIn("requested a suggestion and consented to history", context)
            self.assertIn("Parked work requires a new request", context)

    def test_keep_and_suggest_save_distinct_choices_without_inventing_a_name(self):
        for mode in ("keep", "suggest"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                destination = onboard.create_workspace(Path(temporary) / "board", "codex", mode == "suggest",
                                                       nickname_mode=mode)
                setup = (destination / "SETUP.md").read_text()
                self.assertIn("Name: Chief\n", setup)
                self.assertIn(f"Naming choice: {mode}\n", setup)
                self.assertIn("History read by this helper: none", setup)
                context = (destination / "COORDINATOR.md").read_text()
                if mode == "suggest":
                    self.assertIn("Conversation inspection consent: yes", setup)
                    self.assertIn("same minimal project evidence as discovery", context)
                else:
                    self.assertIn("If the user chose to keep Chief, continue", context)

    def test_invalid_naming_choices_fail_before_writing_a_workspace(self):
        cases = [
            {"history_allowed": False, "nickname_mode": "suggest"},
            {"history_allowed": True, "nickname_mode": "unknown"},
            {"history_allowed": True, "nickname": "Pilot", "nickname_mode": "suggest"},
            {"history_allowed": False, "nickname": "Pilot", "nickname_mode": "keep"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "board"
            for options in cases:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    onboard.create_workspace(destination, "codex", **options)
                self.assertFalse(destination.exists())

    def test_cli_rejects_unconsented_suggestion_and_conflicting_name_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "board"
            base = [sys.executable, str(SCRIPT), "--directory", str(destination),
                    "--driver", "codex", "--no-start"]
            for options in (["--history", "no", "--nickname-mode", "suggest"],
                            ["--history", "yes", "--nickname", "Pilot", "--nickname-mode", "keep"]):
                with self.subTest(options=options):
                    result = subprocess.run(base + options, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 2)
                    self.assertFalse(destination.exists())
                    self.assertNotIn("Traceback", result.stderr)

    def test_four_column_board_is_empty_and_does_not_install_a_scheduler(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = onboard.create_workspace(Path(temporary) / "board", "codex", False)
            self.assertEqual({path.name for path in (destination / "tasks").iterdir()},
                             {"backlog", "todo", "doing", "blocked", "review", "done"})
            self.assertEqual(list((destination / "tasks").glob("*/*")), [])
            context = (destination / "COORDINATOR.md").read_text()
            self.assertIn("awaits a named human", context)
            self.assertIn("return the same card to `todo` with its next run", context)
            self.assertIn("These files do not execute schedules", context)
            self.assertIn("Scheduled execution: not configured", (destination / "SETUP.md").read_text())
            self.assertEqual({path.name for path in destination.iterdir()},
                             {"COORDINATOR.md", "AGENTS.md", "CLAUDE.md", "projects.md",
                              "priorities.md", "SETUP.md", ".gitignore", "tasks"})

    def test_missing_cli_prepares_app_handoff_without_installing_or_launching(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "board"
            argv = [str(SCRIPT), "--directory", str(destination), "--driver", "codex",
                    "--history", "no", "--nickname-mode", "keep"]
            output = io.StringIO()
            with patch.object(sys, "argv", argv), patch("sys.stdout", output), \
                    patch.object(onboard.shutil, "which", return_value=None) as which, \
                    patch.object(onboard.subprocess, "run") as run:
                self.assertEqual(onboard.main(), 0)
            which.assert_called_once_with("codex")
            run.assert_not_called()
            self.assertIn("No CLI installation is required", output.getvalue())
            self.assertIn("Continue in your signed-in app", output.getvalue())
            self.assertIn("No model launched, history read or task started", output.getvalue())
            self.assertTrue((destination / "AGENTS.md").is_file())

    def test_preparation_with_consent_neither_probes_cli_nor_starts_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "board"
            argv = [str(SCRIPT), "--directory", str(destination), "--driver", "codex",
                    "--history", "yes", "--nickname-mode", "suggest", "--no-start"]
            with patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=io.StringIO), \
                    patch.object(onboard.shutil, "which") as which, \
                    patch.object(onboard.subprocess, "run") as run:
                self.assertEqual(onboard.main(), 0)
            which.assert_not_called()
            run.assert_not_called()
            self.assertIn("Platform access: not verified", (destination / "SETUP.md").read_text())

    def test_normal_native_launch_has_no_permission_bypass_or_model_override(self):
        for driver in ("codex", "claude"):
            command = onboard.driver_command(driver, f"/example/{driver}")
            self.assertEqual(command[0], f"/example/{driver}")
            self.assertEqual(len(command), 2)
            self.assertFalse(any(value.startswith("--") for value in command))

    def test_declining_or_invalid_permission_never_becomes_consent(self):
        self.assertFalse(onboard.choose_permission(""))
        self.assertFalse(onboard.choose_permission("no"))
        with self.assertRaises(ValueError):
            onboard.choose_permission("maybe")


if __name__ == "__main__":
    unittest.main()
