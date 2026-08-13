import base64
import hashlib
import json
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from telegram_downloader.update_contract import (
    AssetVerificationError,
    ManifestSignatureError,
    UpdateContractError,
    UpdatePolicyError,
    canonical_json,
    parse_latest_pointer,
    verify_asset,
    verify_manifest,
)


def manifest_value(version: str = "0.2.0") -> dict:
    runtime = f"TelegramDownloader-{version}-win-x64-portable.zip"
    installer = f"TelegramDownloader-{version}-win-x64-setup.exe"
    return {
        "schemaVersion": 1,
        "channel": "stable",
        "platform": "windows",
        "architecture": "x64",
        "version": version,
        "publishedAt": "2026-08-13T12:00:00Z",
        "minimumUpdaterVersion": "0.1.0",
        "keyId": "release-test",
        "releaseNotes": "首个在线更新测试版本",
        "assets": {
            "runtime": {
                "name": runtime,
                "size": 10,
                "sha256": "a" * 64,
                "urls": {
                    "github": f"https://github.com/lx3559359/TelegramDownloader/releases/download/v{version}/{runtime}",
                    "modelscope": "https://www.modelscope.cn/api/v1/models/lx3559359/TelegramDownloader/repo"
                    f"?Revision=main&FilePath=releases%2Fstable%2F{version}%2F{runtime}",
                },
            },
            "installer": {
                "name": installer,
                "size": 20,
                "sha256": "b" * 64,
                "urls": {
                    "github": f"https://github.com/lx3559359/TelegramDownloader/releases/download/v{version}/{installer}",
                    "modelscope": "https://www.modelscope.cn/api/v1/models/lx3559359/TelegramDownloader/repo"
                    f"?Revision=main&FilePath=releases%2Fstable%2F{version}%2F{installer}",
                },
            },
        },
    }


def signed_manifest(value: dict):
    private = Ed25519PrivateKey.generate()
    content = canonical_json(value)
    signature = base64.b64encode(private.sign(content)) + b"\n"
    return content, signature, {"release-test": private.public_key()}


def test_verifies_canonical_ed25519_manifest() -> None:
    content, signature, keys = signed_manifest(manifest_value())

    verified = verify_manifest(content, signature, keys, installed_version="0.1.0")

    assert verified.manifest.version == "0.2.0"
    assert verified.manifest.runtime.size == 10


@pytest.mark.parametrize("mutation", ["signature", "unknown-key", "extra-field"])
def test_rejects_tampering_unknown_keys_and_extra_fields(mutation: str) -> None:
    value = manifest_value()
    content, signature, keys = signed_manifest(value)
    if mutation == "signature":
        signature = base64.b64encode(b"x" * 64) + b"\n"
    elif mutation == "unknown-key":
        keys = {}
    else:
        altered = deepcopy(value)
        altered["unexpected"] = True
        content = canonical_json(altered)

    with pytest.raises((ManifestSignatureError, UpdateContractError)):
        verify_manifest(content, signature, keys)


def test_rejects_noncanonical_json_duplicate_fields_and_bom() -> None:
    value = manifest_value()
    content, signature, keys = signed_manifest(value)

    with pytest.raises(UpdateContractError):
        verify_manifest(json.dumps(value).encode(), signature, keys)
    with pytest.raises(UpdateContractError):
        parse_latest_pointer(
            b'{"schemaVersion":1,"channel":"stable","version":"1.0.0","version":"2.0.0"}'
        )
    with pytest.raises(UpdateContractError):
        parse_latest_pointer(b"\xef\xbb\xbf{}")


def test_rejects_downgrade_and_incompatible_updater() -> None:
    value = manifest_value("0.1.0")
    content, signature, keys = signed_manifest(value)
    with pytest.raises(UpdatePolicyError):
        verify_manifest(content, signature, keys, installed_version="0.2.0")

    value = manifest_value("0.3.0")
    value["minimumUpdaterVersion"] = "0.2.0"
    content, signature, keys = signed_manifest(value)
    with pytest.raises(UpdatePolicyError):
        verify_manifest(content, signature, keys, installed_version="0.1.0")


def test_asset_verification_checks_size_and_sha256(tmp_path) -> None:
    asset = tmp_path / "asset.zip"
    asset.write_bytes(b"runtime")
    digest = hashlib.sha256(b"runtime").hexdigest()

    verify_asset(asset, 7, digest)
    with pytest.raises(AssetVerificationError):
        verify_asset(asset, 8, digest)
    with pytest.raises(AssetVerificationError):
        verify_asset(asset, 7, "0" * 64)


def test_latest_pointer_is_strict() -> None:
    pointer = canonical_json({"schemaVersion": 1, "channel": "stable", "version": "1.2.3"})
    assert parse_latest_pointer(pointer).version == "1.2.3"

    with pytest.raises(UpdateContractError):
        parse_latest_pointer(b'{"schemaVersion": 1, "channel": "stable", "version": "1.2.3"}')


@pytest.mark.parametrize("version", ["1.2", "1.2.3-rc1", "01.2.3", "v1.2.3"])
def test_rejects_non_release_versions(version: str) -> None:
    value = manifest_value()
    value["version"] = version
    content, signature, keys = signed_manifest(value)

    with pytest.raises(UpdateContractError):
        verify_manifest(content, signature, keys)


def test_rejects_oversized_documents() -> None:
    with pytest.raises(UpdateContractError):
        parse_latest_pointer(b"{" + b" " * (16 * 1024) + b"}")
