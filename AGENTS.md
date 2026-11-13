# Chattanooga PD Scanner

Read `README.md` first. It covers what the project does, the architecture, setup, running, configuration, output files, reception tuning, the test set and its results, and the radio system and talkgroups. This file holds what's only relevant when changing the code.

## Layout

| Path | What |
|---|---|
| `compose.yaml` | The three services. `trunk-recorder-config` (one-shot, stock `python:3.12-slim`, runs the bind-mounted `render_config.py`) must exit 0 before `trunk-recorder` starts. `transcriber` has no `depends_on`: it only shares the run directory. The run directory is mounted as `/app` in trunk-recorder and `/data` in the other two; `config/` is `/config` (read-only). `config/scanner.conf` is each service's `env_file`. |
| `compose.usb.yaml` | Optional override for USB SDRs, merged in with `-f` or `COMPOSE_FILE`. Gives trunk-recorder `/dev/bus/usb` (bind mount plus a cgroup rule for char major 189, so replugged dongles keep working) and the host's SDR group (`SDR_USB_GID`, default 46), and sets `SDR_USB=1` for `trunk-recorder-config`. |
| `render_config.py` | Renders `config.json` (each source's `device`/`gain`/`ppm` from the `SDR_*` environment) and `talkgroups.csv` into the run directory, and prints the coverage check. Standard library only. A local device string (`rtl=…`, matched by `LOCAL_DEVICE`) is refused unless `SDR_USB` is set, which only `compose.usb.yaml` does. It writes no Priority column to `talkgroups.csv`. |
| `Dockerfile`, `requirements.txt`, `.dockerignore` | The transcriber image: Ubuntu 24.04, its numpy and scipy, `tr_transcriber.py` and the pinned pip packages, with nothing else in the build context. Runs as uid 1000 unless compose's `user:` says otherwise, so nothing in the image may depend on the uid (`HOME=/tmp`). |
| `tr_transcriber.py` | Whisper transcription. The `Transcriber` class watches `logs/trunk-recorder/media/` for finished calls (the JSON lands first, and the WAV is transcribed once it has settled). Transcribes each call once with word timestamps, then splits the text back into transmissions using the call JSON's `freqList`/`srcList`. Tracks finished calls through the `call` field in `transcripts.jsonl`. Paths default to the repo's `config/` and `logs/trunk-recorder/`; the image points them at `/config` and `/data` with `SCANNER_CONFIG_DIR` and `SCANNER_DATA_DIR`. SIGTERM or SIGINT finishes the call in progress and exits 0. The main loop touches `$SCANNER_HEALTH_FILE` every few seconds, and `--healthcheck` (the image's `HEALTHCHECK`) fails once that file is `HEALTH_MAX_AGE` old. Also holds the Whisper helpers `testset.py` uses (`load_model()`, `transcribe()`, `clean_text()`, `split_words()`, `Prompts`) and `TalkgroupColors`. |
| `testset.py` | Hand-labelled WER test set. `--mode clip` transcribes each transmission alone (default `call` matches `tr_transcriber.py`); `score --prompts FILE` and `--context N` test per-talkgroup prompts and recent-line context. `--help` covers the rest. |
| `testset/` | `clips.jsonl`, `labels.jsonl` (newest line per clip wins), `calls/` (call WAV+JSON copies), `results/` (per-clip output of each score run). Tracked in git. |
| `config/` | See README. `trunk-recorder.json` is a template; `render_config.py` overwrites each source's `device`/`gain`/`ppm`. `scanner.conf` must stay plain `KEY=value` with comments on their own lines, so Compose, `docker run --env-file` and bash all read it the same way. |
| `venv/` | Python venv (created with `--system-site-packages`). Only `testset.py` and host-side experiments use it; the live transcriber runs from the image. |
| `logs/trunk-recorder/` | The run directory. Files directly under `logs/` (`op25.log`, `transcripts.*`, …) are left over from the earlier OP25 setup. |

## Implementation notes

- `tr_transcriber.py` needs `compressWav: false`: it reads the WAVs directly, because faster-whisper's `decode_audio` (PyAV) is broken in this venv.
- Splitting a call into transmissions relies on Whisper word timestamps, so a word at a key-up boundary sometimes lands on the neighbouring RID. Back-to-back transmissions from the same radio are merged.
- Talkgroup priority: a talkgroup with priority N is recorded only when at least N digital recorders are idle on its source ("Not recording talkgroup. Priority is 5 but only 4 recorders are available."). With no Priority column every talkgroup gets 1. `digitalRecorders` (7 and 9) is one per site channel in each source's window; keep it in step if the centers change.
- **numpy and scipy must be Ubuntu 24.04's apt builds** (`python3-numpy` 1.26.4, `python3-scipy` 1.11.4), in the image and in the venv (which is why the venv uses `--system-site-packages`, and why the image is `ubuntu:24.04` and not `python:slim`). The PyPI numpy wheel (OpenBLAS) rounds the mel features differently from Ubuntu's build (reference BLAS). Audio and VAD output stay bit-identical, but close beam-search choices flip: an image built on `python:3.12-slim` with PyPI numpy scored 12.3% WER against 10.7% on the same 187 clips (2026-11-13, saved as the `docker-pypi-numpy` run). Don't add numpy or scipy to `requirements.txt`.
- `requirements.txt` pins faster-whisper and its own dependencies (onnxruntime runs the VAD). Change a pin, or the image's base, only together with a test set run inside the image: `docker run --rm --entrypoint python -v "$PWD/testset.py:/app/testset.py:ro" -v "$PWD/testset:/app/testset" -v "$HOME/.cache/huggingface:/models" scanner-transcriber /app/testset.py score --name NAME`.
- A WAV that can't be read yet (still being copied) is retried every 2 s and given up on after `PENDING_TIMEOUT`.
- USB SDRs are untested with real hardware (no dongle is attached to this machine). Verified 2026-11-13: with `compose.usb.yaml` the trunk-recorder container runs as uid 1000 with group 46 and can read the host's USB device nodes, and the image has librtlsdr and gr-osmosdr but no `rtl_test`/`rtl_eeprom`.
- Talkgroup IDs come from RadioReference. Confirm new ones against live traffic (`docker compose logs trunk-recorder`) before relying on them.

## Environment constraints and gotchas

- **No sudo during agent sessions.** System packages were installed by the user.
- **Compose v2 is required.** The apt `docker-compose` 1.29 crashes against Docker 29 (`KeyError: 'id'`), though `docker-compose -f compose.yaml config` still parses the file.
- **Python packages go in `venv/`, never apt.** This is the user's preference. Use `./venv/bin/pip`.
- **brotli:** the venv pins `brotli>=1.2`, which shadows the system brotli 1.1. Don't remove it.
- **No GPU acceleration.** The GPU is an AMD integrated one, so Whisper runs on the CPU (24 cores; the transcriber uses half by default). Whisper pads every clip to 30 s, so each call costs about 3–4 s with large-v3 no matter how long it is. Smaller models aren't much faster. large-v3-turbo was observed to fall into repetition loops.
- **The SDRs are remote and shared.** Each rtl_tcp port accepts only one client at a time, so stop trunk-recorder (`docker compose stop trunk-recorder`) before running any other test against the SDRs.

## Making changes

- **Tune transcription:** edit `DEFAULT_PROMPT` (local vocabulary), `HALLUCINATIONS` and `clean_text()` in `tr_transcriber.py`. Measure every change against the test set: `./venv/bin/python testset.py score --drafts` for the live baseline, then e.g. `testset.py score --set vad_filter=False --name no-vad`. `transcribe()` passes extra keyword arguments straight to faster-whisper.
- **Detailed test set results** (2026-11-08, 187 clips, large-v3, call mode unless noted; `testset.py compare` reproduces them; the README has the summary):
  - Current settings: 10.7% WER. Under 2 s: 25%; 2–5 s: 12%; above: 6%.
  - Call mode vs clip mode: 10.7% vs 17.0%, and 25% vs 42% on clips under 2 s.
  - No prompt: 17.1%. Removing `DEFAULT_PROMPT`'s place-name list: 13.9%. Example-sentence prompts per agency: 13.3%. The same list-style prompt adapted per agency: 11.1% (HCSO worse, the other groups too small to tell).
  - `--context`: 13.5% (2 lines, call mode), 18.0% (3 lines, clip mode). It stays off.
  - Whisper medium (tested 2026-11-10, same settings): 22.0% (42% / 28% / 14% by length), about double large-v3's error on every length bucket. Kept large-v3.
  - The transcriber image (tested 2026-11-13): 10.7%, the same errors as the venv (`docker-image` run).
  - VAD off: 13.4%. VAD threshold 0.35: 12.1%. `no_speech_threshold=None`: identical output. Keep the current VAD settings.
- **Testing:** there are no automated tests. After changing `tr_transcriber.py`, rebuild and verify with a short live run: `docker compose up -d --build`, wait two minutes, then check `logs/trunk-recorder/transcripts.jsonl` (`proc_s` is transcription time), `docker compose logs transcriber` (`backlog` warnings) and `docker compose ps` (the transcriber should be `healthy`). To test the transcriber without the SDRs, copy a few calls' WAV+JSON into a scratch directory laid out like `media/` and run the image on it with `--once`. `testset.py score --drafts` is a quick check that the Whisper helpers still import.
- **Docs:** user-facing changes (settings, commands, talkgroups, results) go in `README.md`; implementation details go here.
- **Git:** the repo tracks only the project's own files (`README.md`, `AGENTS.md`, the scripts, the Docker files, `config/`, `testset/`). `.gitignore` excludes `venv/`, `logs/` and `__pycache__/`; don't commit them.
