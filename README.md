# spooltag

Tap your phone on a spool tag, then on a tool or storage tag, and everything knows where that spool is.

spooltag is a small web service for Klipper printers, built for an 8-tool INDX on a Sovol SV08 alongside a Voron V0. NFC tags hold plain URLs, so tapping one opens a page in the phone's browser. The service pairs a spool scan with a tool or storage scan, in either order, and keeps these in step:

| Where | What | Who reads it |
|---|---|---|
| Each printer's Moonraker `lane_data` → `T<n>` | `lane`, `spool_id`, `material`, `color`, `vendor_name`, `name`, temps, optional `filament_id` | OrcaSlicer's Moonraker filament sync, plus [PipSpool Slot Sync](https://github.com/HappyPants9999/orca_spoolman_presets) |
| Spoolman spool `location` | e.g. `SV08 T3`, `Voron V0 T0`, `Dry box 1` | Spoolman, Mainsail |
| Klipper variables *(optional, per printer)* | `t3__spool_id` save variable / `T3.spool_id` | Mainsail's per-tool spool display |

Housekeeping:
- **A spool is in one place only.** Loading it into a tool, on any printer, or storing it in a location removes it from whatever tool it was in before.
- The spool a new scan pushes out of a tool is moved to the unload location set in Settings.
- The `T<n>` key matches Mainsail's, so picking a spool in Mainsail overwrites the same record instead of creating a duplicate.
- **Bambu AMS slots go through Bambuddy.** Scanning a spool and an AMS slot tag asks Bambuddy to assign that Spoolman spool to the slot (see below).

## Pages

| URL | Purpose |
|---|---|
| `/s/<spool id>` | spool tag |
| `/t/<printer>/<n>` | tool tag, e.g. `/t/sv08/3`, `/t/v0/0`; AMS slot tag, e.g. `/t/p1s/1` … `/t/p1s/4` |
| `/t/<n>` | tool of the first printer (tags from 0.1 keep working) |
| `/l/<location>` | storage tag, e.g. `/l/dry-box-1`. Scanned on its own, it shows what's stored there. |
| `/` | every printer's tools with Clear buttons, storage with spool counts, and recent scans |
| `/locations/<location>` | what's stored in a location |
| `/tags` | URL and QR code for every tool, storage location and spool |
| `/settings` | general settings, printers, storage locations, admin password, backup & restore |
| `/login` | only when an admin password is set |

Tag scans pair per phone: a spool scan and a tool or storage scan pair up only if they come from the same browser (a cookie), so two people scanning at once don't mix. Tag scans never need a login. See [Security](#security) for what's protected and what isn't.

## Deploy (Dockhand)

1. Push this repo to GitHub. The **Build image** workflow runs the tests and publishes `ghcr.io/happypants9999/spooltag:latest` for amd64 and arm64.
2. After the first build, open GitHub → your profile → **Packages → spooltag → Package settings** and set its visibility to **Public**. Otherwise you have to log in to GHCR on the Docker VM.
3. In Dockhand, create a stack from [`compose.yaml`](compose.yaml).
4. Open `/settings` and fill in **General** (Spoolman URL and tag base URL first), then add printers and storage locations.
5. Set an admin password at the bottom of `/settings`.
6. In `moonraker.conf`, add the Docker VM's IP to `[authorization] trusted_clients`, or give the printer an API key in Settings.

To update after a code change: push to GitHub, wait for the build, then redeploy the stack in Dockhand (pull the latest image).

## Settings

Everything is set on the **/settings** page and stored in `/data/spooltag.db`; the compose file only needs the image, port and data volume.

**General** (top of the page):

| Setting | Default | |
|---|---|---|
| Spoolman URL | `http://spoolman:8000` | Spoolman's base URL |
| Tag base URL | the request's host | the URL written onto tags. **Keep it stable**, because changing it breaks every tag already written |
| Spoolman location for a tool | `{printer} T{tool}` | location text for a loaded spool |
| Location for a replaced or cleared spool | *(empty = leave as is)* | e.g. `Dry rack` |
| Seconds to wait for the second scan | `120` | 10–3600 |
| Time zone | the container's | for the scan log on `/`, e.g. `America/Denver` |
| Other host names | *(none)* | host names the UI is opened by, e.g. `spooltag.lan`. IP addresses and the tag base URL's host always work |
| Allow public addresses | off | lets service URLs point at public internet addresses |

**Printers:** each Klipper printer has its own Moonraker URL, an optional API key, a tool count, and the two Klipper-variable switches. A Bambu AMS has a Bambuddy URL, API key and printer id instead.

**Environment variables** are only starting values for a new install. On start, any general setting the database doesn't have yet is taken from `SPOOLMAN_URL`, `BASE_URL`, `LOCATION_FORMAT`, `UNLOAD_LOCATION`, `PENDING_TIMEOUT` or `TZ`, and if there are no printers, `MOONRAKER_URL`, `PRINTER_NAME`, `NUM_TOOLS`, `MOONRAKER_API_KEY`, `KLIPPER_SAVE_VARIABLES` and `KLIPPER_MACRO_VARIABLE` create the first one. After that the Settings page wins and the variables can be removed.

Read on every start: `DATA_DIR` (`/data` in the image), `PUID`/`PGID` (the user the server runs as, default `10001`), `ALLOWED_HOSTS` (extra host names, comma-separated; `*` turns the Host check off) and `ADMIN_PASSWORD` (used only when no admin password is set yet).

### Bambu AMS via Bambuddy

Add the AMS as a printer of type **Bambu AMS (via Bambuddy)**:

- **URL:** Bambuddy's base URL, e.g. `http://10.10.2.176:8000`.
- **API key:** a Bambuddy API key with **Read status** and **Manage inventory**.
- **Printer id in Bambuddy:** the `id` from Bambuddy's `GET /api/v1/printers/`.
- **Tools / AMS slots:** 4 per AMS unit. Slot tags are numbered from 1, like the AMS itself: `/t/p1s/1` is AMS slot 1 (Bambuddy `ams_id 0`, `tray_id 0`). With more than one AMS, slot 5 is the second unit's first slot.

Bambuddy must be in Spoolman mode. On a scan, spooltag calls `POST /api/v1/spoolman/inventory/slot-assignments` with `move: true`. Bambuddy saves the assignment and configures the slot over MQTT (type, colour, preset, K-profile), the same as assigning it in Bambuddy's UI. spooltag then sets the Spoolman location (e.g. `P1S AMS 2`), which Bambuddy leaves alone, and moves any spool it replaced to the unload location. Loading the spool into a Klipper tool or storing it calls `DELETE …/slot-assignments/<spool id>`.

Load the spool into the AMS **before** scanning: if the printer reports the slot as empty, Bambuddy saves the assignment but doesn't configure the slot.

### Exact Orca profile matching (optional)

Orca only matches a lane to a preset by a **string** `filament_id`. If a Spoolman spool or filament has a text extra field `orca_filament_id`, spooltag passes that value through. Without it, Orca falls back to a generic profile for the material, and a slot-sync plugin can choose the exact PipSpool profile using `spool_id`.

## Backup & restore

At the bottom of **/settings** (admin only):

- **Download backup** saves `spooltag-backup-<date>-<time>.db`. It's a consistent snapshot taken with SQLite's backup API while spooltag keeps running, so there's no need to stop it. It holds the general settings, printers (with their tag ids and API keys), storage locations, the admin password hash and the scan log. Login sessions and half-finished scans are left out.
- **Restore backup** replaces **all** spooltag configuration with a backup file (10 MB max). The file is checked first: it must be an intact spooltag database, not from a newer spooltag, with no unexpected tables, views or triggers. Nothing is changed if the check fails. Then:
  1. the current database is saved to `backups/spooltag-before-restore-<time>.db` in the data folder (the newest 5 are kept);
  2. the backup is copied into the live database in one SQLite transaction, so it either fully happens or doesn't, and the file keeps its owner and `0600` mode;
  3. spooltag reloads the restored settings immediately, with no restart. Everyone is logged out and pending scans are cancelled. If the backup had an admin password, log in with that password.

  If anything fails after step 1, the previous configuration is copied back automatically.

Printer ids, location ids and the tag base URL come from the backup, so the tags written for it keep working, as long as spooltag is reachable at that tag base URL.

**Keep backups private.** A backup contains your Moonraker and Bambuddy API keys in plain text, plus the admin password hash. Store it like a password file, not in a shared folder. The safety copies in `/appdata/spooltag/backups/` contain the same things.

Spoolman, Moonraker, Bambuddy and the printers' own config are **not** part of a spooltag backup. Back those up with their own tools; Spoolman's spool locations live in Spoolman.

To restore without the web page (e.g. spooltag won't start), stop the container, copy the backup over `/appdata/spooltag/spooltag.db`, delete any `spooltag.db-journal` next to it, and start it again. The entrypoint fixes ownership and permissions on start.

If you forget a restored backup's admin password: `docker exec spooltag python -m app.admin reset-password`.

### Moving spooltag to another server

Every tag holds the tag base URL (e.g. `http://10.10.2.176:7913`), so the new server must answer at that same address.

1. On the old server, **Download backup**.
2. Give the new Docker host the old IP address (a DHCP reservation, or move the IP), or, if the tag base URL uses a host name, point that name at the new host. Keep the same host port (`7913`).
3. Deploy the stack on the new host from [`compose.yaml`](compose.yaml). The data folder can be empty.
4. Open the new server **by IP address** (until the restore, it doesn't know your host names), go to `/settings`, and **Restore backup**.
5. Log in with the backup's admin password if it had one. Then check `/` shows every printer and tap a tool tag and a spool tag.
6. Make sure Spoolman, Moonraker (`trusted_clients`) and Bambuddy accept requests from the new host's IP. Their URLs come from the backup; if any of them moved too, update them under Settings.
7. Stop the old container so two servers don't both write to Spoolman and the printers.

If the address has to change, the tags have to be rewritten: restore the backup, change the tag base URL under General, and rewrite every tag from `/tags`.

## Upgrading from 0.3 or earlier

Your settings, printers, storage locations, API keys and every tag URL carry over unchanged. Three things are new:

1. **The server no longer runs as root.** The container still starts as root, gives `/data` to user `10001:10001` (directories `0700`, files `0600`; symlinks aren't followed), then drops root and every capability before the web server starts. On the first start the log shows `gave N item(s) to 10001:10001`. You don't need to do anything, but:
   - If you add the hardening options from the new [`compose.yaml`](compose.yaml) (`cap_drop: [ALL]` etc.), keep its `cap_add` line: those capabilities are only used for that start-up step.
   - On Unraid, or to match an existing owner, set `PUID`/`PGID` (e.g. `99`/`100`).
   - If you start the container as non-root yourself (compose `user:`), first run on the Docker host: `sudo chown -R 10001:10001 /appdata/spooltag && sudo chmod -R u+rwX,go-rwx /appdata/spooltag`. Otherwise it exits with that instruction.
   - Going back to an older image needs nothing: old images run as root, so they can still read the files.
2. **The web UI only answers to IP addresses, `localhost`, the tag base URL's host, and names you list.** If you open spooltag by a name such as `spooltag.lan`, either open it by IP once and add the name under **Settings → General → Other host names**, or set `ALLOWED_HOSTS: spooltag.lan` in the compose file. A browser on a non-listed name gets "doesn't answer to the host name".
3. **Service URLs must be on your network** (private, CGNAT/Tailscale, loopback or IPv6 unique-local addresses; Docker service names and `.lan` names resolve to these). If yours is on a public address, tick **Allow public addresses** in General.

Then set an admin password (bottom of `/settings`).

## Security

spooltag is built for a home network you trust. It is **not** meant to be exposed to the internet.

**What's protected**

- **Other websites can't use your browser against spooltag** (CSRF). Forms only accept requests from spooltag's own pages (`Sec-Fetch-Site`, then `Origin`/`Referer`). Tag URLs can't carry a secret, so scans use the browser's fetch metadata instead. A tag tap, QR scan, bookmark or typed URL is a browser-started navigation and acts at once. A scan URL loaded by another website is refused if it's hidden (an image or script), or shows a **Confirm this scan?** page if you followed a link.
- **DNS rebinding:** the UI refuses host names it doesn't know (see Upgrading, step 2).
- **Optional admin password** for Settings and the Clear buttons. It's stored as an scrypt hash. Logins are HttpOnly, `SameSite=Strict` cookies that last 30 days, and 5 wrong passwords lock that address out for 15 minutes. Changing or removing the password logs out every other browser. If you forget it, run `docker exec spooltag python -m app.admin reset-password` (or `set-password`).
- **API keys are write-only** in the UI: they're never sent back to the browser. Leave the field blank to keep the saved key.
- **Outgoing requests** (Spoolman, Moonraker, Bambuddy) may only go to local-network addresses. The address is checked after DNS lookup and the connection is pinned to it. Link-local and cloud-metadata addresses are always refused, and proxy environment variables are ignored.
- **Headers:** a strict Content-Security-Policy (no inline script), no framing, `nosniff`, `Referrer-Policy: same-origin`, and `no-store` on pages.
- **Container:** runs as an unprivileged user with no capabilities, can run with a read-only root filesystem and `no-new-privileges`, and keeps the database `0600` in a `0700` folder.
- **Supply chain:** dependencies and GitHub Actions are pinned (actions by commit SHA), Dependabot proposes weekly updates (the image stays on Python 3.12 and gets its patch releases), and every pull request runs the tests plus a container smoke test before anything is published.

**Limitations (accepted risks)**

- **No password = open Settings.** Without an admin password, anyone on your network can change settings. Someone on your network can also set a password first; recover with the `reset-password` command above.
- **Tag scans are open by design.** Anyone who can reach the page can move spools by opening tag URLs. A link opened from *another app* (a chat message, an email) looks to the browser exactly like a tag tap, so it acts without a confirm page. The result page shows what changed.
- **Browsers older than Chrome 76 or Safari 16.4** don't send fetch metadata. For those, spooltag falls back to the `Referer` header, which a page can suppress, so a hidden cross-site request could still trigger a scan. Even then, it can only pair with a scan from the same browser.
- **Both scans must happen in the same browser.** If a phone opens one tag in Safari and the other in an app's built-in browser, they won't pair. Private-browsing tabs or blocked cookies stop pairing too.
- **Plain HTTP on the LAN.** Passwords and cookies cross your network unencrypted unless you put spooltag behind an HTTPS reverse proxy (it honours `X-Forwarded-Proto` from `127.0.0.1` only). API keys to Moonraker and Bambuddy also travel over plain HTTP.
- **API keys are stored in plain text** in `spooltag.db`, in downloaded backups, and in the safety copies under `backups/`. The database and safety copies are `0600` and owned by the service user, so keep host access to `/appdata/spooltag` restricted. Treat downloaded backups like a password file.
- **Moonraker trusts your LAN.** Your printers already accept commands from any address in `trusted_clients`, which is more access than spooltag adds.

**Recommended deployment**

- Use the provided [`compose.yaml`](compose.yaml) as is: read-only root filesystem, `cap_drop: [ALL]`, `no-new-privileges`, and data on local disk.
- Set an admin password.
- Don't forward the port on your router. For remote access, use a VPN such as WireGuard or Tailscale rather than exposing the page.
- If you can, bind the port to the LAN interface only (`"10.10.2.176:7913:8000"`), or limit it with the host firewall.
- If you add an HTTPS reverse proxy, add its host name under **Other host names**, and set the tag base URL to the HTTPS address only before writing tags.
- Merge Dependabot's pull requests once their checks pass.

## Writing tags

1. Open `/tags` on a computer.
2. On the phone, point the camera at a QR code and copy the URL.
3. In NFC Tools: **Write → Add a record → Custom URL**, paste the URL, and write it to the tag.

Write tool and storage tags once. Rewrite a box's spool tag whenever a new spool goes into that box.

## Develop

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
SPOOLMAN_URL=http://<vm>:7912 MOONRAKER_URL=http://<printer>:7125 DATA_DIR=./data \
  uvicorn app.main:app --reload --port 8090      # open http://127.0.0.1:8090 (IP, so the Host check passes)
```
