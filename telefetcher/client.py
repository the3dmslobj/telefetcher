"""Session handling and turning whatever the user typed into a real chat."""

from __future__ import annotations

import re
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInviteAlready, ChatInvitePeek

from .config import Config, session_path

INVITE_RE = re.compile(r"(?:t\.me/(?:joinchat/|\+)|^\+)([A-Za-z0-9_-]{8,})")
PRIVATE_LINK_RE = re.compile(r"t\.me/c/(\d+)")
PUBLIC_LINK_RE = re.compile(r"t\.me/([A-Za-z][A-Za-z0-9_]{3,})")


class ChatNotFound(RuntimeError):
    pass


def _lock_down(path: Path) -> None:
    """A session file is a live login — nobody else on the box needs to read it."""
    try:
        if path.exists() and path.stat().st_mode & 0o077:
            path.chmod(0o600)
    except OSError:
        pass


def secure_session(session: str) -> None:
    """Call after connecting — Telethon creates the file lazily, so the chmod in
    make_client runs too early to catch a brand new session."""
    _lock_down(session_path(session))


def make_client(config: Config, session: str) -> TelegramClient:
    path = session_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    _lock_down(path)
    return TelegramClient(
        str(path.with_suffix("")),
        config.api_id,
        config.api_hash,
        device_model="telefetcher",
        system_version="1.0",
        app_version=__import__("telefetcher").__version__,
    )


async def interactive_login(client: TelegramClient) -> None:
    """Phone -> code -> optional 2FA password. Telethon persists the session."""
    session_file = Path(f"{client.session.filename}")
    if await client.is_user_authorized():
        _lock_down(session_file)
        return
    phone = input("Phone number (international format, e.g. +15551234567): ").strip()
    await client.send_code_request(phone)
    code = input("Login code from Telegram: ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        import getpass

        password = getpass.getpass("Two-step verification password: ")
        await client.sign_in(password=password)
    _lock_down(session_file)


async def _from_invite(client: TelegramClient, invite_hash: str):
    """An invite link resolves without joining, as long as you're already in."""
    result = await client(CheckChatInviteRequest(invite_hash))
    if isinstance(result, (ChatInviteAlready, ChatInvitePeek)):
        return result.chat
    raise ChatNotFound(
        "That invite link points to a chat you haven't joined yet. "
        "Open it in Telegram and join first, then re-run."
    )


async def _by_title(client: TelegramClient, needle: str):
    """Private channels have no @username, so fall back to matching titles."""
    needle_lower = needle.casefold()
    exact, partial = [], []
    async for dialog in client.iter_dialogs():
        title = (dialog.name or "").casefold()
        if title == needle_lower:
            exact.append(dialog)
        elif needle_lower in title:
            partial.append(dialog)

    matches = exact or partial
    if not matches:
        raise ChatNotFound(
            f"No joined chat matches {needle!r}. Run `tf chats` to see what's available."
        )
    if len(matches) > 1:
        listing = "\n".join(f"  {d.id:>16}  {d.name}" for d in matches[:10])
        raise ChatNotFound(
            f"{needle!r} matches {len(matches)} chats — use the id instead:\n{listing}"
        )
    return matches[0].entity


async def resolve_chat(client: TelegramClient, ref: str):
    """Accepts an id, @username, t.me link, invite link, or channel title."""
    ref = ref.strip()

    invite = INVITE_RE.search(ref)
    if invite:
        return await _from_invite(client, invite.group(1))

    private = PRIVATE_LINK_RE.search(ref)
    if private:
        return await client.get_entity(int(f"-100{private.group(1)}"))

    if re.fullmatch(r"-?\d+", ref):
        chat_id = int(ref)
        try:
            return await client.get_entity(chat_id)
        except (ValueError, TypeError):
            # Telethon needs the peer cached; a dialog sweep populates it.
            async for dialog in client.iter_dialogs():
                if dialog.id == chat_id:
                    return dialog.entity
            raise ChatNotFound(f"No joined chat with id {chat_id}.") from None

    if ref.startswith("@") or PUBLIC_LINK_RE.search(ref):
        handle = ref if ref.startswith("@") else "@" + PUBLIC_LINK_RE.search(ref).group(1)
        try:
            return await client.get_entity(handle)
        except (ValueError, TypeError):
            pass

    return await _by_title(client, ref)
