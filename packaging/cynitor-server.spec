# PyInstaller spec for the Cynitor backend sidecar.
#
# Build:
#   cd cynitor/packaging
#   python3 -m PyInstaller cynitor-server.spec --noconfirm
#
# Output: dist/cynitor-server  (single-file executable)

import sys
from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path(SPECPATH).resolve().parent
SERVER_DIR = PROJECT_ROOT / "server"
COMPILED_DSDL = PROJECT_ROOT / "python_compiled_messages"
DSDL_SOURCES = PROJECT_ROOT / "dsdl_messages"

# All server .py modules live flat in server/ — collect them as the analysis
# entry plus hidden imports so PyInstaller traces their dependency trees.
server_modules = [
    "websocket_server",
    "scanner_node",
    "node_info",
    "telemetry_manager",
    "event_logger",
    "allocator",
    "startup_setup",
    "log_store",
    "replay",
    "dsdl_manager",
    "frame_capture",
    "node_identity_map",
]

# pycyphal uses dynamic imports extensively. Enumerate the subpackages that
# our code actually touches so PyInstaller doesn't miss them.
pycyphal_hidden = [
    "pycyphal",
    "pycyphal.application",
    "pycyphal.application._node",
    "pycyphal.application._node_factory",
    "pycyphal.application._register_server",
    "pycyphal.application._registry_factory",
    "pycyphal.application._transport_factory",
    "pycyphal.application._port_list_publisher",
    "pycyphal.application.diagnostic",
    "pycyphal.application.heartbeat_publisher",
    "pycyphal.application.node_tracker",
    "pycyphal.application.plug_and_play",
    "pycyphal.application.register",
    "pycyphal.application.register._registry",
    "pycyphal.application.register._value",
    "pycyphal.application.register.backend",
    "pycyphal.application.register.backend.dynamic",
    "pycyphal.application.register.backend.static",
    "pycyphal.dsdl",
    "pycyphal.dsdl._compiler",
    "pycyphal.dsdl._import_hook",
    "pycyphal.dsdl._support_wrappers",
    "pycyphal.presentation",
    "pycyphal.presentation._port",
    "pycyphal.presentation._port._client",
    "pycyphal.presentation._port._publisher",
    "pycyphal.presentation._port._server",
    "pycyphal.presentation._port._subscriber",
    "pycyphal.presentation._presentation",
    "pycyphal.transport",
    "pycyphal.transport.can",
    "pycyphal.transport.can._can",
    "pycyphal.transport.can._frame",
    "pycyphal.transport.can._identifier",
    "pycyphal.transport.can._session",
    "pycyphal.transport.can._session._base",
    "pycyphal.transport.can._session._input",
    "pycyphal.transport.can._session._output",
    "pycyphal.transport.can._tracer",
    "pycyphal.transport.can.media",
    "pycyphal.transport.can.media._filter",
    "pycyphal.transport.can.media._media",
    "pycyphal.transport.can.media.pythoncan",
    "pycyphal.transport.can.media.pythoncan._pythoncan",
    "pycyphal.transport.can.media.socketcan",
    "pycyphal.transport.can.media.socketcan._socketcan",
    "pycyphal.transport.commons",
    "pycyphal.transport.commons.crc",
]

# python-can interface backends loaded by name at runtime.
# socketcan (Linux) and pcan (Windows) are the primary targets.
python_can_hidden = [
    "can",
    "can.interfaces",
    "can.interfaces.socketcan",
    "can.interfaces.pcan",
    "can.interfaces.slcan",
    "can.interfaces.virtual",
    "can.interfaces.socketcand",
]

hidden_imports = (
    server_modules
    + pycyphal_hidden
    + python_can_hidden
    + [
        "aiohttp",
        "aiohttp.web",
        "numpy",
        "nunavut",
    ]
)

# Bundle pre-compiled DSDL types and DSDL source definitions as data.
datas = []
if COMPILED_DSDL.is_dir():
    datas.append((str(COMPILED_DSDL), "python_compiled_messages"))
if DSDL_SOURCES.is_dir():
    datas.append((str(DSDL_SOURCES), "dsdl_messages"))

a = Analysis(
    [str(SERVER_DIR / "main.py")],
    pathex=[str(SERVER_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(Path(SPECPATH) / "frozen_hook.py")],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "pandas",
        "PIL",
        "IPython",
        "notebook",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Single-file executable so Tauri can manage it as one sidecar binary.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="cynitor-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=True,
)
