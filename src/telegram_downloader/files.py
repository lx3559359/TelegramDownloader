from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

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


def disambiguate_target(target: Path, message_id: int) -> Path:
    suffix = f"__{message_id}"
    if target.stem.endswith(suffix):
        return target
    return target.with_name(f"{target.stem}_{message_id}{target.suffix}")
