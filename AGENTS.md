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
| `transcriber.py` | Audio relay, segmenter, Whisper transcription. Holds a talkgroup's transmissions until it has been quiet for `--group-gap` seconds (3 s, with nothing being received) and transcribes them together, then splits the text back per transmission by word timestamps, the way `tr_transcriber.py` handles a call. Lines print after the pause; `--group-gap 0` transcribes each transmission alone. Also holds the shared Whisper helpers (`load_model()`, `transcribe()`, `clean_text()`, `split_words()`, `Prompts`). `--help` lists the options. |
| `trunk-recorder.sh` | Runs [trunk-recorder](https://github.com/TrunkRecorder/trunk-recorder) in Docker (`robotastic/trunk-recorder:5.2.1`) for comparison with OP25. Uses both SDRs from `scanner.conf` (`SDR_*` and `SDR2_*`) and the same whitelist as OP25. Writes `config.json` and `talkgroups.csv` into `logs/trunk-recorder/` (mounted as `/app`) and records to `logs/trunk-recorder/media/`. Unless `TRANSCRIBE=0`, it also runs `tr_transcriber.py` on the host; arguments after `--` go to it. |
| `tr_transcriber.py` | Whisper for trunk-recorder. Watches `logs/trunk-recorder/media/` for finished calls (the JSON lands first, and the WAV is transcribed once it has settled). Transcribes each call once with word timestamps, then splits the text back into transmissions using the call JSON's `freqList`/`srcList`, so every line has its RID. Writes the same record format as `transcriber.py`, plus `call` and `error_count`, to `logs/trunk-recorder/transcripts.{txt,jsonl}`. It uses the Whisper helpers in `transcriber.py`. |
| `testset.py` | Hand-labelled WER test set. `label --add N` samples N random transmissions from trunk-recorder's transcripts, copies their calls into `testset/calls/`, then plays each clip and asks for the correct text (prefilled with the live transcript; `--blank` starts empty). `score` re-transcribes the labelled clips and prints WER with breakdowns; `--drafts` scores the saved live transcripts instead, `--mode clip` transcribes each transmission alone like `transcriber.py` (default `call` matches `tr_transcriber.py`), and `--set key=value` overrides any faster-whisper option. `compare` re-scores every saved run in `testset/results/` against the current labels, side by side, so fixing a label doesn't mean re-running Whisper. `score --prompts FILE` and `--context N` test per-talkgroup prompts and recent-line context. |
| `testset/` | `clips.jsonl` (sampled transmissions), `labels.jsonl` (labels, newest line per clip wins), `calls/` (call WAV+JSON copies), `results/` (per-clip output of each score run). Not gitignored. |
| `build-op25.sh` | Rebuilds OP25 from `op25/` into `./local`, without sudo. |
| `config/scanner.conf` | User settings (bash `KEY=value`): SDR host/port (or full `SDR_ARGS`), gains, ppm, the second SDR (`SDR2_*`, trunk-recorder only), `TRANSCRIBE`, `WHISPER_MODEL`, `OP25_VERBOSITY`. An env var of the same name overrides the file. |
| `config/chattanooga-cpd.json` | OP25 multi_rx config template: trunking system, audio and terminal settings. `scanner.sh` overwrites the first device's `args`/`gains`/`ppm` from `scanner.conf`. |
| `config/trunk-recorder.json` | trunk-recorder config template. Two sources (low and high half of the site). `trunk-recorder.sh` overwrites each source's `device`/`gain`/`ppm` from `scanner.conf`. |
| `config/talkgroups.tsv` | `tgid<TAB>tag<TAB>priority`. A lower number is a higher priority and preempts other talkgroups. |
| `config/prompts.json` (optional, doesn't exist) | Per-talkgroup Whisper prompts: `{"default": "...", "groups": [{"name": ..., "tgids": [...], "prompt": "..."}]}`. Both transcribers read it via `--prompts` (re-read when it changes) and use `DEFAULT_PROMPT` when it's missing. Per-agency prompts tested worse, see below. |
| `config/whitelist.tsv` | The talkgroups OP25 and trunk-recorder follow: CPD, HCSO, the CHA airport, and the main city/county fire, county EMS, East Ridge, Soddy-Daisy and UTC police, and Erlanger Life Force channels. OP25 reloads it while running; trunk-recorder reads it only at startup. |
| `op25/` | Upstream [boatbod/op25](https://github.com/boatbod/op25), its own git clone (depth 1) and gitignored here. Treat it as vendored and avoid editing it; changes wouldn't be tracked. To recreate: `git clone --depth 1 https://github.com/boatbod/op25.git op25 && ./build-op25.sh`. |
| `local/` | Install prefix for OP25 (`lib/python3.12/site-packages`, `lib/x86_64-linux-gnu`). Build output; can be regenerated. |
| `venv/` | Python venv created with `--system-site-packages`, so it can see the apt-installed GNU Radio. |
| `logs/` | `op25.log`, `transcriber.log`, `transcripts.txt`, `transcripts.jsonl`, and `audio/` (WAVs, only with `--save-audio`). `trunk-recorder/` holds trunk-recorder's own run: its log, `media/`, `transcriber.log` and `transcripts.{txt,jsonl}`. |

## Running

```bash
./scanner.sh                          # scanner + live transcription
./scanner.sh -- --save-audio          # also keep a WAV per transmission
SDR_HOST=10.0.0.5 ./scanner.sh        # any scanner.conf setting can be overridden from the env
TRANSCRIBE=0 ./scanner.sh             # audio only (transcriber runs as a relay without Whisper)
WHISPER_MODEL=distil-large-v3 ./scanner.sh
OP25_VERBOSITY=10 ./scanner.sh        # detailed OP25 log (grants, "voice update: tg(...)")
```

```bash
./trunk-recorder.sh                 # trunk-recorder in Docker on both dongles + live transcription (stop scanner.sh first)
SDR2_PORT= ./trunk-recorder.sh      # first dongle only (7 of 16 site channels)
TRANSCRIBE=0 ./trunk-recorder.sh    # record only
./trunk-recorder.sh -- --backfill   # also transcribe calls recorded earlier that aren't transcribed yet
./venv/bin/python tr_transcriber.py --once   # offline: transcribe every untranscribed call, then exit
```

In the terminal, `trunk-recorder.sh` shows only transcripts, warnings and errors. trunk-recorder's info lines and the transcriber's status lines go only to `logs/trunk-recorder/trunk-recorder.log` and `transcriber.log`.

`tr_transcriber.py` keeps track of finished calls through the `call` field in `transcripts.jsonl`, so restarting it never transcribes a call twice. New calls go ahead of the backfill. It needs `compressWav: false`: it reads the WAVs directly, and faster-whisper's `decode_audio` (PyAV) is broken in this venv. Splitting a call into transmissions relies on Whisper word timestamps, so a word at a key-up boundary sometimes lands on the neighbouring RID. Back-to-back transmissions from the same radio are merged.

trunk-recorder can't retune the way OP25 does. It records only channels inside its fixed SDR windows, so covering the site's 16 channels (854.3875–858.4375, about 4.05 MHz) takes two RTL-SDRs. Both are on `192.168.8.132`: port 1234 (`SDR_*`) is centered on 855.3125 MHz and covers 854.3875–856.2375, including the primary CC. Port 1235 (`SDR2_*`, used only by trunk-recorder) is centered on 857.6 MHz and covers 856.7625–858.4375. Centers live in `config/trunk-recorder.json`; at launch the script prints which site channels each window covers. The user is in the `docker` group. A shell started before that was set up needs `sg docker -c './trunk-recorder.sh'`. Stop it with `docker stop trunk-recorder`.

trunk-recorder gotchas:
- It logs `Requested Gain of 40.2 not supported, driver using: 0`. Ignore it: gr-osmosdr's rtl_tcp `get_gain()` always returns 0, and the 40.2 dB gain is applied (verified on the wire).
- Both dongles read low by about 2–3.4 kHz (roughly −3 to −4 ppm). OP25 corrects this itself; trunk-recorder uses `autoTune: true` and may warn that the offset exceeds 3.5 ppm. Per-call `freq_error` and `freqList[].error_count` in the call JSON show tuning and decode quality.

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
- **Site channels (16, per RadioReference):** 854.3875, 854.9125, 855.1875, 855.2125c, 855.9875, 856.0875, 856.2375c, 856.7625c, 856.9625, 857.2375c, 857.4375, 857.4875, 857.7125, 857.9375, 858.2375, 858.4375 ("c" marks a control channel; the alternates likely carry voice when not active). CPD calls have been seen on 9 of them so far: all of the voice channels except 855.1875, 856.9625 and 858.4375.
- **Voice channels:** span about 854.39–858.44 MHz. That's wider than one RTL's bandwidth (about 2.4 MHz), which is why OP25 runs in single-tuner retune mode at 1 MS/s. trunk-recorder and SDRTrunk would need wideband coverage of the whole span.
- **CPD voice:** observed as Phase I FDMA in the clear.
- **CPD talkgroups:**
  - 1027, 1029, 1051: Adam, Baker and Charlie dispatch
  - 1031: Info
  - 1035: Car-to-Car
  - 1041, 1043, 1049, 1053, 1055: Special Ops, City Net 2, City Net 1, Detectives, SWAT 1
  - 1569, 1571, 1573: Tac 1–3
- **HCSO (Hamilton County Sheriff) talkgroups**, from RadioReference category "TN-Hamilton Co-Public Safety" (tgCat 14594). They're whitelisted at a lower priority than CPD (4 for dispatch, 5 for the rest), so CPD preempts them in OP25:
  - 1223, 1225: Dispatch East and West
  - 1491, 1323: Info and Talk
  - 1227, 1229, 2061: CID, Narcotics, and Narcotics/Special Ops
  - 1233, 1493: SWAT 1 and 2
  - 1235, 1237, 1281, 1395, 1325: Fugitive, Civil, Park Rangers, Court Security, Jail Ops
- **Other whitelisted talkgroups** (RadioReference categories 14617, 14594, 36448, 36449, 36675, 21393), all seen live on the site. Dispatch channels are priority 4, Life Force is 5:
  - 1061, 1241: Chattanooga Fire Dispatch, Hamilton County Fire Dispatch Main
  - 1165: Hamilton County EMS Main
  - 1191, 1367, 1401: East Ridge PD Main, Soddy-Daisy PD Main, UTC Police Dispatch
  - 1389, 1391: Erlanger Life Force Dispatch and Med Net (medevac helicopter)
- **Encryption:** encrypted calls are skipped (`crypt_behavior: 2`). County "HC Law" (1125) and Sheriff SRO (1127) are listed as encrypted.

Talkgroup data comes from RadioReference, category "TN-Hamilton Co-Chattanooga" (tgCat 14617). Confirm IDs against live traffic (the `/8080` API or `OP25_VERBOSITY=10`) before relying on them.

## Environment constraints and gotchas

- **No sudo during agent sessions.** The system packages (`gnuradio`, `gnuradio-dev`, `gr-osmosdr`, `librtlsdr-dev`, `libspdlog-dev`, `cmake`, and so on) were installed by the user. OP25 is installed to `./local` instead of `/usr/local`, so don't run upstream `install.sh`: it edits the apt sources, blacklists the DVB driver and runs `sudo make install`.
- **Python packages go in `venv/`, never apt.** This is the user's preference. Use `./venv/bin/pip`.
- **brotli:** the venv pins `brotli>=1.2`, which shadows the system brotli 1.1. The old version crashes Hugging Face model downloads (`process() takes no keyword arguments`). Don't remove it.
- **OP25 Python:** `op25/op25/gr-op25_repeater/apps/op25_python` points at the venv Python.
- **No GPU acceleration.** The GPU is an AMD integrated one, so Whisper runs on the CPU (24 cores; the transcriber uses half by default). Whisper pads every clip to 30 s, so each call costs about 3–4 s with large-v3 no matter how long it is. Smaller models aren't much faster. large-v3-turbo was observed to fall into repetition loops.
- **The SDRs are remote and shared.** Two R820T dongles run rtl_tcp on `192.168.8.132`, ports 1234 and 1235. Each port accepts only one client at a time, so stop the scanner before running any other test against the SDRs.
- **Segmenting:** OP25 sends a 2-byte DRAIN flag at the end of each transmission. The transcriber treats it as a soft boundary (0.5 s grace period), with a 1.2 s gap fallback, because OP25 emits many short grant/release cycles mid-conversation. `transcriber.py` then groups transmissions into conversations (see its row in the layout table).

## Making changes

- **Change what you hear:** edit `config/whitelist.tsv`, or set `"whitelist": ""` in the JSON to hear everything. Give new talkgroups names in `config/talkgroups.tsv`.
- **Change the SDR / tune reception:** in `config/scanner.conf`, set `SDR_HOST`/`SDR_PORT` (or `SDR_ARGS`, e.g. `rtl=0` for a local dongle), `SDR_GAINS` (currently `LNA:40`) and `SDR_PPM`. Don't edit these in the JSON; they get overwritten at launch. Check `logs/op25.log` for `control channel timeout` or `p25_framer ... error check failed`.
- **Tune transcription:** edit `DEFAULT_PROMPT` (local vocabulary), `HALLUCINATIONS` and `clean_text()` in `transcriber.py`. Measure every change against the test set: `./venv/bin/python testset.py score --drafts` for the live baseline, then e.g. `testset.py score --set vad_filter=False --name no-vad`. Scoring normalizes case, punctuation and number formats ("10-8" = "ten eight"), and `transcribe()` passes extra keyword arguments straight to faster-whisper.
- **What the test set showed** (2026-11-08, 187 clips, large-v3, call mode unless noted; `testset.py compare` reproduces it):
  - Current settings: 10.7% WER. Most errors are in transmissions under 2 s (25%; 12% at 2–5 s, 6% above).
  - Transcribing whole calls/conversations beats one transmission at a time: 10.7% vs 17.0%, and 25% vs 42% on clips under 2 s. That's why `transcriber.py` groups.
  - `DEFAULT_PROMPT` is worth 6 points (no prompt: 17.1%). Removing its place-name list: 13.9%. Example-sentence prompts per agency: 13.3%. The same list-style prompt adapted per agency: 11.1% (HCSO worse, the other groups too small to tell).
  - Adding the last lines heard on the talkgroup to the prompt (`--context`) hurts: 13.5% (2 lines, call mode), 18.0% (3 lines, clip mode). It stays off.
  - VAD off: 13.4%. VAD threshold 0.35: 12.1%. `no_speech_threshold=None`: identical output. Keep the current VAD settings.
- **Rebuild OP25** after pulling upstream: `./build-op25.sh`.
- **Testing:** there are no automated tests. Verify with a timed live run, e.g. `timeout 120 ./scanner.sh`, then check `logs/transcripts.jsonl` (the `proc_s` field shows transcription time) and `logs/transcriber.log` (`backlog` warnings mean Whisper can't keep up).
- **Git:** the repo tracks only the project's own files (`AGENTS.md`, the scripts, `transcriber.py`, `config/`). `.gitignore` excludes `op25/`, `local/`, `venv/`, `logs/` and `__pycache__/`; don't commit them.
