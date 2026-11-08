#!/bin/bash
# Run OP25 scanner for Chattanooga PD (TACN P25, Chattanooga Simulcast site) with live Whisper transcription.
#   Status/web UI: http://127.0.0.1:8080    Audio: default PulseAudio/PipeWire sink
#   Transcripts:   printed here, and appended to logs/transcripts.txt + logs/transcripts.jsonl
# Usage: ./scanner.sh [config.json] [-- transcriber options]
# Settings: config/scanner.conf (SDR host/port/gain/ppm, Whisper model, ...).
#           Any setting can be overridden with an env var of the same name, e.g. TRANSCRIBE=0 ./scanner.sh
#           SCANNER_CONF=path selects a different settings file.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
CFG="$ROOT/config/chattanooga-cpd.json"
if [[ $# -gt 0 && "$1" != "--" ]]; then CFG="$(realpath "$1")"; shift; fi
[[ "${1:-}" == "--" ]] && shift

# Load settings; env vars that are already set take precedence over the file.
SETTINGS=(SDR_HOST SDR_PORT SDR_ARGS SDR_GAINS SDR_PPM TRANSCRIBE WHISPER_MODEL OP25_VERBOSITY)
declare -A ENV_OVERRIDES=()
for v in "${SETTINGS[@]}"; do [[ -v $v ]] && ENV_OVERRIDES[$v]="${!v}"; done
SCANNER_CONF="${SCANNER_CONF:-$ROOT/config/scanner.conf}"
# shellcheck source=config/scanner.conf
[[ -f "$SCANNER_CONF" ]] && source "$SCANNER_CONF"
for v in "${!ENV_OVERRIDES[@]}"; do printf -v "$v" '%s' "${ENV_OVERRIDES[$v]}"; done
: "${SDR_PORT:=1234}" "${SDR_GAINS:=LNA:40}" "${SDR_PPM:=0.0}" "${TRANSCRIBE:=1}" "${WHISPER_MODEL:=large-v3}" "${OP25_VERBOSITY:=1}"
if [[ -z "${SDR_ARGS:-}" ]]; then
    [[ -n "${SDR_HOST:-}" ]] || { echo "SDR_HOST or SDR_ARGS must be set in $SCANNER_CONF" >&2; exit 1; }
    SDR_ARGS="rtl_tcp=${SDR_HOST}:${SDR_PORT}"
fi
export WHISPER_MODEL

PYDIR="$(find "$ROOT/local/lib" -maxdepth 3 -type d \( -name site-packages -o -name dist-packages \) | head -1)"
export PYTHONPATH="${PYDIR}${PYTHONPATH:+:$PYTHONPATH}"
export LD_LIBRARY_PATH="$ROOT/local/lib:$ROOT/local/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
mkdir -p "$ROOT/logs"

# Render the OP25 config with the SDR settings applied to its first device.
RUN_CFG="$ROOT/logs/op25-config.json"
"$ROOT/venv/bin/python" - "$CFG" "$RUN_CFG" "$SDR_ARGS" "$SDR_GAINS" "$SDR_PPM" <<'PY'
import json, sys
src, dst, args, gains, ppm = sys.argv[1:]
cfg = json.load(open(src))
cfg["devices"][0].update(args=args, gains=gains, ppm=float(ppm))
json.dump(cfg, open(dst, "w"), indent=4)
PY
echo "SDR: $SDR_ARGS  gains=$SDR_GAINS  ppm=$SDR_PPM"

TX_ARGS=("$@")
[[ "$TRANSCRIBE" == "0" ]] && TX_ARGS+=(--no-whisper)
"$ROOT/venv/bin/python" -u "$ROOT/transcriber.py" "${TX_ARGS[@]}" 2> >(tee -a "$ROOT/logs/transcriber.log" >&2) &
TX_PID=$!
trap 'kill $TX_PID 2>/dev/null; wait $TX_PID 2>/dev/null' EXIT

cd "$ROOT/op25/op25/gr-op25_repeater/apps"
echo "OP25 starting — open http://127.0.0.1:8080 (log: $ROOT/logs/op25.log)"
"$ROOT/venv/bin/python" ./multi_rx.py -v "$OP25_VERBOSITY" -c "$RUN_CFG" 2> "$ROOT/logs/op25.log"
