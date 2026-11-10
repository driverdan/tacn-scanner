#!/usr/bin/env python3
"""Real-time Whisper transcription of OP25 call audio.

OP25 sends decoded audio as UDP packets: 320 bytes = 20 ms of 8 kHz s16le mono PCM,
or a 2-byte flag (0 = end of transmission/drain, 1 = drop). This program:
  * listens where OP25 sends audio (--listen-port, plus port+1 for the 2nd TDMA slot),
  * relays every packet unchanged to OP25's sockaudio player (--play-port) so you still hear it,
  * splits audio into transmissions (on the end flag or a silence gap),
  * tags each transmission with talkgroup/radio ID from OP25's HTTP status API,
  * transcribes it with faster-whisper and prints/logs the result.
"""
import argparse
import datetime as dt
import json
import re
import os
import queue
import select
import socket
import sys
import threading
import time
import urllib.request
import wave

import numpy as np
from scipy.signal import resample_poly

PCM_RATE = 8000
FRAME_BYTES = 320
GROUP_PAD = 0.3  # seconds of silence between transmissions transcribed as a group

# Vocabulary hint for Whisper (biases spelling of local names/jargon).
DEFAULT_PROMPT = (
    "Chattanooga police dispatch radio. Adam, Baker, Charlie, David, Edward, Frank. "
    "10-4, copy, en route, on scene, 10-8, 10-7, 10-23, signal, subject, suspect, "
    "Hamilton County, Brainerd, East Ridge, Red Bank, Hixson, Lookout Mountain, "
    "Highway 153, I-24, I-75, Market Street, Broad Street, MLK, Amnicola, Shallowford."
)

# Whisper hallucinates these on noise/silence.
HALLUCINATIONS = {
    "", "you", "thank you.", "thank you", "thanks for watching!", "thanks for watching.",
    "bye.", "bye", ".", "okay.", "so", "uh", "um",
}


_REPEAT = re.compile(r"(\b.{2,30}?)(?:[,.]?\s+\1){3,}", re.IGNORECASE)


def clean_text(text):
    """Collapse Whisper repetition loops ("10-8, 10-8, 10-8, ...") and drop known hallucinations."""
    text = _REPEAT.sub(r"\1", text).strip()
    return "" if text.lower() in HALLUCINATIONS else text


def split_words(segments, txs):
    """Assign each word to the transmission it falls in (by word midpoint)."""
    texts = [[] for _ in txs]
    starts = [t[0] for t in txs]
    for seg in segments:
        for w in seg.words or []:
            mid = (w.start + w.end) / 2
            i = max([k for k, s in enumerate(starts) if s <= mid] or [0])
            texts[i].append(w.word)
    return [clean_text("".join(t).strip()) for t in texts]


def load_model(name, compute_type="int8", threads=None):
    from faster_whisper import WhisperModel
    threads = threads or max(1, (os.cpu_count() or 4) // 2)
    log(f"loading whisper model {name} ({compute_type}, {threads} threads)...")
    model = WhisperModel(name, device="cpu", compute_type=compute_type, cpu_threads=threads)
    log("whisper ready")
    return model


def transcribe(model, audio16k, prompt=DEFAULT_PROMPT, beam_size=5, word_timestamps=False, **overrides):
    """Run Whisper on 16 kHz float32 audio with the settings tuned for radio traffic; returns segments.

    `overrides` replace or add faster-whisper transcribe() options (testset.py score --set uses this)."""
    opts = dict(
        language="en", beam_size=beam_size, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        initial_prompt=prompt, condition_on_previous_text=False,
        no_speech_threshold=0.6, log_prob_threshold=-1.0, word_timestamps=word_timestamps,
    )
    opts.update(overrides)
    segments, _ = model.transcribe(audio16k, **opts)
    return list(segments)


def log(msg):
    sys.stderr.write(f"{dt.datetime.now():%H:%M:%S} {msg}\n")
    sys.stderr.flush()


PROMPTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "prompts.json")
CONTEXT_CHARS = 400  # cap on recent-line context; Whisper keeps only the last ~223 prompt tokens


