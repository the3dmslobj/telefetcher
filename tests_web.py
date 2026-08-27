"""Drives the local web interface against a stub Telegram client (no network)."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
from telethon.tl.types import Channel, Document, DocumentAttributeVideo

from telefetcher.web import build_app

ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  ok   {label}")
    else:
        fail += 1
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


SIZE = 300_000
BLOB = bytes(range(256)) * (SIZE // 256 + 1)
BLOB = BLOB[:SIZE]
NOW = datetime.now(timezone.utc)


def channel(cid, title, **kw):
    return Channel(id=cid, title=title, photo=None, date=NOW, broadcast=True, **kw)


def message(mid, name):
    doc = Document(
        id=mid, access_hash=1, file_reference=b"", date=NOW, mime_type="video/mp4",
        size=SIZE, dc_id=2, attributes=[DocumentAttributeVideo(duration=12, w=640, h=480)],
    )
    return SimpleNamespace(id=mid, document=doc, message=name, date=NOW)


CHANNELS = [
    channel(111, "Private Leaks", noforwards=True),
    channel(222, "Open Channel", username="openchan"),
    channel(333, "Cooking Club"),
]
MESSAGES = {111: [message(9, "alpha"), message(8, "beta"), message(7, "gamma")],
            222: [message(5, "solo")], 333: []}


class StubClient:
    def __init__(self):
        self.downloads = 0

    async def get_me(self):
        return SimpleNamespace(first_name="Dana", last_name="", username="dana", id=42)

    def iter_dialogs(self):
        async def gen():
            for ch in CHANNELS:
                yield SimpleNamespace(id=ch.id, name=ch.title, entity=ch)
        return gen()

    def iter_messages(self, chat, **kw):
        async def gen():
            for m in MESSAGES.get(chat.id, []):
                yield m
        return gen()

    def iter_download(self, document, offset=0):
        async def gen():
            self.downloads += 1
            pos = offset
            while pos < SIZE:
                yield BLOB[pos:pos + 100_000]
                pos += 100_000
        return gen()


async def main():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "downloads"
        stub = StubClient()
        server = TestServer(build_app(stub, root))
        async with TestClient(server) as http:

            print("account + chat listing")
            me = await (await http.get("/api/me")).json()
            check("account name", me["name"], "Dana")
            check("root reported", me["root"], str(root))

            chats = await (await http.get("/api/chats")).json()
            check("all chats listed", [c["name"] for c in chats],
                  ["Private Leaks", "Open Channel", "Cooking Club"])
            check("private flag", [c["private"] for c in chats], [True, False, True])
            check("protection surfaced", [c["protected"] for c in chats], [True, False, False])

            filtered = await (await http.get("/api/chats?q=cook")).json()
            check("search filters", [c["name"] for c in filtered], ["Cooking Club"])

            print("\nserving the page")
            page = await http.get("/")
            check("index served", page.status, 200)
            check("index is html", "text/html" in page.headers["content-type"], True)
            body = await page.text()
            check("no external asset refs", ("http://" in body or "https://" in body), False)

            print("\nscanning a chat")
            scan = await (await http.post("/api/scan", json={"chat_id": 111})).json()
            check("title returned", scan["title"], "Private Leaks")
            check("videos found", len(scan["items"]), 3)
            check("newest first", [i["msg_id"] for i in scan["items"]], [9, 8, 7])
            check("names built", scan["items"][0]["filename"], "000009_alpha.mp4")
            check("nothing owned yet", [i["have"] for i in scan["items"]], [False] * 3)

            empty = await (await http.post("/api/scan", json={"chat_id": 333})).json()
            check("empty chat handled", empty["items"], [])

            print("\ndownloading")
            bad = await http.post("/api/download", json={"chat_id": 111, "msg_ids": []})
            check("empty selection rejected", bad.status, 400)

            job = await (await http.post(
                "/api/download", json={"chat_id": 111, "msg_ids": [9, 8], "workers": 2})).json()
            check("job created", bool(job["job_id"]), True)

            for _ in range(100):
                jobs = await (await http.get("/api/jobs")).json()
                if jobs and jobs[0]["status"] != "running":
                    break
                await asyncio.sleep(0.05)
            jobs = await (await http.get("/api/jobs")).json()
            j = jobs[0]
            check("job finished", j["status"], "done")
            check("both files done", j["finished"], 2)
            check("none failed", j["failed"], 0)
            check("bytes accounted", j["bytes_done"], SIZE * 2)
            check("per-file status", sorted(f["status"] for f in j["files"]), ["done", "done"])

            out = root / "Private Leaks"
            check("files on disk", sorted(p.name for p in out.glob("*.mp4")),
                  ["000008_beta.mp4", "000009_alpha.mp4"])
            check("contents correct", (out / "000009_alpha.mp4").read_bytes() == BLOB, True)
            check("no .part leftovers", list(out.glob("*.part")), [])

            print("\nrescan reflects what we now own")
            scan2 = await (await http.post("/api/scan", json={"chat_id": 111})).json()
            check("owned files marked", {i["msg_id"]: i["have"] for i in scan2["items"]},
                  {9: True, 8: True, 7: False})

            print("\nfile management")
            files = await (await http.get("/api/files")).json()
            check("both listed", len(files), 2)
            check("folder reported", files[0]["folder"], "Private Leaks")

            target = str(out / "000008_beta.mp4")
            res = await http.post("/api/files/delete", json={"path": target})
            check("delete succeeds", res.status, 200)
            check("file gone", Path(target).exists(), False)
            files = await (await http.get("/api/files")).json()
            check("listing updated", len(files), 1)

            print("\npath guard (the browser is not trusted)")
            outside = Path(tmp) / "secret.txt"
            outside.write_text("do not delete me")
            res = await http.post("/api/files/delete", json={"path": str(outside)})
            check("outside root refused", res.status, 403)
            check("file untouched", outside.read_text(), "do not delete me")

            res = await http.post("/api/files/delete",
                                  json={"path": str(out / ".." / ".." / "secret.txt")})
            check("traversal refused", res.status, 403)
            check("still untouched", outside.exists(), True)

            res = await http.post("/api/files/reveal", json={"path": str(outside)})
            check("reveal outside root refused", res.status, 403)

            res = await http.post("/api/files/delete", json={"path": str(out / "nope.mp4")})
            check("missing file is 404", res.status, 404)

            print("\nclearing finished jobs")
            await http.post("/api/jobs/clear")
            jobs = await (await http.get("/api/jobs")).json()
            check("cleared", jobs, [])

    print(f"\n{ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
