# Kanbanano

A small self-hosted kanban board for a household or a few people. Auth relies on one
shared password instead of user accounts.

- Boards, lists and cards, with drag and drop
- Labels and people, and a filter for both
- Checklists on cards, and Markdown card descriptions
- A lists panel for putting lists you aren't using out of the way
- A board background: one of nine colors, any custom color, or a photo
- An archive for cards, lists and boards, so nothing is deleted by accident
- A pomodoro timer
- Changes from other people showing up within a few seconds

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

By default the app listens on `127.0.0.1:18001`, so only this machine can reach it.
To reach it from other devices, either serve it on your LAN or put Tailscale in
front. Both are below.

The data lives in a Docker volume called `kanbanano_data`, so it survives rebuilds.

### On your LAN

Set these in `.env`:

```sh
BIND_ADDRESS=0.0.0.0
ALLOW_HTTP=1
```

Then run `docker compose up -d` and open `http://<server-ip>:18001`. Include the
`http://`: some browsers try HTTPS first if you leave it off.

`BIND_ADDRESS=0.0.0.0` publishes the port on every network interface instead of
only on this machine. `ALLOW_HTTP=1` is needed because the login cookie is normally
marked Secure, and browsers won't send a Secure cookie back over plain HTTP, so
you'd log in and end up on the login page again.

Plain HTTP isn't encrypted. Anyone who can see traffic on your network can read the
board, the password when someone logs in, and the login cookie. The cookie is sent
with every request and gets its holder in for a year without the password. That's
a reasonable tradeoff on a home network where you trust every device, but not on a
shared one.

### Behind Tailscale

Both options below need the default `BIND_ADDRESS=127.0.0.1` and `ALLOW_HTTP=0`,
and need MagicDNS and HTTPS Certificates enabled for the tailnet (Admin console →
DNS). Without certificates, Tailscale accepts the connection and then fails the TLS
handshake, which browsers report as an unhelpful generic error.

Each device that connects needs Tailscale installed and signed in to your tailnet.
The exact syntax has changed between Tailscale versions; check
`tailscale serve --help` if a command doesn't work.

**On the machine's own name.** The simplest form:

```sh
tailscale serve --bg 18001
```

The app is then at `https://<machine>.<tailnet>.ts.net`, and it takes over the root
of that name, so the machine can only serve one app this way.

**On its own name, as a service.** Better if the machine hosts anything else:

```sh
tailscale serve --service=svc:kanbanano --bg 18001
```

This gives the app its own virtual IP and its own name,
`https://kanbanano.<tailnet>.ts.net`, independent of the machine hosting it. It
stays at the root of that name, so nothing here has to know about path prefixes,
and other apps on the same machine get their own names the same way.

A service needs approving in the admin console and needs its traffic allowed in the
tailnet policy file before it will answer. `tailscale serve status` shows whether
it's up. Note that clients need a recent Tailscale for virtual-IP services. Older clients may not resolve the name.

Serving through Tailscale doesn't close the published port. The app stays reachable
at `127.0.0.1:18001` on the host, and on the LAN too if `BIND_ADDRESS` is `0.0.0.0`.

### Moving from LAN to Tailscale

1. In `.env`, set `BIND_ADDRESS=127.0.0.1` and `ALLOW_HTTP=0`. Otherwise the app
   stays reachable over plain HTTP alongside Tailscale.
2. Change `SECRET_KEY`. Login cookies issued over HTTP stay valid for up to a year
   and may have been seen on the network; a new key invalidates them, and everyone
   logs in once more.
3. Run `docker compose up -d`, then set up Tailscale Serve as above.

### Updating

```sh
git pull
docker compose up -d --build
```

## Configuration

Set these in `.env`.

| Variable         | Required | Meaning                                                                                           |
| ---------------- | -------- | ------------------------------------------------------------------------------------------------- |
| `BOARD_PASSWORD` | yes      | The password everyone uses to log in.                                                             |
| `SECRET_KEY`     | yes      | Signs the login cookie. Changing it logs out every device.                                        |
| `BIND_ADDRESS`   | no       | Where the port is published. `127.0.0.1` (default) for this machine only, `0.0.0.0` for your LAN. |
| `ALLOW_HTTP`     | no       | `1` to allow logging in over plain `http://`. Default `0`.                                        |

The app refuses to start if a required variable is missing. After changing `.env`,
run `docker compose up -d` to apply it.

Logins last a year from a device's last visit. Changing `BOARD_PASSWORD` doesn't
log anyone out, because a session doesn't record which password was used. To log
everyone out, change `SECRET_KEY` as well, then run `docker compose up -d`.

## Tradeoffs

The app's design makes the following assumptions:

- **Only devices on a network you control can reach it.** One shared password
  gets you in, and there is no rate limiting or lockout to slow down guessing.
  Somewhere public, guesses are limited only by how fast the network answers.
  Use a long password, and host the app behind Tailscale or on your own LAN.
- **Everyone with the password is trusted.** People are labels, the same as
  any other label, and there are no accounts. Nothing records who changed what,
  and anyone who's in can do anything, including permanently deleting boards.
- **Everyone's browser honors `SameSite`.** The app marks the login cookie
  `SameSite=Lax`, which stops another site from acting as you. Since there are
  no CSRF tokens, an older browser can allow any page you open to post to the
  board while you're logged in.

## Backups

`backup.sh` copies the database out of the running container:

```sh
./backup.sh /path/to/backups
```

This writes `board-YYYY-MM-DD.db`. It's safe to run while people are using the
board. Uploaded background photos are in the database too, so they're included. To run it nightly, add a cron entry for a user that can run `docker`:

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

Lint and format with [Ruff](https://docs.astral.sh/ruff/):

```sh
uv run ruff check .
uv run ruff format .
```
