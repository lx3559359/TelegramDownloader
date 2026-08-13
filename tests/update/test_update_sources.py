from dataclasses import replace

import pytest

from telegram_downloader.update_contract import VerifiedManifest
from telegram_downloader.update_sources import (
    GitHubSourceUrls,
    ModelScopeSourceUrls,
    SourceCheck,
    SourceReconciliationError,
    SourceStatus,
    UpdateSourceId,
    reconcile_sources,
)
from tests.update.test_update_contract import manifest_value, signed_manifest


def verified(version: str) -> VerifiedManifest:
    content, signature_file, keys = signed_manifest(manifest_value(version))
    from telegram_downloader.update_contract import verify_manifest

    return verify_manifest(content, signature_file, keys)


def valid(source, value, latency=10):
    return SourceCheck(source, SourceStatus.VALID, latency, value)


def unavailable(source):
    return SourceCheck(source, SourceStatus.UNAVAILABLE, 20, error="offline")


def test_matching_sources_are_both_available_in_latency_order() -> None:
    release = verified("0.2.0")
    checks = (
        valid(UpdateSourceId.GITHUB, release, 30),
        valid(UpdateSourceId.MODELSCOPE, release, 10),
    )

    result = reconcile_sources(checks, "0.1.0")

    assert result.version == "0.2.0"
    assert result.available_sources == (
        UpdateSourceId.MODELSCOPE,
        UpdateSourceId.GITHUB,
    )


def test_single_valid_source_can_proceed_when_other_is_unavailable() -> None:
    result = reconcile_sources(
        (
            valid(UpdateSourceId.GITHUB, verified("0.2.0")),
            unavailable(UpdateSourceId.MODELSCOPE),
        ),
        "0.1.0",
    )

    assert result.available_sources == (UpdateSourceId.GITHUB,)
    assert result.blocked is False


def test_same_version_content_conflict_and_invalid_source_fail_closed() -> None:
    first = verified("0.2.0")
    conflict = replace(first, canonical=first.canonical + b" ")
    with pytest.raises(SourceReconciliationError):
        reconcile_sources(
            (
                valid(UpdateSourceId.GITHUB, first),
                valid(UpdateSourceId.MODELSCOPE, conflict),
            ),
            "0.1.0",
        )
    with pytest.raises(SourceReconciliationError):
        reconcile_sources(
            (
                valid(UpdateSourceId.GITHUB, first),
                SourceCheck(UpdateSourceId.MODELSCOPE, SourceStatus.INVALID, 1),
            ),
            "0.1.0",
        )


def test_newer_of_two_valid_versions_is_selected() -> None:
    result = reconcile_sources(
        (
            valid(UpdateSourceId.GITHUB, verified("0.2.0")),
            valid(UpdateSourceId.MODELSCOPE, verified("0.3.0")),
        ),
        "0.1.0",
    )

    assert result.version == "0.3.0"
    assert result.available_sources == (UpdateSourceId.MODELSCOPE,)


def test_source_url_builders_use_public_release_and_modelscope_api_paths() -> None:
    github = GitHubSourceUrls("lx3559359", "TelegramDownloader")
    modelscope = ModelScopeSourceUrls("lx3559359/TelegramDownloader")

    assert github.latest() == (
        "https://github.com/lx3559359/TelegramDownloader/releases/latest/download/latest.json"
    )
    assert github.manifest("1.2.3").endswith("/releases/download/v1.2.3/update-manifest.json")
    assert "Revision=main" in modelscope.latest()
    assert "FilePath=releases%2Fstable%2F1.2.3%2Fupdate-manifest.sig" in (
        modelscope.signature("1.2.3")
    )
