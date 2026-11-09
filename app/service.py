"""Assignment logic: put a spool into a tool and keep Spoolman, lane_data and Klipper in step."""
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .clients import Moonraker, Spoolman, lane_entry, spool_label
from .config import Settings
from .store import Store


@dataclass
class Result:
    ok: bool
    spool: Optional[dict] = None
    tool: Optional[int] = None
    steps: list[tuple[bool, str]] = field(default_factory=list)  # (succeeded, description)

    def step(self, ok: bool, text: str) -> None:
        self.steps.append((ok, text))
        if not ok:
            self.ok = False


def lane_key(tool: int) -> str:
    # "T<n>" matches Mainsail's key, so a spool picked in Mainsail overwrites the same record.
    return f"T{tool}"


def _err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return type(e).__name__ + (f": {e}" if str(e) else "")


class Service:
    def __init__(self, settings: Settings, store: Store, spoolman: Spoolman,
                 moonraker: Optional[Moonraker]):
        self.s = settings
        self.store = store
        self.spoolman = spoolman
        self.moonraker = moonraker

    def valid_tool(self, tool: int) -> bool:
        return 0 <= tool < self.s.num_tools

    async def assign(self, spool_id: int, tool: int) -> Result:
        result = Result(ok=True, tool=tool)
        try:
            spool = await self.spoolman.spool(spool_id)
        except Exception as e:  # noqa: BLE001 - surfaced on the page
            result.step(False, f"Spoolman unreachable ({_err(e)})")
            return result
        if spool is None:
            result.step(False, f"Spool #{spool_id} not found in Spoolman")
            return result
        result.spool = spool

        # 1. lane_data: what OrcaSlicer reads. Also drop the spool from any other tool, and
        #    remember which spool this tool held before.
        previous_spool: Optional[int] = None
        if self.moonraker:
            try:
                lanes = await self.moonraker.lane_data()
                for key, lane in lanes.items():
                    if not isinstance(lane, dict):
                        continue
                    if str(lane.get("lane")) == str(tool) and key != lane_key(tool):
                        # Same tool under another key (e.g. "lane3"): remove the duplicate.
                        await self.moonraker.delete_lane(key)
                    if key == lane_key(tool) or str(lane.get("lane")) == str(tool):
                        sid = lane.get("spool_id")
                        if isinstance(sid, int) and sid != spool_id:
                            previous_spool = sid
                    elif lane.get("spool_id") == spool_id:
                        await self.moonraker.delete_lane(key)
                        result.step(True, f"Removed spool #{spool_id} from T{lane.get('lane')}")
                await self.moonraker.put_lane(lane_key(tool), lane_entry(tool, spool))
                result.step(True, f"Printer lane_data T{tool} updated")
            except Exception as e:  # noqa: BLE001
                result.step(False, f"Printer lane_data not updated ({_err(e)}) — is the printer on?")
        else:
            result.step(False, "MOONRAKER_URL not set — lane_data not updated")

        # 2. Spoolman location for this spool, and for the one it replaced.
        try:
            await self.spoolman.set_location(spool_id, self.s.location_for(tool))
            spool["location"] = self.s.location_for(tool)
            result.step(True, f"Spoolman location → {self.s.location_for(tool)}")
        except Exception as e:  # noqa: BLE001
            result.step(False, f"Spoolman location not set ({_err(e)})")
        if previous_spool and self.s.unload_location:
            try:
                await self.spoolman.set_location(previous_spool, self.s.unload_location)
                result.step(True, f"Spool #{previous_spool} → {self.s.unload_location}")
            except Exception as e:  # noqa: BLE001
                result.step(False, f"Spool #{previous_spool} location not changed ({_err(e)})")

        # 3. Optional Klipper variables (Mainsail's per-tool spool display uses these).
        if self.moonraker and (self.s.klipper_macro_variable or self.s.klipper_save_variables):
            lines = []
            if self.s.klipper_macro_variable:
                lines.append(f"SET_GCODE_VARIABLE MACRO=T{tool} VARIABLE=spool_id VALUE={spool_id}")
            if self.s.klipper_save_variables:
                lines.append(f"SAVE_VARIABLE VARIABLE=t{tool}__spool_id VALUE={spool_id}")
            try:
                await self.moonraker.gcode("\n".join(lines))
                result.step(True, "Klipper variables set")
            except Exception as e:  # noqa: BLE001
                result.step(False, f"Klipper variables not set ({_err(e)})")

        self.store.log("assign" if result.ok else "assign-partial", spool_id, tool,
                       spool_label(spool))
        return result

    async def clear(self, tool: int) -> Result:
        result = Result(ok=True, tool=tool)
        if not self.moonraker:
            result.step(False, "MOONRAKER_URL not set")
            return result
        spool_id = None
        try:
            lanes = await self.moonraker.lane_data()
            for key, lane in lanes.items():
                if isinstance(lane, dict) and (key == lane_key(tool) or str(lane.get("lane")) == str(tool)):
                    if isinstance(lane.get("spool_id"), int):
                        spool_id = lane["spool_id"]
                    await self.moonraker.delete_lane(key)
            result.step(True, f"T{tool} cleared on the printer")
        except Exception as e:  # noqa: BLE001
            result.step(False, f"Printer not updated ({_err(e)})")
        if spool_id and self.s.unload_location:
            try:
                await self.spoolman.set_location(spool_id, self.s.unload_location)
                result.step(True, f"Spool #{spool_id} → {self.s.unload_location}")
            except Exception as e:  # noqa: BLE001
                result.step(False, f"Spoolman not updated ({_err(e)})")
        if self.s.klipper_save_variables:
            try:
                await self.moonraker.gcode(f"SAVE_VARIABLE VARIABLE=t{tool}__spool_id VALUE=None")
            except Exception:  # noqa: BLE001
                pass
        self.store.log("clear", spool_id, tool)
        return result

    async def tools(self) -> tuple[dict[int, dict], Optional[str]]:
        """Current lane_data per tool, plus an error message if the printer can't be read."""
        if not self.moonraker:
            return {}, "MOONRAKER_URL not set"
        try:
            lanes = await self.moonraker.lane_data()
        except Exception as e:  # noqa: BLE001
            return {}, f"Printer unreachable ({_err(e)})"
        by_tool: dict[int, dict] = {}
        for lane in lanes.values():
            if isinstance(lane, dict):
                try:
                    by_tool[int(str(lane.get("lane")))] = lane
                except ValueError:
                    continue
        return by_tool, None
