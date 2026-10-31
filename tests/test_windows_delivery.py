"""Exercise publishing against disposable local Git repositories, never GitHub."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from scripts import windows_delivery


def git(directory, *args):
    return subprocess.run(
        ["git", "-C", str(directory), *args], check=True,
        text=True, capture_output=True,
    ).stdout.strip()


class WindowsDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="stockjournal-publish-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.remote = self.base / "remote.git"
        self.repo = self.base / "checkout"
        self.delivery = self.base / "delivery"
        git(self.base, "init", "--bare", "--initial-branch=main", str(self.remote))
        git(self.base, "clone", str(self.remote), str(self.repo))
        git(self.repo, "config", "user.name", "Delivery Test")
        git(self.repo, "config", "user.email", "delivery@example.invalid")
        git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "stockjournal").mkdir()
        (self.repo / "stockjournal" / "__init__.py").write_text("__version__ = '0.2.0'\n", encoding="utf-8")
        (self.repo / "source.txt").write_text("original source\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Source")
        git(self.repo, "push", "origin", "HEAD:main")
        self.source = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "--detach", self.source)
        self.delivery.mkdir()
        self.binary = b"MZ\x00\x00synthetic executable fixture\x00"
        (self.delivery / "StockJournal.exe").write_bytes(self.binary)
        self.manifest = {
            "schema_version": 1,
            "version": "0.2.0",
            "source_commit": self.source,
            "built_at_utc": "2026-10-31T12:00:00Z",
            "platform": "windows-x64",
            "filename": "StockJournal.exe",
            "sha256": hashlib.sha256(self.binary).hexdigest(),
            "size_bytes": len(self.binary),
            "verification": {"source_gui": True, "frozen_gui": True},
        }
        self.write_manifest()

    def write_manifest(self):
        (self.delivery / "StockJournal-build.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )

    def advance_source(self, branch="main"):
        other = self.base / "other"
        git(self.base, "clone", str(self.remote), str(other))
        git(other, "config", "user.name", "Another Developer")
        git(other, "config", "user.email", "another@example.invalid")
        (other / "source.txt").write_text("new user source\n", encoding="utf-8")
        git(other, "add", "source.txt")
        git(other, "commit", "-m", "User update")
        git(other, "push", "origin", "HEAD:" + branch)
        return git(other, "rev-parse", "HEAD")

    def publish(self):
        return windows_delivery.publish_delivery(self.repo, self.delivery, self.source)

    def test_publishes_only_binary_and_manifest_with_source_as_parent(self):
        (self.delivery / "private-records.json").write_text("never publish this", encoding="utf-8")
        result = self.publish()
        self.assertEqual(result["status"], "published")
        self.assertEqual(git(self.remote, "rev-parse", "main^"), self.source)
        self.assertEqual(
            set(git(self.remote, "diff-tree", "--no-commit-id", "--name-only", "-r", "main").splitlines()),
            {"StockJournal.exe", "StockJournal-build.json"},
        )
        published = subprocess.run(
            ["git", "-C", str(self.remote), "show", "main:StockJournal.exe"],
            check=True, capture_output=True,
        ).stdout
        self.assertEqual(published, self.binary)
        self.assertEqual(git(self.remote, "show", "main:source.txt"), "original source")
        self.assertIn("[skip ci]", git(self.remote, "log", "-1", "--format=%s", "main"))

    def test_skips_when_remote_source_advanced_during_build(self):
        newer = self.advance_source()
        result = self.publish()
        self.assertEqual(result["status"], "stale")
        self.assertEqual(git(self.remote, "rev-parse", "main"), newer)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.source)
        self.assertFalse((self.repo / "StockJournal.exe").exists())

    def test_concurrent_update_during_push_is_not_overwritten(self):
        newer = self.advance_source(branch="race")
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_text(
            "#!/bin/sh\n" + "git --git-dir=" + shlex.quote(self.remote.as_posix())
            + " update-ref refs/heads/main " + newer + " " + self.source + "\n",
            encoding="utf-8", newline="\n",
        )
        hook.chmod(0o755)
        result = self.publish()
        self.assertEqual(result["status"], "stale")
        self.assertEqual(git(self.remote, "rev-parse", "main"), newer)
        self.assertEqual(git(self.remote, "show", "main:source.txt"), "new user source")
        self.assertNotIn("StockJournal.exe", git(self.remote, "ls-tree", "--name-only", "main"))

    def test_rejects_artifact_for_a_different_source(self):
        self.manifest["source_commit"] = "a" * 40
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "source"):
            self.publish()
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.source)

    def test_rejects_corrupted_executable_before_touching_git(self):
        (self.delivery / "StockJournal.exe").write_bytes(self.binary[:-1] + b"!")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.publish()
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.source)

    def test_rejects_unverified_delivery(self):
        self.manifest["verification"]["frozen_gui"] = False
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "verif"):
            self.publish()
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.source)

    def test_refuses_to_commit_existing_worktree_edits(self):
        (self.repo / "source.txt").write_text("uncommitted user edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "clean"):
            self.publish()
        self.assertEqual((self.repo / "source.txt").read_text(encoding="utf-8"), "uncommitted user edit")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.source)

    def test_sealed_manifest_binds_the_source_version_binary_and_two_reports(self):
        dist = self.repo / "dist"
        dist.mkdir()
        (dist / "StockJournal.exe").write_bytes(self.binary)
        reports = []
        for name, frozen in (("source.json", False), ("frozen.json", True)):
            path = self.base / name
            path.write_text(json.dumps({
                "ok": True, "closed": True, "frozen": frozen,
                "pages": ["dashboard", "trades", "imports", "dividends", "instruments", "settings", "strategy"],
            }), encoding="utf-8")
            reports.append(path)
        result = windows_delivery.seal_delivery(self.repo, self.source, *reports)
        saved = json.loads((dist / "StockJournal-build.json").read_text(encoding="utf-8"))
        self.assertEqual(result, saved)
        self.assertEqual(saved["version"], "0.2.0")
        self.assertEqual(saved["source_commit"], self.source)
        self.assertEqual(saved["sha256"], hashlib.sha256(self.binary).hexdigest())
        self.assertEqual(saved["size_bytes"], len(self.binary))
        self.assertTrue(saved["built_at_utc"].endswith("Z"))
        for key, path in zip(("source_report_sha256", "frozen_report_sha256"), reports):
            self.assertEqual(saved["verification"][key], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(windows_delivery.validate_delivery(self.repo, dist, self.source), saved)


class DeliveryVerificationTests(unittest.TestCase):
    def test_rejects_success_report_for_source_instead_of_frozen_exe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "smoke.json"
            path.write_text(json.dumps({
                "ok": True, "closed": True, "frozen": False,
                "pages": ["dashboard", "trades", "imports", "dividends", "instruments", "settings", "strategy"],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frozen"):
                windows_delivery.validate_smoke_report(path, frozen=True)

    def test_rejects_incomplete_or_failed_gui_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "smoke.json"
            for changes in ({"ok": False}, {"closed": False}, {"pages": ["dashboard"]}):
                with self.subTest(changes=changes):
                    report = {
                        "ok": True, "closed": True, "frozen": True,
                        "pages": ["dashboard", "trades", "imports", "dividends", "instruments", "settings", "strategy"],
                        **changes,
                    }
                    path.write_text(json.dumps(report), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        windows_delivery.validate_smoke_report(path, frozen=True)


if __name__ == "__main__":
    unittest.main()
