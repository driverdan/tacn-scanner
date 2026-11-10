#!/usr/bin/env python3
"""Whisper transcription of trunk-recorder calls.

trunk-recorder writes one audio file plus one JSON per call under its captureDir
(<media>/<system>/YYYY/M/D/<tgid>-<epoch>_<freq>-call_N.{wav,json}); the JSON appears once the
call is over. This program watches for new call JSONs, transcribes the audio with the same
Whisper setup as transcriber.py, and splits the text back into the call's transmissions
(freqList/srcList) using word timestamps, so each line carries the radio ID that spoke it.

Output matches transcriber.py's (one record per transmission), written to
<outdir>/transcripts.{txt,jsonl}. Calls already in transcripts.jsonl are never redone.
"""
import argparse
import datetime as dt
import json
import os
import time
import wave
from math import gcd

import numpy as np
from scipy.signal import resample_poly

from transcriber import PROMPTS_FILE, Prompts, load_model, log, split_words, transcribe

ROOT = os.path.dirname(os.path.abspath(__file__))
PENDING_TIMEOUT = 120  # seconds a new call may wait for its WAV


def wav_of(json_path):
    return json_path[:-len(".json")] + ".wav"


def wav_settled(json_path, quiet=2.0):
    """True once the call's WAV exists and hasn't been written to for `quiet` seconds."""
    try:
        return time.time() - os.stat(wav_of(json_path)).st_mtime > quiet
    except FileNotFoundError:
        return False


