"""Platform-backed secret store. No secret ever touches disk in plaintext
on Windows; on Linux, protection is filesystem permissions (see below).

Windows: each secret is encrypted with DPAPI (CryptProtectData, current-user
scope) and written as a ``.bin`` blob under the secrets directory. Decryption
only succeeds for the SAME Windows account on the SAME machine — so seed
secrets while logged in as the account the Task Scheduler job runs as
(SETUP.md walks through this).

Linux: there is no DPAPI equivalent available headless (no TPM, no Secret
Service daemon on a bare Pi) — see DESIGN_NOTES.md for why this was a
deliberate, discussed tradeoff rather than a silent downgrade. Secrets are
written as plaintext bytes, protected the same way this host already
protects its other production secrets (see /opt/bot/secrets on clawdpi):
mode 700 directory, mode 600 files, both owned by the service account. The
secrets directory itself must never be world- or group-readable; this module
enforces that on every write but the directory's *parent* permissions are
the operator's responsibility (SETUP.md covers this for the Pi deploy).

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

import os
import stat
import sys
from pathlib import Path

from .errors import ConfigError

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import ctypes
    import ctypes.wintypes

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

    def _protect(data: bytes) -> bytes:
        return dpapi_protect(data)

    def _unprotect(data: bytes) -> bytes:
        return dpapi_unprotect(data)

    _BACKEND_LABEL = "DPAPI, user scope"
else:
    # No OS-backed secret vault available headless on Linux (no TPM, no
    # Secret Service daemon on a bare Pi). Protection is filesystem
    # permissions only, matching this host's existing convention for other
    # production secrets (/opt/bot/secrets: 700 dir, 600 files) - see the
    # module docstring.
    def _protect(data: bytes) -> bytes:
        return data

    def _unprotect(data: bytes) -> bytes:
        return data

    _BACKEND_LABEL = "plain file, user-only permissions (dir 700 / file 600)"


class SecretStore:
    def __init__(self, secrets_dir: str | Path):
        self.dir = Path(secrets_dir)
        if _IS_WINDOWS:
            self.dir.mkdir(parents=True, exist_ok=True)
        else:
            self.dir.mkdir(parents=True, exist_ok=True, mode=stat.S_IRWXU)  # 700
            # Belt-and-suspenders for a directory that already existed with
            # looser permissions (e.g. from before this fix): mkdir's mode
            # is a no-op when exist_ok finds it already there.
            os.chmod(self.dir, stat.S_IRWXU)

    def _path(self, name: str) -> Path:
        if not name.replace("_", "").replace("-", "").isalnum():
            raise ConfigError(f"invalid secret name: {name!r}")
        return self.dir / f"{name}.bin"

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def set_bytes(self, name: str, value: bytes) -> None:
        p = self._path(name)
        data = _protect(value)
        if _IS_WINDOWS:
            p.write_bytes(data)
        else:
            # Create already at 600, not write-then-chmod: the latter has a
            # window (default umask, commonly 644) where a plaintext secret
            # is world-readable on disk before the chmod lands.
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)

    def get_bytes(self, name: str) -> bytes:
        p = self._path(name)
        if not p.exists():
            raise ConfigError(
                f"secret '{name}' not found in {self.dir}. Seed it with: python -m skysync.secrets set {name}"
            )
        return _unprotect(p.read_bytes())

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

    ap = argparse.ArgumentParser(prog="skysync.secrets", description="Secret store")
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
        print(f"stored '{ns.name}' ({_BACKEND_LABEL}) in {store.dir}")
    elif ns.action == "delete":
        store.delete(ns.name)
        print(f"deleted '{ns.name}'")
    elif ns.action == "check":
        v = store.get(ns.name)  # raises if missing/undecryptable
        print(f"OK: '{ns.name}' decrypts ({len(v)} chars). Value not shown.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
