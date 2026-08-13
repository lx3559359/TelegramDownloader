from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MANIFEST_MAX_BYTES = 64 * 1024
SIGNATURE_MAX_BYTES = 4096
LATEST_MAX_BYTES = 16 * 1024
_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PUBLISHED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class UpdateContractError(ValueError):
    pass


class ManifestSignatureError(UpdateContractError):
    pass


class UpdatePolicyError(UpdateContractError):
    pass


class AssetVerificationError(UpdateContractError):
    pass


@dataclass(frozen=True, slots=True)
class AssetUrls:
    github: str
    modelscope: str


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    size: int
    sha256: str
    urls: AssetUrls


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    schema_version: int
    channel: str
    platform: str
    architecture: str
    version: str
    published_at: str
    minimum_updater_version: str
    key_id: str
    release_notes: str
    runtime: ReleaseAsset
    installer: ReleaseAsset


@dataclass(frozen=True, slots=True)
class VerifiedManifest:
    manifest: ReleaseManifest
    canonical: bytes
    signature: bytes


@dataclass(frozen=True, slots=True)
class LatestPointer:
    schema_version: int
    channel: str
    version: str


def canonical_json(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise UpdateContractError("更新清单无法规范化") from exc
    return encoded.encode("utf-8") + b"\n"


def parse_version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str) or len(value) > 64 or _VERSION.fullmatch(value) is None:
        raise UpdateContractError("版本号必须是严格的 X.Y.Z")
    return cast(tuple[int, int, int], tuple(int(part) for part in value.split(".")))


def parse_latest_pointer(content: bytes) -> LatestPointer:
    value = _strict_json(content, LATEST_MAX_BYTES, "版本指针")
    if canonical_json(value) != content:
        raise UpdateContractError("版本指针不是规范 JSON")
    _exact_keys(value, {"schemaVersion", "channel", "version"}, "版本指针")
    if value["schemaVersion"] != 1 or value["channel"] != "stable":
        raise UpdateContractError("版本指针协议无效")
    parse_version(value["version"])
    return LatestPointer(1, "stable", value["version"])


def parse_manifest(content: bytes) -> ReleaseManifest:
    value = _strict_json(content, MANIFEST_MAX_BYTES, "更新清单")
    if canonical_json(value) != content:
        raise UpdateContractError("更新清单不是规范 JSON")
    _exact_keys(
        value,
        {
            "schemaVersion",
            "channel",
            "platform",
            "architecture",
            "version",
            "publishedAt",
            "minimumUpdaterVersion",
            "keyId",
            "releaseNotes",
            "assets",
        },
        "更新清单",
    )
    if (
        value["schemaVersion"] != 1
        or value["channel"] != "stable"
        or value["platform"] != "windows"
        or value["architecture"] != "x64"
    ):
        raise UpdateContractError("更新清单平台或协议无效")
    version = value["version"]
    minimum = value["minimumUpdaterVersion"]
    parse_version(version)
    parse_version(minimum)
    published = value["publishedAt"]
    if not isinstance(published, str) or _PUBLISHED_AT.fullmatch(published) is None:
        raise UpdateContractError("发布时间格式无效")
    try:
        datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UpdateContractError("发布时间无效") from exc
    key_id = value["keyId"]
    if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
        raise UpdateContractError("签名密钥编号无效")
    notes = value["releaseNotes"]
    if not isinstance(notes, str) or not notes.strip() or len(notes) > 4000:
        raise UpdateContractError("发行说明摘要无效")

    assets = value["assets"]
    _exact_keys(assets, {"runtime", "installer"}, "更新资产")
    runtime = _parse_asset(assets["runtime"], version, "runtime")
    installer = _parse_asset(assets["installer"], version, "installer")
    return ReleaseManifest(
        1,
        "stable",
        "windows",
        "x64",
        version,
        published,
        minimum,
        key_id,
        notes,
        runtime,
        installer,
    )


def verify_manifest(
    manifest_bytes: bytes,
    signature_bytes: bytes,
    trusted_keys: Mapping[str, Ed25519PublicKey],
    *,
    installed_version: str | None = None,
) -> VerifiedManifest:
    manifest = parse_manifest(manifest_bytes)
    public_key = trusted_keys.get(manifest.key_id)
    if not isinstance(public_key, Ed25519PublicKey):
        raise ManifestSignatureError("更新清单使用了未知签名密钥")
    signature = _decode_signature(signature_bytes)
    try:
        public_key.verify(signature, manifest_bytes)
    except InvalidSignature as exc:
        raise ManifestSignatureError("更新清单签名无效") from exc
    if installed_version is not None:
        installed = parse_version(installed_version)
        if parse_version(manifest.version) < installed:
            raise UpdatePolicyError("拒绝安装低于当前版本的更新")
        if installed < parse_version(manifest.minimum_updater_version):
            raise UpdatePolicyError("当前更新器版本过低，无法安全应用该更新")
    return VerifiedManifest(manifest, manifest_bytes, signature)


