#!/usr/bin/env python3
"""Render trunk-recorder's config.json and talkgroups.csv into its run directory.

compose.yaml runs this as the one-shot `trunk-recorder-config` service before trunk-recorder
starts. The SDR settings come from the environment (config/scanner.conf via env_file):
  SDR_*   first dongle  -> low half of the site,  854.3875-856.2375 (incl. primary CC 855.2125)
  SDR2_*  second dongle -> high half of the site, 856.7625-858.4375
Leave SDR2_PORT and SDR2_ARGS empty to run on the first dongle alone (partial coverage).
Each dongle is either remote (rtl_tcp, from SDR_HOST/SDR_PORT) or plugged into this machine
(SDR_ARGS=rtl=<index or serial>, which needs compose.usb.yaml for the USB passthrough).
Window centers and rates live in config/trunk-recorder.json.

An SDRplay RSP (SDR_ARGS=soapy=0,driver=sdrplay, which also needs compose.sdrplay.yaml) is wide
enough for the whole site, so it replaces both dongles with one source: see sdrplay_source().
Standard library only.
"""
import csv
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.environ.get("SCANNER_CONFIG_DIR") or os.path.join(ROOT, "config")
RUN_DIR = os.environ.get("SCANNER_DATA_DIR") or os.path.join(ROOT, "logs", "trunk-recorder")

# Snap to the nearest R820T gain step (as rtl_tcp reports them) so the requested gain is exact.
# trunk-recorder then logs "Requested Gain of 40.2 not supported, driver using: 0". That's cosmetic:
# gr-osmosdr's rtl_tcp get_gain() always returns 0. The gain is sent after manual mode and applies.
R820T_GAINS = [0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28,
               29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48, 49.6]

# osmosdr device strings for hardware attached to this machine (as opposed to rtl_tcp=host:port).
LOCAL_DEVICE = re.compile(r"(rtl|airspy|airspyhf|hackrf|bladerf|miri|soapy)(=|,|$)")
# An SDRplay RSP, reached through SoapySDR. trunk-recorder also looks for "sdrplay" in the string.
SDRPLAY = re.compile(r"(^|,)driver=sdrplay(,|$)")
# The RSP's sample rate. It must be a multiple of 24 kHz for trunk-recorder and at least 6 MS/s:
# SoapySDRPlay3 picks the IF filter from the rate, and below 6 MS/s that is 5 MHz or narrower,
# which leaves little or nothing beyond the site's 4.05 MHz.
SDRPLAY_RATE = 6000000
# SoapySDRPlay3's gain stages. Both are gain *reductions*: IFGR in dB, RFGR as an LNA state.
SDRPLAY_GAINS = {"IFGR": (20, 59), "RFGR": (0, 9)}

# The Chattanooga Simulcast site's 16 channels (RadioReference sid 6355), for the coverage check.
SITE = [854.3875, 854.9125, 855.1875, 855.2125, 855.9875, 856.0875, 856.2375, 856.7625,
        856.9625, 857.2375, 857.4375, 857.4875, 857.7125, 857.9375, 858.2375, 858.4375]


def setting(name, default=""):
    return (os.environ.get(name) or "").strip() or default


def devices():
    """[(osmosdr device string, gain string, ppm)] for the configured dongles."""
    host, port = setting("SDR_HOST"), setting("SDR_PORT", "1234")
    gains, ppm = setting("SDR_GAINS", "LNA:40"), setting("SDR_PPM", "0.0")
    args = setting("SDR_ARGS")
    if not args:
        if not host:
            sys.exit("SDR_HOST or SDR_ARGS must be set in config/scanner.conf")
        args = f"rtl_tcp={host}:{port}"
    devs = [(args, gains, ppm)]

    host2, port2, args2 = setting("SDR2_HOST", host), setting("SDR2_PORT"), setting("SDR2_ARGS")
    if not args2 and port2:
        if not host2:
            sys.exit("SDR2_HOST must be set when SDR2_PORT is")
        args2 = f"rtl_tcp={host2}:{port2}"
    if args2:
        devs.append((args2, setting("SDR2_GAINS", gains), setting("SDR2_PPM", ppm)))

    # compose.usb.yaml sets SDR_USB. Without its passthrough trunk-recorder would only find no device.
    usb = [a for a, _, _ in devs if LOCAL_DEVICE.match(a)]
    if any(SDRPLAY.search(a) for a in usb):
        if not SDRPLAY.search(args):
            sys.exit(f"{args2}: an SDRplay RSP must be the first SDR (SDR_ARGS). It covers the whole site alone")
        if len(devs) > 1:
            sys.exit(f"{args}: an SDRplay RSP covers the whole site alone. "
                     "Leave SDR2_PORT and SDR2_ARGS empty in config/scanner.conf")
        if not setting("SDR_SDRPLAY"):
            sys.exit(f"{args}: an SDRplay RSP needs compose.usb.yaml and compose.sdrplay.yaml. Run\n"
                     "  docker compose -f compose.yaml -f compose.usb.yaml -f compose.sdrplay.yaml up -d --build\n"
                     "or put COMPOSE_FILE=compose.yaml:compose.usb.yaml:compose.sdrplay.yaml in .env")
    if usb and not setting("SDR_USB"):
        sys.exit(f"{', '.join(usb)}: a USB SDR needs compose.usb.yaml for the USB passthrough. Run\n"
                 "  docker compose -f compose.yaml -f compose.usb.yaml up -d\n"
                 "or put COMPOSE_FILE=compose.yaml:compose.usb.yaml in .env")
    if len(usb) == 2 and usb[0] == usb[1]:
        sys.exit(f"Both SDRs are set to {usb[0]}: give each its own index or serial")
    return devs


