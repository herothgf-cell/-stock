"""Native Windows smoke test using an entirely synthetic, temporary image.

Once the en-US OCR engine is present, every bridge or recognition failure is a
test failure. This deliberately does not convert adapter errors into skips.
"""

import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


_PREFLIGHT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $language = New-Object -TypeName Windows.Globalization.Language -ArgumentList 'en-US'
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    if ($null -eq $engine) {
        [Console]::Out.Write('UNAVAILABLE: Windows en-US OCR language/engine is not installed.')
        exit 3
    }
    [Console]::Out.Write('AVAILABLE')
    exit 0
}
catch {
    $unavailable = $_.FullyQualifiedErrorId -match '^(ASSEMBLY_NOT_FOUND|TypeNotFound)(,|$)'
    $exception = $_.Exception
    while ($null -ne $exception) {
        if ($exception -is [System.TypeLoadException] -or
            $exception -is [System.IO.FileNotFoundException] -or
            $exception.HResult -eq -2147221164) {
            $unavailable = $true
        }
        $exception = $exception.InnerException
    }
    if ($unavailable) {
        [Console]::Out.Write('UNAVAILABLE: Windows WinRT OCR runtime is unavailable: ' + $_.Exception.Message)
        exit 3
    }
    [Console]::Error.WriteLine('Unexpected OCR preflight failure: ' + $_.Exception.Message)
    exit 1
}
"""


@unittest.skipUnless(sys.platform == "win32", "Native Windows.Media.Ocr integration requires Windows.")
class NativeWindowsOCRTests(unittest.TestCase):
    def test_synthetic_english_trade_image_recognizes_code_quantity_and_price(self):
        windows_root = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows")
        powershell = windows_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if not powershell.is_file():
            self.skipTest("Windows PowerShell 5.1 required for the WinRT OCR bridge is not installed.")

        # This probe uses only fixed code. No image paths, user content, or
        # credentials are interpolated, and unexpected failures stay failures.
        preflight = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
             "-EncodedCommand", base64.b64encode(_PREFLIGHT.encode("utf-16le")).decode("ascii")],
            shell=False, capture_output=True, timeout=20, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
        output = preflight.stdout.decode("utf-8-sig", errors="replace").strip()
        error = preflight.stderr.decode("utf-8-sig", errors="replace").strip()
        if preflight.returncode == 3 and output.startswith("UNAVAILABLE:"):
            self.skipTest(output.removeprefix("UNAVAILABLE:").strip())
        self.assertEqual(preflight.returncode, 0, f"OCR preflight failed: {error or output}")
        self.assertEqual(output, "AVAILABLE", f"Unexpected OCR preflight response: {output!r}; {error}")

        try:
            from PIL import Image, ImageDraw, ImageFont
        except ModuleNotFoundError as exc:
            if exc.name in {"PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont"}:
                self.skipTest("Pillow build/test dependency is not installed; synthetic OCR image cannot be generated.")
            raise

        from stockjournal.ocr import extract_text

        # Arial is the fixed Windows fixture font. A missing/corrupt font is a
        # fixture error, not grounds to silently skip an available OCR engine.
        font = ImageFont.truetype(str(windows_root / "Fonts" / "arial.ttf"), 64)
        with tempfile.TemporaryDirectory(prefix="stockjournal-ocr-") as temporary:
            image_path = Path(temporary) / "synthetic-trade.png"
            with Image.new("RGB", (1500, 500), "white") as image:
                draw = ImageDraw.Draw(image)
                for index, line in enumerate(("BUY STOCK TRADE", "Code 005930", "Quantity 10", "Price 70000")):
                    draw.text((60, 35 + index * 110), line, font=font, fill="black")
                image.save(image_path, format="PNG")

            # Do not catch OCRError here: script, decoding, timeout, and OCR
            # failures must fail CI after the runtime availability probe passes.
            recognized = extract_text(image_path, language="en-US", timeout=60)

        self.assertRegex(recognized, r"(?<!\d)005930(?!\d)", f"Stock code was not recognized: {recognized!r}")
        self.assertRegex(recognized, r"\b10\b", f"Quantity was not recognized: {recognized!r}")
        self.assertRegex(recognized, r"(?<!\d)70[ ,]?000(?!\d)", f"Price was not recognized: {recognized!r}")


if __name__ == "__main__":
    unittest.main()
