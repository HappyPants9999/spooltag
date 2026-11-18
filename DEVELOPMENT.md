# Developing Spooltag

This file is for people changing Spooltag's code. To install and use Spooltag, see the [README](README.md).

## Run it locally

You need Python 3.12 (the version the image uses).

```bash
git clone https://github.com/HappyPants9999/spooltag
cd spooltag
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

To start the server against your own services:

```bash
SPOOLMAN_URL=http://192.168.1.10:7912 MOONRAKER_URL=http://192.168.1.50:7125 DATA_DIR=./data \
  uvicorn app.main:app --reload --port 8090
```

Open `http://127.0.0.1:8090`, using the IP address: the host-name check rejects unknown names. The environment variables only seed a new `./data/spooltag.db`; after that, use the Settings page.

To build and run the image locally:

```bash
docker build -t spooltag:dev .
docker run --rm -p 8090:8000 -v "$PWD/data:/data" spooltag:dev
```

## Project layout

| Path | What's in it |
|---|---|
| `app/main.py` | FastAPI app: routes, request guards, login, settings, backup/restore and icon routes |
| `app/service.py` | What a scan does: assign to a Klipper tool or AMS slot, store, clear, "one place per spool" |
| `app/clients.py` | Spoolman, Moonraker and Bambuddy API clients; the `lane_data` record format |
| `app/store.py` | SQLite database: settings, printers, locations, per-device pending scans, sessions, log |
| `app/pages.py` | HTML pages. One shared `page()` head, CSS, and the only script (`APP_JS`, served as `/static/app.js`) |
| `app/websec.py` | Host allowlist, cross-site request checks, security headers, device cookie |
| `app/auth.py` | Optional admin password: scrypt hashing, sessions, login rate limit |
| `app/netguard.py` | Outgoing-request guard (SSRF): resolves, checks and pins addresses |
| `app/backup.py` | Backup snapshot, restore validation and the transactional restore |
| `app/entrypoint.py` | Container start-up: fixes `/data` ownership, drops root, starts the server |
| `app/admin.py` | `python -m app.admin reset-password` / `set-password` |
| `app/config.py` | Settings and environment variables |
| `app/static/` | Favicon and web-app icons, served from the site root |
| `tests/` | `test_app.py` (scanning and integrations, with simulated services), `test_security.py`, `test_backup.py`, `test_icons.py` |

## Tests

```bash
pytest -q                 # everything
pytest -q tests/test_security.py -k cross_site   # one area
```

The tests use `FakeBackends` in `tests/test_app.py`, a simulated Spoolman, Moonraker and Bambuddy behind an `httpx.MockTransport`, and a fake DNS resolver. No real printer or network is needed.

A few tests only run as root (the entrypoint's ownership change) or only as a non-root user (permission errors). CI covers the real container in its `smoke` job.

## Rules the code relies on

Keep these intact when changing things; tests check most of them.

- **No inline JavaScript or event-handler attributes.** The Content-Security-Policy allows scripts only from `/static/app.js`. Confirm dialogs use `data-confirm`.
- **Escape everything** placed in HTML with `html.escape`, including names from Spoolman and the database.
- **G-code gets numbers only.** Spooltag sends just `SAVE_VARIABLE` and `SET_GCODE_VARIABLE`, built from integers.
- **Outgoing HTTP goes through the shared client.** It's the one wrapped in `GuardedTransport`, with `trust_env=False`; don't create other `httpx` clients.
- **State-changing requests are POST**, and Settings and admin actions take `dependencies=admin`. Scan routes stay GET for NFC tags, go through `scan_guard`, and have POST twins for the confirm page.
- **Tag addresses are permanent.** Don't change the `/s/`, `/t/`, `/l/` routes, or how printer and location ids are stored.
- **Database changes:** if you add a table or column, update `_init_schema` in `store.py` (with a migration for existing databases), raise `SCHEMA_VERSION` if older Spooltag versions couldn't read the result, and update `KNOWN_TABLES`/`REQUIRED` in `backup.py` so restores accept the new layout.

## CI, images and releases

`.github/workflows/docker.yml` has three jobs:

- **test** runs `pytest` on Python 3.12.
- **smoke** builds the image, starts it with the hardened Compose settings against a deliberately root-owned data folder, and checks the user, capabilities, file modes, request guards and icons.
- **publish** builds `linux/amd64` and `linux/arm64` and pushes `ghcr.io/happypants9999/spooltag`. It runs only for pushes to `main` (tags `latest` and `sha-<commit>`), version tags `v*` (tag `<version>`), and manual runs. Pull requests run **test** and **smoke** only.

GitHub Actions are pinned to commit SHAs, and jobs get only the permissions they need. Dependabot (`.github/dependabot.yml`) proposes weekly updates for pip packages, Actions and the base image. The base image stays on Python 3.12 (`3.12.x-slim`): minor and major Python updates are ignored for now.

## Security checks used during development

Besides the test suite, these have been run on the code: `bandit -r app`, `pip-audit -r requirements.txt`, `semgrep` with the community Python, Dockerfile and GitHub Actions rules, and `detect-secrets`. They're useful before a pull request that touches request handling, files or the database.