def read_wav(path):
    """16 kHz mono float32 audio from a PCM WAV (trunk-recorder's compressWav must be false)."""
    with wave.open(path) as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{width * 8}-bit WAV not supported")
    audio = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch).mean(axis=1).astype(np.float32) / 32768.0
    if rate != 16000:
        g = gcd(16000, rate)
        audio = resample_poly(audio, 16000 // g, rate // g).astype(np.float32)
    return audio


class CallScanner:
    """Finds new call JSONs under the media dir, listing only directories whose mtime changed."""

    def __init__(self, root):
        self.root = root
        self.dir_mtimes = {}
        self.seen = set()

    def scan(self):
        found = []
        stack = [self.root]
        while stack:
            d = stack.pop()
            try:
                mtime = os.stat(d).st_mtime_ns
                entries = list(os.scandir(d))
            except FileNotFoundError:
                continue
            changed = self.dir_mtimes.get(d) != mtime
            self.dir_mtimes[d] = mtime
            for e in entries:
                if e.is_dir():
                    stack.append(e.path)  # subdirs may have changed even if this one didn't
                elif changed and e.name.endswith(".json") and e.path not in self.seen:
                    self.seen.add(e.path)
                    found.append(e.path)
        return found


def load_call(path, tries=3):
    """Parse a call JSON, allowing for one caught mid-write."""
    for i in range(tries):
        try:
            with open(path) as f:
                return json.load(f)
        except json.JSONDecodeError:
            if i == tries - 1:
                raise
            time.sleep(1)


def transmissions(call):
    """[(start_s, len_s, rid, freq, error_count)] per transmission; back-to-back ones by one radio merged."""
    txs = _transmissions(call)
    merged = [txs[0]]
    for t in txs[1:]:
        p = merged[-1]
        if t[2] == p[2] and t[3] == p[3] and p[1] is not None and t[1] is not None:
            errs = None if p[4] is None and t[4] is None else (p[4] or 0) + (t[4] or 0)
            merged[-1] = (p[0], t[0] + t[1] - p[0], p[2], p[3], errs)
        else:
            merged.append(t)
    return merged


def _transmissions(call):
    srcs = sorted(call.get("srcList") or [], key=lambda s: s.get("pos", 0))

    def rid_at(pos):
        rid = None
        for s in srcs:
            if s.get("pos", 0) <= pos + 0.05:
                rid = s.get("src") or rid
        return rid if rid and rid > 0 else None

    freqs = sorted(call.get("freqList") or [], key=lambda f: f.get("pos", 0))
    if freqs:
        return [(f.get("pos", 0.0), f.get("len"), rid_at(f.get("pos", 0.0)), f.get("freq") or call.get("freq"),
                 f.get("error_count")) for f in freqs]
    if srcs:
        return [(s.get("pos", 0.0), None, rid_at(s.get("pos", 0.0)), call.get("freq"), None) for s in srcs]
    return [(0.0, None, None, call.get("freq"), None)]



def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--media-dir", default=os.path.join(ROOT, "logs", "trunk-recorder", "media"),
                    help="trunk-recorder captureDir")
    ap.add_argument("--outdir", default=os.path.join(ROOT, "logs", "trunk-recorder"))
    ap.add_argument("--backfill", action="store_true",
                    help="also transcribe calls recorded before startup (oldest first; new calls jump the queue)")
    ap.add_argument("--once", action="store_true", help="transcribe all pending calls, then exit (implies --backfill)")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between scans for new calls")
    ap.add_argument("--model", default=os.environ.get("WHISPER_MODEL", "large-v3"))
    ap.add_argument("--compute-type", default="int8")
    ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--min-dur", type=float, default=0.6, help="skip calls shorter than this (seconds)")
    ap.add_argument("--prompt", help="one initial prompt for every talkgroup (default: per talkgroup from --prompts)")
    ap.add_argument("--prompts", default=PROMPTS_FILE,
                    help="per-talkgroup prompts (JSON, re-read when it changes; DEFAULT_PROMPT if missing)")
    ap.add_argument("--context", type=int, default=0, metavar="N",
                    help="append the last N lines heard on the same talkgroup to the prompt")
    ap.add_argument("--context-window", type=float, default=300.0, metavar="SEC",
                    help="only use context lines from the last SEC seconds")
    args = ap.parse_args()
    if args.once:
        args.backfill = True

    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(args.media_dir, exist_ok=True)
    jsonl_path = os.path.join(args.outdir, "transcripts.jsonl")
    txt_path = os.path.join(args.outdir, "transcripts.txt")

    done = set()
    if os.path.exists(jsonl_path):
        with open(jsonl_path) as f:
            for line in f:
                try:
                    done.add(json.loads(line).get("call"))
                except json.JSONDecodeError:
                    pass

    def rel(path):
        return os.path.relpath(path, args.media_dir)

    scanner = CallScanner(args.media_dir)
    existing = [p for p in scanner.scan() if rel(p) not in done]
    backlog = sorted(existing, key=os.path.getmtime, reverse=True) if args.backfill else []
    log(f"watching {args.media_dir}: {len(existing)} untranscribed calls already there"
        + (" (transcribing them oldest first; new calls jump the queue)" if args.backfill else " (skipped; --backfill to include)"))

    prompts = Prompts(None if args.prompt is not None else args.prompts, args.context, args.context_window)
    if args.prompt is not None:
        prompts.default = args.prompt
    model = load_model(args.model, args.compute_type, args.threads)

    def process(path):
        key = rel(path)
        if key in done:
            return
        try:
            call = load_call(path)
        except (OSError, json.JSONDecodeError) as e:
            log(f"warning: skipping {key}: {e}")
            return
        if call.get("encrypted"):
            return
        audio_path = wav_of(path)
        if not os.path.exists(audio_path):
            log(f"warning: skipping {key}: no .wav (is compressWav on?)")
            return

        txs = transmissions(call)
        t_call = call.get("start_time_ms", call.get("start_time", 0) * 1000) / 1000
        tg, tag = call.get("talkgroup"), call.get("talkgroup_tag") or ""
        started = time.time()
        try:
            audio = read_wav(audio_path)
        except (EOFError, wave.Error):
            return False  # still being copied into place
        dur = len(audio) / 16000
        texts = [""] * len(txs)
        if dur >= args.min_dur:
            segments = transcribe(model, audio, prompts.build(tg, t_call), args.beam_size, word_timestamps=True)
            texts = split_words(segments, txs)
        proc = time.time() - started

        label = tag or f"TG {tg}"
        recs, lines = [], []
        for i, ((pos, length, rid, freq, errs), text) in enumerate(zip(txs, texts)):
            if length is None:
                nxt = txs[i + 1][0] if i + 1 < len(txs) else dur
                length = max(0.0, nxt - pos)
            ts = dt.datetime.fromtimestamp(t_call + pos)
            prompts.remember(tg, t_call + pos, text)
            recs.append({
                "time": ts.isoformat(timespec="seconds"), "duration": round(length, 2),
                "tgid": tg, "tag": tag, "rid": rid, "freq": freq, "slot": call.get("tdma_slot", 0),
                "text": text, "model": args.model, "proc_s": round(proc, 2),
                "error_count": errs, "call": key, "wav": audio_path,
            })
            if text:
                lines.append(f"[{ts:%H:%M:%S}] {label}" + (f" ({tg})" if tag else "")
                             + (f" RID {rid}" if rid else "") + f": {text}")
        with open(jsonl_path, "a") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        if lines:
            with open(txt_path, "a") as f:
                f.write("\n".join(lines) + "\n")
            print("\n".join(lines), flush=True)
        done.add(key)
        if len(pending) > 3:
            log(f"warning: transcription backlog {len(pending)} calls (last {dur:.1f}s call took {proc:.1f}s)")

    # trunk-recorder writes the call JSON first, then copies the WAV in from its temp dir, so a new
    # call waits in `pending` until its WAV exists and has stopped changing.
    pending = {}  # call JSON -> time first seen
    last_scan = 0.0
    while True:
        now = time.time()
        if not args.once and now - last_scan >= args.interval:
            last_scan = now
            for p in scanner.scan():
                if rel(p) not in done:
                    pending.setdefault(p, now)
        path = next((p for p in sorted(pending, key=pending.get) if wav_settled(p)), None)
        if path:
            seen_at = pending.pop(path)
        else:
            for p, t in list(pending.items()):
                if now - t > PENDING_TIMEOUT:
                    log(f"warning: skipping {rel(p)}: no complete .wav after {PENDING_TIMEOUT}s")
                    del pending[p]
            if backlog:
                path, seen_at = backlog.pop(), None  # oldest first; live calls go ahead of these
            elif args.once:
                break
            else:
                time.sleep(0.25)
                continue
        try:
            if process(path) is False and seen_at is not None:
                pending[path] = seen_at
        except Exception as e:  # keep watching even if one call's audio is bad
            log(f"error: {rel(path)}: {e!r}")
    log(f"done: {len(done)} calls in {jsonl_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
