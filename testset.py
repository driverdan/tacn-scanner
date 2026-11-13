#!/usr/bin/env python3
"""Build a hand-labelled test set of radio transmissions and measure Whisper's word error rate on it.

  testset.py label --add 50        sample 50 more transmissions from trunk-recorder's calls, then label them
  testset.py label                 label whatever is still unlabelled
  testset.py score --drafts        WER of the live transcripts saved at sampling time (no Whisper run)
  testset.py score                 re-transcribe the labelled clips with the current settings and report WER
  testset.py score --set vad_filter=False --name no-vad
  testset.py compare               all saved runs side by side, re-scored against the current labels

Clips are single transmissions, cut from trunk-recorder calls at the call JSON's freqList boundaries
(the same split tr_transcriber.py uses). Everything lives under testset/:
  calls/        copies of the sampled calls' WAV + JSON, so the set survives clearing logs/
  clips.jsonl   one line per sampled transmission: call, index, start/end, metadata, live transcript
  labels.jsonl  your labels, appended as you go; the newest line per clip wins
  results/      per-clip output of each score run, for comparing runs

Scoring lowercases, drops punctuation, turns number words into digits and splits digit strings into
single digits, so "10-8", "ten eight" and "1-0-8"... compare as "1 0 8". Don't worry about number
formatting when labelling.
"""
import argparse
import ast
import collections
import datetime as dt
import glob
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
SR = 16000

LABEL_HELP = """\
Type what was said, editing the live transcript shown at the prompt (Ctrl-U clears the line).
Numbers can be digits or words. Use [?] for a word you can't make out.
Commands, alone (with or without the colon) or at the end of the line (the rest of the line is kept):
  :r replay   :c play the whole call   :n no speech (noise/tones only)
  :x unintelligible (left out of scoring)   :s skip for now   :q quit
"""


# ---------------------------------------------------------------- files

def read_jsonl(path):
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def append_jsonl(path, rec):
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def latest_labels(d):
    return {r["id"]: r for r in read_jsonl(os.path.join(d, "labels.jsonl"))}


def write_wav(path, audio):
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


class CallAudio:
    """16 kHz call audio from testset/calls/, cached (labelled clips often share a call)."""

    def __init__(self, d):
        self.d, self.cache = d, {}

    def call(self, clip):
        from tr_transcriber import read_wav
        path = os.path.join(self.d, clip["wav"])
        if path not in self.cache:
            self.cache = {path: read_wav(path)}  # one call at a time is enough
        return self.cache[path]

    def clip(self, clip):
        return self.call(clip)[int(clip["start"] * SR):int(clip["end"] * SR)]


# ---------------------------------------------------------------- sampling

CONTEXT_KEEP, CONTEXT_MAX_AGE = 5, 600  # lines of earlier same-talkgroup traffic stored per clip, and their max age


def context_index(records):
    """{tgid: [(datetime, call, text)]} of live transcript lines, oldest first."""
    idx = collections.defaultdict(list)
    for r in records:
        if r.get("text") and r.get("time"):
            idx[r.get("tgid")].append((dt.datetime.fromisoformat(r["time"]), r.get("call"), r["text"]))
    for lines in idx.values():
        lines.sort(key=lambda x: x[0])
    return idx


def call_context(idx, tgid, start, call_key):
    """[[seconds before the call, text]] for the last few live lines on tgid before a call started."""
    out = [[round((start - t).total_seconds(), 1), text] for t, key, text in idx.get(tgid, [])
           if key != call_key and 0 <= (start - t).total_seconds() <= CONTEXT_MAX_AGE]
    return out[-CONTEXT_KEEP:]


def call_start(call):
    return dt.datetime.fromtimestamp(call.get("start_time_ms", call.get("start_time", 0) * 1000) / 1000)