class Prompts:
    """Whisper initial prompts: a per-talkgroup base prompt from config/prompts.json (re-read when it
    changes; DEFAULT_PROMPT if it's missing), optionally followed by the last few lines transcribed on
    the same talkgroup, so Whisper hears each transmission as part of the conversation."""

    def __init__(self, path=PROMPTS_FILE, context_lines=0, context_window=300.0):
        self.path, self.context_lines, self.context_window = path, context_lines, context_window
        self.mtime, self.default, self.by_tg = None, DEFAULT_PROMPT, {}
        self.recent = {}  # tgid -> [(time, text)]
        self._load()

    def _load(self):
        try:
            mtime = os.stat(self.path).st_mtime if self.path else None
        except OSError:
            mtime = None
        if mtime is None or mtime == self.mtime:
            return
        self.mtime = mtime
        try:
            with open(self.path) as f:
                cfg = json.load(f)
            by_tg = {int(tg): g["prompt"] for g in cfg.get("groups", []) for tg in g.get("tgids", [])}
        except (OSError, ValueError, KeyError, TypeError) as e:
            log(f"warning: ignoring {self.path}: {e!r}")
            return
        self.default, self.by_tg = cfg.get("default", DEFAULT_PROMPT), by_tg

    def base(self, tgid):
        self._load()
        try:
            return self.by_tg.get(int(tgid), self.default)
        except (TypeError, ValueError):
            return self.default

    def remember(self, tgid, t, text):
        if self.context_lines and tgid is not None and text:
            lines = self.recent.setdefault(tgid, [])
            lines.append((t, text))
            del lines[:-self.context_lines]

    def recent_lines(self, tgid, t):
        return [text for when, text in self.recent.get(tgid, []) if 0 <= t - when <= self.context_window]

    def build(self, tgid, t=None, recent=None):
        """Prompt for a transmission on `tgid` at time `t`; `recent` overrides the remembered lines."""
        lines = self.recent_lines(tgid, t) if recent is None else recent
        lines = lines[-self.context_lines:] if self.context_lines else []
        ctx = " ".join(lines)
        if len(ctx) > CONTEXT_CHARS:
            ctx = ctx[-CONTEXT_CHARS:].split(" ", 1)[-1]
        return f"{self.base(tgid)} {ctx}".strip()


class StatusPoller(threading.Thread):
    """Polls OP25's HTTP API and remembers the most recent active call metadata."""

    def __init__(self, url, interval=0.4):
        super().__init__(daemon=True)
        self.url, self.interval = url, interval
        self.lock = threading.Lock()
        self.history = []  # (time, {tgid, tag, srcaddr, srctag, freq, encrypted})

    def _fetch(self):
        body = json.dumps([{"command": "update", "arg1": 0, "arg2": 0}]).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read())

    def run(self):
        while True:
            try:
                for msg in self._fetch():
                    if msg.get("json_type") != "channel_update":
                        continue
                    for ch in msg.get("channels", []):
                        c = msg.get(ch, {})
                        if c.get("tgid"):
                            with self.lock:
                                self.history.append((time.time(), {
                                    "tgid": c.get("tgid"), "tag": c.get("tag") or "",
                                    "srcaddr": c.get("srcaddr") or None, "srctag": c.get("srctag") or "",
                                    "freq": c.get("freq"), "encrypted": c.get("encrypted"),
                                }))
                                cutoff = time.time() - 120
                                self.history = [h for h in self.history if h[0] > cutoff]
            except Exception:
                pass  # OP25 not up yet / transient
            time.sleep(self.interval)

    def lookup(self, t0, t1):
        """Best metadata observed during [t0-1, t1]; prefer entries that have a radio ID."""
        with self.lock:
            hits = [m for t, m in self.history if t0 - 1.0 <= t <= t1 + 0.2]
        if not hits:
            return {}
        with_rid = [m for m in hits if m.get("srcaddr")]
        # most common tgid wins
        tgids = [m["tgid"] for m in hits]
        tg = max(set(tgids), key=tgids.count)
        pick = [m for m in (with_rid or hits) if m["tgid"] == tg] or hits
        return pick[-1]


class Segmenter:
    """Accumulates PCM per slot; emits a segment on end-flag or gap."""

    def __init__(self, gap, min_dur, max_dur, emit, drain_gap=0.5):
        self.gap, self.min_dur, self.max_dur, self.emit = gap, min_dur, max_dur, emit
        self.drain_gap = drain_gap
        self.bufs, self.start, self.last, self.ended = {}, {}, {}, {}

    def drain(self, slot):
        """End-of-transmission flag: end the segment unless audio resumes shortly."""
        if slot in self.bufs:
            self.ended[slot] = time.time()

    def audio(self, slot, data):
        now = time.time()
        if slot not in self.bufs:
            self.bufs[slot], self.start[slot] = bytearray(), now
        self.bufs[slot] += data
        self.last[slot] = now
        if len(self.bufs[slot]) >= self.max_dur * PCM_RATE * 2:
            self.flush(slot)

    def flush(self, slot):
        buf = self.bufs.pop(slot, None)
        self.ended.pop(slot, None)
        if buf is None:
            return
        t0, t1 = self.start.pop(slot), self.last.pop(slot)
        pcm = np.frombuffer(bytes(buf), dtype=np.int16)
        # drop trailing/leading digital silence before measuring length
        nz = np.nonzero(np.abs(pcm) > 64)[0]
        if len(nz) == 0 or (nz[-1] - nz[0]) / PCM_RATE < self.min_dur:
            return
        self.emit(slot, t0, t1, pcm[max(0, nz[0] - 800): nz[-1] + 800])

    def tick(self):
        now = time.time()
        for slot in list(self.last):
            t = self.last[slot]
            e = self.ended.get(slot)
            if now - t > self.gap or (e is not None and e >= t and now - e > self.drain_gap):
                self.flush(slot)


