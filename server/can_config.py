"""CAN interface-spec and bitrate rules shared by everything that opens the bus.

On Linux, SocketCAN owns the bitrate (set with ``ip link``) and ignores what an
application asks for. Every other python-can interface -- PCAN, gs_usb, slcan,
Kvaser, ... -- runs at whatever bitrate the process that opens it chooses. The
allocator and the scanner each open the interface, so they must agree on one
value, and it must be the bus's: a node that joins at the wrong speed floods
the bus with error frames. Cynitor therefore never guesses one; it has to be
given for every interface except SocketCAN.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

MAX_CLASSIC_CAN_BITRATE = 1_000_000
# Sanity bound for the CAN FD data phase; adapters top out between 8 and 12 Mbit/s.
MAX_FD_DATA_BITRATE = 12_000_000

BITRATE_ENV = "UAVCAN__CAN__BITRATE"
MTU_ENV = "UAVCAN__CAN__MTU"
CLASSIC_MTU = 8   # Cyphal/CAN payload per frame: Classic CAN
FD_MTU = 64       # ... and CAN FD

# python-can interfaces the hub can open as CAN FD, given a data bitrate.
# Others (gs_usb, slcan, ...) have no CAN FD mode in python-can, or only a
# partial one; on Linux such adapters do CAN FD as SocketCAN instead.
FD_INTERFACES = frozenset({"pcan", "kvaser", "vector", "ixxat", "virtual"})

# A SocketCAN interface carries CAN FD frames once its MTU is that of
# struct canfd_frame (`ip link set can0 ... fd on`, or `mtu 72` for vcan).
_CANFD_MTU = 72
_SYSFS_NET = Path("/sys/class/net")

# The local allocator's node-ID. Here rather than in allocator.py, which needs
# compiled DSDL to import, so that the node-ID picker can steer clear of it.
ALLOCATOR_NODE_ID = 1

# PythonCANMedia insists on a bitrate even for SocketCAN, which ignores it.
_SOCKETCAN_IGNORED_BITRATE = 500_000

# Earlier docs spelled python-can specs as "pythoncan:pcan:PCAN_USBBUS1".
# pycyphal splits on the first colon, so it looked for an interface named
# "pythoncan" and failed. The prefix is accepted and dropped so those
# instructions keep working.
_LEGACY_PREFIX = "pythoncan:"


def normalize_can_iface(iface: str) -> str:
    """Turn user input into a pycyphal ``<interface>:<channel>`` spec.

    A bare name such as ``vcan0`` means SocketCAN, which is what the CLI was
    built around. Anything with a colon is already a spec (``gs_usb:0``,
    ``pcan:PCAN_USBBUS1``, ``slcan:COM5@115200``) and passes through.
    """
    value = iface.strip()
    if value.lower().startswith(_LEGACY_PREFIX):
        value = value[len(_LEGACY_PREFIX):]
    if ":" in value:
        return value
    return f"socketcan:{value}"


def is_explicit_spec(iface: str) -> bool:
    """Whether ``iface`` names its python-can interface rather than a bare SocketCAN name."""
    return ":" in iface


def is_socketcan(iface: str) -> bool:
    """Whether ``iface`` opens SocketCAN, where the kernel owns the bitrate."""
    return normalize_can_iface(iface).startswith("socketcan:")


def socketcan_device(iface: str) -> str:
    """The kernel device name behind a SocketCAN ``iface`` (``socketcan:can0`` -> ``can0``)."""
    spec = normalize_can_iface(iface)
    if not spec.startswith("socketcan:"):
        raise ValueError(f"{iface!r} is not a SocketCAN interface")
    return spec[len("socketcan:"):]


def validate_bitrate(bitrate: object) -> int:
    """Return ``bitrate`` as an int, or raise ValueError if it is not a usable classic-CAN bitrate."""
    # bool is an int subclass; True would otherwise pass as 1 bit/s.
    if isinstance(bitrate, bool) or not isinstance(bitrate, int):
        raise ValueError(f"CAN bitrate must be an integer, got {bitrate!r}")
    if not 0 < bitrate <= MAX_CLASSIC_CAN_BITRATE:
        raise ValueError(
            f"CAN bitrate must be between 1 and {MAX_CLASSIC_CAN_BITRATE} bit/s, got {bitrate}"
        )
    return bitrate


def validate_data_bitrate(data_bitrate: object) -> int:
    """Return ``data_bitrate`` as an int, or raise ValueError if it is not a usable CAN FD data rate."""
    if isinstance(data_bitrate, bool) or not isinstance(data_bitrate, int):
        raise ValueError(f"CAN FD data bitrate must be an integer, got {data_bitrate!r}")
    if not 0 < data_bitrate <= MAX_FD_DATA_BITRATE:
        raise ValueError(
            f"CAN FD data bitrate must be between 1 and {MAX_FD_DATA_BITRATE} bit/s, got {data_bitrate}"
        )
    return data_bitrate


def resolve_data_bitrate(iface: str, data_bitrate: Optional[object]) -> Optional[int]:
    """The CAN FD data bitrate to open ``iface`` with, or None for Classic CAN.

    SocketCAN always gets None: the kernel owns its rates, and whether it runs
    CAN FD follows the interface's own setting (see socketcan_supports_fd).
    Raises ValueError for an adapter that cannot run CAN FD.
    """
    if data_bitrate is None or is_socketcan(iface):
        return None
    interface = normalize_can_iface(iface).split(":", 1)[0]
    if interface not in FD_INTERFACES:
        raise ValueError(
            f"{interface} adapters cannot run CAN FD through Cynitor; leave the data bitrate "
            f"out for Classic CAN (on Linux, use the adapter as SocketCAN for CAN FD)"
        )
    return validate_data_bitrate(data_bitrate)


def socketcan_supports_fd(device: str, sysfs: Path = _SYSFS_NET) -> bool:
    """Whether SocketCAN ``device`` is set up for CAN FD frames."""
    try:
        return int((sysfs / device / "mtu").read_text(encoding="utf-8")) == _CANFD_MTU
    except (OSError, ValueError):
        return False


def supports_fd(iface: str) -> bool:
    """Whether a session on ``iface`` can run CAN FD.

    For SocketCAN, whether the interface is set up for it; for anything else,
    whether Cynitor can open the adapter as CAN FD when given a data bitrate.
    """
    if is_socketcan(iface):
        return socketcan_supports_fd(socketcan_device(iface))
    return normalize_can_iface(iface).split(":", 1)[0] in FD_INTERFACES


def resolve_bitrate(iface: str, bitrate: Optional[object]) -> Optional[int]:
    """The bitrate to open ``iface`` at: validated, and required unless it is SocketCAN.

    Returns None only for SocketCAN given no bitrate. Raises ValueError when
    any other interface is given none, since there is no safe default.
    """
    if bitrate is None:
        if is_socketcan(iface):
            return None
        raise ValueError(
            f"A bitrate is required for {normalize_can_iface(iface)}: unlike SocketCAN, "
            f"the adapter runs at whatever speed Cynitor opens it with, so it must "
            f"match the bus (for example 500000)"
        )
    return validate_bitrate(bitrate)


def bitrate_env_value(bitrate: int, data_bitrate: Optional[int] = None) -> str:
    """Format the bitrates for the ``uavcan.can.bitrate`` register.

    The register holds ``[arbitration, data]``. A single number is read as
    ``[n, 0]``, which pycyphal takes for CAN FD with a zero-rate data phase,
    so classic CAN must repeat the value.
    """
    return f"{bitrate} {data_bitrate or bitrate}"


def bitrates_from_env() -> Optional[tuple[int, int]]:
    """The (arbitration, data) bitrates that ``prepare_runtime`` published, if any."""
    parts = os.environ.get(BITRATE_ENV, "").split()
    try:
        return (int(parts[0]), int(parts[-1])) if parts else None
    except ValueError:
        return None


def bitrate_from_env() -> Optional[int]:
    """The arbitration bitrate that ``prepare_runtime`` published, if any."""
    rates = bitrates_from_env()
    return rates[0] if rates else None


def media_bitrate(iface: str) -> Union[int, tuple[int, int]]:
    """The bitrate to hand PythonCANMedia for ``iface``, as published by ``prepare_runtime``.

    One number for Classic CAN; (arbitration, data) when the data phase differs.
    """
    rates = bitrates_from_env()
    if rates is not None:
        return rates[0] if rates[0] == rates[1] else rates
    if is_socketcan(iface):
        return _SOCKETCAN_IGNORED_BITRATE
    raise RuntimeError(
        f"No bitrate published for {iface}; prepare_runtime must run before the bus is opened"
    )


def media_mtu() -> int:
    """The MTU that ``prepare_runtime`` published: FD_MTU for CAN FD, else CLASSIC_MTU."""
    try:
        return int(os.environ.get(MTU_ENV, CLASSIC_MTU))
    except ValueError:
        return CLASSIC_MTU
