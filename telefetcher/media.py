"""Deciding what counts as a video, and what to call the file on disk."""

from __future__ import annotations

import mimetypes
import re
import unicodedata
from dataclasses import dataclass

from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)

VIDEO = "video"
VIDEO_NOTE = "video_note"  # the round selfie-camera clips
GIF = "gif"                # Telegram "GIFs" are silent looping mp4s

_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_SPACE = re.compile(r"\s+")
MAX_STEM = 110


@dataclass(frozen=True)
class VideoItem:
    msg_id: int
    kind: str
    document: object
    size: int
    duration: int | None
    filename: str

    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024)


def _attr(document, cls):
    for attribute in document.attributes:
        if isinstance(attribute, cls):
            return attribute
    return None


def classify(message) -> str | None:
    """Return the video kind for a message, or None if it holds no video."""
    document = getattr(message, "document", None)
    if document is None:
        return None

    mime = (document.mime_type or "").lower()
    video_attr = _attr(document, DocumentAttributeVideo)
    if video_attr is None and not mime.startswith("video/"):
        return None
    if video_attr is not None and getattr(video_attr, "round_message", False):
        return VIDEO_NOTE
    if _attr(document, DocumentAttributeAnimated) is not None:
        return GIF
    return VIDEO


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFC", text).strip()
    text = _UNSAFE.sub(" ", text)
    text = _SPACE.sub(" ", text).strip(" .")
    return text[:MAX_STEM].strip(" .")


def _extension(document, fallback: str = ".mp4") -> str:
    name_attr = _attr(document, DocumentAttributeFilename)
    if name_attr and "." in name_attr.file_name:
        suffix = "." + name_attr.file_name.rsplit(".", 1)[-1]
        if 2 <= len(suffix) <= 6:
            return suffix.lower()
    guessed = mimetypes.guess_extension(document.mime_type or "")
    return guessed or fallback


def build_item(message, kind: str) -> VideoItem:
    """Name files `<msg_id>_<something readable>.<ext>`.

    The id prefix keeps a directory sorted in channel order and makes every
    name unique, which matters because captions repeat and many uploads
    arrive as plain `video.mp4`.
    """
    document = message.document
    video_attr = _attr(document, DocumentAttributeVideo)
    name_attr = _attr(document, DocumentAttributeFilename)

    stem = ""
    if name_attr:
        stem = _slug(name_attr.file_name.rsplit(".", 1)[0])
    if not stem and message.message:
        stem = _slug(message.message.splitlines()[0])
    if not stem:
        stem = kind

    return VideoItem(
        msg_id=message.id,
        kind=kind,
        document=document,
        size=document.size or 0,
        duration=int(video_attr.duration) if video_attr and video_attr.duration else None,
        filename=f"{message.id:06d}_{stem}{_extension(document)}",
    )


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024 or unit == "TB":
            return f"{num_bytes:.0f}{unit}" if unit == "B" else f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}TB"


def human_duration(seconds: int | None) -> str:
    if not seconds:
        return "--:--"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
