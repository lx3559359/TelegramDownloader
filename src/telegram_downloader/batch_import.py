from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from telegram_downloader.domain import ParsedLink
from telegram_downloader.links import InvalidTelegramLink, parse_telegram_link

MAX_BATCH_LINKS = 100
MAX_BATCH_TEXT_BYTES = 1024 * 1024
MAX_BATCH_MEDIA = 20_000


@dataclass(frozen=True, slots=True)
class BatchLinkIssue:
    line_number: int
    message: str


@dataclass(frozen=True, slots=True)
class BatchLinkCollection:
    input_count: int
    links: tuple[ParsedLink, ...]
    duplicate_count: int
    issues: tuple[BatchLinkIssue, ...]


def parse_batch_links(values: Iterable[str]) -> BatchLinkCollection:
    rows = [(line_number, value.strip()) for line_number, value in enumerate(values, 1)]
    rows = [(line_number, value) for line_number, value in rows if value]
    if len(rows) > MAX_BATCH_LINKS:
        raise ValueError(f"批量导入最多支持 {MAX_BATCH_LINKS} 条非空链接")

    links: list[ParsedLink] = []
    issues: list[BatchLinkIssue] = []
    seen: set[str] = set()
    duplicate_count = 0
    for line_number, value in rows:
        try:
            parsed = parse_telegram_link(value)
        except InvalidTelegramLink as error:
            issues.append(BatchLinkIssue(line_number, str(error)))
            continue
        if parsed.normalized_url in seen:
            duplicate_count += 1
            continue
        seen.add(parsed.normalized_url)
        links.append(parsed)

    if not links:
        raise ValueError("请至少输入一条有效的 t.me 链接")
    return BatchLinkCollection(
        len(rows),
        tuple(links),
        duplicate_count,
        tuple(issues),
    )


def read_batch_text_files(paths: Iterable[Path]) -> str:
    selected = tuple(Path(path) for path in paths)
    if not selected:
        raise ValueError("请选择 TXT 文件")
    chunks: list[str] = []
    total = 0
    for path in selected:
        if path.suffix.casefold() != ".txt" or not path.is_file():
            raise ValueError("批量导入只支持 TXT 文件")
        payload = path.read_bytes()
        total += len(payload)
        if total > MAX_BATCH_TEXT_BYTES:
            raise ValueError("TXT 文件合计不能超过 1 MiB")
        try:
            chunks.append(payload.decode("utf-8-sig"))
        except UnicodeDecodeError:
            try:
                chunks.append(payload.decode("gb18030"))
            except UnicodeDecodeError as error:
                raise ValueError(f"无法识别 TXT 文件编码：{path.name}") from error
    return "\n".join(chunk.rstrip("\r\n") for chunk in chunks if chunk)
