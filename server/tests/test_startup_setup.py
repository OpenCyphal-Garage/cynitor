"""Tests for startup_setup: project-root resolution and the CAN runtime environment."""

import os
import sys
import types
from pathlib import Path

import pytest

import startup_setup
from startup_setup import prepare_runtime, resolve_project_root


class TestResolveProjectRoot:
    def test_resolves_to_repository_root_from_source(self):
        root = resolve_project_root()
        # The root is the parent of server/, so it holds the DSDL definitions.
        assert (root / "dsdl_messages").is_dir()
        assert (root / "server" / "startup_setup.py").is_file()

    def test_uses_meipass_when_frozen(self, monkeypatch, tmp_path):
        # PyInstaller unpacks bundled data to sys._MEIPASS; the source tree is
        # absent in a frozen binary, so the root must follow the extraction dir.
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert resolve_project_root() == Path(tmp_path)

    def test_ignores_meipass_when_not_frozen(self, monkeypatch, tmp_path):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert resolve_project_root() != Path(tmp_path)


@pytest.fixture
def runtime_env(monkeypatch):
    """Run prepare_runtime without compiling DSDL or calling yakut, and undo its env changes."""
    for name in ("UAVCAN__CAN__IFACE", "UAVCAN__CAN__MTU", "UAVCAN__CAN__BITRATE",
                 "CYPHAL_PATH", "PYCYPHAL_PATH", "PYTHONPATH", "PATH"):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    # A preset node-ID skips `yakut accommodate`, which would open the bus.
    monkeypatch.setenv("UAVCAN__NODE__ID", "42")
    monkeypatch.setattr(startup_setup, "_run_nnvg", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return monkeypatch


class TestPrepareRuntimeCanEnv:
    def test_bare_name_becomes_socketcan(self, runtime_env):
        prepare_runtime("vcan0")
        assert os.environ["UAVCAN__CAN__IFACE"] == "socketcan:vcan0"

    def test_spec_is_used_as_given(self, runtime_env):
        prepare_runtime("gs_usb:0", bitrate=500_000)
        assert os.environ["UAVCAN__CAN__IFACE"] == "gs_usb:0"

    def test_legacy_pythoncan_prefix_is_dropped(self, runtime_env):
        prepare_runtime("pythoncan:pcan:PCAN_USBBUS1", bitrate=500_000)
        assert os.environ["UAVCAN__CAN__IFACE"] == "pcan:PCAN_USBBUS1"

    def test_publishes_bitrate_for_both_phases(self, runtime_env):
        # Unset, pycyphal would open the adapter at 1 Mbit/s; a single number
        # would be read as CAN FD with a zero data phase.
        prepare_runtime("gs_usb:0", bitrate=250_000)
        assert os.environ["UAVCAN__CAN__BITRATE"] == "250000 250000"

    def test_adapter_without_bitrate_is_refused_before_touching_env(self, runtime_env):
        os.environ["UAVCAN__CAN__IFACE"] = "untouched"
        with pytest.raises(ValueError, match="bitrate is required"):
            prepare_runtime("gs_usb:0")
        assert os.environ["UAVCAN__CAN__IFACE"] == "untouched"

    def test_rejects_invalid_bitrate_before_touching_env(self, runtime_env):
        os.environ["UAVCAN__CAN__IFACE"] = "untouched"
        with pytest.raises(ValueError):
            prepare_runtime("gs_usb:0", bitrate=0)
        assert os.environ["UAVCAN__CAN__IFACE"] == "untouched"

    def test_socketcan_without_bitrate_clears_a_stale_one(self, runtime_env):
        # The kernel's setting applies; a value left by an earlier adapter
        # session must not reach the allocator.
        os.environ["UAVCAN__CAN__BITRATE"] = "250000 250000"
        prepare_runtime("vcan0")
        assert "UAVCAN__CAN__BITRATE" not in os.environ


class TestLibusbOnPath:
    @pytest.fixture
    def fake_libusb(self, monkeypatch, tmp_path):
        dll = tmp_path / "libusb-1.0.dll"
        module = types.SimpleNamespace(get_library_path=lambda: dll)
        monkeypatch.setitem(sys.modules, "libusb_package", module)
        monkeypatch.setenv("PATH", "existing")
        return dll

    def test_prepends_dll_directory_on_windows(self, monkeypatch, fake_libusb):
        monkeypatch.setattr(sys, "platform", "win32")
        startup_setup._ensure_libusb_on_path()
        first = os.environ["PATH"].split(os.pathsep)[0]
        assert first == str(fake_libusb.parent.resolve())

    def test_no_op_off_windows(self, monkeypatch, fake_libusb):
        monkeypatch.setattr(sys, "platform", "linux")
        startup_setup._ensure_libusb_on_path()
        assert os.environ["PATH"] == "existing"

    def test_no_op_without_the_package(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "libusb_package", None)  # import raises ImportError
        monkeypatch.setenv("PATH", "existing")
        startup_setup._ensure_libusb_on_path()
        assert os.environ["PATH"] == "existing"