class Grouper:
    """Holds consecutive transmissions on one talkgroup until the conversation pauses, so Whisper
    transcribes them together: a short reply ("5-14, copy") is far more accurate with the exchange around
    it (testset.py: 17% WER one transmission at a time vs 11% for whole calls). A group is emitted once
    `gap` seconds pass with nobody transmitting (a transmission still coming in may be its next one), or
    before it would pass `max_dur` seconds."""

    def __init__(self, gap, max_dur, emit):
        self.gap, self.max_dur, self.emit = gap, max_dur, emit
        self.groups = {}  # talkgroup -> ([(slot, t0, t1, pcm, meta)], time of last add)

    def add(self, item):
        key = item[4].get("tgid")
        items, _ = self.groups.get(key, ([], 0))
        if items and (sum(len(it[3]) for it in items) + len(item[3])) / PCM_RATE > self.max_dur:
            self.flush(key)
            items = []
        self.groups[key] = (items + [item], time.time())
        if self.gap <= 0:
            self.flush(key)

    def flush(self, key):
        items, _ = self.groups.pop(key, ([], 0))
        if items:
            self.emit(items)

    def tick(self, receiving=False):
        """`receiving`: a transmission is in progress; hold every group (it may continue one) and restart the wait."""
        now = time.time()
        if receiving:
            self.groups = {k: (items, now) for k, (items, _) in self.groups.items()}
            return
        for key, (_, last) in list(self.groups.items()):
            if now - last > self.gap:
                self.flush(key)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen-port", type=int, default=23470, help="UDP port OP25 sends audio to")
    ap.add_argument("--play-port", type=int, default=23456, help="UDP port of OP25 sockaudio player (0 = don't relay)")
    ap.add_argument("--op25-url", default="http://127.0.0.1:8080/")
    ap.add_argument("--model", default=os.environ.get("WHISPER_MODEL", "large-v3"),
                    help="faster-whisper model (e.g. small.en, medium.en, distil-large-v3, large-v3)")
    ap.add_argument("--compute-type", default="int8")
    ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--gap", type=float, default=1.2, help="seconds of no audio that ends a transmission")
    ap.add_argument("--min-dur", type=float, default=0.6, help="ignore transmissions shorter than this")
    ap.add_argument("--max-dur", type=float, default=30.0, help="force-split transmissions longer than this")
    ap.add_argument("--group-gap", type=float, default=3.0, metavar="SEC",
                    help="transcribe a talkgroup's transmissions together until it's quiet this long "
                         "(0 = each transmission on its own, as before; lines print after the pause)")
    ap.add_argument("--group-max", type=float, default=25.0, metavar="SEC",
                    help="most audio to transcribe as one group")
    ap.add_argument("--prompt", help="one initial prompt for every talkgroup (default: per talkgroup from --prompts)")
    ap.add_argument("--prompts", default=PROMPTS_FILE,
                    help="per-talkgroup prompts (JSON, re-read when it changes; DEFAULT_PROMPT if missing)")
    ap.add_argument("--context", type=int, default=0, metavar="N",
                    help="append the last N lines heard on the same talkgroup to the prompt")
    ap.add_argument("--context-window", type=float, default=300.0, metavar="SEC",
                    help="only use context lines from the last SEC seconds")
    ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
    ap.add_argument("--save-audio", action="store_true", help="also save each transmission as WAV")
    ap.add_argument("--no-whisper", action="store_true", help="relay audio only")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    jsonl_path = os.path.join(args.outdir, "transcripts.jsonl")
    txt_path = os.path.join(args.outdir, "transcripts.txt")

    # Sockets: slot A on listen_port, slot B on listen_port+1 (matches OP25 sockaudio convention).
    socks = []
    for i in range(2):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", args.listen_port + i))
        socks.append(s)
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    log(f"listening on udp 127.0.0.1:{args.listen_port}-{args.listen_port + 1}, "
        f"relaying to {args.play_port if args.play_port else 'nowhere'}")

    poller = StatusPoller(args.op25_url)
    poller.start()

    work = queue.Queue(maxsize=200)

    def enqueue(items):
        try:
            work.put_nowait(items)
        except queue.Full:
            log(f"warning: transcription queue full, dropping {len(items)} transmissions")

    group = Grouper(args.group_gap, args.group_max, enqueue)
    seg = Segmenter(args.gap, args.min_dur, args.max_dur,
                    lambda slot, t0, t1, pcm: group.add((slot, t0, t1, pcm, poller.lookup(t0, t1)))
                    if not args.no_whisper else None)

    prompts = Prompts(None if args.prompt is not None else args.prompts, args.context, args.context_window)
    if args.prompt is not None:
        prompts.default = args.prompt

    def whisper_worker():
        model = load_model(args.model, args.compute_type, args.threads)
        while True:
            items = work.get()
            started = time.time()
            texts = transcribe_group(model, items)
            proc = time.time() - started
            for (slot, t0, t1, pcm, meta), text in zip(items, texts):
                write(slot, t0, pcm, meta, text, proc)
            if work.qsize() > 3:
                log(f"warning: transcription backlog {work.qsize()} groups (last {len(items)} transmissions, "
                    f"{sum(len(it[3]) for it in items) / PCM_RATE:.1f}s of audio, took {proc:.1f}s)")

    def transcribe_group(model, items):
        """Text for each transmission: one alone as before, several joined with short silences between
        them, transcribed with word timestamps and split back at the joins."""
        pad = np.zeros(int(GROUP_PAD * 16000), dtype=np.float32)
        pieces, starts, pos = [], [], 0.0
        for i, (_, _, _, pcm, _) in enumerate(items):
            audio = resample_poly(pcm.astype(np.float32) / 32768.0, 2, 1).astype(np.float32)
            if i:
                pieces.append(pad)
                pos += GROUP_PAD
            starts.append((pos - GROUP_PAD / 2 if i else 0.0,))  # boundary mid-way through the silence
            pieces.append(audio)
            pos += len(audio) / 16000
        tg, t0 = items[0][4].get("tgid"), items[0][1]
        if len(items) == 1:
            segments = transcribe(model, pieces[0], prompts.build(tg, t0), args.beam_size)
            texts = [clean_text(" ".join(s.text.strip() for s in segments).strip())]
        else:
            segments = transcribe(model, np.concatenate(pieces), prompts.build(tg, t0), args.beam_size,
                                  word_timestamps=True)
            texts = split_words(segments, starts)
        for (_, t, _, _, meta), text in zip(items, texts):
            prompts.remember(meta.get("tgid"), t, text)
        return texts

    def write(slot, t0, pcm, meta, text, proc):
        dur = len(pcm) / PCM_RATE
        ts = dt.datetime.fromtimestamp(t0)
        tg = meta.get("tgid")
        label = (meta.get("tag") or (f"TG {tg}" if tg else "unknown TG"))
        rid = meta.get("srcaddr")
        rec = {
            "time": ts.isoformat(timespec="seconds"), "duration": round(dur, 2),
            "tgid": tg, "tag": meta.get("tag"), "rid": rid, "freq": meta.get("freq"),
            "slot": slot, "text": text, "model": args.model, "proc_s": round(proc, 2),
        }
        if args.save_audio:
            d = os.path.join(args.outdir, "audio", ts.strftime("%Y-%m-%d"))
            os.makedirs(d, exist_ok=True)
            fn = os.path.join(d, f"{ts:%H%M%S}_{tg or 0}_{rid or 0}.wav")
            with wave.open(fn, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(PCM_RATE); w.writeframes(pcm.tobytes())
            rec["wav"] = fn
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        if not text:
            return
        line = f"[{ts:%H:%M:%S}] {label}" + (f" ({tg})" if meta.get("tag") else "") \
               + (f" RID {rid}" if rid else "") + f": {text}"
        with open(txt_path, "a") as f:
            f.write(line + "\n")
        print(line, flush=True)

    if not args.no_whisper:
        threading.Thread(target=whisper_worker, daemon=True).start()

    while True:
        readable, _, _ = select.select(socks, [], [], 0.2)
        for s in readable:
            slot = socks.index(s)
            data, _ = s.recvfrom(4096)
            if args.play_port:
                out.sendto(data, ("127.0.0.1", args.play_port + slot))
            if len(data) == 2:
                if np.frombuffer(data, dtype=np.int16)[0] in (0, 1):
                    seg.drain(slot)
            elif data:
                seg.audio(slot, data)
        seg.tick()
        group.tick(receiving=bool(seg.bufs))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
