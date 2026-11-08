#!/bin/bash
# Build OP25 (boatbod) into ./local — no sudo required.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT/op25"
echo "$ROOT/venv/bin/python" > op25/gr-op25_repeater/apps/op25_python
mkdir -p build && cd build
cmake .. -DCMAKE_INSTALL_PREFIX="$ROOT/local" -DPython3_EXECUTABLE=/usr/bin/python3 2>&1 | tee cmake.log
make -j"$(nproc)" 2>&1 | tee make.log
make install 2>&1 | tee install.log
