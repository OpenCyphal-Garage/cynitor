#!/usr/bin/env python3

import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

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


def prepare_runtime(can_iface: str = "can0", force_compile: bool = False) -> None:
    """Prepare environment variables, sys.path, and optional DSDL compilation for runtime."""
    project_root = Path(__file__).resolve().parent.parent
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

    # If the user passed a full pycyphal transport spec (contains ":") use it
    # verbatim — e.g. "pythoncan:pcan:PCAN_USBBUS1" on Windows or
    # "socketcan:vcan0" cross-platform. Otherwise default to socketcan, which
    # is the Linux SocketCAN path the original CLI was built around.
    iface_spec = can_iface if ":" in can_iface else f"socketcan:{can_iface}"
    os.environ["UAVCAN__CAN__IFACE"] = iface_spec
    os.environ["UAVCAN__CAN__MTU"] = "8"

    if "UAVCAN__NODE__ID" not in os.environ:
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
