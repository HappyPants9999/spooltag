"""Settings, read once from environment variables."""
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
class Settings:
    spoolman_url: str
    moonraker_url: str
    moonraker_api_key: str
    base_url: str
    num_tools: int
    printer_name: str
    location_format: str
    unload_location: str
    pending_timeout: int
    klipper_save_variables: bool
    klipper_macro_variable: bool
    data_dir: str

    def location_for(self, tool: int) -> str:
        return self.location_format.format(printer=self.printer_name, tool=tool)


def load_settings() -> Settings:
    return Settings(
        spoolman_url=os.environ.get("SPOOLMAN_URL", "http://spoolman:8000").rstrip("/"),
        moonraker_url=os.environ.get("MOONRAKER_URL", "").rstrip("/"),
        moonraker_api_key=os.environ.get("MOONRAKER_API_KEY", ""),
        # Public base for tag URLs, e.g. http://192.168.1.20:8090. Empty = use the request's host.
        base_url=os.environ.get("BASE_URL", "").rstrip("/"),
        num_tools=_int("NUM_TOOLS", 8),
        printer_name=os.environ.get("PRINTER_NAME", "SV08"),
        # Spoolman location text for a loaded spool. {printer} and {tool} are substituted.
        location_format=os.environ.get("LOCATION_FORMAT", "{printer} T{tool}"),
        # Location given to the spool that a new assignment pushes out of a tool. Empty = leave it.
        unload_location=os.environ.get("UNLOAD_LOCATION", ""),
        pending_timeout=_int("PENDING_TIMEOUT", 120),
        klipper_save_variables=_bool("KLIPPER_SAVE_VARIABLES", False),
        klipper_macro_variable=_bool("KLIPPER_MACRO_VARIABLE", False),
        data_dir=os.environ.get("DATA_DIR", "/data"),
    )
