# Chattanooga TACN Radio Scanner

A local radio scanner for Chattanooga Police Department and other Hamilton County public-safety traffic. It records calls from the Tennessee Advanced Communications Network (TACN), a P25 trunked radio system, with [trunk-recorder](https://github.com/TrunkRecorder/trunk-recorder), and transcribes each call with Whisper as soon as it ends.

Nothing plays live. Calls are recorded and transcribed, and the transcripts appear in the transcriber's log and in files.

## How it works

```
2 × RTL-SDR (R820T) ── rtl_tcp 192.168.8.132:1234 and :1235, or plugged in over USB
(or 1 × SDRplay RSP1B over USB)
        │  IQ samples over TCP or USB
        ▼
trunk-recorder  (container, robotastic/trunk-recorder:5.2.1)
  • follows the control channel, records every whitelisted call inside its two SDR windows
  • one WAV + one JSON per call → logs/trunk-recorder/media/
        │  files
        ▼
transcriber  (container, built from the Dockerfile: tr_transcriber.py)
  • watches media/ for finished calls
  • faster-whisper large-v3 (CPU, int8), one pass per call, split back per transmission
  • → its log + logs/trunk-recorder/transcripts.{txt,jsonl}
```

Docker Compose (`compose.yaml`) runs both containers. They share nothing but the `logs/trunk-recorder/` directory, so either can be stopped, restarted or rebuilt without touching the other.

Each call is transcribed in one pass with word timestamps, then split back into its transmissions using the radio IDs in trunk-recorder's call JSON, so every transcript line says which radio spoke it.

## Requirements

- **Two RTL-SDR dongles**, each either served over the network with `rtl_tcp` (one per port) or plugged into this machine (see [USB dongles](#usb-dongles)). The site's channels span about 4.05 MHz, more than one RTL-SDR can see, so it takes two. One dongle works but covers only 7 of the 16 channels. A single **SDRplay RSP1B** on USB covers the whole site instead (see [SDRplay RSP1B](#sdrplay-rsp1b)).
- **Docker with Compose v2** (`docker compose version` must work; the old `docker-compose` 1.x doesn't work with current Docker). Your user must be in the `docker` group.
- **A reasonably fast CPU.** Whisper runs on the CPU and takes about 3–4 s per call with large-v3, however short the call is.
- For `testset.py` only: Python 3 with a venv (see [Measuring transcription accuracy](#measuring-transcription-accuracy)), and for `testset.py label` an audio player (`paplay`, `pw-play`, `aplay` or `ffplay`).

## Setup

1. Set your SDRs' addresses in `config/scanner.conf` (see [Configuration](#configuration)). For dongles plugged into this machine, follow [USB dongles](#usb-dongles) or [SDRplay RSP1B](#sdrplay-rsp1b).
2. Create the directories the containers write to, so that they belong to you and not to root: `mkdir -p logs/trunk-recorder ~/.cache/huggingface`
3. If your user ID isn't 1000 (`id -u`), put `SCANNER_UID=…` and `SCANNER_GID=…` in a `.env` file next to `compose.yaml`. The containers run as that user.

The Whisper model (about 3 GB) downloads on the transcriber's first start, into `~/.cache/huggingface`.

## Running

```bash
docker compose up -d --build            # start everything (and rebuild the transcriber after a code change)
docker compose logs -f transcriber      # follow the transcripts
docker compose ps                       # state and health of the containers
docker compose down                     # stop everything

docker compose up -d trunk-recorder     # record only
docker compose stop transcriber         # stop transcribing, keep recording
docker compose restart trunk-recorder   # after editing a talkgroup list or the SDR settings
docker compose logs trunk-recorder-config   # which site channels each SDR window covers
docker compose run --rm transcriber --once  # transcribe every untranscribed call, then exit
```

Both containers restart on their own after a crash or a reboot, until you stop them. Stopping the transcriber lets the call in progress finish first.

The transcriber's log shows the transcripts, plus its warnings and errors. Each line is colored by talkgroup. The talkgroups share a 24-color palette in `whitelist.tsv` order, so the 25th talkgroup onward repeats a color. `tail -f logs/trunk-recorder/transcripts.txt` shows the same lines without color.

The transcriber remembers which calls it has finished, so restarting never transcribes a call twice. When it starts it first catches up on calls recorded while it was down, oldest first; new calls go ahead of that backlog. To skip the catch-up, remove `--backfill` from the transcriber's `command` in `compose.yaml`. `docker compose run --rm transcriber --help` lists its other options.

### Output

Everything goes under `logs/trunk-recorder/`:

| File | What |
|---|---|
| `transcripts.txt` | One plain-text line per transmission |
| `transcripts.jsonl` | The same as JSON, with talkgroup, radio ID, timing and transcription time (`proc_s`). `wav` is the recording's path relative to this directory |
| `media/` | Each call's WAV and JSON from trunk-recorder |

The containers' own output is in Docker's logs (`docker compose logs trunk-recorder` or `transcriber`), capped at 100 MB per container. `backlog` warnings from the transcriber mean Whisper can't keep up.

Nothing deletes old recordings or transcripts: `media/` and the transcript files grow until you clear them.

Encrypted calls are not recorded or transcribed.

## Configuration

| File | What |
|---|---|
| `config/scanner.conf` | Your settings, as plain `KEY=value` lines: the SDRs (`SDR_HOST`/`SDR_PORT` or a full `SDR_ARGS`, `SDR_GAINS`, `SDR_PPM`, and the `SDR2_*` equivalents) and `WHISPER_MODEL`. Leave `SDR2_PORT` empty to run on the first dongle alone. Apply a change with `docker compose up -d`. |
| `config/whitelist.tsv` | The talkgroups to record, one ID per line. Read at startup, so run `docker compose restart trunk-recorder` after editing. |
| `config/talkgroups.tsv` | `tgid<TAB>name`. Display names for talkgroups; may list more than the whitelist. |
| `config/trunk-recorder.json` | trunk-recorder's config template: the SDR window centers, sample rates and recorder counts. Don't set device, gain or ppm here; they are filled in from `scanner.conf`. |
| `config/prompts.json` | Optional per-talkgroup Whisper prompts (`{"default": "...", "groups": [{"name": ..., "tgids": [...], "prompt": "..."}]}`). Not present by default: per-agency prompts tested worse than the built-in one. |
| `compose.yaml` | The containers. `TR_IMAGE` (environment or `.env`) picks another trunk-recorder image. |
| `compose.usb.yaml` | Adds USB passthrough to trunk-recorder, for [USB dongles](#usb-dongles). |
| `compose.sdrplay.yaml` | Builds and runs trunk-recorder with SDRplay support (`Dockerfile.sdrplay`), for an [SDRplay RSP1B](#sdrplay-rsp1b). |

**To record more or fewer talkgroups**, edit `config/whitelist.tsv`, name any new ones in `config/talkgroups.tsv`, and run `docker compose restart trunk-recorder`.

**SDR windows.** With RTL-SDRs, the first SDR (`SDR_*`, port 1234) is centered on 855.3125 MHz and covers 854.3875–856.2375, including the primary control channel. The second (`SDR2_*`, port 1235) is centered on 857.6 MHz and covers 856.7625–858.4375. trunk-recorder records only channels inside these windows. If you move the centers, keep each source's `digitalRecorders` equal to the number of site channels in its window (currently 7 and 9).

### USB dongles

A dongle plugged into this machine is used directly, without `rtl_tcp`. The two SDRs are set separately, so one can be USB and the other remote.

1. Install the RTL-SDR udev rules on the host, so that the dongles belong to the `plugdev` group and the kernel's DVB-T driver leaves them alone. On Debian and Ubuntu: `sudo apt install rtl-sdr`, then replug the dongles. `ls -l /dev/bus/usb/*/` should show them as `root plugdev`.
2. In `config/scanner.conf`, set the device strings:

   ```
   SDR_ARGS=rtl=0
   SDR2_ARGS=rtl=1
   ```

   The number is the device index, in the order `rtl_test` lists the dongles. That order can change when they are replugged, which would swap the two windows. To pin them, use serial numbers (`SDR_ARGS=rtl=00000101`). Most dongles ship with the same serial, `00000001`; give each its own with `rtl_eeprom -d 0 -s 00000101` and replug it.
3. Add `compose.usb.yaml`, which passes the USB devices into the trunk-recorder container. Put this in `.env` next to `compose.yaml`:

   ```
   COMPOSE_FILE=compose.yaml:compose.usb.yaml
   ```

   Every `docker compose` command then includes it. Without `.env`, pass `-f compose.yaml -f compose.usb.yaml` each time.
4. `docker compose up -d`, then check `docker compose logs trunk-recorder`.

If trunk-recorder can't open a dongle:

- `usb_open error -3`: the container may not write to the device. Check step 1. If your host's group for SDRs isn't gid 46 (`getent group plugdev`), set `SDR_USB_GID=…` in `.env`.
- `usb_claim_interface error -6`: the kernel's DVB-T driver holds the dongle. `sudo rmmod dvb_usb_rtl28xxu`, and blacklist it to make that permanent (`echo blacklist dvb_usb_rtl28xxu | sudo tee /etc/modprobe.d/blacklist-rtl.conf`).
- Only one program can use a dongle at a time: stop `rtl_tcp`, `rtl_test` and other SDR software first.

Gain and ppm work as they do over `rtl_tcp`. This has been tested as far as the container reaching the host's USB devices, not with a dongle attached: the dongles here are remote.

### SDRplay RSP1B

One RSP1B plugged into this machine replaces both RTL-SDRs: it samples 6 MHz at once, centered on 856.4125 MHz, which takes in all 16 site channels. The other RSP models should work the same way but haven't been considered in detail.

The stock trunk-recorder image has no SDRplay driver, so `compose.sdrplay.yaml` builds one that does. The build downloads SDRplay's proprietary API (3.15.2) from sdrplay.com; by building the image you accept [SDRplay's license](https://www.sdrplay.com/api). Nothing from SDRplay has to be installed on the host. If the SDRplay API is installed there anyway, stop its service (`sudo systemctl disable --now sdrplay`), because only one service can hold the RSP.

1. Let your SDR group use the RSP. Without this, only root can open it:

   ```bash
   echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="1df7", MODE="0660", GROUP="plugdev"' | sudo tee /etc/udev/rules.d/66-sdrplay.rules
   sudo udevadm control --reload
   ```

   Then replug the RSP. `lsusb -d 1df7:` shows its bus and device number, and `ls -l /dev/bus/usb/BUS/DEVICE` should show `root plugdev`.
2. In `config/scanner.conf`:

   ```
   SDR_ARGS=soapy=0,driver=sdrplay
   SDR_GAINS=RFGR:3,IFGR:40
   SDR_PPM=0
   SDR2_PORT=
   SDR2_ARGS=
   ```

   With more than one RSP attached, pick one by serial number: `SDR_ARGS=soapy=0,driver=sdrplay,serial=…`. Further `key=value` pairs in the string are passed to the driver as settings, e.g. `,biasT_ctrl=true` for the bias tee.
3. Add both USB override files. Put this in `.env` next to `compose.yaml`:

   ```
   COMPOSE_FILE=compose.yaml:compose.usb.yaml:compose.sdrplay.yaml
   ```

4. `docker compose up -d --build`, then check `docker compose logs trunk-recorder-config` (it should report 16/16 site channels from one SDR) and `docker compose logs trunk-recorder`.

**Gain.** An RSP has two stages, and both numbers are gain *reductions*: a higher number means less gain. `RFGR` is the LNA state, 0 (most gain) to 9 (least). `IFGR` is the IF reduction in dB, 20 to 59. `RFGR:3,IFGR:40` is only a starting point. If the control channel decodes badly, try more gain (lower numbers) first; if calls get worse when the site is busy, the front end is overloading, so raise `RFGR`. The RSP's AGC stays off.

**Frequency correction.** The RSP1B's oscillator is good to 0.5 ppm, so start with `SDR_PPM=0` (the −4 in `scanner.conf` belongs to the RTL-SDRs). Fractions of a ppm are accepted.

If trunk-recorder logs `no available RSP devices found`, the RSP isn't plugged in, step 1 hasn't taken effect (replug it), or another program or SDRplay service has it. Restart trunk-recorder after replugging the RSP.

This has been tested as far as the image building and trunk-recorder loading the driver and asking SDRplay's API for a device, not with an RSP attached.

### Tuning reception

- trunk-recorder logs `Requested Gain of 40.2 not supported, driver using: 0`. It's harmless: gr-osmosdr's rtl_tcp driver always reports 0, but the gain is applied. (USB dongles report the real gain.)
- Uncorrected, both dongles read low by about 3.3 and 3.7 kHz (−3.9 and −4.3 ppm), so `SDR_PPM` and `SDR2_PPM` are −4. RTL-SDRs take whole ppm only, and trunk-recorder's `autoTune` corrects the remainder. If it starts warning that the AutoTune offset exceeds 3.5 ppm, take the `AutoTune:` values from `docker compose logs trunk-recorder` and add Hz ÷ center MHz to the ppm. The sign matters: the wrong sign doubles the error and the control channel barely decodes.
- Each call JSON's `freq_error` and `freqList[].error_count` show tuning and decode quality.
- Every talkgroup gets the default priority 1, so it can always be recorded as long as a recorder is free.

## Measuring transcription accuracy

`testset.py` builds a hand-labelled test set and measures Whisper's word error rate (WER) on it. It runs on the host from a venv with the same packages as the transcriber image. That includes Ubuntu 24.04's own numpy and scipy (`sudo apt install python3-numpy python3-scipy`), which the venv picks up through `--system-site-packages`; other builds of numpy give measurably different transcripts:

```bash
python3 -m venv --system-site-packages venv
./venv/bin/pip install -r requirements.txt 'brotli>=1.2'

./venv/bin/python testset.py label --add 50    # sample 50 transmissions, play each and type the correct text
./venv/bin/python testset.py score --drafts    # WER of the live transcripts (no Whisper run)
./venv/bin/python testset.py score             # re-transcribe the labelled clips with current settings
./venv/bin/python testset.py score --set vad_filter=False --name no-vad   # try a faster-whisper option
./venv/bin/python testset.py compare           # every saved run side by side, against the current labels
```

`brotli>=1.2` matters on systems whose packaged brotli is 1.1: the old version crashes Hugging Face model downloads with `process() takes no keyword arguments`.

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
