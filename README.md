# Kanbanano

A small self-hosted kanban board for a household or a few people: boards, lists and
cards with drag and drop, labels, people, checklists, and changes from other people
showing up within a few seconds. Auth relies on one shared password instead of user
accounts.

It's a FastAPI app with server-rendered HTML (htmx, Alpine.js, SortableJS) and a
single SQLite file. There's no build step, and the JavaScript libraries are bundled,
so it works on a network with no internet access.

## Running it

You need Docker with the Compose plugin.

1. Create your settings:

   ```sh
   cp .env.example .env
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # paste as SECRET_KEY
   ```

   Then set `BOARD_PASSWORD` in `.env`.

2. Start it:

   ```sh
   docker compose up -d --build
   ```

The app listens on `127.0.0.1:8000`, so only this machine can reach it. To reach it
from other devices, put Tailscale Serve in front (recommended) or open it up to the
LAN (below).

The data lives in a Docker volume called `kanbanano_data`, so it survives rebuilds.

### Behind Tailscale Serve

```sh
tailscale serve --bg 8000
```

This serves the app at `https://<machine>.<tailnet>.ts.net` to devices on your
tailnet, with HTTPS handled by Tailscale. The exact syntax has changed between
Tailscale versions; check `tailscale serve --help` if that doesn't work.

### Directly on your LAN

In `compose.yaml`, change the port line to `"8000:8000"`, and in `.env` set
`ALLOW_HTTP=1`. Then open `http://<server-ip>:8000`.

`ALLOW_HTTP` is needed because the login cookie is normally marked Secure, and
browsers won't send a Secure cookie back over plain HTTP, so you'd log in and end up
on the login page again. Over plain HTTP the password crosses your network
unencrypted.

### Updating

```sh
git pull
docker compose up -d --build
```

## Configuration

Set these in `.env`.

| Variable         | Required | Meaning                                                        |
| ---------------- | -------- | -------------------------------------------------------------- |
| `BOARD_PASSWORD` | yes      | The password everyone uses to log in.                          |
| `SECRET_KEY`     | yes      | Signs the login cookie. Changing it logs out every device.     |
| `ALLOW_HTTP`     | no       | `1` to allow logging in over plain `http://`. Default `0`.     |

The app refuses to start if a required variable is missing.

Logins last a year from a device's last visit. Changing `BOARD_PASSWORD` doesn't
log anyone out, because a session doesn't record which password was used. To log
everyone out, change `SECRET_KEY` as well, then run `docker compose up -d`.

## Backups

`backup.sh` copies the database out of the running container:

```sh
./backup.sh /path/to/backups
```

This writes `board-YYYY-MM-DD.db`. It's safe to run while people are using the
board. To run it nightly, add a cron entry for a user that can run `docker`:

```cron
0 3 * * * /path/to/kanbanano/backup.sh /path/to/backups
```

Backups aren't pruned. To keep the last 30 days, add something like
`find /path/to/backups -name 'board-*.db' -mtime +30 -delete` to the same crontab.

### Restoring

Stop the app, replace the database, and start it again:

```sh
docker compose stop
docker compose run --rm --no-deps -T kanbanano \
  sh -c 'rm -f /data/board.db-wal /data/board.db-shm && cat > /data/board.db' \
  < /path/to/backups/board-2026-09-16.db
docker compose start
```

This uses a throwaway container instead of `docker cp` so the restored file is owned
by the app's user instead of root.

## Development

You need [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run seed.py     # optional: two sample boards in ./board.db
BOARD_PASSWORD=dev SECRET_KEY=dev uv run uvicorn app.main:app --reload
```

Then open <http://localhost:8000>. Browsers treat `localhost` as secure, so logging
in works without `ALLOW_HTTP`. If you use another hostname or an IP, add
`ALLOW_HTTP=1`.

Run the tests with:

```sh
uv run pytest
```

The tests use their own temporary databases and never touch `board.db`.