def verify_asset(path: Path, expected_size: int, expected_sha256: str) -> None:
    if expected_size <= 0 or _SHA256.fullmatch(expected_sha256) is None:
        raise AssetVerificationError("资产校验参数无效")
    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        raise AssetVerificationError("更新资产不存在") from exc
    if actual_size != expected_size:
        raise AssetVerificationError(f"更新资产大小不符: 期望 {expected_size}，实际 {actual_size}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise AssetVerificationError("更新资产 SHA-256 不匹配")


def load_trusted_keys(path: Path) -> Mapping[str, Ed25519PublicKey]:
    value = _strict_json(path.read_bytes(), MANIFEST_MAX_BYTES, "可信密钥")
    if not isinstance(value, dict):
        raise UpdateContractError("可信密钥文件无效")
    keys: dict[str, Ed25519PublicKey] = {}
    for key_id, encoded in value.items():
        if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
            raise UpdateContractError("可信密钥编号无效")
        if not isinstance(encoded, str):
            raise UpdateContractError("可信公钥格式无效")
        try:
            der = base64.b64decode(encoded, validate=True)
            loaded = serialization.load_der_public_key(der)
        except (ValueError, TypeError) as exc:
            raise UpdateContractError("可信公钥格式无效") from exc
        if not isinstance(loaded, Ed25519PublicKey):
            raise UpdateContractError("可信公钥不是 Ed25519")
        keys[key_id] = loaded
    return MappingProxyType(keys)


def _parse_asset(value: object, version: str, kind: str) -> ReleaseAsset:
    _exact_keys(value, {"name", "size", "sha256", "urls"}, "更新资产")
    expected_name = (
        f"TelegramDownloader-{version}-win-x64-portable.zip"
        if kind == "runtime"
        else f"TelegramDownloader-{version}-win-x64-setup.exe"
    )
    name = value["name"]
    size = value["size"]
    digest = value["sha256"]
    if name != expected_name:
        raise UpdateContractError("更新资产名称与版本不一致")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise UpdateContractError("更新资产大小无效")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise UpdateContractError("更新资产 SHA-256 无效")
    urls = value["urls"]
    _exact_keys(urls, {"github", "modelscope"}, "更新资产 URL")
    github = _validated_asset_url(urls["github"], name, "github")
    modelscope = _validated_asset_url(urls["modelscope"], name, "modelscope")
    return ReleaseAsset(name, size, digest, AssetUrls(github, modelscope))


def _validated_asset_url(value: object, name: str, source: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or value != value.strip():
        raise UpdateContractError("更新资产 URL 无效")
    parsed = urlparse(value)
    allowed_hosts = {"github.com"} if source == "github" else {"modelscope.cn", "www.modelscope.cn"}
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.fragment
    ):
        raise UpdateContractError("更新资产 URL 来源无效")
    query_path = parse_qs(parsed.query).get("FilePath", [""])[0]
    remote_name = Path(unquote(query_path or parsed.path)).name
    if remote_name != name:
        raise UpdateContractError("更新资产 URL 与文件名不一致")
    return value


def _decode_signature(content: bytes) -> bytes:
    if not content or len(content) > SIGNATURE_MAX_BYTES:
        raise ManifestSignatureError("更新签名文件大小无效")
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ManifestSignatureError("更新签名编码无效") from exc
    if not text.endswith("\n") or "\n" in text[:-1] or "\r" in text:
        raise ManifestSignatureError("更新签名格式无效")
    encoded = text[:-1]
    try:
        signature = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ManifestSignatureError("更新签名格式无效") from exc
    if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != encoded:
        raise ManifestSignatureError("更新签名格式无效")
    return signature


def _strict_json(content: bytes, maximum: int, label: str) -> dict[str, Any]:
    if not content or len(content) > maximum or content.startswith(b"\xef\xbb\xbf"):
        raise UpdateContractError(f"{label}大小或编码无效")
    try:
        text = content.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_pairs_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateContractError(f"{label}不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise UpdateContractError(f"{label}顶层必须是对象")
    return value


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise UpdateContractError("JSON 包含重复字段")
        value[key] = item
    return value


def _exact_keys(value: object, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise UpdateContractError(f"{label}字段集合无效")
