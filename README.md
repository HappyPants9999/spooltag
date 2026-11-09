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
- The spool a new scan pushes out of a tool is moved to `UNLOAD_LOCATION`.
- The `T<n>` key matches Mainsail's, so picking a spool in Mainsail overwrites the same record instead of creating a duplicate.
- Bambu AMS slots are left to Bambuddy. Those spools can still be scanned into storage.

## Pages

| URL | Purpose |
|---|---|
| `/s/<spool id>` | spool tag |
| `/t/<printer>/<n>` | tool tag, e.g. `/t/sv08/3`, `/t/v0/0` |
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
3. In Dockhand, create a stack from [`compose.yaml`](compose.yaml) and fill in the `<…>` values.
4. In `moonraker.conf`, add the Docker VM's IP to `[authorization] trusted_clients`, or set `MOONRAKER_API_KEY`.

To update after a code change: push to GitHub, wait for the build, then redeploy the stack in Dockhand (pull the latest image).

## Settings

Printers and storage locations are managed on the **/settings** page and stored in `/data/spooltag.db`. Each printer has its own Moonraker URL, an optional API key, a tool count, and the two Klipper-variable switches.

Environment variables:

| Variable | Default | |
|---|---|---|
| `SPOOLMAN_URL` | `http://spoolman:8000` | Spoolman's base URL |
| `BASE_URL` | the request's host | the URL written onto tags. **Keep it stable**, because changing it breaks every tag |
| `LOCATION_FORMAT` | `{printer} T{tool}` | Spoolman location for a loaded spool |
| `UNLOAD_LOCATION` | *(empty = leave as is)* | location for a spool that's replaced or cleared |
| `PENDING_TIMEOUT` | `120` | seconds to wait for the second scan |
| `MOONRAKER_URL`, `PRINTER_NAME`, `NUM_TOOLS`, `MOONRAKER_API_KEY`, `KLIPPER_SAVE_VARIABLES`, `KLIPPER_MACRO_VARIABLE` | | **Only read when the printer list is empty**, to create the first printer. After that, edit the printer in Settings. |

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