def sdrplay_source(source, gains):
    """Turn the first source into one window over the whole site, for an SDRplay RSP."""
    stages = {}
    for part in filter(None, (p.strip() for p in gains.split(","))):
        name, _, value = part.partition(":")
        name = name.strip().upper()
        if name not in SDRPLAY_GAINS or not re.fullmatch(r"\d+", value.strip()):
            sys.exit(f"SDR_GAINS={gains}: an SDRplay RSP takes IFGR and RFGR, e.g. SDR_GAINS=RFGR:3,IFGR:40")
        low, high = SDRPLAY_GAINS[name]
        if not low <= int(value) <= high:
            sys.exit(f"SDR_GAINS={gains}: {name} must be {low}-{high}")
        stages[name] = int(value)
    if not stages:
        sys.exit("SDR_GAINS must be set for an SDRplay RSP, e.g. SDR_GAINS=RFGR:3,IFGR:40")
    # Centered on the site. No channel is within 100 kHz of the center, where a zero-IF receiver
    # has its DC spike.
    source.update(center=round((SITE[0] + SITE[-1]) / 2 * 1e6), rate=SDRPLAY_RATE,
                  digitalRecorders=len(SITE), gainSettings=stages)
    del source["gain"]  # there is no overall gain: trunk-recorder sets the stages by name


def write_atomic(path, write):
    """Write via a temp file, so trunk-recorder never reads a half-written config."""
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        write(f)
    os.replace(tmp, path)


def main():
    devs = devices()
    with open(os.path.join(CONFIG_DIR, "trunk-recorder.json")) as f:
        cfg = json.load(f)
    if len(devs) > len(cfg["sources"]):
        sys.exit(f"{len(devs)} SDRs configured but trunk-recorder.json has {len(cfg['sources'])} sources")
    cfg["sources"] = cfg["sources"][:len(devs)]
    for s, (args, gains, ppm) in zip(cfg["sources"], devs):
        gain = re.search(r"(-?\d+(?:\.\d+)?)", gains)  # osmosdr "LNA:40" -> 40
        gain = float(gain.group(1)) if gain else 40
        s.update(device=args, ppm=float(ppm), gain=min(R820T_GAINS, key=lambda g: abs(g - gain)))
        if SDRPLAY.search(args):
            sdrplay_source(s, gains)

    # Whitelist + display names. No Priority column: every talkgroup gets trunk-recorder's default
    # priority 1, i.e. it is recorded whenever a recorder is free on its source.
    tags = {}
    with open(os.path.join(CONFIG_DIR, "talkgroups.tsv")) as f:
        for row in csv.reader(f, delimiter="\t"):
            if row and row[0].strip().isdigit():
                tags[int(row[0])] = row[1] if len(row) > 1 else ""
    with open(os.path.join(CONFIG_DIR, "whitelist.tsv")) as f:
        wl = [int(line.split()[0]) for line in f if line.strip() and line.split()[0].isdigit()]
    if not wl:
        sys.exit("config/whitelist.tsv lists no talkgroups")

    def talkgroups(f):
        w = csv.writer(f)
        w.writerow(["Decimal", "Mode", "Alpha Tag", "Description", "Tag", "Category"])
        for tg in wl:
            name = tags.get(tg) or str(tg)
            w.writerow([tg, "D", name, name, "Public Safety", "Chattanooga"])

    os.makedirs(os.path.join(RUN_DIR, "media"), exist_ok=True)
    write_atomic(os.path.join(RUN_DIR, "config.json"), lambda f: json.dump(cfg, f, indent=4))
    write_atomic(os.path.join(RUN_DIR, "talkgroups.csv"), talkgroups)

    covered = set()
    for s in cfg["sources"]:
        half = s["rate"] / 2e6 * 0.85  # stay off the filter roll-off at the edges
        mine = [f for f in SITE if abs(f - s["center"] / 1e6) <= half]
        covered.update(mine)
        gain = s.get("gain", s.get("gainSettings"))
        print(f"SDR {s['device']}  gain={gain}  ppm={s['ppm']}  center {s['center'] / 1e6:.4f} MHz "
              f"@ {s['rate'] / 1e6} MS/s -> {len(mine)} site channels")
    missing = [f for f in SITE if f not in covered]
    print(f"Coverage: {len(covered)}/{len(SITE)} site channels"
          + (f"; not covered: {', '.join(f'{f:.4f}' for f in missing)}" if missing else "")
          + f"  talkgroups={len(wl)}")


if __name__ == "__main__":
    main()
