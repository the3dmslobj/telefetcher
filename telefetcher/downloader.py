"""Finding videos in a chat and pulling them down, resumably."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from telethon.errors import FileReferenceExpiredError, FloodWaitError
from telethon.tl.types import (
    InputMessagesFilterGif,
    InputMessagesFilterRoundVideo,
    InputMessagesFilterVideo,
)
from tqdm import tqdm

from .media import GIF, VIDEO, VIDEO_NOTE, VideoItem, build_item, classify, human_size
from .state import State

# Resume offsets must be a multiple of 4096 for Telethon's direct-download path;
# 1 MiB satisfies that and keeps the re-fetched tail small.
ALIGN = 1024 * 1024
MAX_RETRIES = 5

SERVER_FILTERS = {
    VIDEO: InputMessagesFilterVideo,
    GIF: InputMessagesFilterGif,
    VIDEO_NOTE: InputMessagesFilterRoundVideo,
}


@dataclass
class Options:
    out_dir: Path
    kinds: set[str] = field(default_factory=lambda: {VIDEO})
    limit: int | None = None
    min_id: int = 0
    max_id: int = 0
    since: datetime | None = None
    until: datetime | None = None
    min_size: int = 0
    max_size: int | None = None
    search: str | None = None
    oldest_first: bool = False
    workers: int = 1
    dry_run: bool = False
    overwrite: bool = False


@dataclass
class Summary:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_written: int = 0
    errors: list[str] = field(default_factory=list)


async def collect(client, chat, opts: Options) -> list[VideoItem]:
    """Ask the server for videos directly — far cheaper than scanning history.

    One pass per media kind, since Telegram's search filters are single-valued.
    """
    found: dict[int, VideoItem] = {}

    for kind in sorted(opts.kinds):
        async for message in client.iter_messages(
            chat,
            filter=SERVER_FILTERS[kind](),
            search=opts.search,
            min_id=opts.min_id,
            max_id=opts.max_id,
            reverse=opts.oldest_first,
            offset_date=None if opts.oldest_first else opts.until,
        ):
            if message.id in found:
                continue
            if opts.since and message.date < opts.since:
                if not opts.oldest_first:
                    break  # history is descending; everything after is older
                continue
            if opts.until and message.date > opts.until:
                if opts.oldest_first:
                    break  # ascending; everything after is newer still
                continue

            actual = classify(message)
            if actual is None or actual not in opts.kinds:
                continue

            item = build_item(message, actual)
            if item.size < opts.min_size:
                continue
            if opts.max_size is not None and item.size > opts.max_size:
                continue

            found[message.id] = item
            if opts.limit and len(found) >= opts.limit:
                break

        if opts.limit and len(found) >= opts.limit:
            break

    items = sorted(found.values(), key=lambda i: i.msg_id, reverse=not opts.oldest_first)
    return items[: opts.limit] if opts.limit else items


class TqdmReporter:
    """Per-file progress on a terminal bar."""

    def __init__(self, item: VideoItem, position: int = 0):
        label = item.filename if len(item.filename) <= 42 else item.filename[:39] + "..."
        self.bar = tqdm(
            total=item.size,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=label,
            position=position,
            leave=False,
            dynamic_ncols=True,
        )

    def update(self, count: int) -> None:
        self.bar.update(count)

    def note(self, text: str) -> None:
        self.bar.write(text)

    def close(self) -> None:
        self.bar.close()


class CallbackReporter:
    """Per-file progress handed to a caller — used by the web UI's job records."""

    def __init__(self, item: VideoItem, on_progress, on_note=None):
        self.item, self.done = item, 0
        self.on_progress, self.on_note = on_progress, on_note

    def update(self, count: int) -> None:
        self.done += count
        self.on_progress(self.item, self.done, self.item.size)

    def note(self, text: str) -> None:
        if self.on_note:
            self.on_note(text)

    def close(self) -> None:
        pass


