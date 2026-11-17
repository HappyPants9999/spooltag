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
| `/settings` | add, edit and remove printers and storage locations |

Anyone on your LAN who can reach the page can change Settings, so keep the port off the internet.

## Deploy (Dockhand)

1. Push this repo to GitHub. The **Build image** workflow runs the tests and publishes `ghcr.io/happypants9999/spooltag:latest` for amd64 and arm64.
2. After the first build, open GitHub → your profile → **Packages → spooltag → Package settings** and set its visibility to **Public**. Otherwise you have to log in to GHCR on the Docker VM.
3. In Dockhand, create a stack from [`compose.yaml`](compose.yaml).
4. Open `/settings` and fill in **General** (Spoolman URL and tag base URL first), then add printers and storage locations.
5. In `moonraker.conf`, add the Docker VM's IP to `[authorization] trusted_clients`, or give the printer an API key in Settings.

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

**Printers:** each Klipper printer has its own Moonraker URL, an optional API key, a tool count, and the two Klipper-variable switches. A Bambu AMS has a Bambuddy URL, API key and printer id instead.

**Environment variables** are only starting values for a new install. On start, any general setting the database doesn't have yet is taken from `SPOOLMAN_URL`, `BASE_URL`, `LOCATION_FORMAT`, `UNLOAD_LOCATION`, `PENDING_TIMEOUT` or `TZ`, and if there are no printers, `MOONRAKER_URL`, `PRINTER_NAME`, `NUM_TOOLS`, `MOONRAKER_API_KEY`, `KLIPPER_SAVE_VARIABLES` and `KLIPPER_MACRO_VARIABLE` create the first one. After that the Settings page wins and the variables can be removed. Only `DATA_DIR` (set to `/data` in the image) is read every time.

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

## Writing tags

1. Open `/tags` on a computer.
2. On the phone, point the camera at a QR code and copy the URL.
3. In NFC Tools: **Write → Add a record → Custom URL**, paste the URL, and write it to the tag.

Write tool and storage tags once. Rewrite a box's spool tag whenever a new spool goes into that box.

## Develop

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
SPOOLMAN_URL=http://<vm>:7912 MOONRAKER_URL=http://<printer>:7125 DATA_DIR=./data \
  uvicorn app.main:app --reload --port 8090
```
