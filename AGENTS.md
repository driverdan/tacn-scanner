# Chattanooga PD Scanner

Read `README.md` first. It covers what the project does, the architecture, setup, running, configuration, output files, reception tuning, the test set and its results, and the radio system and talkgroups. This file holds what's only relevant when changing the code.

## Layout

| Path | What |
|---|---|
| `trunk-recorder.sh` | Main launcher. Loads `config/scanner.conf`, renders `config.json` and `talkgroups.csv` into `logs/trunk-recorder/` (mounted as `/app`), starts `tr_transcriber.py` in the background (unless `TRANSCRIBE=0`; arguments after `--` go to it) and runs trunk-recorder in Docker. The transcriber is killed when trunk-recorder exits. It writes no Priority column to `talkgroups.csv`. |
| `tr_transcriber.py` | Whisper transcription. Watches `logs/trunk-recorder/media/` for finished calls (the JSON lands first, and the WAV is transcribed once it has settled). Transcribes each call once with word timestamps, then splits the text back into transmissions using the call JSON's `freqList`/`srcList`. Tracks finished calls through the `call` field in `transcripts.jsonl`. Also holds the Whisper helpers `testset.py` uses (`load_model()`, `transcribe()`, `clean_text()`, `split_words()`, `Prompts`) and `TalkgroupColors`. |
| `testset.py` | Hand-labelled WER test set. `--mode clip` transcribes each transmission alone (default `call` matches `tr_transcriber.py`); `score --prompts FILE` and `--context N` test per-talkgroup prompts and recent-line context. `--help` covers the rest. |
| `testset/` | `clips.jsonl`, `labels.jsonl` (newest line per clip wins), `calls/` (call WAV+JSON copies), `results/` (per-clip output of each score run). Tracked in git. |
| `config/` | See README. `trunk-recorder.json` is a template; `trunk-recorder.sh` overwrites each source's `device`/`gain`/`ppm`. |
| `venv/` | Python venv (created with `--system-site-packages`). |
| `logs/trunk-recorder/` | The run directory. Files directly under `logs/` (`op25.log`, `transcripts.*`, …) are left over from the earlier OP25 setup. |

## Implementation notes

- `tr_transcriber.py` needs `compressWav: false`: it reads the WAVs directly, because faster-whisper's `decode_audio` (PyAV) is broken in this venv.
- Splitting a call into transmissions relies on Whisper word timestamps, so a word at a key-up boundary sometimes lands on the neighbouring RID. Back-to-back transmissions from the same radio are merged.
- Talkgroup priority: a talkgroup with priority N is recorded only when at least N digital recorders are idle on its source ("Not recording talkgroup. Priority is 5 but only 4 recorders are available."). With no Priority column every talkgroup gets 1. `digitalRecorders` (7 and 9) is one per site channel in each source's window; keep it in step if the centers change.
- Talkgroup IDs come from RadioReference. Confirm new ones against live traffic (`logs/trunk-recorder/trunk-recorder.log`) before relying on them.

## Environment constraints and gotchas

- **No sudo during agent sessions.** System packages were installed by the user.
- **Python packages go in `venv/`, never apt.** This is the user's preference. Use `./venv/bin/pip`.
- **brotli:** the venv pins `brotli>=1.2`, which shadows the system brotli 1.1. Don't remove it.
- **No GPU acceleration.** The GPU is an AMD integrated one, so Whisper runs on the CPU (24 cores; the transcriber uses half by default). Whisper pads every clip to 30 s, so each call costs about 3–4 s with large-v3 no matter how long it is. Smaller models aren't much faster. large-v3-turbo was observed to fall into repetition loops.
- **The SDRs are remote and shared.** Each rtl_tcp port accepts only one client at a time, so stop the scanner (`docker stop trunk-recorder`) before running any other test against the SDRs.

## Making changes

- **Tune transcription:** edit `DEFAULT_PROMPT` (local vocabulary), `HALLUCINATIONS` and `clean_text()` in `tr_transcriber.py`. Measure every change against the test set: `./venv/bin/python testset.py score --drafts` for the live baseline, then e.g. `testset.py score --set vad_filter=False --name no-vad`. `transcribe()` passes extra keyword arguments straight to faster-whisper.
- **Detailed test set results** (2026-11-08, 187 clips, large-v3, call mode unless noted; `testset.py compare` reproduces them; the README has the summary):
  - Current settings: 10.7% WER. Under 2 s: 25%; 2–5 s: 12%; above: 6%.
  - Call mode vs clip mode: 10.7% vs 17.0%, and 25% vs 42% on clips under 2 s.
  - No prompt: 17.1%. Removing `DEFAULT_PROMPT`'s place-name list: 13.9%. Example-sentence prompts per agency: 13.3%. The same list-style prompt adapted per agency: 11.1% (HCSO worse, the other groups too small to tell).
  - `--context`: 13.5% (2 lines, call mode), 18.0% (3 lines, clip mode). It stays off.
  - Whisper medium (tested 2026-11-10, same settings): 22.0% (42% / 28% / 14% by length), about double large-v3's error on every length bucket. Kept large-v3.
  - VAD off: 13.4%. VAD threshold 0.35: 12.1%. `no_speech_threshold=None`: identical output. Keep the current VAD settings.
- **Testing:** there are no automated tests. Verify with a timed live run, e.g. `timeout 120 ./trunk-recorder.sh`, then check `logs/trunk-recorder/transcripts.jsonl` (`proc_s` is transcription time) and `transcriber.log` (`backlog` warnings). `testset.py score --drafts` is a quick check that the Whisper helpers still import.
- **Docs:** user-facing changes (settings, commands, talkgroups, results) go in `README.md`; implementation details go here.
- **Git:** the repo tracks only the project's own files (`README.md`, `AGENTS.md`, the scripts, `config/`, `testset/`). `.gitignore` excludes `venv/`, `logs/` and `__pycache__/`; don't commit them.
