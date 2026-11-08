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


def log(msg):
    sys.stderr.write(f"{dt.datetime.now():%H:%M:%S} {msg}\n")
    sys.stderr.flush()


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
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
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
    seg = Segmenter(args.gap, args.min_dur, args.max_dur,
                    lambda slot, t0, t1, pcm: work.put_nowait((slot, t0, t1, pcm)) if not args.no_whisper else None)

    def whisper_worker():
        from faster_whisper import WhisperModel
        log(f"loading whisper model {args.model} ({args.compute_type}, {args.threads} threads)...")
        model = WhisperModel(args.model, device="cpu", compute_type=args.compute_type, cpu_threads=args.threads)
        log("whisper ready")
        while True:
            slot, t0, t1, pcm = work.get()
            meta = poller.lookup(t0, t1)
            audio16k = resample_poly(pcm.astype(np.float32) / 32768.0, 2, 1).astype(np.float32)
            started = time.time()
            segments, _ = model.transcribe(
                audio16k, language="en", beam_size=args.beam_size, vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                initial_prompt=args.prompt, condition_on_previous_text=False,
                no_speech_threshold=0.6, log_prob_threshold=-1.0,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
            proc = time.time() - started
            dur = len(pcm) / PCM_RATE
            text = clean_text(text)
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
                continue
            line = f"[{ts:%H:%M:%S}] {label}" + (f" ({tg})" if meta.get("tag") else "") \
                   + (f" RID {rid}" if rid else "") + f": {text}"
            with open(txt_path, "a") as f:
                f.write(line + "\n")
            print(line, flush=True)
            if work.qsize() > 3:
                log(f"warning: transcription backlog {work.qsize()} (last {dur:.1f}s audio took {proc:.1f}s)")

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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
