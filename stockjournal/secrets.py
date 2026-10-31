"""Local user secrets: Windows DPAPI, or process memory on other systems.

The encrypted binary file is deliberately separate from journal data and its
exports. No plaintext persistence fallback is available.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import threading
from pathlib import Path


_MAGIC = b"STOCKJOURNAL-DPAPI\x00\x01"
_ENTROPY = b"StockJournal local user secrets v1"
_MAX_BYTES = 1_048_576


class SecretStoreError(RuntimeError):
    """A sanitized local secret protection or persistence error."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise SecretStoreError("이 운영체제에서는 Windows 비밀 저장소를 사용할 수 없습니다.")
    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    blob_pointer = ctypes.POINTER(_DataBlob)
    crypt32.CryptProtectData.argtypes = [blob_pointer, ctypes.c_wchar_p, blob_pointer,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_pointer]
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = [blob_pointer, ctypes.POINTER(ctypes.c_wchar_p), blob_pointer,
                                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_pointer]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    entropy_buffer = (ctypes.c_ubyte * len(_ENTROPY)).from_buffer_copy(_ENTROPY)
    source = _DataBlob(len(data), source_buffer)
    entropy = _DataBlob(len(_ENTROPY), entropy_buffer)
    output = _DataBlob()
    try:
        # CRYPTPROTECT_UI_FORBIDDEN; deliberately omit CRYPTPROTECT_LOCAL_MACHINE.
        if protect:
            success = crypt32.CryptProtectData(ctypes.byref(source), "Stock Journal",
                                               ctypes.byref(entropy), None, None, 1, ctypes.byref(output))
        else:
            success = crypt32.CryptUnprotectData(ctypes.byref(source), None,
                                                 ctypes.byref(entropy), None, None, 1, ctypes.byref(output))
        if not success:
            raise SecretStoreError("Windows 사용자 비밀 보호에 실패했습니다.")
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if output.pbData:
            kernel32.LocalFree(output.pbData)


def _dpapi_encrypt(data: bytes) -> bytes:
    return _dpapi(data, protect=True)


def _dpapi_decrypt(data: bytes) -> bytes:
    return _dpapi(data, protect=False)


class SecretStore:
    """Store string values under string keys, protected for the Windows user.

    ``supported`` means encrypted disk persistence is available. With a false
    value, the store is intentionally per-instance, memory-only storage.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.supported = sys.platform == "win32"
        self._values: dict[str, str] = {}
        self._lock = threading.RLock()
        if self.supported:
            self._load()

    def _load(self) -> None:
        try:
            with self.path.open("rb") as stream:
                encoded = stream.read(_MAX_BYTES + 1)
        except FileNotFoundError:
            return
        except OSError:
            raise SecretStoreError("로컬 비밀 저장소를 읽을 수 없습니다.") from None
        try:
            if len(encoded) > _MAX_BYTES or not encoded.startswith(_MAGIC):
                raise ValueError
            values = json.loads(_dpapi_decrypt(encoded[len(_MAGIC):]).decode("utf-8"))
            if not isinstance(values, dict) or not all(
                isinstance(key, str) and key and isinstance(value, str) for key, value in values.items()
            ):
                raise ValueError
        except (OSError, ValueError, TypeError, SecretStoreError):
            raise SecretStoreError("로컬 비밀 저장소를 복호화할 수 없습니다. Windows 사용자 또는 파일을 확인하세요.") from None
        self._values = values

    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            return self._values.get(key, default)

    def set(self, key: str, value: str) -> None:
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise SecretStoreError("비밀 저장소의 키와 값은 문자열이어야 합니다.")
        with self._lock:
            updated = {**self._values, key: value}
            if self.supported:
                self._persist(updated)
            self._values = updated

    def _persist(self, values: dict[str, str]) -> None:
        temporary: Path | None = None
        try:
            plaintext = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            protected = _MAGIC + _dpapi_encrypt(plaintext)
            if len(protected) > _MAX_BYTES:
                raise SecretStoreError("비밀 저장소 크기 제한을 초과했습니다.")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".stockjournal-secrets-", dir=self.path.parent,
                                             delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(protected)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except (OSError, ValueError, TypeError, SecretStoreError):
            raise SecretStoreError("로컬 비밀을 안전하게 저장하지 못했습니다.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # An abandoned temporary file contains only DPAPI ciphertext.
