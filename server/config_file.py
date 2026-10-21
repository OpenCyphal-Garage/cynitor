"""cynitor.toml: the server's settings in a file, for a server started without a command line.

A double-clicked executable or AppImage gets no options, so whatever it should do other than the
defaults (listen on the network, open a bus at start) goes in a file. Its settings are named as the
options are (`bind`, `listen-only`, `data-bitrate`, ...); the command line overrides them, and so
does the environment for those that stand for a variable (`auth-token` for CYNITOR_AUTH_TOKEN).

The file is looked for where --config or CYNITOR_CONFIG says, else next to the executable (the
AppImage file, not its mount), else in the data folder.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10: tomli, which became tomllib
    import tomli as tomllib

from can_config import validate_bitrate, validate_data_bitrate

FILE_NAME = "cynitor.toml"
CONFIG_ENV = "CYNITOR_CONFIG"
_ENV = "env:"

# Each setting: its type, and the option's argparse dest, or the environment variable it stands for.
SETTINGS: dict[str, tuple[type, str]] = {
    "bind": (str, "bind"),
    "port": (int, "port"),
    "auth-token": (str, _ENV + "CYNITOR_AUTH_TOKEN"),
    "insecure-no-auth": (bool, "insecure_no_auth"),
    "dev-origins": (str, _ENV + "CYNITOR_DEV_ORIGINS"),
    "access-log": (bool, "access_log"),
    "can": (str, "can"),
    "bitrate": (int, "bitrate"),
    "data-bitrate": (int, "data_bitrate"),
    "reconnect": (bool, "reconnect"),
    "listen-only": (bool, "listen_only"),
    "no-commands": (bool, "no_commands"),
    "allocator": (bool, "allocator"),
    "decode-on-demand": (bool, "decode_on_demand"),
    "node-id": (int, _ENV + "UAVCAN__NODE__ID"),
    "allow-any-interface": (bool, "allow_any_interface"),
    "no-update-check": (bool, "no_update_check"),
    "no-frontend": (bool, "no_frontend"),
    "data-dir": (str, _ENV + "CYNITOR_DATA_DIR"),
}

_TYPE_NAMES = {str: "a string, in quotes", int: "a whole number", bool: "true or false"}

# What a first start writes: every setting, at its default or an example, commented out.
TEMPLATE = """\
# Cynitor's settings. Each is optional: take out the # in front of one to set it,
# then restart Cynitor. The command line overrides this file, and so do the
# environment variables (CYNITOR_AUTH_TOKEN, ...). cynitor-server --help says more.

# ── Network ──
# Where the dashboard and API listen. "0.0.0.0" opens them to the network,
# which needs auth-token (or insecure-no-auth, on a network you trust).
#bind = "127.0.0.1"
#port = 8080
# The token every request then carries (CYNITOR_AUTH_TOKEN): a long random value.
#auth-token = "change-me"
#insecure-no-auth = false
# Other pages that may use the API, comma-separated (CYNITOR_DEV_ORIGINS).
#dev-origins = "http://localhost:5500"
# Log every HTTP request.
#access-log = false

# ── A bus to open at start; without one, it is picked in the dashboard ──
#can = "can0"
# bit/s: needed for every adapter Cynitor opens itself (PCAN, gs_usb, slcan, ...).
#bitrate = 1000000
# CAN FD's data phase, in bit/s; leave it out for Classic CAN.
#data-bitrate = 4000000
# Open the bus again whenever its session ends (an adapter unplugged, a bus fault).
#reconnect = false
#listen-only = false
#no-commands = false
#allocator = false
#decode-on-demand = false
# Cynitor's own node-ID on the bus, 0 to 127 (UAVCAN__NODE__ID); picked per bus without it.
#node-id = 127

# ── Other ──
#allow-any-interface = false
#no-update-check = false
#no-frontend = false
# Where history, recordings and raw logs are kept (CYNITOR_DATA_DIR); a relative
# path is taken from this file's folder, so "data" keeps them next to it. A
# Windows path goes in single quotes, as a backslash in double ones is an escape:
# 'D:\\Cynitor data'.
#data-dir = "data"
"""


class ConfigError(ValueError):
    """A settings file that cannot be used, and why."""


def executable_dir(environ: Mapping[str, str] = os.environ) -> Optional[Path]:
    """The folder of the file the user started: the AppImage, else a frozen executable; None from source."""
    appimage = environ.get("APPIMAGE")  # set by the AppImage runtime; the executable is in its mount
    if appimage:
        return Path(appimage).resolve().parent
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def find(explicit: Optional[str], data_dir: Path,
         environ: Mapping[str, str] = os.environ) -> tuple[Optional[Path], list[Path]]:
    """The settings file to read, if any, and the places it was looked for. One named must exist."""
    named = explicit or environ.get(CONFIG_ENV)
    if named:
        path = Path(named).expanduser()
        if not path.is_file():
            raise ConfigError(f"{path}: no such settings file")
        return path, [path]
    places = [folder / FILE_NAME for folder in (executable_dir(environ), data_dir) if folder is not None]
    return next((place for place in places if place.is_file()), None), places


def load(path: Path) -> dict[str, Any]:
    """The settings in ``path``, checked: an unknown name or a value of the wrong type is refused."""
    try:
        with open(path, "rb") as file:
            raw = tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"{path}: {exc.strerror or exc}") from None
    for name, value in raw.items():
        if name not in SETTINGS:
            guess = difflib.get_close_matches(name.replace("_", "-"), SETTINGS, n=1)
            hint = f"did you mean {guess[0]!r}?" if guess else f"the settings are {', '.join(SETTINGS)}"
            raise ConfigError(f"{path}: there is no setting {name!r}; {hint}")
        kind = SETTINGS[name][0]
        # bool is an int to Python: true is no port.
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ConfigError(f"{path}: {name} must be {_TYPE_NAMES[kind]}, not {value!r}")
    _check_ranges(path, raw)
    return raw


def _check_ranges(path: Path, settings: Mapping[str, Any]) -> None:
    checks = {"bitrate": validate_bitrate, "data-bitrate": validate_data_bitrate}
    for name, check in checks.items():
        if name in settings:
            try:
                check(settings[name])
            except ValueError as exc:
                raise ConfigError(f"{path}: {name}: {exc}") from None
    for name, low, high in (("port", 1, 65535), ("node-id", 0, 127)):
        if name in settings and not low <= settings[name] <= high:
            raise ConfigError(f"{path}: {name} must be between {low} and {high}, not {settings[name]}")


def apply(settings: Mapping[str, Any], path: Path, parser: argparse.ArgumentParser,
          environ: MutableMapping[str, str] = os.environ) -> None:
    """Make ``settings`` the defaults the command line overrides; one that stands for an
    environment variable is set only where the environment has not set it."""
    defaults = {}
    for name, value in settings.items():
        target = SETTINGS[name][1]
        if name == "data-dir":
            value = str(path.parent / Path(value).expanduser())  # an absolute path stays as it is
        if target.startswith(_ENV):
            environ.setdefault(target[len(_ENV):], str(value))
        else:
            defaults[target] = value
    parser.set_defaults(**defaults)


def write_template(path: Path) -> None:
    """Write TEMPLATE to ``path``, for its owner only, as it may come to hold the token. Never over a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
        file.write(TEMPLATE)
