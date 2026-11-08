# Chattanooga PD Scanner

A local radio scanner. It decodes Chattanooga Police Department traffic on the Tennessee Advanced Communications Network (TACN), a P25 trunked radio system. It plays the audio and transcribes each transmission in real time with Whisper.

## How it fits together

```
RTL-SDR (R820T) ── rtl_tcp 192.168.8.132:1234
        │  IQ samples over TCP
        ▼
OP25 multi_rx.py  (op25/…/apps, built into ./local)
  • follows the control channel, retunes the single dongle to each voice grant
  • decodes P25 Phase I/II voice, whitelists CPD talkgroups
  • HTTP status/UI + JSON API on 127.0.0.1:8080
        │  UDP PCM → 127.0.0.1:23470 (+23471 for TDMA slot B)
        ▼
transcriber.py  (venv)
  • relays every packet unchanged → 127.0.0.1:23456 (OP25 sockaudio → PulseAudio speakers)
  • segments audio into transmissions, tags them with tgid/RID polled from :8080
  • faster-whisper large-v3 (CPU, int8) → stdout + logs/transcripts.{txt,jsonl}
```

`scanner.sh` starts both processes. The transcriber runs in the background and is killed when OP25 exits.

## Layout

| Path | What |
|---|---|
| `scanner.sh` | Main launcher. Loads `config/scanner.conf`, renders the OP25 config to `logs/op25-config.json`, sets PYTHONPATH/LD_LIBRARY_PATH for `./local`, starts the transcriber and OP25. |
| `transcriber.py` | Audio relay, segmenter, Whisper transcription. `--help` lists the options. |
| `build-op25.sh` | Rebuilds OP25 from `op25/` into `./local`, without sudo. |
| `config/scanner.conf` | User settings (bash `KEY=value`): SDR host/port (or full `SDR_ARGS`), gains, ppm, `TRANSCRIBE`, `WHISPER_MODEL`, `OP25_VERBOSITY`. An env var of the same name overrides the file. |
| `config/chattanooga-cpd.json` | OP25 multi_rx config template: trunking system, audio and terminal settings. `scanner.sh` overwrites the first device's `args`/`gains`/`ppm` from `scanner.conf`. |
| `config/talkgroups.tsv` | `tgid<TAB>tag<TAB>priority`. A lower number is a higher priority and preempts other talkgroups. |
| `config/whitelist-cpd.tsv` | The talkgroups OP25 will follow (CPD only). Reloaded dynamically. |
| `op25/` | Upstream [boatbod/op25](https://github.com/boatbod/op25), cloned with depth 1. Treat it as vendored and avoid editing it. |
| `local/` | Install prefix for OP25 (`lib/python3.12/site-packages`, `lib/x86_64-linux-gnu`). Build output; can be regenerated. |
| `venv/` | Python venv created with `--system-site-packages`, so it can see the apt-installed GNU Radio. |
| `logs/` | `op25.log`, `transcriber.log`, `transcripts.txt`, `transcripts.jsonl`, and `audio/` (WAVs, only with `--save-audio`). |

## Running

```bash
./scanner.sh                          # scanner + live transcription
./scanner.sh -- --save-audio          # also keep a WAV per transmission
SDR_HOST=10.0.0.5 ./scanner.sh        # any scanner.conf setting can be overridden from the env
TRANSCRIBE=0 ./scanner.sh             # audio only (transcriber runs as a relay without Whisper)
WHISPER_MODEL=distil-large-v3 ./scanner.sh
OP25_VERBOSITY=10 ./scanner.sh        # detailed OP25 log (grants, "voice update: tg(...)")
```

The audio path always goes through `transcriber.py`, because OP25 sends to port 23470, not straight to the player. If the transcriber isn't running, you hear nothing.

You can query live status without the UI:
```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '[{"command":"update","arg1":0,"arg2":0}]' http://127.0.0.1:8080/
```
The response contains a `trunk_update` message (site, control channel, active frequencies) and a `channel_update` message (current tgid, tag, srcaddr).

## Radio system facts

- **System:** TACN, P25 Phase II capable. WACN `BEE00`, SysID `2A5`. RadioReference sid 6355.
- **Site:** Chattanooga Simulcast, RFSS 2, site 10 (shown as "Site 2.10"), NAC `2A0`.
- **Control channels:** 855.2125 primary, with alternates 856.2375, 856.7625 and 857.2375.
- **Voice channels:** span about 854.39–858.44 MHz. That's wider than one RTL's bandwidth (about 2.4 MHz), which is why OP25 runs in single-tuner retune mode at 1 MS/s. trunk-recorder and SDRTrunk would need wideband coverage of the whole span.
- **CPD voice:** observed as Phase I FDMA in the clear.
- **CPD talkgroups:**
  - 1027, 1029, 1051: Adam, Baker and Charlie dispatch
  - 1031: Info
  - 1035: Car-to-Car
  - 1041, 1043, 1049, 1053, 1055: Special Ops, City Net 2, City Net 1, Detectives, SWAT 1
  - 1569, 1571, 1573: Tac 1–3
- **Encryption:** encrypted calls are skipped (`crypt_behavior: 2`). County "HC Law" (1125) and Sheriff SRO (1127) are listed as encrypted.

Talkgroup data comes from RadioReference, category "TN-Hamilton Co-Chattanooga" (tgCat 14617). Confirm IDs against live traffic (the `/8080` API or `OP25_VERBOSITY=10`) before relying on them.

## Environment constraints and gotchas

- **No sudo during agent sessions.** The system packages (`gnuradio`, `gnuradio-dev`, `gr-osmosdr`, `librtlsdr-dev`, `libspdlog-dev`, `cmake`, and so on) were installed by the user. OP25 is installed to `./local` instead of `/usr/local`, so don't run upstream `install.sh`: it edits the apt sources, blacklists the DVB driver and runs `sudo make install`.
- **Python packages go in `venv/`, never apt.** This is the user's preference. Use `./venv/bin/pip`.
- **brotli:** the venv pins `brotli>=1.2`, which shadows the system brotli 1.1. The old version crashes Hugging Face model downloads (`process() takes no keyword arguments`). Don't remove it.
- **OP25 Python:** `op25/op25/gr-op25_repeater/apps/op25_python` points at the venv Python.
- **No GPU acceleration.** The GPU is an AMD integrated one, so Whisper runs on the CPU (24 cores; the transcriber uses half by default). Whisper pads every clip to 30 s, so each call costs about 3–4 s with large-v3 no matter how long it is. Smaller models aren't much faster. large-v3-turbo was observed to fall into repetition loops.
- **The SDR is remote and shared.** Only one client can use rtl_tcp at a time, so stop the scanner before running any other test against the SDR.
- **Segmenting:** OP25 sends a 2-byte DRAIN flag at the end of each transmission. The transcriber treats it as a soft boundary (0.5 s grace period), with a 1.2 s gap fallback, because OP25 emits many short grant/release cycles mid-conversation.

## Making changes

- **Change what you hear:** edit `config/whitelist-cpd.tsv`, or set `"whitelist": ""` in the JSON to hear everything. Give new talkgroups names in `config/talkgroups.tsv`.
- **Change the SDR / tune reception:** in `config/scanner.conf`, set `SDR_HOST`/`SDR_PORT` (or `SDR_ARGS`, e.g. `rtl=0` for a local dongle), `SDR_GAINS` (currently `LNA:40`) and `SDR_PPM`. Don't edit these in the JSON; they get overwritten at launch. Check `logs/op25.log` for `control channel timeout` or `p25_framer ... error check failed`.
- **Tune transcription:** edit `DEFAULT_PROMPT` (local vocabulary), `HALLUCINATIONS` and `clean_text()` in `transcriber.py`. To compare models, record with `--save-audio` and run them offline on the WAVs in `logs/audio/`.
- **Rebuild OP25** after pulling upstream: `./build-op25.sh`.
- **Testing:** there are no automated tests. Verify with a timed live run, e.g. `timeout 120 ./scanner.sh`, then check `logs/transcripts.jsonl` (the `proc_s` field shows transcription time) and `logs/transcriber.log` (`backlog` warnings mean Whisper can't keep up).
- Not a git repo yet.
