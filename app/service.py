"""Scan logic: put spools into printer tools, AMS slots or storage locations, and keep Spoolman,
every Klipper printer's lane_data, Bambuddy's AMS slot assignments and the optional Klipper
variables in step."""
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .clients import Bambuddy, Moonraker, Spoolman, lane_entry, spool_label
from .config import Settings
from .store import Location, Printer, Store


@dataclass
class Result:
    ok: bool
    spool: Optional[dict] = None
    target: str = ""                     # "SV08 T3" or "Dry box 1"
    steps: list[tuple[bool, str]] = field(default_factory=list)

    def step(self, ok: bool, text: str) -> None:
        self.steps.append((ok, text))
        if not ok:
            self.ok = False


def lane_key(tool: int) -> str:
    # "T<n>" matches Mainsail's key, so a spool picked in Mainsail overwrites the same record.
    return f"T{tool}"


def _err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        detail = ""
        try:
            body = e.response.json()
            detail = body.get("detail") if isinstance(body, dict) else ""
        except ValueError:
            pass
        return f"HTTP {e.response.status_code}" + (f": {detail}" if isinstance(detail, str) and detail else "")
    return type(e).__name__ + (f": {e}" if str(e) else "")


def _lane_tool(lane: dict) -> Optional[int]:
    try:
        return int(str(lane.get("lane")))
    except (TypeError, ValueError):
        return None


