"""DPAPI secret store tests (Windows-only; the whole app is Windows-hosted)."""

from __future__ import annotations

import sys

import pytest

from skysync.errors import ConfigError

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")


def test_roundtrip_and_ciphertext_on_disk(tmp_path):
    from skysync.secrets import SecretStore

    store = SecretStore(tmp_path)
    store.set("skylight_password", "hunter2-unicode-✓")
    assert store.get("skylight_password") == "hunter2-unicode-✓"
    blob = (tmp_path / "skylight_password.bin").read_bytes()
    assert b"hunter2" not in blob  # never plaintext on disk


def test_missing_secret_message_names_the_seed_command(tmp_path):
    from skysync.secrets import SecretStore

    store = SecretStore(tmp_path)
    with pytest.raises(ConfigError, match="skysync.secrets set graph_client_secret"):
        store.get("graph_client_secret")
    assert store.get_optional("graph_client_secret") is None


def test_list_delete_and_name_validation(tmp_path):
    from skysync.secrets import SecretStore

    store = SecretStore(tmp_path)
    store.set("a_token", "x")
    store.set("b-token", "y")
    assert store.list_names() == ["a_token", "b-token"]
    store.delete("a_token")
    assert store.list_names() == ["b-token"]
    with pytest.raises(ConfigError):
        store.set("../evil", "z")
