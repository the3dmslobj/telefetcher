# telefetcher

Download videos from Telegram channels and groups **you're a member of**, including
private ones.

## Why this needs your account, not a bot

The Telegram Bot API can't read a private channel unless the bot has been added to it
as an admin. Membership belongs to *your* account, so telefetcher signs in as you over
MTProto (via [Telethon](https://docs.telethon.dev)) and reads exactly what you can
already read in the app. It never joins anything on your behalf — if you haven't joined
a channel, it will tell you to join it in Telegram first.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./tf login
```

`login` asks for an `api_id` / `api_hash` the first time. Get them from
<https://my.telegram.org> → *API development tools* → create an app. They identify the
app, not your account. Then it asks for your phone number, the code Telegram sends you,
and your two-step password if you have one.

Credentials land in `~/.telefetcher/config.json` and the login session in
`~/.telefetcher/default.session` — both `chmod 600`, both outside the repo. The session
file is a live login: treat it like a password, and run `./tf logout` to revoke it.

## Use

```bash
./tf chats                       # every chat you've joined, with ids
./tf chats leaks                 # ...filtered by title

./tf get "My Private Channel"    # all videos -> ./My Private Channel/
./tf get -1001234567890 -n 10    # by id, newest 10
./tf get "https://t.me/+AbCdEfGh"  # by invite link (must already be joined)
```

A private channel has no `@username`, so the friendliest handle is its **title** — the
same text you see in the app. Ids and `t.me/c/…` links work too, and `./tf chats` prints
both.

### Picking what to grab

```bash
./tf get "Channel" --dry-run              # list first, download nothing
./tf get "Channel" --since 2025-01-01 --min-size 50MB
./tf get "Channel" --search "episode"     # only messages mentioning it
./tf get "Channel" --new                  # only what's arrived since last run
./tf get "Channel" --gifs --notes         # also GIFs and round video notes
./tf get "Channel" -o ~/Movies/dump -j 4  # custom folder, 4 parallel downloads
```

| flag | effect |
| --- | --- |
| `-n, --limit N` | stop after N videos |
| `--since` / `--until` | date window, `YYYY-MM-DD` |
| `--min-id` / `--max-id` | message id window |
| `--new` | resume from the highest id downloaded last time |
| `--min-size` / `--max-size` | `900K`, `50MB`, `1.5G` |
| `--oldest-first` | walk history forwards instead of newest-first |
| `-j, --workers` | parallel downloads (default 2) |
| `--overwrite` | re-download files already present |
| `--dry-run` | print the plan and exit |

### Interrupting is safe

Each file streams into `name.part` and is renamed only when complete, so Ctrl-C loses at
most the last megabyte. Re-run the same command and it picks up mid-file. Finished
downloads are logged in `<out>/.telefetcher/state.db`, so a second run skips them without
re-checking the network.

## Notes

- Telegram rate-limits heavy downloading. On a `FloodWaitError` telefetcher prints the
  wait and sleeps it out rather than failing; keep `--workers` low (2–4) on big channels.
- Some channels set *content protection* (`noforwards`). Forwarding those messages is
  refused by Telegram itself (`CHAT_FORWARDS_RESTRICTED`), but the save lock you see in
  the official apps is applied by the app, not the server, so a download may still go
  through. telefetcher prints a note when a chat has the flag, and lets the attempt
  report its own result rather than guessing. Respect the channel owner's intent, and
  keep in mind the videos may not be yours to redistribute.
- Files are named `<message id>_<caption or original name>.<ext>` so a folder sorts in
  channel order and nothing collides.

## Tests

```bash
.venv/bin/python tests_offline.py     # naming, filters, parsers, state ledger
.venv/bin/python tests_download.py    # download / resume / retry against a stub client
```

Both run entirely offline — no login, no network.
