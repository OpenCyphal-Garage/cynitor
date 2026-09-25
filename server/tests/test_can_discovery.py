"""Tests for adapter discovery and the cached catalog the dashboard reads.

Every probe is replaced, so nothing here touches USB, serial ports or vendor
drivers.
"""

import logging
import types

import pytest

import can_discovery
from can_discovery import Adapter, AdapterCatalog, discover_adapters


@pytest.fixture
def probes(monkeypatch):
    """Pin each probe's result; tests override the ones they care about."""
    results = {"vendor": [], "gs_usb": [], "slcan": []}
    monkeypatch.setattr(can_discovery, "_vendor_adapters", lambda: results["vendor"])
    monkeypatch.setattr(can_discovery, "_gs_usb_adapters", lambda: results["gs_usb"])
    monkeypatch.setattr(can_discovery, "_slcan_adapters", lambda: results["slcan"])
    # Not whatever this machine's vcan0 happens to be set up as.
    monkeypatch.setattr(can_discovery, "supports_fd", lambda iface: iface in results["fd"])
    results["fd"] = set()
    return results


class TestDiscoverAdapters:
    def test_socketcan_first_and_needs_no_bitrate(self, probes):
        probes["vendor"] = [Adapter("pcan:PCAN_USBBUS1", "PEAK PCAN_USBBUS1", True)]
        adapters = discover_adapters(["vcan0"], platform="linux")
        assert adapters[0] == Adapter("vcan0", "vcan0 (SocketCAN)", False)
        assert adapters[1].interface == "pcan:PCAN_USBBUS1"

    def test_usb_and_serial_adapters_are_scanned_off_linux(self, probes):
        probes["gs_usb"] = [Adapter("gs_usb:0", "CANable 0", True)]
        probes["slcan"] = [Adapter("slcan:COM5", "CANable (slcan) COM5", True)]
        interfaces = [a.interface for a in discover_adapters([], platform="win32")]
        assert interfaces == ["gs_usb:0", "slcan:COM5"]

    def test_linux_leaves_usb_and_serial_adapters_to_the_kernel(self, probes):
        # There they are can0 / slcan0; opening them over USB would fight
        # the kernel's driver for the device.
        probes["gs_usb"] = [Adapter("gs_usb:0", "x", True)]
        probes["slcan"] = [Adapter("slcan:/dev/ttyACM0", "x", True)]
        assert discover_adapters(["can0"], platform="linux") == [
            Adapter("can0", "can0 (SocketCAN)", False),
        ]

    def test_socketcan_set_up_for_can_fd_says_so(self, probes):
        probes["fd"] = {"can1"}
        assert discover_adapters(["can0", "can1"], platform="linux") == [
            Adapter("can0", "can0 (SocketCAN)", False, False),
            Adapter("can1", "can1 (SocketCAN)", False, True),
        ]

    def test_as_dict(self):
        assert Adapter("gs_usb:0", "CANable", True).as_dict() == {
            "interface": "gs_usb:0", "label": "CANable", "needs_bitrate": True, "supports_fd": False,
        }


class TestVendorProbe:
    def test_turns_python_can_configs_into_specs(self, monkeypatch):
        import can

        def detect(interfaces):
            return {
                "pcan": [{"interface": "pcan", "channel": "PCAN_USBBUS1"}],
                "kvaser": [{"interface": "kvaser", "channel": 0}],
            }.get(interfaces[0], [])

        monkeypatch.setattr(can, "detect_available_configs", detect)
        # Both drivers run CAN FD through python-can.
        assert can_discovery._vendor_adapters() == [
            Adapter("pcan:PCAN_USBBUS1", "PEAK PCAN_USBBUS1", True, True),
            Adapter("kvaser:0", "Kvaser 0", True, True),
        ]

    def test_a_failing_probe_skips_only_that_vendor(self, monkeypatch):
        import can

        def detect(interfaces):
            if interfaces[0] == "pcan":
                raise OSError("PCANBasic.dll crashed")
            if interfaces[0] == "ixxat":
                return [{"interface": "ixxat", "channel": 0}]
            return []

        monkeypatch.setattr(can, "detect_available_configs", detect)
        assert [a.interface for a in can_discovery._vendor_adapters()] == ["ixxat:0"]

    def test_missing_driver_warnings_stay_out_of_the_log(self, monkeypatch, caplog):
        # python-can warns at import when a vendor library is absent, which
        # is the normal case; the dashboard's log panel should not fill up.
        import can

        def detect(interfaces):
            logging.getLogger("can.kvaser").warning("Kvaser canlib is unavailable.")
            return []

        monkeypatch.setattr(can, "detect_available_configs", detect)
        level_before = logging.getLogger("can").level
        with caplog.at_level(logging.INFO):
            can_discovery._vendor_adapters()
        assert "canlib" not in caplog.text
        assert logging.getLogger("can").level == level_before  # restored


class TestSlcanProbe:
    def test_lists_only_known_slcan_adapters(self, monkeypatch):
        list_ports = pytest.importorskip("serial.tools.list_ports")
        port = lambda device, vid, pid: types.SimpleNamespace(device=device, vid=vid, pid=pid)
        monkeypatch.setattr(list_ports, "comports", lambda: [
            port("COM11", 0x0483, 0x374B),   # ST-Link debug probe
            port("COM5", 0x16D0, 0x117E),    # CANable with slcan firmware
            port("COM1", None, None),        # built-in serial port
        ])
        assert can_discovery._slcan_adapters() == [
            Adapter("slcan:COM5", "CANable (slcan) COM5", True),
        ]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TestAdapterCatalog:
    @pytest.fixture
    def scans(self):
        return []

    @pytest.fixture
    def clock(self):
        return FakeClock()

    @pytest.fixture
    def catalog(self, scans, clock):
        def discover():
            scans.append(clock.now)
            return [Adapter(f"gs_usb:{len(scans)}", "x", True)]
        return AdapterCatalog(discover, max_age=10.0, clock=clock)

    def test_first_get_scans(self, catalog, scans):
        assert catalog.get()[0].interface == "gs_usb:1"
        assert scans == [0.0]

    def test_serves_the_cache_while_fresh(self, catalog, scans, clock):
        catalog.get()
        clock.now = 9.9
        catalog.get()
        assert scans == [0.0]

    def test_rescans_once_stale(self, catalog, scans, clock):
        catalog.get()
        clock.now = 10.0
        assert catalog.get()[0].interface == "gs_usb:2"

    def test_refresh_forces_a_rescan(self, catalog, scans):
        catalog.get()
        catalog.get(refresh=True)
        assert len(scans) == 2

    def test_no_rescan_while_told_not_to(self, catalog, scans, clock):
        # While connected: the adapter in use is not probed, even if stale.
        catalog.get()
        clock.now = 60.0
        assert catalog.get(refresh=True, rescan=False)[0].interface == "gs_usb:1"
        assert scans == [0.0]

    def test_failed_discovery_keeps_the_last_list(self, clock):
        results = [[Adapter("gs_usb:0", "x", True)], RuntimeError("usb stack gone")]

        def discover():
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        catalog = AdapterCatalog(discover, max_age=10.0, clock=clock)
        catalog.get()
        clock.now = 10.0
        assert [a.interface for a in catalog.get()] == ["gs_usb:0"]
