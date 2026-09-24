"""Find the CAN adapters the dashboard can offer in its interface list.

SocketCAN interfaces come from the kernel (see main.discover_can_interfaces).
Everything else is found here, per kind of adapter:

- vendor drivers python-can can enumerate: PEAK, Kvaser, Vector, IXXAT;
- candleLight adapters such as the CANable (gs_usb), by USB scan;
- slcan adapters, by the USB IDs of their serial ports.

On Linux the kernel drives candleLight and slcan adapters itself (as can0, or
slcan0 via slcand), so they are only scanned for elsewhere; opening them over
USB or the tty would fight the kernel for the device.

Every probe is best effort: a missing driver or library means no entries from
that probe, never an error.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

_VENDOR_LABELS = {
    "pcan": "PEAK",
    "kvaser": "Kvaser",
    "vector": "Vector",
    "ixxat": "IXXAT",
}

# USB vendor/product IDs of serial-port adapters that speak slcan. Only known
# ones are listed: offering every COM port would include debug probes and
# other serial devices that are not CAN adapters at all.
_SLCAN_USB_IDS = {
    (0x16D0, 0x117E): "CANable (slcan)",
}


@dataclass(frozen=True)
class Adapter:
    interface: str        # what --can and POST /api/can/connect take
    label: str            # what the dropdown shows
    needs_bitrate: bool   # False only for SocketCAN, where the kernel owns it

    def as_dict(self) -> dict:
        return asdict(self)


@contextlib.contextmanager
def _quiet_python_can() -> Iterator[None]:
    """Hold back python-can's import-time warnings about absent vendor libraries.

    "Kvaser canlib is unavailable" and the like are expected on any machine
    without that vendor's driver, and would otherwise land in the dashboard's
    log panel.
    """
    can_logger = logging.getLogger("can")
    previous = can_logger.level
    can_logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        can_logger.setLevel(previous)


def _vendor_adapters() -> list[Adapter]:
    import can

    found = []
    for interface, vendor in _VENDOR_LABELS.items():
        try:
            with _quiet_python_can():
                configs = can.detect_available_configs(interfaces=[interface])
        except Exception as exc:
            logger.debug("Adapter discovery: %s probe failed: %s", interface, exc)
            continue
        for config in configs:
            channel = config.get("channel")
            if channel is None:
                continue
            found.append(Adapter(f"{interface}:{channel}", f"{vendor} {channel}", True))
    return found


def _gs_usb_adapters() -> list[Adapter]:
    try:
        from startup_setup import ensure_libusb_on_path
        ensure_libusb_on_path()
        from gs_usb.gs_usb import GsUsb
        devices = GsUsb.scan()
    except Exception as exc:
        logger.debug("Adapter discovery: gs_usb scan failed: %s", exc)
        return []
    found = []
    for index, device in enumerate(devices):
        # Reading the serial number opens the device, which fails while
        # another program holds it; the adapter is still worth listing.
        try:
            serial = device.serial_number
        except Exception:
            serial = None
        # Short enough for the sidebar; the last digits tell two adapters apart.
        label = f"CANable {index}" + (f" …{serial[-4:]}" if serial else "")
        found.append(Adapter(f"gs_usb:{index}", label, True))
    return found


def _slcan_adapters() -> list[Adapter]:
    try:
        from serial.tools import list_ports
        ports = list_ports.comports()
    except Exception as exc:
        logger.debug("Adapter discovery: serial port scan failed: %s", exc)
        return []
    found = []
    for port in sorted(ports, key=lambda p: p.device):
        name = _SLCAN_USB_IDS.get((port.vid, port.pid))
        if name:
            found.append(Adapter(f"slcan:{port.device}", f"{name} {port.device}", True))
    return found


def discover_adapters(socketcan_names: Iterable[str],
                      platform: str = sys.platform) -> list[Adapter]:
    """Every adapter found, SocketCAN interfaces first."""
    adapters = [Adapter(name, f"{name} (SocketCAN)", False) for name in socketcan_names]
    adapters += _vendor_adapters()
    if not platform.startswith("linux"):
        adapters += _gs_usb_adapters()
        adapters += _slcan_adapters()
    return adapters


class AdapterCatalog:
    """The last discovery result, refreshed at most every ``max_age`` seconds.

    The dashboard polls /api/status every few seconds; discovery loads vendor
    libraries and walks USB, so it is not repeated on every poll.
    """

    def __init__(self, discover: Callable[[], list[Adapter]], max_age: float = 10.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._discover = discover
        self._max_age = max_age
        self._clock = clock
        self._lock = threading.Lock()
        self._adapters: list[Adapter] = []
        self._taken_at: Optional[float] = None

    def get(self, refresh: bool = False, rescan: bool = True) -> list[Adapter]:
        """The adapter list; rescanned if stale or ``refresh``, unless ``rescan`` is False.

        Pass ``rescan=False`` while connected: probing a USB adapter that the
        session holds cannot tell anything new and only risks disturbing it.
        """
        with self._lock:
            stale = self._taken_at is None or self._clock() - self._taken_at >= self._max_age
            if rescan and (refresh or stale):
                try:
                    self._adapters = self._discover()
                except Exception as exc:
                    logger.warning("Adapter discovery failed: %s", exc)
                self._taken_at = self._clock()
            return list(self._adapters)
