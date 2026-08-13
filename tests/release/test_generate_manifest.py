from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.release.generate_manifest import GenerateReleaseError, generate_release_documents
from telegram_downloader.update_contract import load_trusted_keys, verify_manifest


def key_files(tmp_path: Path, *, trusted: bool = True) -> tuple[Path, Path]:
    private = Ed25519PrivateKey.generate()
    private_path = tmp_path / "private.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if not trusted:
        public = (
            Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
    trusted_path = tmp_path / "trusted.json"
    trusted_path.write_text(
        json.dumps({"release-2026-01": base64.b64encode(public).decode()}) + "\n",
        encoding="utf-8",
    )
    return private_path, trusted_path


def artifacts(tmp_path: Path, version: str = "0.1.0") -> tuple[Path, Path]:
    portable = tmp_path / f"TelegramDownloader-{version}-win-x64-portable.zip"
    installer = tmp_path / f"TelegramDownloader-{version}-win-x64-setup.exe"
    portable.write_bytes(b"portable-runtime")
    installer.write_bytes(b"installer")
    return portable, installer


def test_release_documents_are_deterministic_and_verifiable(tmp_path) -> None:
    private, trusted = key_files(tmp_path)
    portable, installer = artifacts(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"

    for output in (first, second):
        generate_release_documents(
            version="0.1.0",
            published_at="2026-08-13T12:00:00Z",
            release_notes="首个正式版本",
            portable=portable,
            installer=installer,
            private_key=private,
            trusted_keys=trusted,
            key_id="release-2026-01",
            output=output,
        )

    for name in ("update-manifest.json", "update-manifest.sig", "latest.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    verified = verify_manifest(
        (first / "update-manifest.json").read_bytes(),
        (first / "update-manifest.sig").read_bytes(),
        load_trusted_keys(trusted),
    )
    assert verified.manifest.runtime.size == len(b"portable-runtime")
    assert verified.manifest.installer.size == len(b"installer")


def test_generation_rejects_private_key_not_matching_trusted_key(tmp_path) -> None:
    private, trusted = key_files(tmp_path, trusted=False)
    portable, installer = artifacts(tmp_path)

    with pytest.raises(GenerateReleaseError):
        generate_release_documents(
            version="0.1.0",
            published_at="2026-08-13T12:00:00Z",
            release_notes="首个正式版本",
            portable=portable,
            installer=installer,
            private_key=private,
            trusted_keys=trusted,
            key_id="release-2026-01",
            output=tmp_path / "release",
        )


def test_generation_rejects_package_version_mismatch(tmp_path) -> None:
    private, trusted = key_files(tmp_path)
    portable, installer = artifacts(tmp_path, "0.2.0")

    with pytest.raises(GenerateReleaseError):
        generate_release_documents(
            version="0.1.0",
            published_at="2026-08-13T12:00:00Z",
            release_notes="首个正式版本",
            portable=portable,
            installer=installer,
            private_key=private,
            trusted_keys=trusted,
            key_id="release-2026-01",
            output=tmp_path / "release",
        )


def test_no_tracked_file_contains_private_key_material() -> None:
    root = Path(__file__).parents[2]
    import subprocess

    tracked = subprocess.check_output(
        ["git", "ls-files"], cwd=root, text=True, encoding="utf-8"
    ).splitlines()
    private_marker = b"-----BEGIN " + b"PRIVATE KEY-----"
    for relative in tracked:
        path = root / relative
        if path.is_file():
            assert private_marker not in path.read_bytes()
