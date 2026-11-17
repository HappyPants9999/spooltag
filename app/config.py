"""App-wide settings.

Everything is edited on the /settings page and stored in the database. Environment variables are
only starting values: on first start each general setting missing from the database is seeded
from its variable (SPOOLMAN_URL, BASE_URL, ...), and the old single-printer variables
(MOONRAKER_URL, NUM_TOOLS, PRINTER_NAME, ...) become the first printer if there are none.
After that the database wins and the variables can be removed from the compose file.

Read on every start (never stored):
  DATA_DIR        where the database lives (the image sets /data)
  ALLOWED_HOSTS   extra host names the web UI answers to, comma-separated; "*" turns the Host
                  check off. A way back in if a host name was left out of Settings.
  ADMIN_PASSWORD  sets the admin password only if none is set yet
"""
import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def split_hosts(text: str) -> list[str]:
    """'a.lan, b  c' -> ['a.lan', 'b', 'c'] (lower-cased, ports and brackets removed)."""
    out = []
    for part in text.replace(",", " ").split():
        host = part.strip().lower()
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        elif host.count(":") == 1:
            host = host.split(":", 1)[0]
        if host:
            out.append(host)
    return out


@dataclass(frozen=True)
class LegacyPrinter:
    """The pre-0.2 single printer from env vars, used only to seed an empty printer list."""
    name: str
    moonraker_url: str
    api_key: str
    num_tools: int
    save_variables: bool
    macro_variable: bool


# General settings kept in the database, in the order the Settings page shows them.
GENERAL = ("spoolman_url", "base_url", "location_format", "unload_location", "pending_timeout", "timezone",
           "extra_hosts", "allow_public_urls")


@dataclass
class Settings:
    """One shared instance; the Settings page updates it in place, so changes apply at once."""
    spoolman_url: str
    base_url: str
    location_format: str
    unload_location: str
    pending_timeout: int
    data_dir: str
    legacy_printer: LegacyPrinter | None = None
    timezone: str = ""      # IANA name, e.g. America/Denver; empty = the container's local time
    extra_hosts: str = ""   # host names (besides IP addresses and the tag base URL's) the UI answers to
    allow_public_urls: bool = False   # service URLs may point at public internet addresses
    env_allowed_hosts: list[str] = field(default_factory=list)   # ALLOWED_HOSTS; not stored
    admin_password_seed: str = ""     # ADMIN_PASSWORD; not stored as text

    def location_for(self, printer_name: str, tool: int) -> str:
        return self.location_format.format(printer=printer_name, tool=tool)

    def general(self) -> dict[str, str]:
        return {k: ("1" if v else "0") if isinstance(v := getattr(self, k), bool) else str(v) for k in GENERAL}

    def apply(self, values: dict[str, str]) -> None:
        """Set general settings from stored strings (unknown keys are ignored)."""
        for key, value in values.items():
            if key not in GENERAL:
                continue
            if key == "pending_timeout":
                try:
                    self.pending_timeout = int(value)
                except ValueError:
                    continue
            elif key == "allow_public_urls":
                self.allow_public_urls = value.strip().lower() in ("1", "true", "yes", "on")
            elif key.endswith("_url"):
                setattr(self, key, value.strip().rstrip("/"))
            else:
                setattr(self, key, value)

    def allowed_hosts(self) -> set[str]:
        """Host names (not IP addresses, which are always allowed) the web UI answers to."""
        from urllib.parse import urlsplit
        hosts = {"localhost"} | set(split_hosts(self.extra_hosts)) | set(self.env_allowed_hosts)
        if self.base_url:
            host = urlsplit(self.base_url).hostname
            if host:
                hosts.add(host.lower())
        return hosts


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
        timezone=os.environ.get("TZ", "").strip(),
        extra_hosts="",
        allow_public_urls=_bool("ALLOW_PUBLIC_URLS", False),
        env_allowed_hosts=split_hosts(os.environ.get("ALLOWED_HOSTS", "").replace("*", " * ")),
        admin_password_seed=os.environ.get("ADMIN_PASSWORD", ""),
    )
