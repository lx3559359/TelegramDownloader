import pytest

from telegram_downloader.security import SecretsError, SecretsVault


class ReverseProtector:
    def protect(self, value: bytes) -> bytes:
        return value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        return value[::-1]


def test_secrets_are_not_stored_as_plaintext(tmp_path) -> None:
    path = tmp_path / "config" / "secrets.dat"
    vault = SecretsVault(path, ReverseProtector())

    vault.save({"api_hash": "secret-hash", "session": "session-value"})

    assert b"secret-hash" not in path.read_bytes()
    assert vault.load()["session"] == "session-value"
    assert not path.with_suffix(".dat.tmp").exists()


def test_missing_vault_is_empty_and_tampering_is_reported(tmp_path) -> None:
    path = tmp_path / "secrets.dat"
    vault = SecretsVault(path, ReverseProtector())
    assert vault.load() == {}
    path.write_bytes(b"not-valid-encrypted-json")

    with pytest.raises(SecretsError):
        vault.load()


def test_vault_accepts_only_string_keys_and_values(tmp_path) -> None:
    vault = SecretsVault(tmp_path / "secrets.dat", ReverseProtector())

    with pytest.raises(SecretsError):
        vault.save({"api_hash": 123})  # type: ignore[dict-item]