class Service:
    def __init__(self, settings: Settings, store: Store, spoolman: Spoolman, client: httpx.AsyncClient):
        self.s = settings
        self.store = store
        self.spoolman = spoolman
        self.client = client

    def moonraker(self, printer: Printer) -> Moonraker:
        return Moonraker(self.client, printer.moonraker_url, printer.api_key)

    def bambuddy(self, printer: Printer) -> Bambuddy:
        return Bambuddy(self.client, printer.moonraker_url, printer.api_key)

    def default_printer(self) -> Optional[Printer]:
        printers = self.store.printers()
        return printers[0] if printers else None

    def target_name(self, printer: Printer, tool: int) -> str:
        if printer.is_ams:
            return f"{printer.name} {printer.tool_label(tool)}"
        return self.s.location_for(printer.name, tool)

    async def _fetch_spool(self, spool_id: int, result: Result) -> Optional[dict]:
        try:
            spool = await self.spoolman.spool(spool_id)
        except Exception as e:  # noqa: BLE001 - surfaced on the page
            result.step(False, f"Spoolman unreachable ({_err(e)})")
            return None
        if spool is None:
            result.step(False, f"Spool #{spool_id} not found in Spoolman")
        result.spool = spool
        return spool

    async def _remove_from_printers(self, spool_id: int, result: Result,
                                    keep: Optional[tuple[str, int]] = None) -> None:
        """Delete this spool's lane in every printer except `keep` (printer slug, tool).

        An unreachable printer is reported but doesn't fail the scan: it's usually just off.
        """
        for printer in self.store.printers():
            if printer.is_ams:
                await self._remove_from_ams(printer, spool_id, result, keep)
                continue
            try:
                lanes = await self.moonraker(printer).lane_data()
            except Exception:  # noqa: BLE001
                result.steps.append((True, f"{printer.name} not reachable; not checked for #{spool_id}"))
                continue
            for key, lane in lanes.items():
                if not isinstance(lane, dict) or lane.get("spool_id") != spool_id:
                    continue
                tool = _lane_tool(lane)
                if keep and printer.slug == keep[0] and tool == keep[1]:
                    continue
                try:
                    await self.moonraker(printer).delete_lane(key)
                    result.step(True, f"Removed #{spool_id} from {self.target_name(printer, tool)}")
                    if printer.save_variables and tool is not None:
                        await self.moonraker(printer).gcode(
                            f"SAVE_VARIABLE VARIABLE=t{tool}__spool_id VALUE=None")
                except Exception as e:  # noqa: BLE001
                    result.step(False, f"Couldn't remove #{spool_id} from {printer.name} ({_err(e)})")

    async def _remove_from_ams(self, printer: Printer, spool_id: int, result: Result,
                               keep: Optional[tuple[str, int]]) -> None:
        if keep and keep[0] == printer.slug:
            return  # assigning into this AMS uses move=True, which already clears its other slots
        try:
            slots = await self.bambuddy(printer).slots(printer.remote_id)
        except Exception:  # noqa: BLE001
            result.steps.append((True, f"{printer.name} (Bambuddy) not reachable; not checked for #{spool_id}"))
            return
        held = [slot for slot, sid in slots.items() if sid == spool_id]
        if not held:
            return
        try:
            await self.bambuddy(printer).unassign(spool_id)
            for ams_id, tray_id in held:
                result.step(True, f"Removed #{spool_id} from "
                                  f"{self.target_name(printer, ams_id * 4 + tray_id + 1)}")
        except Exception as e:  # noqa: BLE001
            result.step(False, f"Couldn't remove #{spool_id} from {printer.name} ({_err(e)})")

    async def _previous_unload(self, previous_spool: Optional[int], result: Result) -> None:
        if previous_spool and self.s.unload_location:
            try:
                await self.spoolman.set_location(previous_spool, self.s.unload_location)
                result.step(True, f"Spool #{previous_spool} → {self.s.unload_location}")
            except Exception as e:  # noqa: BLE001
                result.step(False, f"Spool #{previous_spool} location not changed ({_err(e)})")

    async def _assign_ams(self, spool: dict, printer: Printer, tool: int, result: Result) -> None:
        spool_id = spool["id"]
        bb = self.bambuddy(printer)
        ams_id, tray_id = printer.ams_slot(tool)
        # 1. Bambuddy: assign the slot (it configures the AMS over MQTT); note what it replaces.
        previous_spool: Optional[int] = None
        try:
            prev = (await bb.slots(printer.remote_id)).get((ams_id, tray_id))
            if prev is not None and prev != spool_id:
                previous_spool = prev
        except Exception:  # noqa: BLE001 - the assign below reports a real outage
            pass
        try:
            await bb.assign(printer.remote_id, ams_id, tray_id, spool_id)
            result.step(True, f"Bambuddy assigned #{spool_id} to {result.target}")
        except Exception as e:  # noqa: BLE001
            result.step(False, f"Bambuddy didn't assign the slot ({_err(e)})")
        # 2. A spool is in one place only: drop it from every Klipper tool and other AMS.
        await self._remove_from_printers(spool_id, result, keep=(printer.slug, tool))
        # 3. Spoolman location (Bambuddy leaves it alone), and for the spool it replaced.
        await self._set_location(spool, result.target, result)
        await self._previous_unload(previous_spool, result)

    async def assign(self, spool_id: int, printer: Printer, tool: int) -> Result:
        result = Result(ok=True, target=self.target_name(printer, tool))
        spool = await self._fetch_spool(spool_id, result)
        if spool is None:
            return result
        if printer.is_ams:
            await self._assign_ams(spool, printer, tool, result)
            self.store.log("assign" if result.ok else "assign-partial", spool_id, result.target,
                           spool_label(spool))
            return result
        moon = self.moonraker(printer)

        # 1. This printer's lane_data (what OrcaSlicer reads); note the spool it replaces.
        previous_spool: Optional[int] = None
        try:
            lanes = await moon.lane_data()
            for key, lane in lanes.items():
                if isinstance(lane, dict) and _lane_tool(lane) == tool:
                    sid = lane.get("spool_id")
                    if isinstance(sid, int) and sid != spool_id:
                        previous_spool = sid
                    if key != lane_key(tool):       # same tool under another key, e.g. "lane3"
                        await moon.delete_lane(key)
            await moon.put_lane(lane_key(tool), lane_entry(tool, spool))
            result.step(True, f"{printer.name} lane_data T{tool} updated")
        except Exception as e:  # noqa: BLE001
            result.step(False, f"{printer.name} lane_data not updated ({_err(e)}) — is the printer on?")

        # 2. A spool is in one place only: drop it from any other tool on any printer.
        await self._remove_from_printers(spool_id, result, keep=(printer.slug, tool))

        # 3. Spoolman location for this spool, and for the one it replaced.
        await self._set_location(spool, result.target, result)
        await self._previous_unload(previous_spool, result)

        # 4. Optional Klipper variables (Mainsail's per-tool spool display uses these).
        lines = []
        if printer.macro_variable:
            lines.append(f"SET_GCODE_VARIABLE MACRO=T{tool} VARIABLE=spool_id VALUE={spool_id}")
        if printer.save_variables:
            lines.append(f"SAVE_VARIABLE VARIABLE=t{tool}__spool_id VALUE={spool_id}")
        if lines:
            try:
                await moon.gcode("\n".join(lines))
                result.step(True, "Klipper variables set")
            except Exception as e:  # noqa: BLE001
                result.step(False, f"Klipper variables not set ({_err(e)})")

        self.store.log("assign" if result.ok else "assign-partial", spool_id, result.target, spool_label(spool))
        return result

    async def store_at(self, spool_id: int, location: Location) -> Result:
        result = Result(ok=True, target=location.name)
        spool = await self._fetch_spool(spool_id, result)
        if spool is None:
            return result
        await self._remove_from_printers(spool_id, result)
        await self._set_location(spool, location.name, result)
        self.store.log("store" if result.ok else "store-partial", spool_id, location.name, spool_label(spool))
        return result

    async def _set_location(self, spool: dict, location: str, result: Result) -> None:
        try:
            await self.spoolman.set_location(spool["id"], location)
            spool["location"] = location
            result.step(True, f"Spoolman location → {location}")
        except Exception as e:  # noqa: BLE001
            result.step(False, f"Spoolman location not set ({_err(e)})")

    async def clear(self, printer: Printer, tool: int) -> Result:
        result = Result(ok=True, target=self.target_name(printer, tool))
        if printer.is_ams:
            spool_id = None
            try:
                bb = self.bambuddy(printer)
                spool_id = (await bb.slots(printer.remote_id)).get(printer.ams_slot(tool))
                if spool_id is not None:
                    await bb.unassign(spool_id)
                result.step(True, f"{result.target} cleared")
            except Exception as e:  # noqa: BLE001
                result.step(False, f"Bambuddy not updated ({_err(e)})")
            await self._previous_unload(spool_id, result)
            self.store.log("clear", spool_id, result.target)
            return result
        moon = self.moonraker(printer)
        spool_id = None
        try:
            for key, lane in (await moon.lane_data()).items():
                if isinstance(lane, dict) and (key == lane_key(tool) or _lane_tool(lane) == tool):
                    if isinstance(lane.get("spool_id"), int):
                        spool_id = lane["spool_id"]
                    await moon.delete_lane(key)
            result.step(True, f"{result.target} cleared")
        except Exception as e:  # noqa: BLE001
            result.step(False, f"{printer.name} not updated ({_err(e)})")
        if spool_id and self.s.unload_location:
            try:
                await self.spoolman.set_location(spool_id, self.s.unload_location)
                result.step(True, f"Spool #{spool_id} → {self.s.unload_location}")
            except Exception as e:  # noqa: BLE001
                result.step(False, f"Spoolman not updated ({_err(e)})")
        if printer.save_variables:
            try:
                await moon.gcode(f"SAVE_VARIABLE VARIABLE=t{tool}__spool_id VALUE=None")
            except Exception:  # noqa: BLE001
                pass
        self.store.log("clear", spool_id, result.target)
        return result

    async def tools(self, printer: Printer) -> tuple[dict[int, dict], Optional[str]]:
        """Current lane_data per tool, plus an error message if the printer can't be read.

        For an AMS, lane-like records are built from Bambuddy's assignments and Spoolman."""
        if printer.is_ams:
            return await self._ams_tools(printer)
        try:
            lanes = await self.moonraker(printer).lane_data()
        except Exception as e:  # noqa: BLE001
            return {}, f"{printer.name} unreachable ({_err(e)})"
        by_tool: dict[int, dict] = {}
        for lane in lanes.values():
            if isinstance(lane, dict) and (tool := _lane_tool(lane)) is not None:
                by_tool[tool] = lane
        return by_tool, None

    async def _ams_tools(self, printer: Printer) -> tuple[dict[int, dict], Optional[str]]:
        try:
            slots = await self.bambuddy(printer).slots(printer.remote_id)
        except Exception as e:  # noqa: BLE001
            return {}, f"{printer.name}: Bambuddy unreachable ({_err(e)})"
        by_tool: dict[int, dict] = {}
        for (ams_id, tray_id), spool_id in slots.items():
            tool = ams_id * 4 + tray_id + 1
            if tool not in printer.tool_numbers():
                continue
            try:
                spool = await self.spoolman.spool(spool_id)
            except Exception:  # noqa: BLE001
                spool = None
            by_tool[tool] = lane_entry(tool, spool) if spool else {"spool_id": spool_id}
        return by_tool, None
