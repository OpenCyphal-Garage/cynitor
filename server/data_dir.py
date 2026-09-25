"""Where Cynitor keeps what it saves between runs.

- telemetry_events.db: bus history, recordings, node history, service calls
  and the remembered device identities;
- allocator_2app.db: the local allocator's node-ID table, which keeps a
  device on the same node-ID across restarts;
- monitor_app.db: the scanner node's registers.

They used to be written to the working directory, so every folder Cynitor was
started from got a history of its own, a double-clicked executable wrote next
to itself, and the systemd service wrote into /. The scanner's file sat next
to its module, which inside the single-file executable is the temporary unpack
folder, emptied on every exit.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

DATA_DIR_ENV = "CYNITOR_DATA_DIR"

EVENTS_DB = "telemetry_events.db"
ALLOCATOR_DB = "allocator_2app.db"
SCANNER_DB = "monitor_app.db"

# Files a working directory may hold from before the data folder existed.
# The scanner's registers lived next to its module instead, and only hold
# settings it rebuilds, so they are not worth carrying over.
_LEGACY_FILES = (EVENTS_DB, ALLOCATOR_DB)

# In WAL mode, committed transactions can still be in the -wal file rather
# than the database itself, so a database never moves without its companions.
_SQLITE_COMPANIONS = ("-wal", "-shm", "-journal")


def default_data_dir(platform: str = sys.platform,
                     environ: Optional[Mapping[str, str]] = None,
                     home: Optional[Path] = None) -> Path:
    """The per-user folder for Cynitor's data on this OS."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    if platform == "win32":
        return Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Cynitor"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Cynitor"
    # A systemd service with StateDirectory= is told its folder, /var/lib/...
    state = environ.get("STATE_DIRECTORY")
    if state:
        return Path(state.split(":")[0])
    return Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "cynitor"


def resolve_data_dir(cli_value: Optional[str] = None,
                     environ: Optional[Mapping[str, str]] = None) -> Path:
    """--data-dir, else CYNITOR_DATA_DIR, else the OS's per-user folder."""
    environ = os.environ if environ is None else environ
    chosen = cli_value or environ.get(DATA_DIR_ENV)
    return Path(chosen).expanduser() if chosen else default_data_dir(environ=environ)


def prepare_data_dir(data_dir: Path, legacy_dir: Path) -> list[str]:
    """Create ``data_dir`` and move data left in ``legacy_dir`` into it.

    ``legacy_dir`` is where earlier versions wrote: the working directory.
    A file is moved only if the data folder has none of that name yet, so an
    existing history is never overwritten. Returns the names moved.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    if legacy_dir.resolve() == data_dir.resolve():
        return []
    moved = []
    for name in _LEGACY_FILES:
        source = legacy_dir / name
        if not source.is_file():
            continue
        if (data_dir / name).exists():
            logger.warning("Not moving %s: %s already has one, which is kept", source, data_dir)
            continue
        parts = [legacy_dir / (name + suffix) for suffix in ("",) + _SQLITE_COMPANIONS]
        done: list[Path] = []
        try:
            for part in parts:
                if part.is_file():
                    shutil.move(str(part), str(data_dir / part.name))
                    done.append(part)
        except OSError as exc:
            # Typically another Cynitor still has it open (Windows refuses to
            # move open files). Put back what moved: a database separated
            # from its -wal file loses whatever was committed there.
            for part in done:
                shutil.move(str(data_dir / part.name), str(part))
            logger.warning("Could not move %s into %s (%s); left where it is", source, data_dir, exc)
            continue
        moved.append(name)
    return moved
