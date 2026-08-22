from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from string import Formatter

from telegram_downloader.domain import MediaKind

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}
_ARCHIVE_MIMES = {
    "application/zip",
    "application/x-7z-compressed",
    "application/vnd.rar",
    "application/x-rar-compressed",
    "application/x-tar",
    "application/gzip",
}
_DIRECTORY_FIELDS = frozenset(
    {
        "source",
        "year",
        "month",
        "year_month",
        "media_type",
        "message_date",
        "message_id",
    }
)
_FILENAME_FIELDS = _DIRECTORY_FIELDS | {
    "original_name",
    "stem",
    "extension",
}
_EXTENSION_FIELDS = frozenset({"original_name", "extension"})
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
MAX_DOWNLOAD_RELATIVE_LENGTH = 220


@dataclass(frozen=True, slots=True)
class DownloadNamingSettings:
    directory_template: str = "{source}/{year_month}/{media_type}"
    filename_template: str = "{original_name}"

    def __post_init__(self) -> None:
        directory_fields = _validate_template(
            self.directory_template,
            _DIRECTORY_FIELDS,
            "目录模板",
            maximum=500,
        )
        segments = self.directory_template.split("/")
        if (
            self.directory_template.startswith("/")
            or _WINDOWS_DRIVE.match(self.directory_template)
            or "\\" in self.directory_template
            or len(segments) > 8
            or any(not segment or segment in {".", ".."} for segment in segments)
        ):
            raise ValueError("目录模板必须是最多 8 层的安全相对路径")
        _ = directory_fields

        filename_fields = _validate_template(
            self.filename_template,
            _FILENAME_FIELDS,
            "文件名模板",
            maximum=200,
        )
        if "/" in self.filename_template or "\\" in self.filename_template:
            raise ValueError("文件名模板不能包含路径分隔符")
        if not filename_fields & _EXTENSION_FIELDS:
            raise ValueError("文件名模板必须保留原始文件名或扩展名")


def _validate_template(
    template: str,
    allowed_fields: frozenset[str],
    label: str,
    *,
    maximum: int,
) -> frozenset[str]:
    if not isinstance(template, str) or not template.strip():
        raise ValueError(f"{label}不能为空")
    if len(template) > maximum:
        raise ValueError(f"{label}不能超过 {maximum} 个字符")
    fields: set[str] = set()
    try:
        parsed = Formatter().parse(template)
        for _literal, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if field_name not in allowed_fields:
                raise ValueError(f"{label}包含不支持的占位符：{field_name}")
            if format_spec or conversion:
                raise ValueError(f"{label}不支持格式说明或转换")
            fields.add(field_name)
    except (KeyError, ValueError) as error:
        if isinstance(error, ValueError) and str(error).startswith(label):
            raise
        raise ValueError(f"{label}格式无效") from error
    return frozenset(fields)


def sanitize_component(value: str, maximum: int = 120) -> str:
    if maximum < 1:
        raise ValueError("maximum 必须大于零")

    cleaned = _ILLEGAL.sub("_", value).strip().rstrip(" .") or "unnamed"
    reserved_stem = cleaned.split(".", 1)[0].upper()
    if reserved_stem in _RESERVED:
        cleaned = f"_{cleaned}_"
    if len(cleaned) <= maximum:
        return cleaned

    suffix = Path(cleaned).suffix
    if suffix and len(suffix) < maximum:
        cleaned = cleaned[: maximum - len(suffix)].rstrip(" .") + suffix
    else:
        cleaned = cleaned[:maximum].rstrip(" .")
    return cleaned or "unnamed"


def classify_media(
    mime: str | None,
    name: str,
    photo: bool,
    voice: bool,
    video: bool,
) -> MediaKind:
    normalized_mime = (mime or "").lower()
    suffix = Path(name).suffix.lower()
    if suffix in _ARCHIVE_EXTENSIONS or normalized_mime in _ARCHIVE_MIMES:
        return MediaKind.ARCHIVE
    if photo:
        return MediaKind.PHOTO
    if voice:
        return MediaKind.VOICE
    if video or normalized_mime.startswith("video/"):
        return MediaKind.VIDEO
    if normalized_mime.startswith("audio/"):
        return MediaKind.AUDIO
    return MediaKind.DOCUMENT


def archive_target(
    root: Path,
    source_title: str,
    date: datetime,
    kind: MediaKind,
    name: str,
) -> Path:
    safe_source = sanitize_component(source_title)
    safe_name = sanitize_component(name)
    return root / safe_source / date.strftime("%Y-%m") / kind.value / safe_name


def render_download_target(
    root: Path,
    naming: DownloadNamingSettings,
    source_title: str,
    date: datetime,
    kind: MediaKind,
    message_id: int,
    original_name: str,
) -> Path:
    if not isinstance(naming, DownloadNamingSettings):
        raise ValueError("下载命名设置无效")
    original = Path(original_name)
    values = {
        "source": source_title,
        "year": date.strftime("%Y"),
        "month": date.strftime("%m"),
        "year_month": date.strftime("%Y-%m"),
        "media_type": kind.value,
        "message_date": date.strftime("%Y-%m-%d"),
        "message_id": str(message_id),
        "original_name": original_name,
        "stem": original.stem,
        "extension": original.suffix,
    }
    directory = tuple(
        sanitize_component(segment.format_map(values), maximum=64)
        for segment in naming.directory_template.split("/")
    )
    filename = sanitize_component(
        naming.filename_template.format_map(values),
        maximum=120,
    )
    relative = Path(*directory, filename)
    if len(relative.as_posix()) > MAX_DOWNLOAD_RELATIVE_LENGTH:
        raise ValueError("模板生成的下载相对路径不能超过 220 个字符")
    safe_root = root.resolve()
    target = (safe_root / relative).resolve()
    try:
        target.relative_to(safe_root)
    except ValueError as error:
        raise ValueError("模板生成的路径超出下载目录") from error
    return target


def disambiguate_target(target: Path, message_id: int) -> Path:
    suffix = f"__{message_id}"
    if target.stem.endswith(suffix):
        return target
    return target.with_name(f"{target.stem}_{message_id}{target.suffix}")
