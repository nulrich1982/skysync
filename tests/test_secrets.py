"""Secret store tests — platform backend differs (DPAPI on Windows, plain
file + restricted permissions on Linux), interface and behavior don't."""

from __future__ import annotations

import stat
import sys

import pytest

from skysync.errors import ConfigError
from skysync.secrets import SecretStore


def test_roundtrip(tmp_path):
    store = SecretStore(tmp_path)
    store.set("skylight_password", "hunter2-unicode-✓")
    assert store.get("skylight_password") == "hunter2-unicode-✓"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI-specific: ciphertext on disk")
def test_dpapi_ciphertext_on_disk(tmp_path):
    store = SecretStore(tmp_path)
    store.set("skylight_password", "hunter2-unicode-✓")
    blob = (tmp_path / "skylight_password.bin").read_bytes()
    assert b"hunter2" not in blob  # never plaintext on disk


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_linux_restrictive_permissions(tmp_path):
    store = SecretStore(tmp_path)
    store.set("skylight_password", "hunter2-unicode-✓")
    dir_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    file_mode = stat.S_IMODE((tmp_path / "skylight_password.bin").stat().st_mode)
    assert dir_mode == stat.S_IRWXU  # 700: owner-only
    assert file_mode == (stat.S_IRUSR | stat.S_IWUSR)  # 600: owner-only


def test_missing_secret_message_names_the_seed_command(tmp_path):
    store = SecretStore(tmp_path)
    with pytest.raises(ConfigError, match="skysync.secrets set graph_client_secret"):
        store.get("graph_client_secret")
    assert store.get_optional("graph_client_secret") is None


def test_list_delete_and_name_validation(tmp_path):
    store = SecretStore(tmp_path)
    store.set("a_token", "x")
    store.set("b-token", "y")
    assert store.list_names() == ["a_token", "b-token"]
    store.delete("a_token")
    assert store.list_names() == ["b-token"]
    with pytest.raises(ConfigError):
        store.set("../evil", "z")
