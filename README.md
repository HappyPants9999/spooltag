<p align="center">
  <img src="app/static/web-app-manifest-192x192.png" width="128" height="128" alt="Spooltag logo: a filament spool with an NFC tag">
</p>

# Spooltag

**Tap your phone on a spool, then on a printer tool or a storage box, and every part of your setup knows where that spool is.**

Spooltag is a small self-hosted web service for people who track filament in [Spoolman](https://github.com/Donkie/Spoolman). You stick cheap NFC tags (or printed QR codes) on your spools, on each printer tool or AMS slot, and on your shelves and dry boxes. Each tag holds a plain web address, so a phone opens it in its normal browser. You don't need an app to scan.

When you scan a spool and then a tool, Spooltag:

- tells Spoolman where the spool is now (its **location**, e.g. `Toolchanger T3` or `Dry box 1`);
- records the spool against that tool in the Klipper printer's Moonraker database, where **OrcaSlicer** and **Mainsail** can read it;
- or, for a **Bambu Lab AMS**, asks **Bambuddy** to assign the spool to that AMS slot;
- removes the spool from any tool or slot it was in before, because a spool can only be in one place.

> **Project status:** Spooltag is a hobby project. Its behaviour is covered by automated tests that use simulated Spoolman, Moonraker and Bambuddy services, but it has **not yet been verified on physical printers**. See [What has been tested](#what-has-been-tested) and the [disclaimer](#disclaimer).

## Contents

- [How scanning works](#how-scanning-works)
- [What you need](#what-you-need)
- [Quick start](#quick-start)
- [Using Spooltag](#using-spooltag)
- [Optional: OrcaSlicer and PipSpool Integration](#optional-orcaslicer-and-pipspool-integration)
- [Bambu AMS via Bambuddy](#bambu-ams-via-bambuddy)
- [Configuration](#configuration)
- [Backup, restore and moving servers](#backup-restore-and-moving-servers)
- [Upgrading](#upgrading)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [What has been tested](#what-has-been-tested)
- [Disclaimer](#disclaimer)
- [Contributing and support](#contributing-and-support)

## How scanning works

Every assignment takes **two scans from the same phone**:

1. **Scan a spool tag.** The page shows the spool and says *Now scan a tool or storage tag*.
2. **Within two minutes, scan a tool, AMS slot or storage tag.** The page shows what changed, with a green or red line for each system it updated.

The order doesn't matter: tool first, then spool, works too. The waiting time is adjustable. The two scans are paired by a browser cookie, so two people scanning at the same time don't interfere with each other.

Spooltag uses four kinds of tag:

| Tag | Address | Example |
|---|---|---|
| Spool | `/s/<Spoolman spool id>` | `/s/42` |
| Klipper tool | `/t/<printer id>/<tool number>`, counted from 0 | `/t/toolchanger/3`, `/t/mini/0` |
| AMS slot | `/t/<printer id>/<slot number>`, counted from 1 | `/t/ams/1` … `/t/ams/4` |
| Storage location | `/l/<location id>` | `/l/dry-box-1` |

You choose the printer and location ids when you add them in Settings. They're written into the tags, so they can't be changed afterwards.

## What you need

| Component | Required? | What it's for |
|---|---|---|
| **Docker** (with Compose) | Required | Runs Spooltag. Images are published for `amd64` and `arm64`, so a Raspberry Pi 4/5 works. |
| **[Spoolman](https://github.com/Donkie/Spoolman)** | Required | Spooltag looks up spools in Spoolman and writes each spool's location there. |
| **Moonraker** (Klipper) | For Klipper printers | Spooltag stores each tool's spool in Moonraker's database (`lane_data`). Nothing is added to your Klipper config for this. |
| **[Bambuddy](https://github.com/maziggy/bambuddy)** in Spoolman mode | For a Bambu Lab AMS | Spooltag never talks to a Bambu printer directly; it asks Bambuddy to assign the spool to the slot. |
| **NFC tags** and a phone with NFC | Recommended | Small NTAG213/215/216 stickers work well; the addresses fit easily. A tag-writing app such as NFC Tools writes them. |
| **QR codes** | Alternative to NFC | Every tag address also has a QR code you can print. Scanning one with the phone camera works like tapping a tag. |
| **OrcaSlicer**, **PipSpool**, **PipSpool Slot Sync** | Optional | Loaded spools show up in OrcaSlicer's filament slots. See [the OrcaSlicer section](#optional-orcaslicer-and-pipspool-integration). |
| **Mainsail** | Optional | Can show each tool's spool. See [Klipper variables](#klipper-variables-optional). |

You can start with storage locations only (Spoolman + Spooltag) and add printers later.

**Klipper printers:** both **single-tool** printers (one tool, tag `/t/<id>/0`) and **multi-tool** printers (toolchangers, IDEX, multi-extruder; up to 32 tools, tags `/t/<id>/0` … `/t/<id>/31`) are supported. Spooltag stores the spool under key `T<n>` in Moonraker's `lane_data`, the same key Mainsail uses, so the two don't create duplicate records. Spooltag doesn't control the printer or its tool-change macros, so it works alongside whatever toolchanger macros you use.

**Bambu Lab AMS:** see [Bambu AMS via Bambuddy](#bambu-ams-via-bambuddy).

## Quick start

You don't need to clone or build anything; the image is published at `ghcr.io/happypants9999/spooltag`.

### 1. Start the container

Create a folder (for example `spooltag/`) with this `compose.yaml`:

```yaml
services:
  spooltag:
    image: ghcr.io/happypants9999/spooltag:latest
    container_name: spooltag
    restart: unless-stopped
    ports:
      - "7913:8000"                 # Spooltag on port 7913 of the Docker host
    volumes:
      - ./data:/data                # settings database; use local disk, not a network share
    # Recommended hardening. The container starts as root only long enough to give /data
    # to its service user (10001), then drops root and every capability.
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: [ALL]
    cap_add: [CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID]   # start-up only
    security_opt:
      - no-new-privileges:true
```

Then run:

```bash
docker compose up -d
```

The same file is in this repository as [`compose.yaml`](compose.yaml). Any Compose-based tool can run it, such as Portainer, Dockge or Dockhand, if you'd rather paste it into a web UI. To update later, run `docker compose pull && docker compose up -d`, or use your tool's "pull and redeploy".

### 2. Configure it in the browser

Open `http://<docker-host-ip>:7913/settings`, for example `http://192.168.1.10:7913/settings`. Open it by IP address the first time; host names have to be allowed in Settings first.

1. **General** (top of the page):
   - **Spoolman URL**, e.g. `http://192.168.1.10:7912`.
   - **Tag base URL**: the address phones use to reach Spooltag, e.g. `http://192.168.1.10:7913`. **Every tag stores this address, so choose one that won't change.** A DHCP reservation for the Docker host is a good idea.
   - Optionally, a **Location for a replaced or cleared spool** (e.g. `Dry rack`), and your **time zone**.
2. **Printers:** add each Klipper printer with its Moonraker URL (e.g. `http://192.168.1.50:7125`) and its number of tools, or add an AMS through Bambuddy. Each one gets a short id for its tags (e.g. `toolchanger`).
3. **Storage locations:** add shelves, dry boxes and so on (e.g. `Dry box 1`).
4. **Admin password** (near the bottom): set one. Without it, anyone on your network can change Settings.

Moonraker must accept requests from the Docker host. Either its IP is covered by `[authorization] trusted_clients` in `moonraker.conf`, or you enter a Moonraker API key for that printer in Settings.

### 3. Write your tags

Open **Tags** (`/tags`) and write one tag for each tool, AMS slot and storage location, plus one for each spool. See [Creating and writing tags](#creating-and-writing-tags).

## Using Spooltag

### Pages

| Page | What it shows |
|---|---|
| **Status** (`/`) | Each printer's tools or AMS slots with the loaded spool, its colour and a **Clear** button; storage locations with spool counts; recent scans. Refreshes every 30 seconds. |
| **Tags** (`/tags`) | The address and QR code of every tool, AMS slot, storage location and Spoolman spool, each with a **Copy URL** button and a filter box for spools. |
| **Settings** (`/settings`) | General settings, printers, storage locations, admin password, backup & restore. |
| `/locations/<id>` | The spools stored in one location. Scanning a storage tag on its own shows the same list. |

### Creating and writing tags

1. Open **Tags** on your phone.
2. Tap **Copy URL** next to the tag you want.
3. In a tag-writing app such as NFC Tools (Android and iOS): **Write → Add a record → Custom URL**, paste the address, tap **Write**, and hold the phone to the tag.

Write tool, AMS slot and storage tags once. They never change unless you delete and re-create that printer or location.

For spools, either stick a tag on each spool, or keep one tag per dry box or holder and rewrite it when its spool changes.

To check a tag, tap it with the phone. A spool tag should show the spool and ask for a second scan.

### QR codes instead of NFC

Every entry on the Tags page has a QR code. You can print them as labels, for example on a sticker on each tool or shelf. Scanning a QR code with the phone's camera opens the same address as an NFC tap and behaves exactly the same way. You can mix NFC and QR freely.

### Loading a spool

Scan the spool, then the tool or AMS slot (or the other way round). Spooltag then does the following, and shows a line for each step:

- **Klipper tool:** writes the spool to that tool's `T<n>` entry in Moonraker `lane_data`. The entry holds spool id, material, colour, vendor, filament name and temperatures, plus `filament_id` if you set one. Spooltag also sets the Spoolman location (default format `{printer} T{tool}`, e.g. `Toolchanger T3`).
- **AMS slot:** asks Bambuddy to assign the spool to the slot and sets the Spoolman location to `<printer name> AMS <slot>`, e.g. `Bambu AMS 2` for an AMS added under the name `Bambu`.
- The spool is **removed from any other tool or AMS slot**, on any printer.
- If another spool was in that tool, it's moved to the **unload location** from Settings (if you set one).

If a printer is switched off, the Spoolman location is still set. The page shows a red line for the printer step, so you know to scan again later.

### Unloading and moving spools

- **Move a spool to storage:** scan the spool and a storage tag. Spoolman's location becomes that location's name, and the spool is removed from any tool or AMS slot.
- **Move a spool to another tool or printer:** scan the spool and the new tool; the old tool is cleared automatically.
- **Empty a tool without storing the spool:** press **Clear** for that tool on the Status page (an admin login is needed if you set a password). The spool goes to the unload location if one is set.
- **See what's in a storage location:** scan its tag on its own, or open it from the Status page.
- **Cancel a half-finished scan:** use the **Cancel** button on the Status page, or just wait for the timeout.

### Klipper variables (optional)

Each Klipper printer has two optional switches in Settings, for macros or Mainsail panels that read the spool from Klipper itself:

- **SAVE_VARIABLE:** stores `t<n>__spool_id` with `SAVE_VARIABLE`. Needs a `[save_variables]` section in `printer.cfg`.
- **SET_GCODE_VARIABLE:** sets `spool_id` on the `T<n>` macro. Needs `variable_spool_id` on each `T<n>` macro.

Both are off by default. The only G-code Spooltag ever sends is these two commands, with numeric values.

## Optional: OrcaSlicer and PipSpool Integration

Spooltag can make each loaded spool's exact filament profile show up in OrcaSlicer's filament slots, and Spoolman can then track usage per tool. **All of this is optional.** Spooltag works on its own as a spool location tracker.

The workflow uses four pieces:

1. **[PipSpool](https://github.com/Gadonk/pipspool-orcaslicer) creates a filament profile for each spool.** It's an OrcaSlicer plugin that creates one profile per active Spoolman spool, named like `(#42) PLA Galaxy Blue - Manufacturer - PipSpool`. It can also put a spool-ID command (by default `SET_ACTIVE_SPOOL ID=<id>`) in each profile's start G-code.
2. **Spooltag assigns physical spools to printer tools** when you scan NFC tags or QR codes. It writes each tool's spool id, colour and material to Moonraker's `lane_data`.
3. **OrcaSlicer's "Synchronize filament list"** reads the printer's tool count and filament colours from Moonraker through its Moonraker printer connection.
4. **[PipSpool Slot Sync](https://github.com/HappyPants9999/orca_spoolman_presets) selects the profile for each slot.** It's a companion OrcaSlicer plugin. For each slot, it reads the loaded spool's id from `lane_data` and selects the existing PipSpool profile with that id: spool 42 gets `(#42) … - PipSpool`.

**About PipSpool Slot Sync:**

- It's **optional**. Without it, OrcaSlicer still shows the right slot count and colours, and you pick profiles yourself.
- It **requires an OrcaSlicer build that supports Python plugins** (2.5 alpha/nightly or later, per the plugin's README).
- It **doesn't create or modify filament presets**; PipSpool owns them. It only changes which existing preset each slot uses in the current project, as if you had picked it by hand.
- It also needs Moonraker's `[spoolman]` section, and PipSpool synchronized (restart OrcaSlicer after PipSpool adds profiles).
- If the original "Orca Spoolman Presets" plugin is installed, uninstall it first.
- Install and usage instructions are in the [plugin's README](https://github.com/HappyPants9999/orca_spoolman_presets#readme). In short: **Plugins → Install local plugin**, enter your Moonraker URL, then press **Synchronize filament list** followed by **Sync slots from printer**.

**For Spoolman usage tracking per tool** (optional), each Klipper printer needs Moonraker's Spoolman integration and a `SET_ACTIVE_SPOOL` macro. With those in place, the `SET_ACTIVE_SPOOL ID=<id>` line in each PipSpool profile tells Moonraker which spool is printing after every tool change. Check that your toolchanger or MMU macros don't already define `SET_ACTIVE_SPOOL` before adding these:

```ini
# moonraker.conf
[spoolman]
server: http://192.168.1.10:7912
```

```ini
# printer.cfg (or an included file)
[gcode_macro SET_ACTIVE_SPOOL]
gcode:
  {% if params.ID %}
    {% set id = params.ID|int %}
    {action_call_remote_method("spoolman_set_active_spool", spool_id=id)}
  {% else %}
    {action_respond_info("Parameter 'ID' is required")}
  {% endif %}

[gcode_macro CLEAR_ACTIVE_SPOOL]
gcode:
  {action_call_remote_method("spoolman_set_active_spool", spool_id=None)}
```

**Matching a preset without the plugin:** OrcaSlicer can only match a slot to a preset through a **text** `filament_id`. If a Spoolman spool, or its filament, has a text extra field named `orca_filament_id`, Spooltag passes its value on in `lane_data`.

**Credits:** PipSpool is developed by Donko ([Gadonk/pipspool-orcaslicer](https://github.com/Gadonk/pipspool-orcaslicer)). PipSpool Slot Sync is a fork of DIEHARDave's [orca_spoolman_presets](https://github.com/DIEHARDave/orca_spoolman_presets), changed to match PipSpool profiles by spool id instead of creating presets.

> The OrcaSlicer workflow hasn't been tested end to end on real hardware yet. Moonraker printer connections and Python plugins are recent OrcaSlicer features, so check that your build has both.

## Bambu AMS via Bambuddy

Spooltag supports a Bambu Lab AMS **through [Bambuddy](https://github.com/maziggy/bambuddy)** running in **Spoolman mode**. Spooltag never connects to the printer itself.

Add the AMS in Settings as a printer of type **Bambu AMS (via Bambuddy)**:

- **URL:** Bambuddy's address, e.g. `http://192.168.1.10:8000`.
- **API key:** a Bambuddy API key with the **Read status** and **Manage inventory** permissions.
- **Printer id in Bambuddy:** the `id` shown by Bambuddy's `GET /api/v1/printers/`.
- **Tools / AMS slots:** 4 per AMS unit. Slots are numbered from 1, like on the AMS. With two AMS units, slots 5–8 are the second unit.

When you scan a spool and an AMS slot, Spooltag asks Bambuddy to assign that Spoolman spool to the slot (`POST /api/v1/spoolman/inventory/slot-assignments`, with `move: true`). Bambuddy then configures the slot over MQTT (type, colour, preset, K-profile), the same as assigning it in Bambuddy's UI. Spooltag sets the Spoolman location itself (`<printer name> AMS <slot>`, e.g. `Bambu AMS 2`; with more than one AMS unit, `Bambu AMS 2-1` is unit 2, slot 1) and moves any replaced spool to the unload location. Loading that spool into a Klipper tool or storing it removes the AMS assignment.

**Load the spool into the AMS before you scan.** If the printer reports the slot as empty, Bambuddy saves the assignment but doesn't configure the slot.

If Bambuddy is unreachable, Klipper loads and storage scans still work; the Status page notes that the AMS couldn't be checked.

## Configuration

Everything is configured on the **Settings** page and stored in Spooltag's database (`/data/spooltag.db`). The compose file only needs the image, port and data folder.

### General settings

| Setting | Default | Notes |
|---|---|---|
| Spoolman URL | `http://spoolman:8000` | Spoolman's base address. A Docker service name works if both are on the same Docker network. |
| Tag base URL | *(the address you opened Settings with)* | The address written into tags. **Changing it breaks every tag already written.** |
| Spoolman location for a tool | `{printer} T{tool}` | The location text for a loaded spool. Only `{printer}` and `{tool}` can be used. |
| Location for a replaced or cleared spool | *(empty: leave it)* | e.g. `Dry rack` |
| Seconds to wait for the second scan | `120` | 10 to 3600 |
| Time zone | *(the container's)* | For the scan log and backup file names, e.g. `Europe/Berlin` |
| Other host names | *(none)* | Host names you open Spooltag by, e.g. `spooltag.local`. IP addresses and the tag base URL's host always work. |
| Allow public addresses | off | Lets service URLs point at public internet addresses. Normally Spoolman, Moonraker and Bambuddy are on your own network. |

### Printers

- **Klipper printer:** name, short id (for tags), Moonraker URL, number of tools (1–32), optional Moonraker API key, and the two [Klipper variable](#klipper-variables-optional) switches.
- **Bambu AMS:** name, short id, Bambuddy URL, number of slots, Bambuddy API key, and the printer's id in Bambuddy.

API keys are never shown again after saving. Leave the field blank to keep the saved key.

### Environment variables

None are required. These are **starting values for a new installation only**: on start, any setting the database doesn't have yet is taken from them. After that, Settings wins and the variables can be removed.

| Variable | Sets |
|---|---|
| `SPOOLMAN_URL`, `BASE_URL`, `LOCATION_FORMAT`, `UNLOAD_LOCATION`, `PENDING_TIMEOUT`, `TZ`, `ALLOW_PUBLIC_URLS` | the matching General settings |
| `MOONRAKER_URL`, `PRINTER_NAME`, `NUM_TOOLS`, `MOONRAKER_API_KEY`, `KLIPPER_SAVE_VARIABLES`, `KLIPPER_MACRO_VARIABLE` | a first Klipper printer, if there are no printers yet |

These are read on **every** start:

| Variable | Default | Purpose |
|---|---|---|
| `PUID` / `PGID` | `10001` | The user and group the server runs as (e.g. `99`/`100` on Unraid). |
| `ALLOWED_HOSTS` | *(empty)* | Extra host names, comma-separated, added to Settings' list. `*` turns the host-name check off. Useful if you locked yourself out. |
| `ADMIN_PASSWORD` | *(empty)* | Sets the admin password, but only while none is set. |
| `DATA_DIR` | `/data` | Where the database lives inside the container. |

## Backup, restore and moving servers

### Backup

At the bottom of **Settings** (admin only), **Download backup** saves a file such as `spooltag-backup-2026-11-17-1730.db`. It's a consistent snapshot taken while Spooltag keeps running. It contains the general settings, printers (with their ids and API keys), storage locations, the admin password hash and the scan log. Login sessions and half-finished scans are left out.

**Keep backups private.** A backup contains your Moonraker and Bambuddy API keys in plain text, plus the admin password hash. Store it like a password file.

A Spooltag backup does **not** include Spoolman, Moonraker, Bambuddy or your printers' configuration. Back those up with their own tools; spool locations themselves live in Spoolman.

### Restore

**Restore backup** replaces **all** Spooltag configuration with a backup file (up to 10 MB).

1. The file is checked first. It must be an intact Spooltag database, not from a newer Spooltag version, with no unexpected tables, views or triggers. If the check fails, nothing changes.
2. The current database is saved to `backups/` in the data folder. The newest 5 safety copies are kept, and they contain credentials too.
3. The backup replaces the live database in one transaction, so it either fully happens or not at all. If anything goes wrong, the previous configuration is put back automatically.
4. The restored settings take effect immediately, with no restart. Everyone is logged out and pending scans are cancelled. If the backup had an admin password, log in with that password.

Printer ids, location ids and the tag base URL come from the backup, so tags written for it keep working, as long as Spooltag is reachable at that tag base URL.

**Restoring without the web page** (for example if Spooltag won't start):

1. Stop the container.
2. Copy the backup over `data/spooltag.db`.
3. Delete any `spooltag.db-journal` file next to it.
4. Start the container again; it fixes ownership and permissions on start.

### Moving Spooltag to another server

Every tag stores the tag base URL (e.g. `http://192.168.1.10:7913`), so **the new server must answer at the same address**.

1. On the old server, **Download backup**.
2. Give the new Docker host the old IP address (DHCP reservation or static IP). If your tag base URL uses a host name, point that name at the new host instead. Keep the same host port.
3. Start Spooltag on the new host with the same `compose.yaml`. The data folder can start empty.
4. Open the new server **by IP address**, go to Settings and **Restore backup**.
5. Log in with the backup's admin password, if it had one. Check that the Status page shows every printer, then test-scan a tool tag and a spool tag.
6. Make sure Spoolman, Moonraker (`trusted_clients`) and Bambuddy accept requests from the new host.
7. Stop the old container so two servers don't both update Spoolman and the printers.

If the address has to change anyway, restore the backup, set the new tag base URL under General, and rewrite every tag from the Tags page.

## Upgrading

Pull the new image and recreate the container (`docker compose pull && docker compose up -d`). Settings, printers, locations, API keys and tag addresses carry over. The database is upgraded automatically on start.

**Upgrading an installation from before 17 November 2026.** That date's security update changed three things:

1. **The server no longer runs as root.** On its first start the container gives the data folder to user `10001:10001` (folders `0700`, files `0600`). It logs `gave N item(s) to 10001:10001` and then drops root. You don't need to do anything, but:
   - If you add the hardening options from the Quick start (`cap_drop: [ALL]` etc.), keep the `cap_add` line. Those capabilities are only used for that start-up step.
   - To match an existing owner (e.g. on Unraid), set `PUID`/`PGID`.
   - If you run the container as a non-root user yourself (Compose `user:`), first run on the Docker host: `sudo chown -R 10001:10001 <data folder> && sudo chmod -R u+rwX,go-rwx <data folder>`. Otherwise the container exits with that instruction.
2. **The web page only answers to known host names.** IP addresses, `localhost` and the tag base URL's host always work. If you open Spooltag by another name, open it by IP once and add the name under **Settings → General → Other host names**, or set `ALLOWED_HOSTS`.
3. **Service URLs must be on your own network**, meaning private, CGNAT/Tailscale, loopback or IPv6 unique-local addresses. Docker service names and local host names normally resolve to these. If a service is on a public address, turn on **Allow public addresses**.

Older single-printer tool tags of the form `/t/<n>` keep working; they refer to the first printer in Settings.

## Security

Spooltag is designed for a **home network you trust**. **Don't expose it to the internet.**

### What's protected

- **Other websites can't make your browser change Spooltag.** Forms only accept requests from Spooltag's own pages. Tag addresses can't carry a secret, so scans check the browser's fetch metadata: a tag tap, QR scan, bookmark or typed address acts immediately. A scan address loaded by another website is refused if it's hidden (an image or script). If you followed a link from another site, Spooltag shows a **Confirm this scan?** page instead.
- **DNS rebinding:** the web page refuses host names it doesn't know.
- **Optional admin password** for Settings, backup/restore and the **Clear** buttons. Tag scanning never needs it.
  - It's stored as an scrypt hash. Logins are HttpOnly, `SameSite=Strict` cookies that last 30 days.
  - After 5 wrong passwords, that address is locked out for 15 minutes.
  - Changing or removing the password logs out every other browser.
- **API keys are write-only** in the web page; they're never sent back to the browser.
- **Outgoing requests** to Spoolman, Moonraker and Bambuddy may only go to local-network addresses unless you allow public ones. The address is checked after the DNS lookup, and the connection goes to the address that was checked. Link-local and cloud-metadata addresses are always refused.
- **Headers:** a strict Content-Security-Policy (no inline scripts), no framing, `nosniff`, `Referrer-Policy: same-origin`, and no caching of pages.
- **Container:** runs as an unprivileged user with no capabilities. It supports a read-only root filesystem and `no-new-privileges`, and keeps the database at `0600` in a `0700` folder.
- **Supply chain:** dependencies and GitHub Actions are pinned. Every change runs the test suite and a container smoke test before an image is published, and Dependabot proposes updates. The image stays on Python 3.12 and receives its patch releases.

### Known limitations

- **No admin password means Settings is open** to anyone on your network. Someone on your network could also set a password before you do.
- **Tag scans are open by design.** Anyone who can reach the page can move spools by opening tag addresses. A link opened from another app (a chat message, an email) looks to the browser exactly like a tag tap, so it acts without a confirm page. The result page always shows what changed.
- **Browsers older than Chrome 76 or Safari 16.4** don't send fetch metadata. For those, Spooltag falls back to the `Referer` header, which a page can suppress.
- **Both scans must happen in the same browser.** A phone that opens one tag in Safari and another in an app's built-in browser won't pair them. Private tabs and blocked cookies also prevent pairing.
- **Plain HTTP:** passwords, cookies and API keys cross your network unencrypted, unless you put Spooltag behind an HTTPS reverse proxy. It trusts `X-Forwarded-Proto` only from `127.0.0.1`.
- **API keys are stored in plain text** in the database, in downloaded backups and in the safety copies under `backups/`. Keep access to the data folder restricted, and treat backups like a password file.
- **Moonraker usually trusts your whole network** through `trusted_clients`, which already gives more access to your printers than Spooltag adds.
- **No independent security audit** has been done; see the [disclaimer](#disclaimer).

### Recommended deployment

- Use the Quick start compose file as is: read-only filesystem, `cap_drop: [ALL]`, `no-new-privileges`, data on local disk.
- Set an admin password.
- Don't forward Spooltag's port on your router. For remote access, use a VPN such as WireGuard or Tailscale.
- If you can, publish the port on your LAN interface only (e.g. `"192.168.1.10:7913:8000"`), or restrict it with the host firewall.
- If you add an HTTPS reverse proxy, add its host name under **Other host names**. Set the tag base URL to the HTTPS address *before* writing tags.

## Troubleshooting

**"Spooltag doesn't answer to the host name …"**
You opened it by a name that isn't allowed yet. Open it by IP address and add the name under **Settings → General → Other host names**. If you can't reach Settings at all, set `ALLOWED_HOSTS: your.host.name` in the compose file and recreate the container.

**The second scan doesn't pair with the first**
Both scans must open in the same browser on the same phone, within the timeout (120 seconds by default). Private tabs, blocked cookies, or one tag opening in another app's built-in browser stop pairing. The Status page shows *Waiting for a second scan after …* while a scan from that browser is waiting.

**A tag tap shows "Confirm this scan?"**
The browser reported that another website started the request. Tap **Yes, scan it** if you meant it. If this happens on every tag tap, please [open an issue](#reporting-a-problem) with your phone, browser and how the tag is opened.

**A printer shows an error on the Status page, or a load shows a red line**
- Check the Moonraker URL in Settings.
- Check that the printer is on.
- Check that Moonraker accepts the Docker host: add its IP to `trusted_clients`, or enter an API key for that printer.

The Spoolman location is still updated, so scan again once the printer is reachable.

**"blocked: … is a public internet address" or "… link-local, metadata or reserved address"**
A service URL resolves to an address outside your local network. Use the service's LAN address. If the service really is on a public address, turn on **Allow public addresses**.

**"Spoolman unreachable" / "Unknown spool"**
Check the Spoolman URL, and that the spool id on the tag still exists in Spoolman (a deleted spool can't be scanned). The Tags page lists only spools that aren't archived.

**The AMS slot doesn't change type or colour**
- Load the spool into the AMS *before* scanning.
- Check that Bambuddy is in Spoolman mode.
- Check that its API key has **Read status** and **Manage inventory**.
- Check that the printer id in Settings matches Bambuddy's.

**OrcaSlicer doesn't show the slots or colours**
- Your OrcaSlicer build needs a Moonraker printer connection with **Synchronize filament list**, and the printer preset must use it. In some builds, printer agents have to be enabled under **Preferences → Developer** first.
- Check that `http://<printer>:7125/server/database/item?namespace=lane_data` shows your tools.

**The container exits immediately, saying `/data` isn't writable**
Either start it as root once (the default) so it can fix ownership, or run the `chown` command it prints on the Docker host. Set `PUID`/`PGID` if the folder must keep another owner.

**Forgot the admin password**
On the Docker host, run `docker exec spooltag python -m app.admin reset-password` to remove it, or `docker exec -it spooltag python -m app.admin set-password` to set a new one.

**A restore is refused**
The message says why. Only Spooltag backup files (or a `spooltag.db` from an older installation) can be restored, up to 10 MB. A backup from a newer Spooltag needs an updated Spooltag.

## What has been tested

| Area | Automated tests (simulated services) | Physical hardware |
|---|---|---|
| Spool, tool and storage scans; pairing; one place per spool | Yes | Not yet verified |
| Klipper/Moonraker `lane_data`, Klipper variables (single- and multi-tool) | Yes, against a simulated Moonraker | Not yet verified on a printer |
| Spoolman locations | Yes, against a simulated Spoolman | Not yet verified |
| Bambu AMS via Bambuddy | Yes, against a simulated Bambuddy API | Not yet verified with an AMS |
| OrcaSlicer filament sync and PipSpool Slot Sync | Not part of Spooltag's tests | Not yet verified end to end |
| NFC tag taps on Android and iOS | Simulated with the headers those browsers send | Not yet verified on phones |
| Security protections, backup/restore, container start-up | Yes, including a real container started on every build | Not applicable |

Reports from real setups are very welcome. See [Contributing and support](#contributing-and-support).

## Disclaimer

Spooltag is a personal hobby project developed with substantial assistance from AI coding tools. The maintainer is a 3D printing and homelab enthusiast, not a professional software developer. Although automated testing and AI-assisted security reviews are performed, the project has not undergone an independent professional security audit.

Use it at your own risk, keep it on a trusted network, and keep your own backups of Spoolman and your printer configurations.

## Contributing and support

This is a hobby project. **There's no guaranteed support, response time or roadmap.** Issues and pull requests are read and appreciated as time allows.

### Reporting a problem

Open a [GitHub issue](https://github.com/HappyPants9999/spooltag/issues) with:

- what you did, what you expected, and what happened;
- the image tag or date you're running. Each build is also tagged `sha-<commit>`, and Docker can show which one you have with `docker image inspect`.
- your setup: Klipper printer(s) and tool count, or AMS via Bambuddy; phone and browser for scanning problems;
- the container log (`docker logs spooltag`).

**Never post API keys, backup files or your `spooltag.db`**; they contain credentials. Remove keys from logs and screenshots.

**Security problems:** please don't describe them in a public issue. If the repository's **Security** tab offers **Report a vulnerability**, use that. Otherwise, open an issue that only says you have a security report and asks for a private contact.

### Contributing code

Pull requests are welcome, preferably small and focused. Explain what the change does and why. Every pull request runs the full test suite and a container smoke test. See [DEVELOPMENT.md](DEVELOPMENT.md) for running Spooltag locally, the tests, and how the project is organized.

New features that need hardware the maintainer doesn't have may be accepted only with clear testing notes from the contributor.
