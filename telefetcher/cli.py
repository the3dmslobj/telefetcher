"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from telethon.tl.types import Channel, Chat, User

from . import __version__
from .client import ChatNotFound, interactive_login, make_client, resolve_chat
from .config import DEFAULT_SESSION, ConfigError, load_config, save_config, session_path
from .downloader import Options, collect, run
from .media import GIF, VIDEO, VIDEO_NOTE, human_duration, human_size
from .state import State

SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([KMGT]?)B?\s*$", re.I)
SIZE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_size(text: str) -> int:
    match = SIZE_RE.match(text)
    if not match:
        raise argparse.ArgumentTypeError(f"bad size {text!r} — try 50MB, 1.5G, 900K")
    return int(float(match.group(1)) * SIZE_UNITS[match.group(2).upper()])


def parse_date(text: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad date {text!r} — try 2025-01-31")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tf",
        description="Download videos from Telegram chats and channels you belong to.",
    )
    parser.add_argument("--version", action="version", version=f"telefetcher {__version__}")
    parser.add_argument(
        "--session",
        default=DEFAULT_SESSION,
        help="named login session (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="sign in with your Telegram account")
    sub.add_parser("logout", help="sign out and delete the saved session")
    sub.add_parser("whoami", help="show the signed-in account")

    chats = sub.add_parser("chats", help="list chats you've joined, with their ids")
    chats.add_argument("filter", nargs="?", help="only show titles containing this text")
    chats.add_argument("--all", action="store_true", help="include private chats and bots")

    get = sub.add_parser("get", help="download videos from a chat")
    get.add_argument(
        "chat",
        help="channel title, id, @username, t.me link, or invite link",
    )
    get.add_argument("-o", "--out", type=Path, help="output directory (default: ./<title>)")
    get.add_argument("-n", "--limit", type=int, help="stop after this many videos")
    get.add_argument("--gifs", action="store_true", help="include GIFs / silent loops")
    get.add_argument("--notes", action="store_true", help="include round video notes")
    get.add_argument("--only-gifs", action="store_true", help="fetch only GIFs")
    get.add_argument("--search", help="only messages whose text matches")
    get.add_argument("--since", type=parse_date, metavar="YYYY-MM-DD")
    get.add_argument("--until", type=parse_date, metavar="YYYY-MM-DD")
    get.add_argument("--min-id", type=int, default=0, help="only messages after this id")
    get.add_argument("--max-id", type=int, default=0, help="only messages before this id")
    get.add_argument("--new", action="store_true", help="only messages newer than the last run")
    get.add_argument("--min-size", type=parse_size, default=0, metavar="SIZE")
    get.add_argument("--max-size", type=parse_size, metavar="SIZE")
    get.add_argument("--oldest-first", action="store_true", help="walk history forwards")
    get.add_argument("-j", "--workers", type=int, default=2, help="parallel downloads")
    get.add_argument("--overwrite", action="store_true", help="re-download existing files")
    get.add_argument("--dry-run", action="store_true", help="list what would be downloaded")
    get.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    return parser


def _safe_dirname(title: str) -> str:
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*]', " ", title or "chat")
    return re.sub(r"\s+", " ", cleaned).strip(" .")[:80] or "chat"


async def cmd_login(client, _args) -> int:
    await interactive_login(client)
    me = await client.get_me()
    print(f"signed in as {me.first_name or ''} (@{me.username or me.id})".strip())
    return 0


async def cmd_whoami(client, _args) -> int:
    if not await client.is_user_authorized():
        print("not signed in — run `tf login`")
        return 1
    me = await client.get_me()
    print(f"{me.first_name or ''} {me.last_name or ''}".strip())
    print(f"  username: @{me.username}" if me.username else "  username: (none)")
    print(f"  user id:  {me.id}")
    return 0


async def cmd_chats(client, args) -> int:
    needle = (args.filter or "").casefold()
    rows = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not args.all and isinstance(entity, User):
            continue
        if needle and needle not in (dialog.name or "").casefold():
            continue
        if isinstance(entity, Channel):
            kind = "channel" if entity.broadcast else "group"
            access = "public" if entity.username else "private"
        elif isinstance(entity, Chat):
            kind, access = "group", "private"
        else:
            kind, access = "chat", "private"
        rows.append((dialog.id, kind, access, dialog.name or ""))

    if not rows:
        print("no matching chats")
        return 1
    width = max(len(str(row[0])) for row in rows)
    for chat_id, kind, access, name in rows:
        print(f"{chat_id:>{width}}  {kind:<7} {access:<7}  {name}")
    print(f"\n{len(rows)} chats")
    return 0


