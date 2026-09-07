#!/usr/bin/env bash
# Build script for Cynitor desktop distribution.
#
# Prerequisites (one-time):
#   sudo apt-get install -y \
#     libwebkit2gtk-4.0-dev libgtk-3-dev librsvg2-dev patchelf
#   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
#   pip install pyinstaller
#
# Usage:
#   cd cynitor/packaging
#   ./build.sh          # debug build (fast)
#   ./build.sh release  # optimized + .deb + .AppImage

set -euo pipefail
cd "$(dirname "$0")"

PACKAGING_DIR="$(pwd)"
TARGET_TRIPLE="$(rustc -vV | awk '/^host:/ {print $2}')"
MODE="${1:-debug}"

echo "=== Cynitor build (${MODE}) ==="
echo "Target: ${TARGET_TRIPLE}"
echo ""

# ── Step 1: Freeze the Python backend into a single-file executable ──
echo "── Step 1: PyInstaller (cynitor-server) ──"
python3 -m PyInstaller cynitor-server.spec --noconfirm
echo "Backend frozen: dist/cynitor-server ($(du -h dist/cynitor-server | cut -f1))"
echo ""

# ── Step 2: Stage sidecar for Tauri ──
echo "── Step 2: Stage sidecar ──"
SIDECAR_DIR="${PACKAGING_DIR}/tauri/sidecar"
mkdir -p "${SIDECAR_DIR}"
SIDECAR_BIN="${SIDECAR_DIR}/cynitor-server-${TARGET_TRIPLE}"
cp "${PACKAGING_DIR}/dist/cynitor-server" "${SIDECAR_BIN}"
chmod +x "${SIDECAR_BIN}"
echo "Sidecar staged: ${SIDECAR_BIN}"
echo ""

# ── Step 3: Build Tauri app ──
echo "── Step 3: Tauri build ──"
cd "${PACKAGING_DIR}/tauri"

if [ "${MODE}" = "release" ]; then
    cargo tauri build
    echo ""
    echo "=== Build complete ==="
    echo "Installers:"
    find target/release/bundle -type f \( -name "*.deb" -o -name "*.AppImage" \) 2>/dev/null
else
    cargo build
    echo ""
    echo "=== Debug build complete ==="
    echo "Binary: target/debug/cynitor"
    echo "Run:    cd packaging/tauri && cargo run"
fi
