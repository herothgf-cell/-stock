"""Boundary tests: this test host does not have the Windows OCR runtime."""

import base64
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class OCRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.image = Path(self.temp.name) / "거래 $(Write-Host oops) ' screenshot.png"
        self.image.write_bytes(b"test image; actual decoding belongs to the Windows runtime")

    def ocr(self):
        from stockjournal import ocr

        return ocr

    def test_non_windows_reports_local_runtime_requirement(self):
        ocr = self.ocr()
        with patch.object(ocr.sys, "platform", "linux"):
            with self.assertRaisesRegex(RuntimeError, "Windows"):
                ocr.extract_text(self.image)

    def test_missing_image_is_reported_before_process_start(self):
        with self.assertRaises(FileNotFoundError):
            self.ocr().extract_text(Path(self.temp.name) / "missing.png")

    def test_unicode_paths_are_data_and_text_output_is_utf8(self):
        ocr = self.ocr()
        calls = []

        def process(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return SimpleNamespace(returncode=0, stdout="삼성전자 3주\r\n매수 70,000원\r\n".encode("utf-8"), stderr=b"")

        with patch.object(ocr.sys, "platform", "win32"), patch.object(ocr, "_powershell_executable", return_value=Path("powershell.exe")), patch.object(ocr.subprocess, "run", side_effect=process):
            result = ocr.extract_text(self.image, language="ko-KR", timeout=12)
        self.assertEqual(result, "삼성전자 3주\n매수 70,000원")
        arguments, options = calls[0]
        self.assertIsInstance(arguments, list)
        self.assertFalse(options.get("shell", False))
        self.assertEqual(options["timeout"], 12)
        encoded = arguments[arguments.index("-EncodedCommand") + 1]
        command = base64.b64decode(encoded).decode("utf-16le")
        self.assertNotIn(str(self.image), command)
        self.assertNotIn("Write-Host oops", command)
        self.assertIn(base64.b64encode(str(self.image.resolve()).encode("utf-8")).decode("ascii"), command)
        self.assertIn(base64.b64encode(b"ko-KR").decode("ascii"), command)

    def test_timeout_is_actionable_runtime_error(self):
        ocr = self.ocr()
        with patch.object(ocr.sys, "platform", "win32"), patch.object(ocr, "_powershell_executable", return_value=Path("powershell.exe")), patch.object(ocr.subprocess, "run", side_effect=subprocess.TimeoutExpired("powershell", 5)):
            with self.assertRaisesRegex(RuntimeError, "5"):
                ocr.extract_text(self.image, timeout=5)

    def test_process_errors_do_not_become_recognized_text(self):
        ocr = self.ocr()
        result = SimpleNamespace(returncode=1, stdout=b"", stderr="Requested OCR language ko-KR is not installed.".encode("utf-8"))
        with patch.object(ocr.sys, "platform", "win32"), patch.object(ocr, "_powershell_executable", return_value=Path("powershell.exe")), patch.object(ocr.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "ko-KR"):
                ocr.extract_text(self.image)

    def test_invalid_timeout_is_rejected(self):
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    self.ocr().extract_text(self.image, timeout=timeout)

    def test_resource_lookup_supports_frozen_application_layout(self):
        ocr = self.ocr()
        root = Path(self.temp.name) / "bundle"
        resource = root / "stockjournal" / "resources" / "windows_ocr.ps1"
        resource.parent.mkdir(parents=True)
        resource.write_text("# packaged script", encoding="utf-8")
        with patch.object(ocr.sys, "_MEIPASS", str(root), create=True):
            self.assertEqual(ocr._resource_path(), resource)


if __name__ == "__main__":
    unittest.main()
