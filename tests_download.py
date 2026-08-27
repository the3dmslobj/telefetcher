"""Exercises the real download loop against a stub client (no network)."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from telethon.errors import FloodWaitError
from telethon.tl.types import Document, DocumentAttributeVideo

from telefetcher.downloader import ALIGN, CallbackReporter, Options, download_one, run
from telefetcher.media import build_item
from telefetcher.state import State

import telefetcher.downloader as _dl
_real_sleep = asyncio.sleep  # bind before patching, or the stub calls itself
_dl.asyncio.sleep = lambda *a, **k: _real_sleep(0)  # no real backoff in tests

ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  ok   {label}")
    else:
        fail += 1
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


SIZE = ALIGN * 2 + 4096          # 2 MiB + a partial chunk
BLOB = bytes((i * 7 + 3) % 256 for i in range(SIZE))


class Msg:
    def __init__(self, id, size, name=""):
        self.id, self.message = id, name
        self.document = Document(
            id=id, access_hash=1, file_reference=b"", date=datetime.now(timezone.utc),
            mime_type="video/mp4", size=size, dc_id=2,
            attributes=[DocumentAttributeVideo(duration=10, w=640, h=480)],
        )


class StubClient:
    """Serves BLOB in 512 KiB chunks; can be told to fail or rate-limit once."""

    def __init__(self, fail_after=None, flood_once=False, always_fail_after=None):
        self.fail_after, self.flood_once = fail_after, flood_once
        self.always_fail_after = always_fail_after
        self.offsets = []
        self.bytes_served = 0

    def iter_download(self, document, offset=0):
        self.offsets.append(offset)
        outer = self

        async def gen():
            if outer.flood_once:
                outer.flood_once = False
                raise FloodWaitError(request=None)
            served = 0
            pos = offset
            while pos < len(BLOB):
                chunk = BLOB[pos:pos + 512 * 1024]
                # always_fail_after is an absolute file position, so a resumed
                # attempt trips it too; fail_after is per-call and fires once.
                if outer.always_fail_after is not None and pos >= outer.always_fail_after:
                    raise ConnectionError("link dropped")
                if outer.fail_after is not None and served >= outer.fail_after:
                    outer.fail_after = None
                    raise ConnectionError("link dropped")
                yield chunk
                pos += len(chunk)
                served += len(chunk)
                outer.bytes_served += len(chunk)

        return gen()


def new_opts(out, **kw):
    return Options(out_dir=Path(out), workers=kw.pop("workers", 1), **kw)


async def main():
    print("plain download")
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "chan"
        out.mkdir()
        st = State(out / ".tf" / "s.db")
        c = StubClient()
        item = build_item(Msg(12, SIZE, "clip"), "video")
        outcome, written = await download_one(c, -100, item, new_opts(out), st, 0)
        dest = out / "000012_clip.mp4"
        check("outcome", outcome, "downloaded")
        check("bytes reported", written, SIZE)
        check("file complete", dest.stat().st_size, SIZE)
        check("contents byte-exact", dest.read_bytes() == BLOB, True)
        check("no .part left behind", dest.with_suffix(".mp4.part").exists(), False)
        check("ledger updated", st.done(-100, 12), dest)

        print("\nsecond run skips")
        c2 = StubClient()
        outcome, _ = await download_one(c2, -100, item, new_opts(out), st, 0)
        check("skipped", outcome, "skipped")
        check("no bytes fetched", c2.bytes_served, 0)

        print("\n--overwrite forces refetch")
        c3 = StubClient()
        outcome, _ = await download_one(c3, -100, item, new_opts(out, overwrite=True), st, 0)
        check("downloaded again", outcome, "downloaded")
        check("bytes fetched", c3.bytes_served, SIZE)
        st.close()

    print("\nresume within the retry loop")
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "chan"
        out.mkdir()
        st = State(out / ".tf" / "s.db")
        item = build_item(Msg(13, SIZE, "big"), "video")
        c = StubClient(fail_after=ALIGN + 100_000)   # dies once, mid-stream
        outcome, _ = await download_one(c, -100, item, new_opts(out), st, 0)
        dest = out / "000013_big.mp4"
        check("retry finished the job", outcome, "downloaded")
        check("two attempts made", len(c.offsets), 2)
        check("first attempt from zero", c.offsets[0], 0)
        check("second attempt resumed", c.offsets[1], ALIGN)
        check("resume offset aligned", c.offsets[1] % 4096, 0)
        check("refetched only the tail", c.bytes_served < SIZE + ALIGN, True)
        check("contents byte-exact after resume", dest.read_bytes() == BLOB, True)
        check("no .part left behind", list(out.glob("*.part")), [])
        st.close()

    print("\nresume across separate invocations")
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "chan"
        out.mkdir()
        st = State(out / ".tf" / "s.db")
        item = build_item(Msg(15, SIZE, "big"), "video")
        c = StubClient(always_fail_after=ALIGN + 100_000)   # every attempt dies
        try:
            await download_one(c, -100, item, new_opts(out), st, 0)
            check("gave up after retries", "no raise", "ConnectionError")
        except ConnectionError:
            check("gave up after retries", True, True)
        part = out / "000015_big.mp4.part"
        check("partial survives for next run", part.exists(), True)
        check("partial holds aligned prefix", part.read_bytes() == BLOB[:part.stat().st_size], True)
        check("nothing recorded as done", st.done(-100, 15), None)

        c2 = StubClient()                                   # fresh run, healthy link
        outcome, _ = await download_one(c2, -100, item, new_opts(out), st, 0)
        dest = out / "000015_big.mp4"
        check("second invocation completes", outcome, "downloaded")
        check("picked up mid-file", c2.offsets[0] > 0, True)
        check("contents byte-exact", dest.read_bytes() == BLOB, True)
        st.close()

    print("\nflood wait is survived")
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "chan"
        out.mkdir()
        st = State(out / ".tf" / "s.db")
        item = build_item(Msg(14, SIZE, "rl"), "video")
        c = StubClient(flood_once=True)
        started = asyncio.get_event_loop().time()
        outcome, _ = await download_one(c, -100, item, new_opts(out), st, 0)
        check("recovered after flood wait", outcome, "downloaded")
        check("file complete", (out / "000014_rl.mp4").read_bytes() == BLOB, True)
        st.close()

    print("\nparallel run + summary")
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "chan"
        st = State(Path(tmp) / "s.db")
        items = [build_item(Msg(i, SIZE, f"v{i}"), "video") for i in range(20, 27)]

        class Chat:
            id = -1001234567890

        c = StubClient()
        summary = await run(c, Chat(), items, new_opts(out, workers=3), st)
        check("all downloaded", summary.downloaded, 7)
        check("none failed", summary.failed, 0)
        check("no errors", summary.errors, [])
        check("bytes tallied", summary.bytes_written, SIZE * 7)
        check("files on disk", len(sorted(out.glob("*.mp4"))), 7)
        check("no .part leftovers", list(out.glob("*.part")), [])

        summary2 = await run(c, Chat(), items, new_opts(out, workers=3), st)
        check("re-run skips all", summary2.skipped, 7)
        check("re-run downloads none", summary2.downloaded, 0)
        st.close()

    print("\ncallback reporting (what the web UI consumes)")
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "chan"
        st = State(Path(tmp) / "s.db")
        items = [build_item(Msg(i, SIZE, f"w{i}"), "video") for i in range(40, 43)]
        seen, finished = [], []

        def make_reporter(item, position):
            return CallbackReporter(item, lambda it, done, total: seen.append((it.msg_id, done, total)))

        class Chat:
            id = -1002

        c = StubClient()
        summary = await run(c, Chat(), items, new_opts(out, workers=2), st,
                            make_reporter=make_reporter, quiet=True,
                            on_done=lambda it: finished.append(it.msg_id))
        check("quiet run still downloads", summary.downloaded, 3)
        check("progress reported", len(seen) > 0, True)
        check("progress covers every file", {m for m, _, _ in seen}, {40, 41, 42})
        check("totals are the file size", {t for _, _, t in seen}, {SIZE})
        check("progress is monotonic per file",
              all(sorted(d for m, d, _ in seen if m == 40) == [d for m, d, _ in seen if m == 40]
                  for _ in [0]), True)
        check("final progress reaches the total", max(d for m, d, _ in seen if m == 40), SIZE)
        check("on_done fired per file", sorted(finished), [40, 41, 42])
        st.close()

    print("\ndry run touches nothing")
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "chan"
        st = State(Path(tmp) / "s.db")
        item = build_item(Msg(30, SIZE, "d"), "video")
        c = StubClient()
        outcome, written = await download_one(c, -100, item, new_opts(out, dry_run=True), st, 0)
        check("reports as would-download", outcome, "downloaded")
        check("nothing fetched", c.bytes_served, 0)
        check("no directory written", out.exists(), False)
        st.close()

    print(f"\n{ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
