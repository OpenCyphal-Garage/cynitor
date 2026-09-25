# PyInstaller spec for the Cynitor server.
#
# Build:
#   cd cynitor/packaging
#   python3 -m PyInstaller cynitor-server.spec --noconfirm
#
# Output: dist/cynitor-server  (single-file executable; cynitor-server.exe on Windows)

import sys
from pathlib import Path

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
    "can_config",
    "can_discovery",
    "can_hub",
    "data_dir",
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

# python-can interface backends loaded by name at runtime: socketcan on Linux;
# elsewhere the ones the CAN hub opens and adapter discovery probes
# (can_hub.open_adapter, can_discovery). virtual carries the hub's internal
# channel on every OS.
python_can_hidden = [
    "can",
    "can.interfaces",
    "can.interfaces.socketcan",
    "can.interfaces.pcan",
    "can.interfaces.slcan",
    "can.interfaces.virtual",
    "can.interfaces.socketcand",
    "can.interfaces.kvaser",
    "can.interfaces.vector",
    "can.interfaces.ixxat",
    "can.interfaces.gs_usb",
    "can.interfaces.udp_multicast",
]

# Windows-only requirements (server/requirements.txt): candleLight adapters go
# through pyusb, whose libusb DLL comes from libusb-package -- that package
# ships its own PyInstaller hook, which bundles the DLL -- and slcan
# discovery lists serial ports.
if sys.platform == "win32":
    python_can_hidden += [
        "gs_usb",
        "gs_usb.gs_usb",
        "usb",
        "usb.core",
        "usb.util",
        "usb.backend.libusb1",
        "libusb_package",
        "serial",
        "serial.tools.list_ports",
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
#
# The compiled types are mandatory. nnvg is not bundled, so the frozen binary
# cannot regenerate them: an installer built without this directory starts
# and then fails to import any type. Fail here, loudly, on every build path.
if not COMPILED_DSDL.is_dir():
    raise SystemExit(
        f"{COMPILED_DSDL} is missing. Run `python3 server/startup_setup.py --recompile` first."
    )
datas = [
    (str(COMPILED_DSDL), "python_compiled_messages"),
    (str(DSDL_SOURCES), "dsdl_messages"),
    # The dashboard itself, so one binary serves both the API and the UI.
    (str(PROJECT_ROOT / "website"), "website"),
]

# pydsdl vendors parsimonious under pydsdl/third_party and reaches it by
# prepending that directory to sys.path at import time (pydsdl/__init__.py).
# The directory is not a package, so PyInstaller's static analysis cannot
# follow the import and neither can collect_submodules. Ship the tree as data
# at the same relative location, which is where pydsdl's own sys.path entry
# will look inside the bundle.
#
# Without this the binary starts fine and only fails when something first
# imports pycyphal, i.e. the moment a user connects to a CAN interface:
#   ModuleNotFoundError: No module named 'parsimonious'
import pydsdl as _pydsdl

PYDSDL_THIRD_PARTY = Path(_pydsdl.__file__).resolve().parent / "third_party"
if not PYDSDL_THIRD_PARTY.is_dir():
    raise SystemExit(
        f"{PYDSDL_THIRD_PARTY} is missing. pydsdl has changed how it vendors its "
        "parser dependency; update this spec rather than shipping a binary that "
        "cannot connect to a bus."
    )
datas.append((str(PYDSDL_THIRD_PARTY), "pydsdl/third_party"))

# Custom DSDL types are compiled in-process (dsdl_manager, through pycyphal and
# nunavut), since nnvg is not bundled. nunavut reads its code templates and
# language settings from files under nunavut/lang and loads the language
# modules by name, neither of which PyInstaller's import analysis can see.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas += collect_data_files("nunavut")
hidden_imports += collect_submodules("nunavut")
# pydsdl, which parses the sources, reads its grammar from
# pydsdl/grammar.parsimonious. Only needed since types are compiled at run time.
datas.append((str(Path(_pydsdl.__file__).resolve().parent / "grammar.parsimonious"), "pydsdl"))

a = Analysis(
    [str(SERVER_DIR / "main.py")],
    pathex=[str(SERVER_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
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
    noarchive=False,
)

pyz = PYZ(a.pure)

# Single-file executable: a deployment is this one file plus a browser.
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
