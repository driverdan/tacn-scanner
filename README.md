# Chattanooga TACN Radio Scanner

A local radio scanner for Chattanooga Police Department and other Hamilton County public-safety traffic. It records calls from the Tennessee Advanced Communications Network (TACN), a P25 trunked radio system, with [trunk-recorder](https://github.com/TrunkRecorder/trunk-recorder), and transcribes each call with Whisper as soon as it ends.

Nothing plays live. Calls are recorded and transcribed, and the transcripts appear in the terminal and in log files.

## How it works

```
2 × RTL-SDR (R820T) ── rtl_tcp 192.168.8.132:1234 and :1235
        │  IQ samples over TCP
        ▼
trunk-recorder  (Docker, robotastic/trunk-recorder:5.2.1)
  • follows the control channel, records every whitelisted call inside its two SDR windows
  • one WAV + one JSON per call → logs/trunk-recorder/media/
        │  files
        ▼
tr_transcriber.py  (Python venv)
  • watches media/ for finished calls
  • faster-whisper large-v3 (CPU, int8), one pass per call, split back per transmission
  • → terminal + logs/trunk-recorder/transcripts.{txt,jsonl}
```

Each call is transcribed in one pass with word timestamps, then split back into its transmissions using the radio IDs in trunk-recorder's call JSON, so every transcript line says which radio spoke it.

## Requirements

- **Two RTL-SDR dongles** served over the network with `rtl_tcp` (one per port). The site's channels span about 4.05 MHz, more than one RTL-SDR can see, so it takes two. One dongle works but covers only 7 of the 16 channels.
- **Docker**, for trunk-recorder. Your user must be in the `docker` group. A shell opened before you were added needs `sg docker -c './trunk-recorder.sh'`.
- **Python 3** with a venv in `venv/` containing `faster-whisper`, `numpy` and `scipy`.
- **A reasonably fast CPU.** Whisper runs on the CPU and takes about 3–4 s per call with large-v3, however short the call is.
- For `testset.py label` only: an audio player (`paplay`, `pw-play`, `aplay` or `ffplay`).

## Setup

```bash
python3 -m venv --system-site-packages venv
./venv/bin/pip install faster-whisper numpy scipy 'brotli>=1.2'
```

`brotli>=1.2` matters on systems whose packaged brotli is 1.1: the old version crashes Hugging Face model downloads with `process() takes no keyword arguments`. The Whisper model downloads on first run.

Then set your SDRs' addresses in `config/scanner.conf` (see [Configuration](#configuration)).

## Running

```bash
./trunk-recorder.sh                     # record both SDRs and transcribe live
SDR2_PORT= ./trunk-recorder.sh          # first dongle only (7 of 16 site channels)
SDR_HOST=10.0.0.5 ./trunk-recorder.sh   # any scanner.conf setting can be overridden from the environment
TRANSCRIBE=0 ./trunk-recorder.sh        # record only
WHISPER_MODEL=distil-large-v3 ./trunk-recorder.sh
./trunk-recorder.sh -- --backfill       # also transcribe earlier calls that aren't transcribed yet
./venv/bin/python tr_transcriber.py --once   # offline: transcribe every untranscribed call, then exit
```

Stop it with Ctrl-C or `docker stop trunk-recorder`. Arguments after `--` go to `tr_transcriber.py`; `./venv/bin/python tr_transcriber.py --help` lists them.

At launch the script prints which site channels each SDR window covers. After that the terminal shows only transcripts, warnings and errors. Each line is colored by talkgroup (on a terminal only; `--color always|never` or `NO_COLOR=1` overrides). The talkgroups share a 24-color palette in `whitelist.tsv` order, so the 25th talkgroup onward repeats a color.

The transcriber remembers which calls it has finished, so restarting never transcribes a call twice. New calls go ahead of any backfill.

### Output

Everything goes under `logs/trunk-recorder/`:

| File | What |
|---|---|
| `transcripts.txt` | One plain-text line per transmission |
| `transcripts.jsonl` | The same as JSON, with talkgroup, radio ID, timing and transcription time (`proc_s`) |
| `media/` | Each call's WAV and JSON from trunk-recorder |
| `trunk-recorder.log` | trunk-recorder's full log |
| `transcriber.log` | The transcriber's status lines. `backlog` warnings mean Whisper can't keep up |

Encrypted calls are not recorded or transcribed.

## Configuration

| File | What |
|---|---|
| `config/scanner.conf` | Your settings, as bash `KEY=value`: the SDRs (`SDR_HOST`/`SDR_PORT` or a full `SDR_ARGS`, `SDR_GAINS`, `SDR_PPM`, and the `SDR2_*` equivalents), `TRANSCRIBE` and `WHISPER_MODEL`. An environment variable of the same name overrides the file. |
| `config/whitelist.tsv` | The talkgroups to record, one ID per line. Read at startup, so restart after editing. |
| `config/talkgroups.tsv` | `tgid<TAB>name`. Display names for talkgroups; may list more than the whitelist. |
| `config/trunk-recorder.json` | trunk-recorder's config template: the SDR window centers, sample rates and recorder counts. Don't set device, gain or ppm here; `trunk-recorder.sh` fills those in from `scanner.conf`. |
| `config/prompts.json` | Optional per-talkgroup Whisper prompts (`{"default": "...", "groups": [{"name": ..., "tgids": [...], "prompt": "..."}]}`), used with `--prompts`. Not present by default: per-agency prompts tested worse than the built-in one. |

**To record more or fewer talkgroups**, edit `config/whitelist.tsv`, name any new ones in `config/talkgroups.tsv`, and restart.

**SDR windows.** Port 1234 (`SDR_*`) is centered on 855.3125 MHz and covers 854.3875–856.2375, including the primary control channel. Port 1235 (`SDR2_*`) is centered on 857.6 MHz and covers 856.7625–858.4375. trunk-recorder records only channels inside these windows. If you move the centers, keep each source's `digitalRecorders` equal to the number of site channels in its window (currently 7 and 9).

**Local USB dongles** (`rtl=0`) aren't supported as set up: the container has no USB passthrough. Use `rtl_tcp`.

### Tuning reception

- trunk-recorder logs `Requested Gain of 40.2 not supported, driver using: 0`. It's harmless: gr-osmosdr's rtl_tcp driver always reports 0, but the gain is applied.
- Uncorrected, both dongles read low by about 3.3 and 3.7 kHz (−3.9 and −4.3 ppm), so `SDR_PPM` and `SDR2_PPM` are −4. rtl_tcp takes whole ppm only, and trunk-recorder's `autoTune` corrects the remainder. If it starts warning that the AutoTune offset exceeds 3.5 ppm, take the `AutoTune:` values from `trunk-recorder.log` and add Hz ÷ center MHz to the ppm. The sign matters: the wrong sign doubles the error and the control channel barely decodes.
- Each call JSON's `freq_error` and `freqList[].error_count` show tuning and decode quality.
- Every talkgroup gets the default priority 1, so it can always be recorded as long as a recorder is free.

## Measuring transcription accuracy

`testset.py` builds a hand-labelled test set and measures Whisper's word error rate (WER) on it:

```bash
./venv/bin/python testset.py label --add 50    # sample 50 transmissions, play each and type the correct text
./venv/bin/python testset.py score --drafts    # WER of the live transcripts (no Whisper run)
./venv/bin/python testset.py score             # re-transcribe the labelled clips with current settings
./venv/bin/python testset.py score --set vad_filter=False --name no-vad   # try a faster-whisper option
./venv/bin/python testset.py compare           # every saved run side by side, against the current labels
```

Labels and the sampled calls live in `testset/`, so the set survives clearing `logs/`. Scoring ignores case, punctuation and number formatting ("10-8" = "ten eight").

Results so far (2026-11-08, 187 clips, large-v3):

- Current settings: **10.7% WER**. Most errors are in transmissions under 2 s (25%), against 12% at 2–5 s and 6% above.
- Transcribing whole calls beats one transmission at a time: 10.7% vs 17.0%.
- The built-in prompt of local vocabulary is worth 6 points (17.1% without it). Per-agency prompts, adding recent lines as context, and changing the VAD settings all made it worse.

## Radio system

- **System:** TACN, P25 Phase II capable. WACN `BEE00`, SysID `2A5`. [RadioReference](https://www.radioreference.com/db/sid/6355) sid 6355.
- **Site:** Chattanooga Simulcast, RFSS 2, site 10 ("Site 2.10"), NAC `2A0`.
- **Control channels:** 855.2125 primary; alternates 856.2375, 856.7625 and 857.2375.
- **Site channels (16):** 854.3875, 854.9125, 855.1875, 855.2125c, 855.9875, 856.0875, 856.2375c, 856.7625c, 856.9625, 857.2375c, 857.4375, 857.4875, 857.7125, 857.9375, 858.2375, 858.4375 ("c" = control channel; the alternates likely carry voice when not active).
- **CPD voice:** Phase I FDMA, in the clear.

### Recorded talkgroups

Talkgroup IDs come from RadioReference and have been seen live on the site.

| Agency | Talkgroups |
|---|---|
| Chattanooga PD | 1027, 1029, 1051 Adam, Baker and Charlie dispatch · 1031 Info · 1035 Car-to-Car · 1041 Special Ops · 1043 City Net 2 · 1049 City Net 1 · 1053 Detectives · 1055 SWAT 1 · 1569, 1571, 1573 Tac 1–3 |
| Hamilton County Sheriff | 1223, 1225 Dispatch East and West · 1491 Info · 1323 Talk · 1227 CID · 1229 Narcotics · 2061 Narcotics/Special Ops · 1233, 1493 SWAT 1 and 2 · 1235 Fugitive · 1237 Civil · 1281 Park Rangers · 1395 Court Security · 1325 Jail Ops |
| CHA Airport | 1137 Police Dispatch · 1141 Police Tac · 1139 Fire Dispatch · 1143 Fire Tac |
| Fire and EMS | 1061 Chattanooga Fire Dispatch · 1241 Hamilton County Fire Dispatch Main · 1165 Hamilton County EMS Main |
| Other police | 1191 East Ridge PD Main · 1367 Soddy-Daisy PD Main · 1401 UTC Police Dispatch |
| Erlanger Life Force (medevac) | 1389 Dispatch · 1391 Med Net |

County "HC Law" (1125) and Sheriff SRO (1127) are encrypted.
