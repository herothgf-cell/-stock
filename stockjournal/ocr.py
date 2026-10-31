"""Local Windows OCR adapter. Images are never sent to an external service."""

from __future__ import annotations

import base64
import math
import os
import subprocess
import sys
from pathlib import Path


class OCRError(RuntimeError):
    """An actionable failure from the local Windows OCR runtime."""


def _resource_path() -> Path:
    """Locate the bundled script in source, onedir, and onefile layouts."""
    candidates = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        bundle = Path(bundle_root)
        candidates.extend((bundle / "stockjournal" / "resources" / "windows_ocr.ps1", bundle / "resources" / "windows_ocr.ps1"))
    candidates.append(Path(__file__).resolve().parent / "resources" / "windows_ocr.ps1")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise OCRError("Windows OCR 스크립트가 없습니다. resources/windows_ocr.ps1을 포함하여 앱을 다시 설치하세요.")


def _powershell_executable() -> Path:
    # WinRT's .NET Framework projection is provided by Windows PowerShell 5.1;
    # resolving the OS path also avoids a same-directory executable shadow.
    windows_root = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows")
    executable = windows_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file():
        raise OCRError("Windows PowerShell 5.1을 찾을 수 없습니다. Windows 구성 요소를 확인하세요.")
    return executable


def _base64_utf8(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def extract_text(path: str | Path, language: str = "ko-KR", timeout: float = 60) -> str:
    """Recognize a local image with Windows.Media.Ocr and return UTF-8 text.

    The image path, script path, and language travel as base64 data inside an
    encoded PowerShell command. Quotation marks, Korean filenames, and shell
    metacharacters are consequently not interpreted as executable input.
    """
    image_path = Path(path).expanduser().resolve(strict=True)
    if not image_path.is_file():
        raise ValueError("OCR에는 이미지 파일 경로를 지정하세요.")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("OCR 제한 시간은 0보다 큰 유한한 초 단위 숫자여야 합니다.")
    if not isinstance(language, str) or not language.strip():
        raise ValueError("OCR 언어를 지정하세요 (예: ko-KR).")
    if sys.platform != "win32":
        raise OCRError("이미지 OCR은 Windows 10/11의 로컬 OCR 기능을 사용합니다. 이 환경에서는 텍스트를 직접 붙여 넣으세요.")
    script = _resource_path()
    command = (
        "& ([System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('"
        + _base64_utf8(str(script))
        + "'))) -ImagePathBase64 '"
        + _base64_utf8(str(image_path))
        + "' -LanguageBase64 '"
        + _base64_utf8(language.strip())
        + "'"
    )
    arguments = [
        str(_powershell_executable()), "-NoLogo", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-EncodedCommand",
        base64.b64encode(command.encode("utf-16le")).decode("ascii"),
    ]
    try:
        completed = subprocess.run(
            arguments, capture_output=True, shell=False, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OCRError(f"로컬 OCR이 {timeout:g}초 안에 완료되지 않았습니다. 이미지의 거래 부분만 잘라 다시 시도하세요.") from exc
    except OSError as exc:
        raise OCRError(f"Windows OCR을 시작할 수 없습니다: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8-sig", errors="replace").strip()
        raise OCRError(f"Windows OCR 실패: {detail[:3000] or 'Windows 언어 팩과 이미지 형식을 확인하세요.'}")
    try:
        return completed.stdout.decode("utf-8-sig").replace("\r\n", "\n").strip()
    except UnicodeDecodeError as exc:
        raise OCRError("OCR 출력의 UTF-8 인코딩을 읽지 못했습니다. Windows PowerShell 구성을 확인하세요.") from exc
