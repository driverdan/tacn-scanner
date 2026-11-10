#!/bin/bash
# Run the scanner: trunk-recorder (Docker) records the whitelisted talkgroups, tr_transcriber.py transcribes them.
#   Recordings: logs/trunk-recorder/media/tacn/YYYY/M/D/<tgid>-<time>_<freq>-call_N.{wav,json}
#   Log:        logs/trunk-recorder/trunk-recorder.log
#   Transcripts: printed here, and appended to logs/trunk-recorder/transcripts.{txt,jsonl} (tr_transcriber.py)
# Usage: ./trunk-recorder.sh [-- tr_transcriber options]   e.g. -- --backfill
# Settings come from config/scanner.conf (env vars of the same name override):
#   SDR_*   first dongle  → low half of the site,  854.3875-856.2375 (incl. primary CC 855.2125)
#   SDR2_*  second dongle → high half of the site, 856.7625-858.4375
#   Leave SDR2_PORT and SDR2_ARGS empty to run on the first dongle alone (partial coverage).
#   Window centers/rates live in config/trunk-recorder.json. TR_IMAGE overrides the Docker image.
#   TRANSCRIBE=0 records without Whisper; WHISPER_MODEL picks the model.
# The terminal shows only transcripts, warnings and errors; the full output is in the two log files.
# rtl_tcp takes only one client per dongle: stop anything else using the SDRs first.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$ROOT/config/trunk-recorder.json"
RUN_DIR="$ROOT/logs/trunk-recorder"
IMAGE="${TR_IMAGE:-robotastic/trunk-recorder:5.2.1}"
[[ "${1:-}" == "--" ]] && shift

SETTINGS=(SDR_HOST SDR_PORT SDR_ARGS SDR_GAINS SDR_PPM SDR2_HOST SDR2_PORT SDR2_ARGS SDR2_GAINS SDR2_PPM
          TRANSCRIBE WHISPER_MODEL)
declare -A ENV_OVERRIDES=()
for v in "${SETTINGS[@]}"; do [[ -v $v ]] && ENV_OVERRIDES[$v]="${!v}"; done
SCANNER_CONF="${SCANNER_CONF:-$ROOT/config/scanner.conf}"
# shellcheck source=config/scanner.conf
[[ -f "$SCANNER_CONF" ]] && source "$SCANNER_CONF"
for v in "${!ENV_OVERRIDES[@]}"; do printf -v "$v" '%s' "${ENV_OVERRIDES[$v]}"; done
: "${SDR_PORT:=1234}" "${SDR_GAINS:=LNA:40}" "${SDR_PPM:=0.0}" "${TRANSCRIBE:=1}" "${WHISPER_MODEL:=large-v3}"
: "${SDR2_HOST:=${SDR_HOST:-}}" "${SDR2_PORT:=}" "${SDR2_GAINS:=$SDR_GAINS}" "${SDR2_PPM:=$SDR_PPM}"
if [[ -z "${SDR_ARGS:-}" ]]; then
    [[ -n "${SDR_HOST:-}" ]] || { echo "SDR_HOST or SDR_ARGS must be set in $SCANNER_CONF" >&2; exit 1; }
    SDR_ARGS="rtl_tcp=${SDR_HOST}:${SDR_PORT}"
fi
if [[ -z "${SDR2_ARGS:-}" && -n "$SDR2_PORT" ]]; then
    [[ -n "$SDR2_HOST" ]] || { echo "SDR2_HOST must be set when SDR2_PORT is" >&2; exit 1; }
    SDR2_ARGS="rtl_tcp=${SDR2_HOST}:${SDR2_PORT}"
fi
for a in "$SDR_ARGS" "${SDR2_ARGS:-}"; do
    if [[ "$a" == rtl=* ]]; then
        echo "Local dongles need USB passed into the container (--device /dev/bus/usb); not set up here." >&2
        exit 1
    fi
done

mkdir -p "$RUN_DIR/media"
# Render config.json (SDR settings per source) and talkgroups.csv (whitelist + tags). Every talkgroup
# gets trunk-recorder's default priority 1: record whenever a recorder is free on its source.
python3 - "$TEMPLATE" "$RUN_DIR" "$ROOT/config/whitelist.tsv" "$ROOT/config/talkgroups.tsv" \
    "$SDR_ARGS" "$SDR_GAINS" "$SDR_PPM" "${SDR2_ARGS:-}" "$SDR2_GAINS" "$SDR2_PPM" <<'PY'
