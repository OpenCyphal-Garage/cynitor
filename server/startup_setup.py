#!/usr/bin/env python3

import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from can_config import (
    BITRATE_ENV,
    bitrate_env_value,
    normalize_can_iface,
    resolve_bitrate,
)

logger = logging.getLogger(__name__)


def _prepend_env_path(var_name: str, path: Path) -> None:
    resolved = str(path.resolve())
    current = os.environ.get(var_name, "")
    parts = [item for item in current.split(os.pathsep) if item]
    if resolved not in parts:
        os.environ[var_name] = os.pathsep.join([resolved, *parts]) if parts else resolved


def _ensure_sys_path(path: Path) -> None:
    resolved = str(path.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


def _run_nnvg(args: list[str], description: str) -> None:
    if shutil.which("nnvg") is None:
        logger.warning("nnvg is not available; skipping DSDL compilation step: %s", description)
        return

    try:
        result = subprocess.run(
            ["nnvg", *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        logger.warning("Failed to run nnvg for %s: %s", description, exc)
        return

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        logger.warning(
            "nnvg failed for %s (exit=%s)%s",
            description,
            result.returncode,
            f": {stderr}" if stderr else "",
        )


def _auto_node_id() -> str | None:
    if shutil.which("yakut") is None:
        logger.warning("yakut is not available; UAVCAN__NODE__ID was not auto-assigned")
        return None

    try:
        result = subprocess.run(
            ["yakut", "accommodate"],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        logger.warning("Failed to run 'yakut accommodate': %s", exc)
        return None

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        logger.warning(
            "'yakut accommodate' failed (exit=%s)%s",
            result.returncode,
            f": {stderr}" if stderr else "",
        )
        return None

    value = (result.stdout or "").strip()
    if not value:
        logger.warning("'yakut accommodate' returned an empty node-ID")
        return None
    if not value.isdigit():
        logger.warning("'yakut accommodate' returned non-numeric node-ID: %s", value)
        return None
    return value


def resolve_project_root() -> Path:
    """
    Directory that holds dsdl_messages/ and python_compiled_messages/.

    Under PyInstaller the source tree does not exist on disk: bundled data is
    unpacked to a temporary directory that the frozen binary reports through
    sys._MEIPASS. Everywhere else the root is the parent of server/.
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def ensure_libusb_on_path() -> None:
    """Let pyusb find libusb on Windows, where it is not a system library.

    python-can's gs_usb interface (candleLight adapters such as the CANable)
    reaches the adapter through pyusb, which looks for libusb-1.0.dll on PATH
    and fails with NoBackendError when it is missing. The libusb-package wheel
    bundles the DLL; putting its directory on PATH covers this process and the
    `yakut accommodate` child alike.
    """
    if sys.platform != "win32":
        return
    try:
        import libusb_package
    except ImportError:
        return
    dll_path = libusb_package.get_library_path()
    if dll_path:
        _prepend_env_path("PATH", Path(dll_path).parent)


def prepare_runtime(can_iface: str = "can0", force_compile: bool = False,
                    bitrate: int | None = None, auto_node_id: bool = True) -> None:
    """Prepare environment variables, sys.path, and optional DSDL compilation for runtime.

    `auto_node_id` runs `yakut accommodate` when UAVCAN__NODE__ID is unset. It
    has to be off when `can_iface` is a CAN hub's in-process channel, which a
    child process cannot see.

    `bitrate` is required for every interface except SocketCAN, whose bitrate
    is set with `ip link`; ValueError is raised before anything changes if it
    is missing or invalid.
    """
    bitrate = resolve_bitrate(can_iface, bitrate)
    project_root = resolve_project_root()
    dsdl_dir = project_root / "dsdl_messages"
    public_types_dir = dsdl_dir / "public_regulated_data_types"
    uavcan_dir = public_types_dir / "uavcan"
    reg_dir = public_types_dir / "reg"
    python_output_dir = project_root / "python_compiled_messages"

    already_compiled = all(
        (python_output_dir / name).is_dir() for name in ("uavcan", "reg")
    )

    should_compile = force_compile or not already_compiled
    if not should_compile:
        logger.info("DSDL already compiled in %s — skipping (use --recompile to force)", python_output_dir)

    if should_compile:
        _run_nnvg(
            [
                "--target-language", "py",
                str(reg_dir),
                "--lookup-dir", str(uavcan_dir),
                "--outdir", str(python_output_dir),
            ],
            "reg messages",
        )
        _run_nnvg(
            [
                "--target-language", "py",
                str(uavcan_dir),
                "--lookup-dir", str(reg_dir),
                "--outdir", str(python_output_dir),
            ],
            "uavcan messages",
        )

    _prepend_env_path("CYPHAL_PATH", public_types_dir)
    os.environ["PYCYPHAL_PATH"] = str(python_output_dir.resolve())
    _prepend_env_path("PYTHONPATH", python_output_dir)
    _ensure_sys_path(python_output_dir)

    # A full transport spec (e.g. "gs_usb:0", "pcan:PCAN_USBBUS1") is used as
    # given; a bare name such as "vcan0" means Linux SocketCAN.
    os.environ["UAVCAN__CAN__IFACE"] = normalize_can_iface(can_iface)
    os.environ["UAVCAN__CAN__MTU"] = "8"
    # Published before `yakut accommodate` runs, so that it too opens the
    # interface at this bitrate rather than pycyphal's 1 Mbit/s default.
    # Cleared for SocketCAN, where the kernel's setting applies, so that a
    # previous session's value does not linger.
    if bitrate is None:
        os.environ.pop(BITRATE_ENV, None)
    else:
        os.environ[BITRATE_ENV] = bitrate_env_value(bitrate)
    ensure_libusb_on_path()

    if auto_node_id and "UAVCAN__NODE__ID" not in os.environ:
        node_id = _auto_node_id()
        if node_id is not None:
            os.environ["UAVCAN__NODE__ID"] = node_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare runtime environment and compile DSDL messages")
    parser.add_argument("--can", default="can0", help="CAN interface name (default: can0)")
    parser.add_argument(
        "--recompile",
        action="store_true",
        help="Force DSDL recompilation even if already compiled",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    prepare_runtime(can_iface=args.can, force_compile=args.recompile)
    logger.info("Runtime environment prepared (CAN interface: %s)", args.can)
