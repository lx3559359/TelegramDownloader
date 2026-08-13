from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote, urlencode

from telegram_downloader.update_contract import (
    ReleaseManifest,
    VerifiedManifest,
    parse_version,
)

_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class UpdateSourceId(StrEnum):
    GITHUB = "github"
    MODELSCOPE = "modelscope"


class SourceStatus(StrEnum):
    VALID = "valid"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class SourceReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SourceCheck:
    source: UpdateSourceId
    status: SourceStatus
    latency_ms: float
    verified: VerifiedManifest | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciledUpdate:
    version: str | None
    manifest: ReleaseManifest | None
    available_sources: tuple[UpdateSourceId, ...]
    blocked: bool


@dataclass(frozen=True, slots=True)
class GitHubSourceUrls:
    owner: str
    repository: str

    def __post_init__(self) -> None:
        _safe_segment(self.owner)
        _safe_segment(self.repository)

    def latest(self) -> str:
        return (
            "https://github.com/"
            f"{quote(self.owner)}/{quote(self.repository)}/releases/latest/download/"
            "latest.json"
        )

    def manifest(self, version: str) -> str:
        _version(version)
        return self._release_asset(f"v{version}", "update-manifest.json")

    def signature(self, version: str) -> str:
        _version(version)
        return self._release_asset(f"v{version}", "update-manifest.sig")

    def asset(self, version: str, name: str) -> str:
        _version(version)
        _safe_asset_name(name)
        return self._release_asset(f"v{version}", name)

    def _release_asset(self, tag: str, name: str) -> str:
        return (
            "https://github.com/"
            f"{quote(self.owner)}/{quote(self.repository)}/releases/download/"
            f"{quote(tag)}/{quote(name)}"
        )


@dataclass(frozen=True, slots=True)
class ModelScopeSourceUrls:
    repo_id: str
    revision: str = "main"
    endpoint: str = "https://www.modelscope.cn"

    def __post_init__(self) -> None:
        parts = self.repo_id.split("/")
        if len(parts) != 2:
            raise ValueError("魔搭仓库必须是 owner/repository")
        _safe_segment(parts[0])
        _safe_segment(parts[1])
        _safe_segment(self.revision)
        if self.endpoint != "https://www.modelscope.cn":
            raise ValueError("魔搭更新端点无效")

    def latest(self) -> str:
        return self.file("releases/stable/latest.json")

    def manifest(self, version: str) -> str:
        _version(version)
        return self.file(f"releases/stable/{version}/update-manifest.json")

    def signature(self, version: str) -> str:
        _version(version)
        return self.file(f"releases/stable/{version}/update-manifest.sig")

    def asset(self, version: str, name: str) -> str:
        _version(version)
        _safe_asset_name(name)
        return self.file(f"releases/stable/{version}/{name}")

    def file(self, remote_path: str) -> str:
        owner, repository = self.repo_id.split("/")
        query = urlencode({"Revision": self.revision, "FilePath": remote_path})
        return f"{self.endpoint}/api/v1/models/{quote(owner)}/{quote(repository)}/repo?{query}"


def reconcile_sources(
    checks: tuple[SourceCheck, SourceCheck],
    current_version: str,
) -> ReconciledUpdate:
    parse_version(current_version)
    if {check.source for check in checks} != {
        UpdateSourceId.GITHUB,
        UpdateSourceId.MODELSCOPE,
    }:
        raise SourceReconciliationError("更新来源集合无效")
    if any(check.latency_ms < 0 for check in checks):
        raise SourceReconciliationError("更新来源延迟无效")
    if any(check.status is SourceStatus.INVALID for check in checks):
        raise SourceReconciliationError("更新来源返回了无效签名或不一致内容")

    valid = [
        check
        for check in checks
        if check.status is SourceStatus.VALID and check.verified is not None
    ]
    if not valid:
        return ReconciledUpdate(None, None, (), True)
    if len(valid) == 2:
        left, right = valid
        left_version = parse_version(left.verified.manifest.version)
        right_version = parse_version(right.verified.manifest.version)
        if left_version == right_version:
            if (
                left.verified.canonical != right.verified.canonical
                or left.verified.signature != right.verified.signature
            ):
                raise SourceReconciliationError("两个更新来源的同版本清单不一致")
            selected = left.verified
            sources = tuple(
                check.source for check in sorted(valid, key=lambda item: item.latency_ms)
            )
        else:
            winner = left if left_version > right_version else right
            selected = winner.verified
            sources = (winner.source,)
    else:
        selected_check = valid[0]
        selected = selected_check.verified
        sources = (selected_check.source,)

    if parse_version(selected.manifest.version) <= parse_version(current_version):
        return ReconciledUpdate(None, None, sources, False)
    return ReconciledUpdate(
        selected.manifest.version,
        selected.manifest,
        sources,
        False,
    )


def _safe_segment(value: str) -> None:
    if value in {"", ".", ".."} or _SEGMENT.fullmatch(value) is None:
        raise ValueError("更新来源路径段无效")


def _safe_asset_name(value: str) -> None:
    if "/" in value or "\\" in value:
        raise ValueError("更新资产名无效")
    _safe_segment(value)


def _version(value: str) -> None:
    parse_version(value)
