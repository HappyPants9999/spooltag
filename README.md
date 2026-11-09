# spooltag

Tap your phone on a spool tag, then on a tool tag, and the printer knows what's loaded.

spooltag is a small web service for a Klipper toolchanger (built for an 8-tool INDX on a Sovol SV08). NFC tags hold plain URLs, so tapping one opens a page in the phone's browser. The service pairs the two scans, in either order, and updates three things:

| Where | What | Who reads it |
|---|---|---|
| Moonraker `lane_data` → `T<n>` | `lane`, `spool_id`, `material`, `color`, `vendor_name`, `name`, temps, optional `filament_id` | OrcaSlicer's Moonraker filament sync, plus slot-sync plugins such as [orca_spoolman_presets](https://github.com/DIEHARDave/orca_spoolman_presets) |
| Spoolman spool `location` | e.g. `SV08 T3` | Spoolman, Mainsail |
| Klipper variables *(optional)* | `t3__spool_id` save variable / `T3.spool_id` | Mainsail's per-tool spool display |

Other housekeeping:
- The spool a new scan pushes out of a tool is moved to `UNLOAD_LOCATION`.
- A spool can only be in one tool at a time. Assigning it to a second tool removes it from the first.
- The `T<n>` key matches Mainsail's, so picking a spool in Mainsail overwrites the same record instead of creating a duplicate.

## Pages

| URL | Purpose |
|---|---|
| `/s/<spool id>` | spool tag |
| `/t/<n>` | tool tag (`/t/3` or `/t/T3`) |
| `/` | what's in every tool, a Clear button for each, and recent scans |
| `/tags` | URL and QR code for every tool and spool, to use when writing tags |

## Deploy (Dockhand)

1. Push this repo to GitHub. The **Build image** workflow runs the tests and publishes `ghcr.io/happypants9999/spooltag:latest` for amd64 and arm64.
2. After the first build, open GitHub → your profile → **Packages → spooltag → Package settings** and set its visibility to **Public**. Otherwise you have to log in to GHCR on the Docker VM.
3. In Dockhand, create a stack from [`compose.yaml`](compose.yaml) and fill in the `<…>` values.
4. In `moonraker.conf`, add the Docker VM's IP to `[authorization] trusted_clients`, or set `MOONRAKER_API_KEY`.

To update after a code change: push to GitHub, wait for the build, then redeploy the stack in Dockhand (pull the latest image).

## Settings

| Variable | Default | |
|---|---|---|
| `SPOOLMAN_URL` | `http://spoolman:8000` | Spoolman's base URL |
| `MOONRAKER_URL` | *(empty)* | e.g. `http://192.168.1.50:7125` |
| `MOONRAKER_API_KEY` | | only if the VM isn't a trusted client |
| `BASE_URL` | the request's host | the URL written onto tags. **Keep it stable**, because changing it breaks every tag |
| `NUM_TOOLS` | `8` | |
| `PRINTER_NAME` | `SV08` | used in the location text |
| `LOCATION_FORMAT` | `{printer} T{tool}` | Spoolman location for a loaded spool |
| `UNLOAD_LOCATION` | *(empty = leave as is)* | location for a spool that's replaced or cleared |
| `PENDING_TIMEOUT` | `120` | seconds to wait for the second scan |
| `KLIPPER_SAVE_VARIABLES` | `false` | runs `SAVE_VARIABLE` (needs `[save_variables]`) |
| `KLIPPER_MACRO_VARIABLE` | `false` | runs `SET_GCODE_VARIABLE MACRO=T<n>` (needs `variable_spool_id` on each T macro, so leave it off with DEMON_INDX unless you add one) |

### Exact Orca profile matching (optional)

Orca only matches a lane to a preset by a **string** `filament_id`. If a Spoolman spool or filament has a text extra field `orca_filament_id`, spooltag passes that value through. Without it, Orca falls back to a generic profile for the material, and a slot-sync plugin can choose the exact PipSpool profile using `spool_id`.

## Writing tags

1. Open `/tags` on a computer.
2. On the phone, point the camera at a QR code and copy the URL.
3. In NFC Tools: **Write → Add a record → Custom URL**, paste the URL, and write it to the tag.

Write tool tags once. Rewrite a box's spool tag whenever a new spool goes into that box.

## Develop

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
SPOOLMAN_URL=http://<vm>:7912 MOONRAKER_URL=http://<printer>:7125 DATA_DIR=./data \
  uvicorn app.main:app --reload --port 8090
```
