import os
from pathlib import Path

import pytest

from telegram_downloader.download_paths import (
    DownloadPathError,
    DownloadPathPolicy,
    probe_writable_directory,
)
from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import DownloadStorageSettings


def make_paths(tmp_path: Path) -> PortablePaths:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    return paths


def test_policy_defaults_to_portable_downloads(tmp_path) -> None:
    paths = make_paths(tmp_path)

    policy = DownloadPathPolicy(paths, DownloadStorageSettings())

    assert policy.current_root == paths.downloads.resolve()
    assert policy.roots == (paths.downloads.resolve(),)
    assert policy.guard(paths.downloads / "a.bin") == (paths.downloads / "a.bin").resolve()


def test_prepare_switches_root_and_trusts_previous_root(tmp_path) -> None:
    paths = make_paths(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())

    prepared = policy.prepare(DownloadStorageSettings(str(external)))

    assert prepared.root == str(external.resolve())
    assert str(paths.downloads.resolve()) in prepared.trusted_roots
    policy.apply(prepared)
    assert policy.guard(external / "new.bin") == (external / "new.bin").resolve()
    assert policy.guard(paths.downloads / "old.bin") == (paths.downloads / "old.bin").resolve()


@pytest.mark.parametrize("relative", ("escape.bin", "other/file.bin"))
def test_policy_rejects_targets_outside_trusted_roots(tmp_path, relative) -> None:
    paths = make_paths(tmp_path)
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())

    with pytest.raises(DownloadPathError, match="超出受信"):
        policy.guard(tmp_path / relative)


def test_policy_rejects_root_itself_unless_explicitly_allowed(tmp_path) -> None:
    paths = make_paths(tmp_path)
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())

    with pytest.raises(DownloadPathError):
        policy.guard(paths.downloads)
    assert policy.guard(paths.downloads, allow_root=True) == paths.downloads.resolve()


def test_policy_rejects_symlink_escape_from_trusted_root(tmp_path) -> None:
    paths = make_paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = paths.downloads / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"当前 Windows 环境不能创建测试符号链接: {error}")
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())

    with pytest.raises(DownloadPathError, match="超出受信"):
        policy.guard(link / "blocked.bin")


def test_prepare_rejects_application_data_and_filesystem_root(tmp_path) -> None:
    paths = make_paths(tmp_path)
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())

    with pytest.raises(DownloadPathError, match="应用内部数据"):
        policy.prepare(DownloadStorageSettings(str(paths.data)))
    with pytest.raises(DownloadPathError, match="应用内部数据"):
        policy.prepare(DownloadStorageSettings(str(paths.data / "media")))
    with pytest.raises(DownloadPathError, match="磁盘、共享或应用根目录"):
        policy.prepare(DownloadStorageSettings(str(paths.root)))
    with pytest.raises(DownloadPathError, match="磁盘、共享或应用根目录"):
        policy.prepare(DownloadStorageSettings(Path(tmp_path.anchor).as_posix()))


def test_prepare_rejects_relative_root(tmp_path) -> None:
    policy = DownloadPathPolicy(make_paths(tmp_path), DownloadStorageSettings())

    with pytest.raises(DownloadPathError, match="绝对路径"):
        policy.prepare(DownloadStorageSettings("relative/media"))


def test_prepare_probes_selected_directory_once(tmp_path) -> None:
    paths = make_paths(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    calls: list[Path] = []
    policy = DownloadPathPolicy(
        paths,
        DownloadStorageSettings(),
        probe=lambda root: calls.append(root),
    )

    prepared = policy.prepare(DownloadStorageSettings(str(external)))

    assert calls == [external.resolve()]
    assert prepared.root == str(external.resolve())


def test_prepare_surfaces_probe_failure_without_changing_policy(tmp_path) -> None:
    paths = make_paths(tmp_path)
    external = tmp_path / "external"
    external.mkdir()

    def reject(_root: Path) -> None:
        raise DownloadPathError("下载根目录当前不可写")

    policy = DownloadPathPolicy(paths, DownloadStorageSettings(), probe=reject)

    with pytest.raises(DownloadPathError, match="当前不可写"):
        policy.prepare(DownloadStorageSettings(str(external)))
    assert policy.current_root == paths.downloads.resolve()


def test_saved_offline_root_is_kept_until_writability_is_required(tmp_path) -> None:
    paths = make_paths(tmp_path)
    missing = tmp_path / "offline-drive" / "media"
    settings = DownloadStorageSettings(str(missing.resolve()))

    policy = DownloadPathPolicy(paths, settings)

    assert policy.current_root == missing.resolve()
    with pytest.raises(DownloadPathError, match="不存在"):
        policy.require_current_writable()


def test_default_probe_creates_and_removes_exclusive_file(tmp_path) -> None:
    root = tmp_path / "media"
    root.mkdir()

    probe_writable_directory(root)

    assert list(root.iterdir()) == []


def test_default_probe_reports_create_failure(tmp_path, monkeypatch) -> None:
    root = tmp_path / "media"
    root.mkdir()

    def fail_open(_path: Path, *_args, **_kwargs):
        raise OSError("read only")

    monkeypatch.setattr(Path, "open", fail_open)

    with pytest.raises(DownloadPathError, match="当前不可写"):
        probe_writable_directory(root)


def test_default_probe_reports_cleanup_failure(tmp_path, monkeypatch) -> None:
    root = tmp_path / "media"
    root.mkdir()

    def fail_unlink(_path: Path, *_args, **_kwargs) -> None:
        raise OSError("locked")

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    with pytest.raises(DownloadPathError, match="无法清理"):
        probe_writable_directory(root)


def test_root_ids_are_stable_and_restore_after_restart(tmp_path) -> None:
    paths = make_paths(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    first = DownloadPathPolicy(paths, DownloadStorageSettings())
    prepared = first.prepare(DownloadStorageSettings(str(external)))
    first.apply(prepared)

    restarted = DownloadPathPolicy(paths, prepared)
    root_id = first.root_id(external)

    assert root_id.startswith("download-")
    assert len(root_id) == len("download-") + 16
    assert root_id == restarted.root_id(external)
    assert restarted.root_for_id(root_id) == external.resolve()
    assert restarted.guard(paths.downloads / "old.bin") == (paths.downloads / "old.bin").resolve()
    with pytest.raises(DownloadPathError, match="标识不受信"):
        restarted.root_for_id("download-0000000000000000")


def test_root_history_is_normalized_and_deduplicated(tmp_path) -> None:
    paths = make_paths(tmp_path)
    current = tmp_path / "current"
    previous = tmp_path / "previous"
    current.mkdir()
    previous.mkdir()
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())
    repeated = str(previous / ".." / previous.name)

    prepared = policy.prepare(
        DownloadStorageSettings(
            str(current),
            (str(previous.resolve()), repeated, str(paths.downloads.resolve())),
        )
    )

    normalized = [os.path.normcase(value) for value in prepared.trusted_roots]
    assert len(normalized) == len(set(normalized))
    assert os.path.normcase(str(paths.downloads.resolve())) in normalized
