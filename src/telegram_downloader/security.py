from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Protocol


class SecretsError(ValueError):
    """Raised when encrypted secrets cannot be safely read or written."""


class Protector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class DpapiProtector:
    _UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("DPAPI 仅支持 Windows")
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    def protect(self, value: bytes) -> bytes:
        return self._transform(value, protect=True)

    def unprotect(self, value: bytes) -> bytes:
        return self._transform(value, protect=False)

    def _transform(self, value: bytes, *, protect: bool) -> bytes:
        input_buffer = ctypes.create_string_buffer(value, len(value))
        input_blob = _DataBlob(
            len(value),
            ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        output_blob = _DataBlob()
        if protect:
            succeeded = self._crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                "TelegramDownloader",
                None,
                None,
                None,
                self._UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        else:
            succeeded = self._crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                None,
                None,
                None,
                None,
                self._UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        if not succeeded:
            raise OSError(ctypes.get_last_error())
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                self._kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


class SecretsVault:
    _MAX_ENCRYPTED_BYTES = 4 * 1024 * 1024

    def __init__(self, path: Path, protector: Protector | None = None) -> None:
        self.path = path
        self.protector = protector or DpapiProtector()

    def save(self, values: dict[str, str]) -> None:
        if not isinstance(values, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in values.items()
        ):
            raise SecretsError("凭据必须是文本键值对")
        plaintext = bytearray(
            json.dumps(values, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        )
        try:
            encrypted = self.protector.protect(bytes(plaintext))
        finally:
            plaintext[:] = b"\x00" * len(plaintext)
        self._atomic_write(encrypted)

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            encrypted = self.path.read_bytes()
            if not encrypted or len(encrypted) > self._MAX_ENCRYPTED_BYTES:
                raise SecretsError("凭据文件大小无效")
            plaintext = bytearray(self.protector.unprotect(encrypted))
            try:
                raw = json.loads(plaintext.decode("utf-8"))
            finally:
                plaintext[:] = b"\x00" * len(plaintext)
            if not isinstance(raw, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in raw.items()
            ):
                raise SecretsError("凭据文件内容无效")
            return raw
        except SecretsError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SecretsError("无法解密凭据文件") from exc

    def _atomic_write(self, content: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
