#!/usr/bin/env bash
# Build the Cynitor server: one self-contained executable, plus a .deb and an
# .AppImage wrapping it.
#
# The server serves both the REST/WebSocket API and the dashboard, so a
# deployment is this one file and a browser. Nothing runs on client machines,
# and none of these artifacts open a window.
#
# Prerequisites (one-time):
#   pip install -r ../server/requirements.txt pyinstaller
#   python3 ../server/startup_setup.py --recompile   # generates compiled DSDL
#
# Usage:
#   cd cynitor/packaging
#   ./build.sh            # binary + .deb + .AppImage
#   ./build.sh binary     # just the executable

set -euo pipefail
cd "$(dirname "$0")"

TARGET="${1:-all}"
VERSION="$(grep -m1 '^__version__' ../server/version.py | cut -d'"' -f2)"
BIN="dist/cynitor-server"

echo "=== Cynitor ${VERSION} ==="

# ── Executable ──
echo "── PyInstaller ──"
python3 -m PyInstaller cynitor-server.spec --noconfirm
echo "Binary: $(pwd)/${BIN}  ($(du -h "${BIN}" | cut -f1))"

if [ "${TARGET}" = "binary" ]; then
    echo ""
    echo "Run it:  CYNITOR_AUTH_TOKEN=\$(openssl rand -hex 24) ${BIN} --bind 0.0.0.0"
    exit 0
fi

rm -rf out && mkdir -p out

# ── .deb ──
echo ""
echo "── Debian package ──"
ROOT="build/deb/cynitor-server_${VERSION}_amd64"
rm -rf "${ROOT}"
mkdir -p "${ROOT}/DEBIAN" "${ROOT}/usr/bin" \
         "${ROOT}/lib/systemd/system" "${ROOT}/etc/default" \
         "${ROOT}/usr/share/doc/cynitor-server"
sed "s/@VERSION@/${VERSION}/" deb/control.in > "${ROOT}/DEBIAN/control"
install -m 0755 "${BIN}"                  "${ROOT}/usr/bin/cynitor-server"
install -m 0644 deb/cynitor-server.service "${ROOT}/lib/systemd/system/cynitor-server.service"
install -m 0644 deb/default                "${ROOT}/etc/default/cynitor-server"
install -m 0644 ../README.md               "${ROOT}/usr/share/doc/cynitor-server/README.md"
# The env file carries the auth token, so keep it out of world-readable view.
chmod 0640 "${ROOT}/etc/default/cynitor-server"
echo "/etc/default/cynitor-server" > "${ROOT}/DEBIAN/conffiles"
fakeroot dpkg-deb --build "${ROOT}" >/dev/null
mv "${ROOT}.deb" "out/cynitor-server_${VERSION}_amd64.deb"
echo "Package: out/cynitor-server_${VERSION}_amd64.deb"

# ── .AppImage ──
echo ""
echo "── AppImage ──"
TOOL="build/appimagetool"
if [ ! -x "${TOOL}" ]; then
    echo "Fetching appimagetool..."
    mkdir -p build
    curl -fsSL -o "${TOOL}" \
      https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "${TOOL}"
fi
APPDIR="build/Cynitor.AppDir"
rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr/bin"
install -m 0755 "${BIN}"                        "${APPDIR}/usr/bin/cynitor-server"
install -m 0755 appimage/AppRun                 "${APPDIR}/AppRun"
install -m 0644 appimage/cynitor-server.desktop "${APPDIR}/cynitor-server.desktop"
install -m 0644 appimage/cynitor.png            "${APPDIR}/cynitor.png"
cp "${APPDIR}/cynitor.png" "${APPDIR}/.DirIcon"
# Extraction rather than FUSE: CI runners and many containers have no fuse.
ARCH=x86_64 "${TOOL}" --appimage-extract-and-run "${APPDIR}" \
    "out/cynitor-server-${VERSION}-x86_64.AppImage" >/dev/null 2>&1
chmod +x "out/cynitor-server-${VERSION}-x86_64.AppImage"
echo "AppImage: out/cynitor-server-${VERSION}-x86_64.AppImage"

echo ""
echo "=== Build complete ==="
ls -lh out/ | tail -n +2 | awk '{print "  ", $5, $9}'