async def _stream(client, item: VideoItem, part: Path, bar) -> None:
    """Append to the .part file from wherever it left off."""
    offset = part.stat().st_size if part.exists() else 0
    offset -= offset % ALIGN
    if offset:
        with open(part, "r+b") as handle:  # drop any half-written tail chunk
            handle.truncate(offset)

    bar.update(offset)
    with open(part, "ab" if offset else "wb") as handle:
        async for chunk in client.iter_download(item.document, offset=offset):
            handle.write(chunk)
            bar.update(len(chunk))


async def download_one(
    client,
    chat_id: int,
    item: VideoItem,
    opts: Options,
    state: State,
    position: int,
    make_reporter=None,
) -> tuple[str, int]:
    """Returns (outcome, bytes_written) where outcome is downloaded/skipped/failed."""
    dest = opts.out_dir / item.filename
    part = dest.with_suffix(dest.suffix + ".part")

    if not opts.overwrite:
        if state.done(chat_id, item.msg_id):
            return "skipped", 0
        if dest.exists() and dest.stat().st_size == item.size:
            state.record(chat_id, item.msg_id, dest, item.size)
            return "skipped", 0

    if opts.dry_run:
        return "downloaded", item.size

    make_reporter = make_reporter or TqdmReporter
    label = item.filename if len(item.filename) <= 42 else item.filename[:39] + "..."
    for attempt in range(1, MAX_RETRIES + 1):
        bar = make_reporter(item, position)
        try:
            await _stream(client, item, part, bar)
            part.replace(dest)
            state.record(chat_id, item.msg_id, dest, item.size)
            return "downloaded", item.size
        except FloodWaitError as exc:
            bar.note(f"rate limited by Telegram, waiting {exc.seconds}s — {label}")
            await asyncio.sleep(exc.seconds + 1)
        except FileReferenceExpiredError:
            # File references go stale after a while; re-fetch the message.
            refreshed = await client.get_messages(chat_id, ids=item.msg_id)
            if refreshed is None or classify(refreshed) is None:
                return "failed", 0
            item = build_item(refreshed, classify(refreshed))
        except (OSError, ConnectionError) as exc:
            if attempt == MAX_RETRIES:
                raise
            bar.note(f"retry {attempt}/{MAX_RETRIES} after {exc!r} — {label}")
            await asyncio.sleep(min(2**attempt, 30))
        finally:
            bar.close()

    return "failed", 0


class _NullBar:
    def update(self, count: int) -> None:
        pass

    def close(self) -> None:
        pass


async def run(
    client,
    chat,
    items: list[VideoItem],
    opts: Options,
    state: State,
    make_reporter=None,
    quiet: bool = False,
    on_done=None,
) -> Summary:
    summary = Summary()
    opts.out_dir.mkdir(parents=True, exist_ok=True)
    chat_id = chat.id

    overall = (
        _NullBar()
        if quiet
        else tqdm(
            total=len(items),
            unit="file",
            desc="videos",
            position=opts.workers,
            dynamic_ncols=True,
        )
    )
    slots: asyncio.Queue[int] = asyncio.Queue()
    for slot in range(opts.workers):
        slots.put_nowait(slot)

    async def worker(item: VideoItem) -> None:
        slot = await slots.get()
        try:
            outcome, written = await download_one(
                client, chat_id, item, opts, state, slot, make_reporter
            )
            if outcome == "downloaded":
                summary.downloaded += 1
                summary.bytes_written += written
            elif outcome == "skipped":
                summary.skipped += 1
            else:
                summary.failed += 1
        except Exception as exc:  # keep going; report at the end
            summary.failed += 1
            summary.errors.append(f"msg {item.msg_id} ({item.filename}): {exc!r}")
        finally:
            slots.put_nowait(slot)
            overall.update(1)
            if on_done:
                on_done(item)

    pending: set[asyncio.Task] = set()
    for item in items:
        pending.add(asyncio.create_task(worker(item)))
        if len(pending) >= opts.workers:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    if pending:
        await asyncio.wait(pending)
    overall.close()
    if quiet:
        return summary

    print(
        f"\ndone — {summary.downloaded} downloaded "
        f"({human_size(summary.bytes_written)}), "
        f"{summary.skipped} already had, {summary.failed} failed"
    )
    for error in summary.errors:
        print(f"  ! {error}")
    return summary
