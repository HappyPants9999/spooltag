"""HTTP clients for Spoolman and Moonraker."""
from typing import Any, Optional

import httpx

LANE_NS = "lane_data"


class Spoolman:
    def __init__(self, client: httpx.AsyncClient, base_url: str):
        self.client = client
        self.base = base_url

    async def spool(self, spool_id: int) -> Optional[dict]:
        r = await self.client.get(f"{self.base}/api/v1/spool/{spool_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def spools(self) -> list[dict]:
        r = await self.client.get(f"{self.base}/api/v1/spool", params={"allow_archived": "false"})
        r.raise_for_status()
        return r.json()

    async def set_location(self, spool_id: int, location: str) -> None:
        r = await self.client.patch(f"{self.base}/api/v1/spool/{spool_id}", json={"location": location})
        r.raise_for_status()


class Moonraker:
    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str = ""):
        self.client = client
        self.base = base_url
        self.headers = {"X-Api-Key": api_key} if api_key else {}

    async def lane_data(self) -> dict[str, Any]:
        r = await self.client.get(f"{self.base}/server/database/item",
                                  params={"namespace": LANE_NS}, headers=self.headers)
        if r.status_code == 404:  # namespace does not exist yet
            return {}
        r.raise_for_status()
        return r.json().get("result", {}).get("value") or {}

    async def put_lane(self, key: str, value: dict) -> None:
        r = await self.client.post(f"{self.base}/server/database/item", headers=self.headers,
                                   json={"namespace": LANE_NS, "key": key, "value": value})
        r.raise_for_status()

    async def delete_lane(self, key: str) -> None:
        r = await self.client.delete(f"{self.base}/server/database/item",
                                     params={"namespace": LANE_NS, "key": key}, headers=self.headers)
        if r.status_code != 404:
            r.raise_for_status()

    async def gcode(self, script: str) -> None:
        r = await self.client.post(f"{self.base}/printer/gcode/script", headers=self.headers,
                                   json={"script": script})
        r.raise_for_status()


def lane_entry(tool: int, spool: dict) -> dict:
    """The lane_data record Orca's Moonraker agent and slot-sync plugins read."""
    fil = spool.get("filament") or {}
    vendor = (fil.get("vendor") or {}).get("name")
    color = fil.get("color_hex") or (fil.get("multi_color_hexes") or "").split(",")[0]
    entry = {
        "lane": str(tool),                 # Orca parses this string as the 0-based slot
        "spool_id": spool["id"],
        "material": fil.get("material") or "",
        "color": ("#" + color.lstrip("#")) if color else "",
        "vendor_name": vendor,
        "name": fil.get("name"),
        "nozzle_temp": fil.get("settings_extruder_temp"),
        "bed_temp": fil.get("settings_bed_temp"),
    }
    # Orca only matches a *string* filament_id to a preset; numeric ids are ignored, so only an
    # explicit Spoolman extra field "orca_filament_id" (on the spool or its filament) is passed on.
    fid = _extra(spool, "orca_filament_id") or _extra(fil, "orca_filament_id")
    if fid:
        entry["filament_id"] = fid
    return {k: v for k, v in entry.items() if v is not None}


def _extra(obj: dict, key: str) -> Optional[str]:
    raw = (obj.get("extra") or {}).get(key)
    if not raw:
        return None
    # Spoolman stores extra-field values JSON-encoded, so a text field arrives as '"GFL99"'.
    return raw.strip().strip('"') or None


def spool_label(spool: dict) -> str:
    fil = spool.get("filament") or {}
    vendor = (fil.get("vendor") or {}).get("name")
    parts = [p for p in (vendor, fil.get("material"), fil.get("name")) if p]
    return " ".join(parts) or f"Spool {spool.get('id')}"