def sample(d, tr_dir, n, min_dur, seed):
    """Add up to n random, not-yet-sampled transmissions from trunk-recorder's transcripts to clips.jsonl."""
    from tr_transcriber import load_call, transmissions, wav_of
    clips_path = os.path.join(d, "clips.jsonl")
    have = {c["id"] for c in read_jsonl(clips_path)}
    by_call = collections.OrderedDict()
    records = read_jsonl(os.path.join(tr_dir, "transcripts.jsonl"))
    for r in records:
        if r.get("call"):
            by_call.setdefault(r["call"], []).append(r)
    ctx_idx = context_index(records)
    cands = [(key, i) for key, recs in by_call.items() for i, r in enumerate(recs)
             if f"{os.path.basename(key)[:-5]}#{i}" not in have and (r.get("duration") or 0) >= min_dur]
    random.Random(seed).shuffle(cands)

    os.makedirs(os.path.join(d, "calls"), exist_ok=True)
    added = 0
    for key, i in cands:
        if added >= n:
            break
        src_json = os.path.join(tr_dir, "media", key)
        src_wav = wav_of(src_json)
        if not (os.path.exists(src_json) and os.path.exists(src_wav)):
            continue
        try:
            call = load_call(src_json)
            with wave.open(src_wav) as w:
                call_dur = w.getnframes() / w.getframerate()
        except (OSError, ValueError, EOFError, wave.Error):
            continue
        txs, recs = transmissions(call), by_call[key]
        if len(txs) != len(recs):  # transcribed by an older split; indexes wouldn't line up
            continue
        pos, length, rid, freq, errs = txs[i]
        if length is None:
            length = (txs[i + 1][0] if i + 1 < len(txs) else call_dur) - pos
        end = min(call_dur, pos + length)
        if end - pos < min_dur:
            continue
        base = os.path.basename(key)[:-5]
        for src, ext in ((src_json, ".json"), (src_wav, ".wav")):
            dst = os.path.join(d, "calls", base + ext)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
        r = recs[i]
        append_jsonl(clips_path, {
            "id": f"{base}#{i}", "call": f"calls/{base}.json", "wav": f"calls/{base}.wav", "tx": i,
            "n_tx": len(txs), "start": round(pos, 3), "end": round(end, 3), "time": r.get("time"),
            "tgid": call.get("talkgroup"), "tag": call.get("talkgroup_tag") or r.get("tag") or "",
            "rid": rid, "freq": freq, "error_count": errs,
            "draft": r.get("text") or "", "draft_model": r.get("model"), "source": key,
            "context": call_context(ctx_idx, call.get("talkgroup"), call_start(call), key),
            "call_drafts": [x.get("text") or "" for x in recs],
        })
        added += 1
    print(f"added {added} clips ({len(have) + added} in the set)"
          + (f"; only {added} eligible transmissions left" if added < n else ""))


# ---------------------------------------------------------------- labelling

