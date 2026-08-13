from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from telegram_downloader.update_contract import parse_latest_pointer, parse_version


class GitHubPublishError(RuntimeError):
    pass


def github_asset_names(version: str) -> tuple[str, ...]:
    parse_version(version)
    return (
        f"TelegramDownloader-{version}-source.zip",
        f"TelegramDownloader-{version}-win-x64-portable.zip",
        f"TelegramDownloader-{version}-win-x64-setup.exe",
        "release-notes.md",
        "update-manifest.json",
        "update-manifest.sig",
        "latest.json",
    )


class GitHubPublisher:
    def __init__(self, repository: str, version: str, source: Path, workspace: Path) -> None:
        self.repository = repository
        self.version = version
        self.tag = f"v{version}"
        self.source = source
        self.workspace = workspace

    def stage(self) -> None:
        files = self._validated_source()
        view = self._run(
            ["gh", "release", "view", self.tag, "--repo", self.repository, "--json", "isDraft"],
            check=False,
        )
        if view.returncode != 0:
            self._run(
                [
                    "gh",
                    "release",
                    "create",
                    self.tag,
                    "--repo",
                    self.repository,
                    "--draft",
                    "--verify-tag",
                    "--title",
                    f"TelegramDownloader {self.version}",
                    "--notes-file",
                    str(self.source / "release-notes.md"),
                ]
            )
        else:
            value = json.loads(view.stdout)
            if value.get("isDraft") is not True:
                raise GitHubPublishError("GitHub release already published")
        self._run(
            [
                "gh",
                "release",
                "upload",
                self.tag,
                "--repo",
                self.repository,
                "--clobber",
                *(str(path) for path in files),
            ]
        )

    def verify(self) -> None:
        result = self._run(
            [
                "gh",
                "release",
                "view",
                self.tag,
                "--repo",
                self.repository,
                "--json",
                "assets,isDraft",
            ]
        )
        value = json.loads(result.stdout)
        actual = sorted(asset["name"] for asset in value.get("assets", []))
        if value.get("isDraft") is not True or actual != sorted(github_asset_names(self.version)):
            raise GitHubPublishError("GitHub draft asset set is invalid")

    def save_pointer(self) -> bytes:
        return b"draft\n"

    def promote(self) -> None:
        self._run(
            [
                "gh",
                "release",
                "edit",
                self.tag,
                "--repo",
                self.repository,
                "--draft=false",
                "--latest",
            ]
        )

    def restore(self, previous: bytes) -> None:
        del previous
        self.demote()

    def demote(self) -> None:
        release = self._run(
            [
                "gh",
                "release",
                "view",
                self.tag,
                "--repo",
                self.repository,
                "--json",
                "databaseId",
            ]
        )
        release_id = json.loads(release.stdout).get("databaseId")
        if not isinstance(release_id, int):
            raise GitHubPublishError("GitHub release id is invalid")
        self._run(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{self.repository}/releases/{release_id}",
                "-F",
                "draft=true",
            ]
        )

    def download(self, destination: Path) -> None:
        _empty_directory(destination, self.workspace)
        for name in github_asset_names(self.version):
            self._run(
                [
                    "gh",
                    "release",
                    "download",
                    self.tag,
                    "--repo",
                    self.repository,
                    "--pattern",
                    name,
                    "--dir",
                    str(destination),
                ]
            )

    def verify_pointer(self) -> None:
        destination = self.workspace / ".local" / "temp" / "github-pointer"
        _empty_directory(destination, self.workspace)
        self._run(
            [
                "gh",
                "release",
                "download",
                self.tag,
                "--repo",
                self.repository,
                "--pattern",
                "latest.json",
                "--dir",
                str(destination),
            ]
        )
        if parse_latest_pointer((destination / "latest.json").read_bytes()).version != self.version:
            raise GitHubPublishError("GitHub latest pointer mismatch")

    def _validated_source(self) -> tuple[Path, ...]:
        names = github_asset_names(self.version)
        if not self.source.is_dir() or sorted(
            path.name for path in self.source.iterdir()
        ) != sorted(names):
            raise GitHubPublishError("GitHub source asset set is invalid")
        files = tuple(self.source / name for name in names)
        if any(not path.is_file() or path.stat().st_size <= 0 for path in files):
            raise GitHubPublishError("GitHub source asset is invalid")
        return files

    @staticmethod
    def _run(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            arguments, check=False, capture_output=True, text=True, encoding="utf-8"
        )
        if check and result.returncode != 0:
            raise GitHubPublishError(f"GitHub command failed ({arguments[1]})")
        return result


def _empty_directory(path: Path, workspace: Path) -> None:
    resolved_workspace = workspace.resolve(strict=True)
    resolved = path.resolve()
    try:
        resolved.relative_to(resolved_workspace)
    except ValueError as exc:
        raise GitHubPublishError("GitHub download path escaped workspace") from exc
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=("stage", "verify", "download", "promote", "demote", "verify-pointer"),
    )
    parser.add_argument("--repository", default="lx3559359/TelegramDownloader")
    parser.add_argument("--version", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--workspace", type=Path, default=Path(os.environ.get("GITHUB_WORKSPACE", "."))
    )
    parser.add_argument("--destination", type=Path)
    arguments = parser.parse_args()
    publisher = GitHubPublisher(
        arguments.repository,
        arguments.version,
        arguments.source.resolve(),
        arguments.workspace.resolve(),
    )
    try:
        if arguments.operation == "stage":
            publisher.stage()
        elif arguments.operation == "verify":
            publisher.verify()
        elif arguments.operation == "download":
            if arguments.destination is None:
                raise GitHubPublishError("GitHub download destination missing")
            publisher.download(arguments.destination)
        elif arguments.operation == "promote":
            publisher.promote()
        elif arguments.operation == "demote":
            publisher.demote()
        else:
            publisher.verify_pointer()
    except (OSError, ValueError, GitHubPublishError, json.JSONDecodeError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
