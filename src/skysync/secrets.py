"""DPAPI-backed secret store. No secret ever touches disk in plaintext.

Each secret is encrypted with Windows DPAPI (CryptProtectData, current-user
scope) and written as a ``.bin`` blob under the secrets directory. Decryption
only succeeds for the SAME Windows account on the SAME machine — so seed
secrets while logged in as the account the Task Scheduler job runs as
(SETUP.md walks through this).

Known names used by the app:
    skylight_email, skylight_password   - Skylight login (or skylight_token)
    skylight_token                      - captured bearer/basic token (optional)
    graph_client_secret                 - only if graph.sharepoint_auth = "app_only"
    msal_token_cache                    - managed automatically (rotated every run)

CLI:
    python -m skysync.secrets set NAME      (value prompted, hidden)
    python -m skysync.secrets list
    python -m skysync.secrets delete NAME
    python -m skysync.secrets check NAME    (decrypts, prints only OK/length)
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
from pathlib import Path

from .errors import ConfigError

_ENTROPY = b"skysync-dpapi-v1"  # app-specific salt, not a secret

CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_to_bytes(blob: _DATA_BLOB) -> bytes:
    data = ctypes.string_at(blob.pbData, blob.cbData)
    ctypes.windll.kernel32.LocalFree(blob.pbData)
    return data


def dpapi_protect(data: bytes) -> bytes:
    inp, ent, out = _blob(data), _blob(_ENTROPY), _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(inp), None, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)
    ):
        raise OSError("CryptProtectData failed")
    return _blob_to_bytes(out)


def dpapi_unprotect(data: bytes) -> bytes:
    inp, ent, out = _blob(data), _blob(_ENTROPY), _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(inp), None, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)
    ):
        raise OSError(
            "CryptUnprotectData failed — was this secret seeded by a different "
            "Windows account or on a different machine?"
        )
    return _blob_to_bytes(out)


class SecretStore:
    def __init__(self, secrets_dir: str | Path):
        self.dir = Path(secrets_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not name.replace("_", "").replace("-", "").isalnum():
            raise ConfigError(f"invalid secret name: {name!r}")
        return self.dir / f"{name}.bin"

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def set_bytes(self, name: str, value: bytes) -> None:
        self._path(name).write_bytes(dpapi_protect(value))

    def get_bytes(self, name: str) -> bytes:
        p = self._path(name)
        if not p.exists():
            raise ConfigError(
                f"secret '{name}' not found in {self.dir}. Seed it with: python -m skysync.secrets set {name}"
            )
        return dpapi_unprotect(p.read_bytes())

    def set(self, name: str, value: str) -> None:
        self.set_bytes(name, value.encode("utf-8"))

    def get(self, name: str) -> str:
        return self.get_bytes(name).decode("utf-8")

    def get_optional(self, name: str) -> str | None:
        return self.get(name) if self.exists(name) else None

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def list_names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.bin"))


def _main(argv: list[str]) -> int:
    import argparse
    import getpass

    ap = argparse.ArgumentParser(prog="skysync.secrets", description="DPAPI secret store")
    ap.add_argument("action", choices=["set", "list", "delete", "check"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--secrets-dir", default="secrets")
    ns = ap.parse_args(argv)

    store = SecretStore(ns.secrets_dir)
    if ns.action == "list":
        for n in store.list_names():
            print(n)
        return 0
    if not ns.name:
        ap.error(f"'{ns.action}' requires a secret NAME")
    if ns.action == "set":
        value = getpass.getpass(f"Value for '{ns.name}' (input hidden): ")
        if not value:
            print("empty value, aborting", file=sys.stderr)
            return 1
        store.set(ns.name, value)
        print(f"stored '{ns.name}' (DPAPI, user scope) in {store.dir}")
    elif ns.action == "delete":
        store.delete(ns.name)
        print(f"deleted '{ns.name}'")
    elif ns.action == "check":
        v = store.get(ns.name)  # raises if missing/undecryptable
        print(f"OK: '{ns.name}' decrypts ({len(v)} chars). Value not shown.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