import csv, json, re, sys
src, run_dir, wl_path, tags_path = sys.argv[1:5]
devs = [d for d in (sys.argv[5:8], sys.argv[8:11]) if d[0]]
cfg = json.load(open(src))
cfg["sources"] = cfg["sources"][:len(devs)]
# Snap to the nearest R820T gain step (as rtl_tcp reports them) so the requested gain is exact.
# trunk-recorder then logs "Requested Gain of 40.2 not supported, driver using: 0". That's cosmetic:
# gr-osmosdr's rtl_tcp get_gain() always returns 0. The gain is sent after manual mode and applies.
R820T_GAINS = [0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28,
               29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48, 49.6]
for s, (args, gains, ppm) in zip(cfg["sources"], devs):
    gain = re.search(r"(-?\d+(?:\.\d+)?)", gains)  # osmosdr "LNA:40" -> 40
    gain = float(gain.group(1)) if gain else 40
    s.update(device=args, ppm=float(ppm), gain=min(R820T_GAINS, key=lambda g: abs(g - gain)))
json.dump(cfg, open(f"{run_dir}/config.json", "w"), indent=4)

tags = {}
for row in csv.reader(open(tags_path), delimiter="\t"):
    if row and row[0].strip().isdigit():
        tags[int(row[0])] = row[1] if len(row) > 1 else ""
wl = [int(l.split()[0]) for l in open(wl_path) if l.strip() and l.split()[0].isdigit()]
with open(f"{run_dir}/talkgroups.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Decimal", "Mode", "Alpha Tag", "Description", "Tag", "Category"])
    for tg in wl:
        name = tags.get(tg) or str(tg)
        w.writerow([tg, "D", name, name, "Public Safety", "Chattanooga"])

# Coverage check against the Chattanooga Simulcast site's 16 channels (RadioReference sid 6355).
site = [854.3875, 854.9125, 855.1875, 855.2125, 855.9875, 856.0875, 856.2375, 856.7625,
        856.9625, 857.2375, 857.4375, 857.4875, 857.7125, 857.9375, 858.2375, 858.4375]
covered = set()
for s in cfg["sources"]:
    half = s["rate"] / 2e6 * 0.85  # stay off the RTL's filter roll-off at the edges
    mine = [f for f in site if abs(f - s["center"] / 1e6) <= half]
    covered.update(mine)
    print(f"SDR {s['device']}  gain={s['gain']}  ppm={s['ppm']}  center {s['center']/1e6:.4f} MHz "
          f"@ {s['rate']/1e6} MS/s → {len(mine)} site channels")
missing = [f for f in site if f not in covered]
print(f"Coverage: {len(covered)}/{len(site)} site channels"
      + (f"; not covered: {', '.join(f'{f:.4f}' for f in missing)}" if missing else "")
      + f"  talkgroups={len(wl)}")
PY

if [[ "$TRANSCRIBE" != "0" ]]; then
    # Watches media/ for finished calls. Its status lines ("HH:MM:SS msg") reach the terminal only if
    # they're warnings or errors; anything else on stderr (e.g. a traceback) is shown as is.
    WHISPER_MODEL="$WHISPER_MODEL" "$ROOT/venv/bin/python" -u "$ROOT/tr_transcriber.py" "$@" \
        2> >(tee -a "$RUN_DIR/transcriber.log" | mawk -W interactive \
            '/^[0-9][0-9]:[0-9][0-9]:[0-9][0-9] / && !/warning|error/ {next} {print}' >&2) &
    TX_PID=$!
    trap 'kill $TX_PID 2>/dev/null; wait $TX_PID 2>/dev/null' EXIT
fi

echo "trunk-recorder starting ($IMAGE) — recordings in $RUN_DIR/media"
docker run --rm --init --name trunk-recorder \
    --user "$(id -u):$(id -g)" \
    -v "$RUN_DIR:/app" -w /app \
    "$IMAGE" trunk-recorder --config=/app/config.json 2>&1 | tee -a "$RUN_DIR/trunk-recorder.log" |
    # Log lines look like "[2026-11-08 22:01:33.001] (info)   [tacn] ...": show warning and above.
    # Other output (Docker, GNU Radio startup) is shown only if it looks like a problem.
    mawk -W interactive '
        /^\[[^]]*\] \((warning|error|critical|fatal)\)/ {print; next}
        /^\[[^]]*\] \(/ {next}
        tolower($0) ~ /error|fail|warn|denied|cannot|unable/ {print}' 
