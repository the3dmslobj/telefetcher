"""Offline checks: everything that doesn't need a Telegram login."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from telethon.tl.types import (
    Document,
    DocumentAttributeAnimated,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)

from telefetcher import cli, client, media
from telefetcher.state import State

ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  ok   {label}")
    else:
        fail += 1
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


def doc(mime, attrs, size=1000):
    return Document(
        id=1, access_hash=1, file_reference=b"", date=datetime.now(timezone.utc),
        mime_type=mime, size=size, dc_id=2, attributes=attrs,
    )


class Msg:
    def __init__(self, id, document, message=""):
        self.id, self.document, self.message = id, document, message


print("classification")
vid = DocumentAttributeVideo(duration=95, w=1920, h=1080)
check("mp4 video", media.classify(Msg(1, doc("video/mp4", [vid]))), media.VIDEO)
check("round note", media.classify(Msg(1, doc("video/mp4",
      [DocumentAttributeVideo(duration=5, w=240, h=240, round_message=True)]))), media.VIDEO_NOTE)
check("gif", media.classify(Msg(1, doc("video/mp4", [vid, DocumentAttributeAnimated()]))), media.GIF)
check("mkv without attr", media.classify(Msg(1, doc("video/x-matroska", []))), media.VIDEO)
check("photo doc", media.classify(Msg(1, doc("image/jpeg", []))), None)
check("no document", media.classify(Msg(1, None)), None)

print("\nfilenames")
check("from original name",
      media.build_item(Msg(42, doc("video/mp4", [vid, DocumentAttributeFilename("Ep 3.mkv")])), media.VIDEO).filename,
      "000042_Ep 3.mkv")
check("from caption",
      media.build_item(Msg(7, doc("video/mp4", [vid]), "Part one\nsecond line"), media.VIDEO).filename,
      "000007_Part one.mp4")
check("slashes stripped",
      media.build_item(Msg(8, doc("video/mp4", [vid]), "a/b:c*d"), media.VIDEO).filename,
      "000008_a b c d.mp4")
check("bare fallback",
      media.build_item(Msg(9, doc("video/mp4", [vid])), media.VIDEO).filename,
      "000009_video.mp4")
long_item = media.build_item(Msg(10, doc("video/mp4", [vid]), "x" * 400), media.VIDEO)
check("long caption truncated", len(long_item.filename) <= 125, True)
check("duration carried", media.build_item(Msg(11, doc("video/mp4", [vid])), media.VIDEO).duration, 95)

print("\nformatting")
check("human_size MB", media.human_size(52428800), "50.0MB")
check("human_size B", media.human_size(512), "512B")
check("duration mm:ss", media.human_duration(95), "1:35")
check("duration h:mm:ss", media.human_duration(3725), "1:02:05")
check("duration none", media.human_duration(None), "--:--")

print("\nparsers")
check("50MB", cli.parse_size("50MB"), 52428800)
check("1.5G", cli.parse_size("1.5G"), 1610612736)
check("900K", cli.parse_size("900k"), 921600)
check("bare bytes", cli.parse_size("2048"), 2048)
check("date", cli.parse_date("2025-01-31"), datetime(2025, 1, 31, tzinfo=timezone.utc))
check("dirname sanitised", cli._safe_dirname("My/Channel: leaks"), "My Channel leaks")

print("\nchat reference regexes")
check("invite +hash", bool(client.INVITE_RE.search("https://t.me/+AbCdEfGh12")), True)
check("joinchat link", client.INVITE_RE.search("t.me/joinchat/XYZ12345").group(1), "XYZ12345")
check("private c/ link", client.PRIVATE_LINK_RE.search("https://t.me/c/1234567890/42").group(1), "1234567890")
check("public link", client.PUBLIC_LINK_RE.search("https://t.me/durov").group(1), "durov")
check("plain title not an invite", bool(client.INVITE_RE.search("My Private Channel")), False)

print("\nstate ledger")
with TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    st = State(tmp / "s.db")
    f = tmp / "a.mp4"
    f.write_bytes(b"x" * 100)
    check("unknown message", st.done(-100, 5), None)
    st.record(-100, 5, f, 100)
    check("recorded", st.done(-100, 5), f)
    check("max id", st.max_msg_id(-100), 5)
    f.write_bytes(b"x" * 50)
    check("size mismatch re-downloads", st.done(-100, 5), None)
    f.unlink()
    check("deleted file re-downloads", st.done(-100, 5), None)
    check("other chat isolated", st.max_msg_id(-999), None)
    st.close()

print("\nresume truncation")
from telefetcher.downloader import ALIGN
with TemporaryDirectory() as tmp:
    part = Path(tmp) / "v.mp4.part"
    part.write_bytes(b"y" * (ALIGN + 777))          # a torn tail chunk
    offset = part.stat().st_size
    offset -= offset % ALIGN
    with open(part, "r+b") as h:
        h.truncate(offset)
    check("aligns down to 1MiB", part.stat().st_size, ALIGN)
    check("offset divisible by 4096", offset % 4096, 0)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