async def cmd_get(client, args) -> int:
    try:
        chat = await resolve_chat(client, args.chat)
    except ChatNotFound as exc:
        print(exc, file=sys.stderr)
        return 1

    title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat.id)
    out_dir = args.out or Path.cwd() / _safe_dirname(title)

    kinds = {GIF} if args.only_gifs else {VIDEO}
    if args.gifs:
        kinds.add(GIF)
    if args.notes:
        kinds.add(VIDEO_NOTE)

    state = State(out_dir / ".telefetcher" / "state.db")
    min_id = args.min_id
    if args.new:
        min_id = max(min_id, state.max_msg_id(chat.id) or 0)

    opts = Options(
        out_dir=out_dir,
        kinds=kinds,
        limit=args.limit,
        min_id=min_id,
        max_id=args.max_id,
        since=args.since,
        until=args.until,
        min_size=args.min_size,
        max_size=args.max_size,
        search=args.search,
        oldest_first=args.oldest_first,
        workers=max(1, args.workers),
        dry_run=args.dry_run,
        overwrite=args.overwrite,
    )

    print(f"chat:   {title}  (id {chat.id})")
    print(f"output: {out_dir}")
    if getattr(chat, "noforwards", False):
        # Server-enforced for forwarding; the save lock in the official apps is
        # applied client-side, so downloads here may or may not go through.
        print("note:   this chat has content protection on — forwarding is blocked")
    print("scanning…", end="", flush=True)
    items = await collect(client, chat, opts)
    print(f"\r{len(items)} video(s) found" + " " * 12)
    if not items:
        state.close()
        return 0

    pending_ids = {
        i.msg_id for i in items if opts.overwrite or not state.done(chat.id, i.msg_id)
    }
    pending = [i for i in items if i.msg_id in pending_ids]
    total_bytes = sum(i.size for i in pending)
    print(
        f"{len(pending)} to fetch, {len(items) - len(pending)} already have "
        f"— {human_size(total_bytes)}"
    )

    if args.dry_run:
        for item in items:
            mark = " " if item.msg_id in pending_ids else "."
            print(
                f"{mark} {item.msg_id:>8}  {human_size(item.size):>8}  "
                f"{human_duration(item.duration):>7}  {item.filename}"
            )
        state.close()
        return 0

    if pending and not args.yes and sys.stdin.isatty():
        answer = input("proceed? [Y/n] ").strip().lower()
        if answer and not answer.startswith("y"):
            state.close()
            return 1

    summary = await run(client, chat, items, opts, state)
    state.close()
    return 1 if summary.failed else 0


COMMANDS = {
    "login": cmd_login,
    "logout": None,
    "whoami": cmd_whoami,
    "chats": cmd_chats,
    "get": cmd_get,
}


async def _run(args) -> int:
    if args.command == "logout":
        path = session_path(args.session)
        config = load_config()
        client = make_client(config, args.session)
        await client.connect()
        if await client.is_user_authorized():
            await client.log_out()
        await client.disconnect()
        path.unlink(missing_ok=True)
        print(f"signed out, removed {path}")
        return 0

    config = load_config()
    client = make_client(config, args.session)
    await client.connect()
    try:
        if args.command != "login" and not await client.is_user_authorized():
            print("not signed in — run `tf login` first", file=sys.stderr)
            return 1
        return await COMMANDS[args.command](client, args)
    finally:
        await client.disconnect()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "login":
        try:
            load_config()
        except ConfigError:
            print("First run — grab api_id/api_hash from https://my.telegram.org\n")
            api_id = input("api_id: ").strip()
            api_hash = input("api_hash: ").strip()
            path = save_config(int(api_id), api_hash)
            print(f"saved to {path}\n")
    try:
        return asyncio.run(_run(args))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted — partial files are kept, re-run to resume")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
