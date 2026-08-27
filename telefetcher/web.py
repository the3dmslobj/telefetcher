"""A local web interface for browsing chats and managing downloads.

Binds to loopback only. It drives exactly the same code paths as the CLI —
`collect` to find videos, `run` to fetch them — so behaviour can't drift
between the two front ends.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web
from telethon.tl.types import Channel, Chat, User

from .client import ChatNotFound, resolve_chat
from .downloader import CallbackReporter, Options, collect, run
from .media import GIF, VIDEO, VIDEO_NOTE, VideoItem
from .state import State

STATIC = Path(__file__).parent / "static"


@dataclass
class FileProgress:
    msg_id: int
    filename: str
    size: int
    done: int = 0
    status: str = "queued"  # queued | downloading | done | skipped | failed

    def as_json(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "filename": self.filename,
            "size": self.size,
            "done": self.done,
            "status": self.status,
        }


@dataclass
class Job:
    id: str
    chat_id: int
    chat_title: str
    out_dir: str
    files: dict[int, FileProgress]
    status: str = "running"  # running | done | cancelled | error
    error: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)

    def as_json(self) -> dict:
        files = list(self.files.values())
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "chat_title": self.chat_title,
            "out_dir": self.out_dir,
            "status": self.status,
            "error": self.error,
            "total": len(files),
            "finished": sum(1 for f in files if f.status in ("done", "skipped")),
            "failed": sum(1 for f in files if f.status == "failed"),
            "bytes_total": sum(f.size for f in files),
            "bytes_done": sum(f.done if f.status != "done" else f.size for f in files),
            "files": [f.as_json() for f in files],
        }


class Server:
    def __init__(self, client, root: Path):
        self.client = client
        self.root = root
        self.jobs: dict[str, Job] = {}
        self.entities: dict[int, object] = {}
        self.scans: dict[int, list[VideoItem]] = {}

    # ---- helpers -------------------------------------------------------

    def chat_dir(self, title: str, chat_id: int) -> Path:
        from .cli import _safe_dirname

        return self.root / f"{_safe_dirname(title)}"

    def inside_root(self, path: Path) -> bool:
        """Guard every filesystem mutation — the browser is not a trusted caller."""
        try:
            path.resolve().relative_to(self.root.resolve())
            return True
        except (ValueError, OSError):
            return False

    async def entity(self, chat_id: int):
        if chat_id not in self.entities:
            async for dialog in self.client.iter_dialogs():
                self.entities[dialog.id] = dialog.entity
        if chat_id not in self.entities:
            raise ChatNotFound(f"chat {chat_id} not found")
        return self.entities[chat_id]

    # ---- routes --------------------------------------------------------

    async def index(self, _request):
        return web.FileResponse(STATIC / "index.html")

    async def me(self, _request):
        user = await self.client.get_me()
        return web.json_response(
            {
                "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
                "username": user.username,
                "id": user.id,
                "root": str(self.root),
            }
        )

    async def chats(self, request):
        needle = (request.query.get("q") or "").casefold()
        rows = []
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            self.entities[dialog.id] = entity
            if isinstance(entity, User):
                continue
            name = dialog.name or ""
            if needle and needle not in name.casefold():
                continue
            if isinstance(entity, Channel):
                kind = "channel" if entity.broadcast else "group"
                private = not entity.username
            elif isinstance(entity, Chat):
                kind, private = "group", True
            else:
                kind, private = "chat", True
            rows.append(
                {
                    "id": dialog.id,
                    "name": name,
                    "kind": kind,
                    "private": private,
                    "protected": bool(getattr(entity, "noforwards", False)),
                }
            )
        return web.json_response(rows)

    async def scan(self, request):
        body = await request.json()
        chat_id = int(body["chat_id"])
        try:
            chat = await self.entity(chat_id)
        except ChatNotFound as exc:
            raise web.HTTPNotFound(text=str(exc)) from None

        kinds = {VIDEO}
        if body.get("gifs"):
            kinds.add(GIF)
        if body.get("notes"):
            kinds.add(VIDEO_NOTE)

        title = getattr(chat, "title", None) or str(chat_id)
        out_dir = self.chat_dir(title, chat_id)
        opts = Options(
            out_dir=out_dir,
            kinds=kinds,
            limit=int(body.get("limit") or 200),
            search=body.get("search") or None,
            min_size=int(body.get("min_size") or 0),
        )
        items = await collect(self.client, chat, opts)
        self.scans[chat_id] = items

        state = State(out_dir / ".telefetcher" / "state.db")
        payload = [
            {
                "msg_id": i.msg_id,
                "kind": i.kind,
                "filename": i.filename,
                "size": i.size,
                "duration": i.duration,
                "have": state.done(chat_id, i.msg_id) is not None,
            }
            for i in items
        ]
        state.close()
        return web.json_response(
            {"chat_id": chat_id, "title": title, "out_dir": str(out_dir), "items": payload}
        )

    async def download(self, request):
        body = await request.json()
        chat_id = int(body["chat_id"])
        wanted = {int(m) for m in body.get("msg_ids") or []}
        items = [i for i in self.scans.get(chat_id, []) if i.msg_id in wanted]
        if not items:
            raise web.HTTPBadRequest(text="nothing to download — scan the chat first")

        chat = await self.entity(chat_id)
        title = getattr(chat, "title", None) or str(chat_id)
        out_dir = self.chat_dir(title, chat_id)
        opts = Options(
            out_dir=out_dir,
            workers=max(1, min(8, int(body.get("workers") or 2))),
            overwrite=bool(body.get("overwrite")),
        )

        job = Job(
            id=uuid.uuid4().hex[:12],
            chat_id=chat_id,
            chat_title=title,
            out_dir=str(out_dir),
            files={i.msg_id: FileProgress(i.msg_id, i.filename, i.size) for i in items},
        )
        self.jobs[job.id] = job

        def make_reporter(item, _position):
            progress = job.files[item.msg_id]
            progress.status = "downloading"
            progress.done = 0

            def on_progress(it, done, _total):
                job.files[it.msg_id].done = done

            return CallbackReporter(item, on_progress)

        def on_done(item):
            progress = job.files[item.msg_id]
            if progress.status == "downloading":
                progress.status = "done"
                progress.done = progress.size

        async def worker():
            state = State(out_dir / ".telefetcher" / "state.db")
            try:
                for item in items:  # mark what's already on disk before starting
                    if not opts.overwrite and state.done(chat_id, item.msg_id):
                        job.files[item.msg_id].status = "skipped"
                summary = await run(
                    self.client, chat, items, opts, state,
                    make_reporter=make_reporter, quiet=True, on_done=on_done,
                )
                for progress in job.files.values():
                    if progress.status == "downloading":
                        progress.status = "failed"
                job.status = "done"
                if summary.errors:
                    job.error = "; ".join(summary.errors[:3])
            except asyncio.CancelledError:
                job.status = "cancelled"
                for progress in job.files.values():
                    if progress.status in ("queued", "downloading"):
                        progress.status = "queued"
                raise
            except Exception as exc:
                job.status, job.error = "error", repr(exc)
            finally:
                state.close()

        job.task = asyncio.create_task(worker())
        return web.json_response({"job_id": job.id})

    async def list_jobs(self, _request):
        # note: named list_jobs, not jobs — self.jobs is the registry dict
        return web.json_response([j.as_json() for j in self.jobs.values()])

    async def cancel(self, request):
        job = self.jobs.get(request.match_info["job_id"])
        if job is None:
            raise web.HTTPNotFound(text="no such job")
        if job.task and not job.task.done():
            job.task.cancel()
        return web.json_response({"ok": True})

    async def clear_jobs(self, _request):
        for job_id, job in list(self.jobs.items()):
            if job.status != "running":
                del self.jobs[job_id]
        return web.json_response({"ok": True})

    async def files(self, request):
        chat_id = request.query.get("chat_id")
        folders = []
        if chat_id:
            chat = await self.entity(int(chat_id))
            title = getattr(chat, "title", None) or str(chat_id)
            folders = [self.chat_dir(title, int(chat_id))]
        elif self.root.exists():
            folders = [p for p in sorted(self.root.iterdir()) if p.is_dir()]

        rows = []
        for folder in folders:
            for path in sorted(folder.glob("*")):
                if path.is_dir() or path.suffix == ".part":
                    continue
                stat = path.stat()
                rows.append(
                    {
                        "path": str(path),
                        "name": path.name,
                        "folder": folder.name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        return web.json_response(rows)

    async def delete_file(self, request):
        body = await request.json()
        path = Path(body["path"])
        if not self.inside_root(path):
            raise web.HTTPForbidden(text="path is outside the downloads folder")
        if not path.is_file():
            raise web.HTTPNotFound(text="no such file")
        path.unlink()
        return web.json_response({"ok": True})

    async def reveal(self, request):
        body = await request.json()
        path = Path(body["path"])
        if not self.inside_root(path):
            raise web.HTTPForbidden(text="path is outside the downloads folder")
        opener = {"darwin": ["open", "-R"], "win32": ["explorer", "/select,"]}.get(
            sys.platform, ["xdg-open"]
        )
        target = str(path) if sys.platform == "darwin" else str(path.parent)
        try:
            subprocess.Popen([*opener, target])
        except OSError as exc:
            raise web.HTTPInternalServerError(text=str(exc)) from None
        return web.json_response({"ok": True})


def build_app(client, root: Path) -> web.Application:
    server = Server(client, root)
    app = web.Application()
    app.add_routes(
        [
            web.get("/", server.index),
            web.get("/api/me", server.me),
            web.get("/api/chats", server.chats),
            web.post("/api/scan", server.scan),
            web.post("/api/download", server.download),
            web.get("/api/jobs", server.list_jobs),
            web.post("/api/jobs/{job_id}/cancel", server.cancel),
            web.post("/api/jobs/clear", server.clear_jobs),
            web.get("/api/files", server.files),
            web.post("/api/files/delete", server.delete_file),
            web.post("/api/files/reveal", server.reveal),
        ]
    )
    app.router.add_static("/static/", STATIC)
    return app


async def serve(client, root: Path, host: str, port: int, open_browser: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    runner = web.AppRunner(build_app(client, root), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    url = f"http://{host}:{port}"
    print(f"telefetcher ui on {url}")
    print(f"downloads -> {root}")
    print("ctrl-c to stop")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
