#!/usr/bin/env bash
# Build the Cynitor server as a single self-contained executable.
#
# The result serves both the REST/WebSocket API and the dashboard, so a
# deployment is this one file and a browser. Nothing is needed on client
# machines.
#
# Prerequisites (one-time):
#   pip install -r ../server/requirements.txt pyinstaller
#   python3 ../server/startup_setup.py --recompile   # generates compiled DSDL
#
# Usage:
#   cd cynitor/packaging
#   ./build.sh

set -euo pipefail
cd "$(dirname "$0")"

echo "=== Building cynitor-server ==="
python3 -m PyInstaller cynitor-server.spec --noconfirm

BIN="dist/cynitor-server"
echo ""
echo "=== Build complete ==="
echo "Binary: $(pwd)/${BIN}  ($(du -h "${BIN}" | cut -f1))"
echo ""
echo "Run it:"
echo "  CYNITOR_AUTH_TOKEN=\$(openssl rand -hex 24) ${BIN} --bind 0.0.0.0"
echo "Then open http://<this-machine>:8080 in a browser."
