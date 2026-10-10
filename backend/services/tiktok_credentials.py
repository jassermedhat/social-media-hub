import ctypes
import json
import os
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable


class SecureStorageError(RuntimeError):
    """Raised when TikTok credential storage cannot be used safely."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


_CRYPTPROTECT_UI_FORBIDDEN = 0x01


def _blob_from_bytes(value: bytes):
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(
        len(value),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def _dpapi_protect(value: bytes) -> bytes:
    if sys.platform != "win32":
        raise SecureStorageError(
            "TikTok secure storage requires Windows DPAPI."
        )

    input_blob, input_buffer = _blob_from_bytes(value)
    output_blob = _DataBlob()

    try:
        success = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "SMH TikTok credentials",
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )

        if not success:
            raise ctypes.WinError()

        return ctypes.string_at(
            output_blob.pbData,
            output_blob.cbData,
        )
    except OSError as exc:
        raise SecureStorageError(
            "Windows DPAPI could not encrypt TikTok credentials."
        ) from exc
    finally:
        del input_buffer
        if output_blob.pbData:
            ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def _dpapi_unprotect(value: bytes) -> bytes:
    if sys.platform != "win32":
        raise SecureStorageError(
            "TikTok secure storage requires Windows DPAPI."
        )

    input_blob, input_buffer = _blob_from_bytes(value)
    output_blob = _DataBlob()

    try:
        success = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )

        if not success:
            raise ctypes.WinError()

        return ctypes.string_at(
            output_blob.pbData,
            output_blob.cbData,
        )
    except OSError as exc:
        raise SecureStorageError(
            "Windows DPAPI could not decrypt TikTok credentials."
        ) from exc
    finally:
        del input_buffer
        if output_blob.pbData:
            ctypes.windll.kernel32.LocalFree(output_blob.pbData)


class DpapiJsonFile:
    """Atomic, current-Windows-user DPAPI storage for a JSON object."""

    def __init__(
        self,
        path: Path | str,
        *,
        protect: Callable[[bytes], bytes] = _dpapi_protect,
        unprotect: Callable[[bytes], bytes] = _dpapi_unprotect,
    ):
        self.path = Path(path)
        self._protect = protect
        self._unprotect = unprotect

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> dict[str, Any] | None:
        if not self.exists():
            return None

        try:
            encrypted = self.path.read_bytes()
            plaintext = self._unprotect(encrypted)
            data = json.loads(plaintext.decode("utf-8"))
        except SecureStorageError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecureStorageError(
                "TikTok secure credential storage is unreadable."
            ) from exc

        if not isinstance(data, dict):
            raise SecureStorageError(
                "TikTok secure credential storage is invalid."
            )

        return data

    def save(self, data: dict[str, Any]) -> None:
        try:
            plaintext = json.dumps(
                data,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            encrypted = self._protect(plaintext)
        except SecureStorageError:
            raise
        except (TypeError, ValueError) as exc:
            raise SecureStorageError(
                "TikTok credential data could not be serialized."
            ) from exc

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(encrypted)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            os.replace(temporary_path, self.path)
        except OSError as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

            raise SecureStorageError(
                "TikTok secure credential storage could not be written."
            ) from exc

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise SecureStorageError(
                "TikTok secure credential storage could not be removed."
            ) from exc
