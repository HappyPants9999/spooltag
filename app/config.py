"""App-wide settings from environment variables.

Printers and storage locations live in the database and are edited on the /settings page. The
old single-printer variables (MOONRAKER_URL, NUM_TOOLS, PRINTER_NAME, ...) are still read once:
on first start with an empty printer list they become the first printer.
"""
import os
from dataclasses import dataclass


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@dataclass(frozen=True)
class LegacyPrinter:
    """The pre-0.2 single printer from env vars, used only to seed an empty printer list."""
    name: str
    moonraker_url: str
    api_key: str
    num_tools: int
    save_variables: bool
    macro_variable: bool


@dataclass(frozen=True)
class Settings:
    spoolman_url: str
    base_url: str
    location_format: str
    unload_location: str
    pending_timeout: int
    data_dir: str
    legacy_printer: LegacyPrinter | None = None

    def location_for(self, printer_name: str, tool: int) -> str:
        return self.location_format.format(printer=printer_name, tool=tool)


def load_settings() -> Settings:
    moonraker_url = os.environ.get("MOONRAKER_URL", "").strip().rstrip("/")
    legacy = LegacyPrinter(
        name=os.environ.get("PRINTER_NAME", "SV08").strip() or "SV08",
        moonraker_url=moonraker_url,
        api_key=os.environ.get("MOONRAKER_API_KEY", ""),
        num_tools=_int("NUM_TOOLS", 8),
        save_variables=_bool("KLIPPER_SAVE_VARIABLES", False),
        macro_variable=_bool("KLIPPER_MACRO_VARIABLE", False),
    ) if moonraker_url else None
    return Settings(
        spoolman_url=os.environ.get("SPOOLMAN_URL", "http://spoolman:8000").rstrip("/"),
        # Public base for tag URLs, e.g. http://10.10.2.176:7913. Empty = use the request's host.
        base_url=os.environ.get("BASE_URL", "").rstrip("/"),
        # Spoolman location text for a spool loaded in a tool. {printer} and {tool} are substituted.
        location_format=os.environ.get("LOCATION_FORMAT", "{printer} T{tool}"),
        # Location for a spool pushed out of a tool or cleared. Empty = leave its location alone.
        unload_location=os.environ.get("UNLOAD_LOCATION", ""),
        pending_timeout=_int("PENDING_TIMEOUT", 120),
        data_dir=os.environ.get("DATA_DIR", "/data"),
        legacy_printer=legacy,
    )