class Player:
    def __init__(self):
        cmds = (["paplay"], ["pw-play"], ["aplay", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"])
        self.cmd = next((c for c in cmds if shutil.which(c[0])), None)
        self.proc = self.tmp = None

    def play(self, audio):
        self.stop()
        if not self.cmd:
            print("  (no audio player found: install pulseaudio-utils or alsa-utils)")
            return
        fd, self.tmp = tempfile.mkstemp(prefix="testset-", suffix=".wav")
        os.close(fd)
        write_wav(self.tmp, audio)
        self.proc = subprocess.Popen(self.cmd + [self.tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait()
        if self.tmp:
            try:
                os.unlink(self.tmp)
            except OSError:
                pass
        self.proc = self.tmp = None


_CMD = re.compile(r"(?::([rcnxsq])|^([rcnxsq]))\s*$", re.IGNORECASE)  # ":r" at the end, or a bare "r"


def label(d, blank):
    import readline  # noqa: F401  (gives input() line editing and the prefilled draft)
    pool = read_jsonl(os.path.join(d, "clips.jsonl"))
    labels = latest_labels(d)
    todo = [c for c in pool if c["id"] not in labels]
    if not todo:
        print(f"nothing to label ({len(labels)} labelled); add clips with: testset.py label --add 50")
        return
    print(LABEL_HELP)
    audio, player, done = CallAudio(d), Player(), 0
    try:
        for k, c in enumerate(todo, 1):
            clip = audio.clip(c)
            errs = c.get("error_count")
            print(f"\n[{k}/{len(todo)}] {c['tag'] or 'TG'} ({c['tgid']})"
                  + (f"  RID {c['rid']}" if c.get("rid") else "")
                  + f"  {c['end'] - c['start']:.1f}s  tx {c['tx'] + 1}/{c['n_tx']}"
                  + (f"  decode errors {errs}" if errs else "") + f"  {c['time'] or ''}")
            if blank:
                print(f"  live ({c['draft_model']}): {c['draft'] or '(empty)'}")
            player.play(clip)
            prefill = "" if blank else c["draft"]
            while True:
                readline.set_startup_hook(lambda: readline.insert_text(prefill))
                try:
                    line = input("> ")
                finally:
                    readline.set_startup_hook(None)
                m = _CMD.search(line)
                cmd, text = ((m.group(1) or m.group(2)).lower(), line[:m.start()].strip()) if m else (None, line.strip())
                if cmd in ("r", "c"):
                    player.play(clip if cmd == "r" else audio.call(c))
                    prefill = text
                    continue
                if cmd in ("s", "q"):
                    break
                if cmd == "n":
                    status, text = "noise", ""
                elif cmd == "x":
                    status, text = "unintelligible", ""
                elif not text:
                    print("  empty label; use :n if there's no speech")
                    prefill = ""
                    continue
                else:
                    status = "ok"
                append_jsonl(os.path.join(d, "labels.jsonl"), {
                    "id": c["id"], "status": status, "text": text,
                    "draft_shown": not blank, "labeled_at": dt.datetime.now().isoformat(timespec="seconds"),
                })
                done += 1
                break
            if cmd == "q":
                break
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        player.stop()
    print(f"labelled {done} this session; {len(labels) + done} of {len(pool)} clips done")


# ---------------------------------------------------------------- scoring

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
_ALIASES = {"okay": "ok", "xray": "x ray", "alright": "all right", "cellphone": "cell phone",
            "enroute": "en route", "nought": "0"}


def _small_number(toks, i):
    """Parse 'twenty six' / 'nine' / 'fifteen' at toks[i] -> (value, next index), or (None, i)."""
    w = toks[i]
    if w in _TENS:
        if i + 1 < len(toks) and 0 < _UNITS.get(toks[i + 1], 0) < 10:
            return _TENS[w] + _UNITS[toks[i + 1]], i + 2
        return _TENS[w], i + 1
    if w in _UNITS:
        return _UNITS[w], i + 1
    return None, i


def normalize(text):
    """Word list for WER: lowercase, no punctuation, number words -> digits, digit strings split per digit."""
    t = re.sub(r"\[[^\]]*\]", " ", text.lower())
    t = re.sub(r"[^a-z0-9' ]+", " ", t.replace("-", " "))
    toks = [a for w in t.split() for a in _ALIASES.get(w.strip("'"), w.strip("'")).split()]
    nums, i = [], 0
    while i < len(toks):
        v, j = _small_number(toks, i)
        if v is not None:
            if j < len(toks) and toks[j] == "hundred" and v < 10:  # "three hundred (and) eight"
                v, j = v * 100, j + 1
                k = j + 1 if j < len(toks) and toks[j] == "and" else j
                if k < len(toks):
                    rest, k2 = _small_number(toks, k)
                    if rest is not None and rest < 100:
                        v, j = v + rest, k2
            nums.append(str(v))
            i = j
        else:
            nums.append(toks[i])
            i += 1
    for i, w in enumerate(nums):  # "oh" is a zero only next to other digits: "six oh eight"
        if w == "oh" and any(0 <= k < len(nums) and nums[k].isdigit() for k in (i - 1, i + 1)):
            nums[i] = "0"
    return [c for w in nums for c in (list(w) if w.isdigit() else [w]) if c]


def align(ref, hyp):
    """Word-level edit counts (substitutions, deletions, insertions)."""
    n, m = len(ref), len(hyp)
    d = np.zeros((n + 1, m + 1), dtype=np.int32)
    d[:, 0], d[0, :] = np.arange(n + 1), np.arange(m + 1)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1, d[i - 1, j - 1] + (ref[i - 1] != hyp[j - 1]))
    s = dl = ins = 0
    i, j = n, m
    while i or j:
        if i and j and d[i, j] == d[i - 1, j - 1] + (ref[i - 1] != hyp[j - 1]):
            s += ref[i - 1] != hyp[j - 1]
            i, j = i - 1, j - 1
        elif i and d[i, j] == d[i - 1, j] + 1:
            dl, i = dl + 1, i - 1
        else:
            ins, j = ins + 1, j - 1
    return s, dl, ins


def parse_sets(items):
    out = {}
    for item in items:
        k, sep, v = item.partition("=")
        if not sep:
            sys.exit(f"--set expects key=value, got {item!r}")
        try:
            out[k] = ast.literal_eval(v)
        except (ValueError, SyntaxError):
            out[k] = v
    return out


def progress(msg):
    if sys.stderr.isatty():
        print(f"\r  {msg}", end="", file=sys.stderr, flush=True)


def run_whisper(d, clips, args):
    """{clip id: hypothesis} from transcribing the labelled clips with the given settings."""
    from tr_transcriber import Prompts, clean_text, load_call, load_model, split_words, transcribe, transmissions
    overrides = parse_sets(args.set)
    prompts = Prompts(None if args.prompt is not None else args.prompts, args.context, args.context_window)
    if args.prompt is not None:
        prompts.default = args.prompt

    def prompt_for(c, same_call=False):
        """The clip's talkgroup prompt plus live lines heard before its call (and, in clip mode, earlier in it)."""
        recent = [t for secs, t in c.get("context") or [] if secs <= args.context_window]
        if same_call:
            recent += [t for t in (c.get("call_drafts") or [])[:c["tx"]] if t]
        return prompts.build(c["tgid"], recent=recent)
    model = load_model(args.model, args.compute_type, args.threads)
    audio, hyps, t0 = CallAudio(d), {}, time.time()
    if args.mode == "clip":  # each transmission on its own
        for k, c in enumerate(clips, 1):
            segs = transcribe(model, audio.clip(c), prompt_for(c, same_call=True), args.beam_size, **overrides)
            hyps[c["id"]] = clean_text(" ".join(s.text.strip() for s in segs).strip())
            progress(f"{k}/{len(clips)} clips")
    else:  # whole call, split back into transmissions by word timestamps, as tr_transcriber.py does
        by_call = collections.OrderedDict()
        for c in clips:
            by_call.setdefault(c["call"], []).append(c)
        for k, (call_path, cs) in enumerate(by_call.items(), 1):
            call_audio = audio.call(cs[0])
            texts = [""] * cs[0]["n_tx"]
            if len(call_audio) / SR >= args.min_dur:
                segs = transcribe(model, call_audio, prompt_for(cs[0]), args.beam_size, word_timestamps=True, **overrides)
                texts = split_words(segs, transmissions(load_call(os.path.join(d, call_path))))
            for c in cs:
                hyps[c["id"]] = texts[c["tx"]]
            progress(f"{k}/{len(by_call)} calls")
    print(("\r" if sys.stderr.isatty() else "") + f"  transcribed in {time.time() - t0:.0f}s" + " " * 20, file=sys.stderr)
    return hyps


def evaluate(labels, pool, hyps):
    rows = []
    for l in labels:
        c = pool[l["id"]]
        ref, hyp = normalize(l["text"]), normalize(hyps[l["id"]])
        s, dl, ins = align(ref, hyp)
        rows.append({"id": l["id"], "status": l["status"], "tag": c["tag"], "error_count": c.get("error_count"),
                     "duration": round(c["end"] - c["start"], 2), "ref": l["text"], "hyp": hyps[l["id"]],
                     "words": len(ref), "sub": s, "del": dl, "ins": ins})
    return rows


def wer(rs):
    n = sum(r["words"] for r in rs)
    return (sum(r["sub"] + r["del"] + r["ins"] for r in rs) / n * 100) if n else None


def fmt(rs, nd=1):
    w = wer(rs)
    return f"{w:.{nd}f}%" if w is not None else "n/a (no speech)"


def current_labels(d):
    """(clips by id, labels to score, number left out as unintelligible)"""
    pool = {c["id"]: c for c in read_jsonl(os.path.join(d, "clips.jsonl"))}
    labels = [l for l in latest_labels(d).values() if l["id"] in pool]
    skipped = sum(l["status"] == "unintelligible" for l in labels)
    return pool, [l for l in labels if l["status"] != "unintelligible"], skipped


def compare(d, files):
    """Re-score saved runs against the current labels, on the clips every run covered."""
    files = files or sorted(glob.glob(os.path.join(d, "results", "*.jsonl")))
    pool, labels, _ = current_labels(d)
    runs = []
    for f in files:
        lines = read_jsonl(f)
        if lines:
            name = re.sub(r"^\d{8}-\d{6}-", "", os.path.basename(f)[:-len(".jsonl")])
            runs.append((name, lines[0].get("summary", ""), {r["id"]: r["hyp"] for r in lines[1:]}))
    if not runs:
        sys.exit("no results yet: run testset.py score")
    common = [l for l in labels if all(l["id"] in hyps for _, _, hyps in runs)]
    print(f"{len(common)} labelled clips scored in all {len(runs)} runs (current labels)\n")
    width = max(len(n) for n, _, _ in runs)
    print(f"{'run':<{width}}    WER    sub  del  ins  empty   <2s   2-5s    5s+")
    for name, summary, hyps in runs:
        rows = evaluate(common, pool, hyps)
        speech = [r for r in rows if r["status"] == "ok"]
        cols = [fmt([r for r in rows if lo <= r["duration"] < hi], 0) for lo, hi in ((0, 2), (2, 5), (5, 1e9))]
        print(f"{name:<{width}}  {fmt(rows):>6}  {sum(r['sub'] for r in rows):>4} {sum(r['del'] for r in rows):>4}"
              f" {sum(r['ins'] for r in rows):>4}  {sum(not r['hyp'] for r in speech):>5}  "
              + "  ".join(f"{c:>5}" for c in cols))


def score(d, args):
    pool, labels, skipped = current_labels(d)
    if not labels:
        sys.exit("no labelled clips yet: run testset.py label --add 50")
    clips = [pool[l["id"]] for l in labels]
    if args.drafts:
        hyps = {c["id"]: c["draft"] for c in clips}
        desc = f"live transcripts saved at sampling ({', '.join(sorted({str(c['draft_model']) for c in clips}))})"
    else:
        hyps = run_whisper(d, clips, args)
        desc = f"{args.model}, {args.mode} mode, beam {args.beam_size}" + "".join(f", {s}" for s in args.set) \
            + (f", prompt {args.prompt[:40]!r}" if args.prompt is not None else
               f", prompts {os.path.relpath(args.prompts) if os.path.exists(args.prompts) else 'DEFAULT_PROMPT'}") \
            + (f", context {args.context} lines/{args.context_window:.0f}s" if args.context else "")

    rows = evaluate(labels, pool, hyps)

    speech = [r for r in rows if r["status"] == "ok"]
    noise = [r for r in rows if r["status"] == "noise"]
    tot = {k: sum(r[k] for r in rows) for k in ("words", "sub", "del", "ins")}
    print(f"\n{desc}")
    print(f"{len(speech)} speech clips, {len(noise)} no-speech clips"
          + (f", {skipped} unintelligible left out" if skipped else ""))
    print(f"WER {fmt(rows)}   substitutions {tot['sub']}, deletions {tot['del']}, insertions {tot['ins']}"
          f" over {tot['words']} reference words")
    print(f"  clips with nothing transcribed: {sum(not r['hyp'] for r in speech)}/{len(speech)} speech clips")
    if noise:
        print(f"  no-speech clips with output:    {sum(bool(r['hyp']) for r in noise)}/{len(noise)}")
    clean = [r for r in rows if not r["error_count"]]
    errs = [r for r in rows if r["error_count"]]
    print(f"  by decode errors: none {fmt(clean)} (n={len(clean)}), some {fmt(errs)} (n={len(errs)})")
    by_tag = collections.defaultdict(list)
    for r in rows:
        by_tag[r["tag"] or "?"].append(r)
    print("  by talkgroup:", ", ".join(f"{t} {fmt(rs, 0)} (n={len(rs)})"
                                      for t, rs in sorted(by_tag.items(), key=lambda kv: -len(kv[1]))[:8]))
    worst = sorted((r for r in rows if r["sub"] + r["del"] + r["ins"]),
                   key=lambda r: -(r["sub"] + r["del"] + r["ins"]))[:args.show]
    if worst:
        print("\nmost errors:")
        for r in worst:
            print(f"  [{r['sub'] + r['del'] + r['ins']}] {r['id']}\n      ref: {r['ref'] or '(no speech)'}"
                  f"\n      hyp: {r['hyp'] or '(empty)'}")

    os.makedirs(os.path.join(d, "results"), exist_ok=True)
    name = re.sub(r"[^\w.-]+", "_", args.name or ("drafts" if args.drafts else f"{args.model}-{args.mode}"))
    out = os.path.join(d, "results", f"{dt.datetime.now():%Y%m%d-%H%M%S}-{name}.jsonl")
    with open(out, "w") as f:
        f.write(json.dumps({"summary": desc, "wer": None if wer(rows) is None else round(wer(rows), 2), **tot}) + "\n")
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"\nper-clip results: {os.path.relpath(out) if out.startswith(os.getcwd() + os.sep) else out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.path.join(ROOT, "testset"), help="test set directory")
    sub = ap.add_subparsers(dest="command", required=True)

    lp = sub.add_parser("label", help="label clips (optionally sampling more first)")
    lp.add_argument("--add", type=int, default=0, metavar="N", help="sample N more transmissions first")
    lp.add_argument("--blank", action="store_true",
                    help="start each prompt empty instead of prefilled with the live transcript (less anchoring)")
    lp.add_argument("--tr-dir", default=os.path.join(ROOT, "logs", "trunk-recorder"),
                    help="trunk-recorder output dir (media/ and transcripts.jsonl)")
    lp.add_argument("--min-dur", type=float, default=0.5, help="don't sample transmissions shorter than this")
    lp.add_argument("--seed", type=int, help="random seed for sampling")

    sp = sub.add_parser("score", help="report WER on the labelled clips")
    sp.add_argument("--drafts", action="store_true", help="score the saved live transcripts instead of running Whisper")
    sp.add_argument("--mode", choices=("call", "clip"), default="call",
                    help="call: whole call, split by word timestamps (tr_transcriber.py); "
                         "clip: each transmission alone")
    sp.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override a faster-whisper transcribe() option, e.g. vad_filter=False (repeatable)")
    sp.add_argument("--model", default=os.environ.get("WHISPER_MODEL", "large-v3"))
    sp.add_argument("--compute-type", default="int8")
    sp.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    sp.add_argument("--beam-size", type=int, default=5)
    sp.add_argument("--prompt", help="one initial prompt for every clip (default: per talkgroup from --prompts)")
    sp.add_argument("--prompts", default=os.path.join(ROOT, "config", "prompts.json"),
                    help="per-talkgroup prompts file (DEFAULT_PROMPT for all if it doesn't exist)")
    sp.add_argument("--context", type=int, default=0, metavar="N",
                    help="append the last N live lines from the clip's talkgroup to the prompt")
    sp.add_argument("--context-window", type=float, default=300.0, metavar="SEC",
                    help="only use context lines from this many seconds before the call")
    sp.add_argument("--min-dur", type=float, default=0.6, help="calls shorter than this get no transcript (as live)")
    sp.add_argument("--name", help="label for the results file")
    sp.add_argument("--show", type=int, default=10, help="how many worst clips to print")
    cp = sub.add_parser("compare", help="re-score saved runs against the current labels, side by side")
    cp.add_argument("files", nargs="*", help="results files (default: all in testset/results/)")
    args = ap.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    if args.command == "label":
        if args.add:
            sample(args.dir, args.tr_dir, args.add, args.min_dur, args.seed)
        label(args.dir, args.blank)
    elif args.command == "compare":
        compare(args.dir, args.files)
    else:
        score(args.dir, args)


if __name__ == "__main__":
    main()
