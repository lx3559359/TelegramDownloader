from __future__ import annotations

import argparse
import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from telegram_downloader.update_contract import (
    canonical_json,
    load_trusted_keys,
    parse_version,
    verify_manifest,
)
from telegram_downloader.update_sources import GitHubSourceUrls, ModelScopeSourceUrls


class GenerateReleaseError(RuntimeError):
    pass


def generate_release_documents(
    *,
    version: str,
    published_at: str,
    release_notes: str,
    portable: Path,
    installer: Path,
    private_key: Path,
    trusted_keys: Path,
    key_id: str,
    output: Path,
) -> tuple[Path, Path, Path, Path]:
    parse_version(version)
    expected_portable = f"TelegramDownloader-{version}-win-x64-portable.zip"
    expected_installer = f"TelegramDownloader-{version}-win-x64-setup.exe"
    if portable.name != expected_portable or installer.name != expected_installer:
        raise GenerateReleaseError("发布包文件名与版本不一致")
    if not portable.is_file() or not installer.is_file():
        raise GenerateReleaseError("发布包不存在")
    if not release_notes.strip():
        raise GenerateReleaseError("发行说明不能为空")

    signing_key = _load_private_key(private_key)
    trusted = load_trusted_keys(trusted_keys)
    trusted_key = trusted.get(key_id)
    if trusted_key is None or _public_der(trusted_key) != _public_der(signing_key.public_key()):
        raise GenerateReleaseError("私钥与内置可信公钥不匹配")

    github = GitHubSourceUrls("lx3559359", "TelegramDownloader")
    modelscope = ModelScopeSourceUrls("lx3559359/TelegramDownloader")
    manifest_value = {
        "schemaVersion": 1,
        "channel": "stable",
        "platform": "windows",
        "architecture": "x64",
        "version": version,
        "publishedAt": published_at,
        "minimumUpdaterVersion": "0.1.0",
        "keyId": key_id,
        "releaseNotes": release_notes.strip(),
        "assets": {
            "runtime": _asset_value(portable, github, modelscope, version),
            "installer": _asset_value(installer, github, modelscope, version),
        },
    }
    manifest = canonical_json(manifest_value)
    signature = base64.b64encode(signing_key.sign(manifest)) + b"\n"
    latest = canonical_json({"schemaVersion": 1, "channel": "stable", "version": version})
    notes = release_notes.strip().encode("utf-8") + b"\n"
    verify_manifest(manifest, signature, trusted)

    output.mkdir(parents=True, exist_ok=True)
    paths = (
        output / "update-manifest.json",
        output / "update-manifest.sig",
        output / "latest.json",
        output / "release-notes.md",
    )
    for path, content in zip(paths, (manifest, signature, latest, notes), strict=True):
        _atomic_write(path, content)
    return paths


def _asset_value(
    path: Path,
    github: GitHubSourceUrls,
    modelscope: ModelScopeSourceUrls,
    version: str,
) -> dict[str, object]:
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "sha256": _sha256(path),
        "urls": {
            "github": github.asset(version, path.name),
            "modelscope": modelscope.asset(version, path.name),
        },
    }


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise GenerateReleaseError("无法读取 Ed25519 发布私钥") from exc
    if not isinstance(loaded, Ed25519PrivateKey):
        raise GenerateReleaseError("发布私钥不是 Ed25519")
    return loaded


def _public_der(key) -> bytes:
    return key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--published-at", required=True)
    parser.add_argument("--release-notes", type=Path, required=True)
    parser.add_argument("--portable", type=Path, required=True)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--trusted-keys", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        notes = arguments.release_notes.read_text(encoding="utf-8")
        generate_release_documents(
            version=arguments.version,
            published_at=arguments.published_at,
            release_notes=notes,
            portable=arguments.portable,
            installer=arguments.installer,
            private_key=arguments.private_key,
            trusted_keys=arguments.trusted_keys,
            key_id=arguments.key_id,
            output=arguments.output,
        )
    except (OSError, UnicodeError, ValueError, GenerateReleaseError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
