"""Audio -> sentences with word times (faster-whisper, free, CPU) and viral word-by-word subtitles (ASS)."""
import os
import re


def transcribe(audio, duration, log=print, groq_key=None):
    if groq_key:
        try:
            return _groq(audio, duration, groq_key, log)
        except Exception as e:                          # noqa: BLE001
            log(f"  Groq speech-to-text failed ({str(e)[:150]}) - using the local model")
    return _local(audio, duration, log)


def _groq_send(path, key, model):
    import json
    import urllib.error
    import urllib.request
    import uuid
    bnd = uuid.uuid4().hex
    fields = [("model", model), ("response_format", "verbose_json"),
              ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment"), ("temperature", "0")]
    body = b"".join(f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields)
    with open(path, "rb") as f:
        body += (f'--{bnd}\r\nContent-Disposition: form-data; name="file"; filename="a.mp3"\r\n'
                 f'Content-Type: audio/mpeg\r\n\r\n').encode() + f.read() + f"\r\n--{bnd}--\r\n".encode()
    req = urllib.request.Request("https://api.groq.com/openai/v1/audio/transcriptions", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "User-Agent": "fleet-studio/1.0",
                                          "Content-Type": f"multipart/form-data; boundary={bnd}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:            # keep Groq's own explanation for the log
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None


def _groq(audio, duration, key, log):
    """Whisper large-v3-turbo on Groq (free key): ~30 s for 45 min of audio. Sent in pieces; a refused piece is
    retried smaller and with the second Whisper model."""
    import subprocess
    import tempfile
    import time

    import render
    log(f"speech to text (Groq whisper-large-v3-turbo, {duration / 60:.1f} min audio)...")
    tmp = tempfile.mkdtemp()
    out, lang = [], None

    def cut(t0, length, name):
        p = os.path.join(tmp, name)
        # -vn / -map_metadata -1: no cover picture or tags in the piece (Groq refuses mp3 files with pictures)
        subprocess.run([render.FFMPEG, "-v", "error", "-y", "-ss", str(t0), "-t", str(length), "-i", audio,
                        "-vn", "-map_metadata", "-1", "-ac", "1", "-ar", "16000", "-b:a", "48k", p], check=True)
        return p

    def piece(t0, length, depth=0):
        p = cut(t0, length, f"p{int(t0)}_{int(length)}.mp3")
        err = None
        for model in ("whisper-large-v3-turbo", "whisper-large-v3"):
            for n in range(3):
                try:
                    return [(t0, _groq_send(p, key, model))]
                except RuntimeError as e:
                    err = str(e)
                    if "HTTP 429" in err:                 # free limit per minute -> wait and retry
                        time.sleep(20 * (n + 1))
                        continue
                    break
        if length > 300 and depth < 2:                  # refused -> send it in smaller pieces
            log(f"  Groq refused a {length / 60:.0f}-min piece ({err[:160]}) - sending smaller pieces")
            half = length / 2
            return piece(t0, half, depth + 1) + piece(t0 + half, length - half, depth + 1)
        raise RuntimeError(err or "Groq failed")

    results = []
    step = 20 * 60
    t = 0.0
    while duration - t > 0.5:
        results += piece(t, min(step, duration - t))
        t += step
    for t0, j in results:
        lang = lang or j.get("language")
        words = [{"w": w["word"].strip(), "s": w["start"] + t0, "e": w["end"] + t0} for w in j.get("words", []) if w["word"].strip()]
        wi = 0
        for sg in j.get("segments", []):
            a, b = sg["start"] + t0, sg["end"] + t0
            ws = []
            while wi < len(words) and words[wi]["s"] < b - 0.01:
                ws.append(words[wi])
                wi += 1
            _punctuate(ws, sg["text"])
            out.append({"start": a, "end": b, "text": sg["text"].strip(), "words": ws,
                        "nsp": sg.get("no_speech_prob", 0.0), "lp": sg.get("avg_logprob", 0.0)})
        if wi < len(words) and out:
            out[-1]["words"] += words[wi:]
    names = {"spanish": "es", "english": "en", "hindi": "hi", "urdu": "ur", "portuguese": "pt", "french": "fr",
             "german": "de", "italian": "it", "arabic": "ar", "turkish": "tr", "indonesian": "id", "russian": "ru"}
    lang = names.get((lang or "").lower(), (lang or "")[:2].lower() or None)
    log(f"  language: {lang}, {len(out)} sentences")
    return out, lang


def _norm(w):
    return re.sub(r"[^\w]", "", w.lower())


def _punctuate(words, text):
    """Groq's word list has no punctuation; copy it from the segment text so sentences can be found."""
    toks = text.split()
    j = 0
    for w in words:
        k = j
        while k < len(toks) and k < j + 4 and _norm(toks[k]) != _norm(w["w"]):
            k += 1
        if k < len(toks) and _norm(toks[k]) == _norm(w["w"]):
            w["w"] = toks[k]
            j = k + 1


def _local(audio, duration, log):
    from faster_whisper import WhisperModel
    size = os.environ.get("WHISPER_MODEL") or ("small" if duration <= 8 * 60 else "base")
    log(f"speech to text (whisper {size}, {duration / 60:.1f} min audio)...")
    model = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    import subprocess
    import numpy as np
    import render
    raw = subprocess.run([render.FFMPEG, "-v", "error", "-i", audio, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout          # ffmpeg decodes any format, no PyAV issues
    pcm = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    segs, info = model.transcribe(pcm, word_timestamps=True, vad_filter=True, beam_size=1,
                                  condition_on_previous_text=False)
    out, last = [], 0
    for s in segs:
        words = [{"w": w.word.strip(), "s": w.start, "e": w.end} for w in (s.words or []) if w.word.strip()]
        out.append({"start": s.start, "end": s.end, "text": s.text.strip(), "words": words,
                    "nsp": s.no_speech_prob, "lp": s.avg_logprob})
        if s.end - last > 300:
            last = s.end
            log(f"  {s.end / 60:.0f} min done")
    log(f"  language: {info.language}, {len(out)} sentences")
    return out, info.language


# ------------------------------------------------------------------ scenes
END = (".", "?", "!", "…", "。", "؟", "।")


def sentences(segments):
    """Real sentences (split at . ? ! using exact word times), even when one sentence is spread over 2 segments.
    Without any punctuation (some languages/engines), long runs are cut at the longest pause instead."""
    out, cur = [], []

    def flush():
        if cur:
            out.append({"start": cur[0]["s"], "end": cur[-1]["e"], "text": " ".join(x["w"] for x in cur), "words": list(cur)})
            cur.clear()

    for seg in segments:
        ws = seg.get("words") or []
        if not ws:
            flush()
            out.append(dict(seg))
            continue
        for w in ws:
            if cur and len(cur) >= 30:                     # no full stop for a long time -> cut at the biggest pause
                gaps = [(cur[k + 1]["s"] - cur[k]["e"], k) for k in range(8, len(cur) - 4)]
                if gaps:
                    k = max(gaps)[1]
                    rest = cur[k + 1:]
                    del cur[k + 1:]
                    flush()
                    cur.extend(rest)
            cur.append(w)
            if w["w"].endswith(END) and len(cur) >= 3:
                flush()
    flush()
    return out


def scenes_from(segments, duration):
    """One stock clip per sentence. Very short sentences are joined; long videos use somewhat longer scenes so the
    free Pexels budget lasts (1 h -> about 6 s per scene)."""
    min_len = max(2.0, min(8.0, duration / 600))
    scenes, cur = [], None
    for s in sentences(segments):
        if cur is None:
            cur = {"start": 0.0 if not scenes else scenes[-1]["end"], "end": s["end"], "text": s["text"]}
        else:
            cur["end"], cur["text"] = s["end"], cur["text"] + " " + s["text"]
        if cur["end"] - cur["start"] >= min_len:
            scenes.append(cur)
            cur = None
    if cur:
        if scenes and cur["end"] - cur["start"] < min_len / 2:
            scenes[-1]["end"], scenes[-1]["text"] = cur["end"], scenes[-1]["text"] + " " + cur["text"]
        else:
            scenes.append(cur)
    if not scenes:
        scenes = [{"start": 0.0, "end": duration, "text": ""}]
    scenes[0]["start"] = 0.0
    scenes[-1]["end"] = duration
    for a, b in zip(scenes, scenes[1:]):            # no gaps
        a["end"] = b["start"]
    # very long single scenes (music / silence) are split so the picture keeps changing
    out = []
    for s in scenes:
        n = max(1, round((s["end"] - s["start"]) / 20))
        step = (s["end"] - s["start"]) / n
        for i in range(n):
            out.append({"start": s["start"] + i * step, "end": s["start"] + (i + 1) * step, "text": s["text"]})
    return out


# ------------------------------------------------------------------ subtitles
def _t(x):
    x = max(0.0, x)
    return f"{int(x // 3600)}:{int(x % 3600 // 60):02d}:{x % 60:05.2f}"


def _clean(w):
    return w.replace("{", "(").replace("}", ")").replace("\\", "/")


def ass(segments, out, W, H, t0=0.0, t1=None, left_margin=0, max_words=None):
    """Big bold subtitles: 3-5 words per line, the spoken word in yellow. Times are shifted by -t0."""
    vertical = H > W
    size = int(H * (0.068 if vertical else 0.085))
    max_words = max_words or (3 if vertical else 5)
    mv = int(H * (0.30 if vertical else 0.07))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: S,Anton,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,{max(3, size // 12)},{max(2, size // 25)},2,{left_margin + 40},40,{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    t1 = t1 if t1 is not None else 1e9
    words = []                                       # all words of the window, across segments
    for s in segments:
        if s["end"] <= t0 or s["start"] >= t1:
            continue
        ws = [w for w in s["words"] if w["e"] > t0 and w["s"] < t1]
        if not ws and s["text"]:                     # no word times -> spread the sentence evenly
            toks = s["text"].split()
            step = (s["end"] - s["start"]) / max(1, len(toks))
            ws = [{"w": x, "s": s["start"] + i * step, "e": s["start"] + (i + 1) * step} for i, x in enumerate(toks)]
        words += ws
    groups, sent = [], []                            # per sentence, then balanced lines of <= N words
    for j, w in enumerate(words):
        sent.append(w)
        if w["w"].endswith(END) or j == len(words) - 1:
            n = -(-len(sent) // max_words)
            per = -(-len(sent) // n)
            groups += [sent[x:x + per] for x in range(0, len(sent), per)]
            sent = []
    # fixed position (no stacking / no jumping), never two lines on screen at once
    cx = (left_margin + 40 + W - 40) // 2
    pos = "{\\an2\\pos(%d,%d)}" % (cx, H - mv)
    for g, grp in enumerate(groups):
        nxt = groups[g + 1][0]["s"] if g + 1 < len(groups) else 1e9
        g_end = min(grp[-1]["e"] + 0.25, nxt)
        for k, w in enumerate(grp):
            a = grp[0]["s"] if k == 0 else w["s"]
            b = grp[k + 1]["s"] if k + 1 < len(grp) else g_end
            a, b = max(a, t0) - t0, min(b, t1) - t0
            if b - a < 0.02:
                continue
            txt = " ".join(("{\\c&H00E5FF&}" + _clean(x["w"]).upper() + "{\\c&HFFFFFF&}") if j == k
                           else _clean(x["w"]).upper() for j, x in enumerate(grp))
            lines.append(f"Dialogue: 0,{_t(a)},{_t(b)},S,,0,0,0,,{pos}{txt}")
    with open(out, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + "\n")
    return out


def ff_path(p):
    """Path escaped for the ffmpeg subtitles= filter."""
    p = os.path.abspath(p).replace("\\", "/")
    return re.sub(r"([:'\[\],;])", r"\\\1", p)


# ------------------------------------------------------------------ tighten the voice track
# Whisper "hears" these in pure music; at the very end of a file they are almost never real narration.
_GHOST = re.compile(r"^(thank(s| you)( so much)?( for (watching|listening))?|please subscribe|subscribe|bye|"
                    r"gracias( por ver)?|suscr[ií]bete|obrigad[oa]|merci|danke|ok(ay)?|music|you)[.!? ]*$", re.I)


def _ghost(s, prev_end, segs):
    text = s["text"].strip(" .!?♪")
    if not text or "♪" in s["text"] or _GHOST.match(text):
        return True
    seg = [x for x in segs if x["end"] > s["start"] and x["start"] < s["end"]]     # whisper's own confidence
    if seg and min(x.get("nsp", 0) for x in seg) > 0.6 or seg and max(x.get("lp", 0) for x in seg) < -1.2:
        return True
    return s["start"] - prev_end > 8 and len(text.split()) < 6      # a lone short phrase after a long gap


def plan_cuts(segments, total, cut_middle=150.0, min_len=15 * 60, log=print):
    """Parts of the audio to KEEP: from the first word to the last real word (the music-only intro/outro of the
    reference - often claimed by Content ID - goes), and, for long videos, one block of whole paragraphs of about
    `cut_middle` seconds is removed from the middle. Cuts are only made in the pause between two sentences."""
    sents = [s for s in sentences(segments) if s.get("text", "").strip()]
    if not sents:
        return [(0.0, total)]
    while len(sents) > 1 and _ghost(sents[-1], sents[-2]["end"], segments):     # music at the end heard as words
        log(f"  ignoring '{sents[-1]['text'][:60]}' at {sents[-1]['start'] / 60:.1f} min (music, not narration)")
        sents.pop()
    a = max(0.0, sents[0]["start"] - 0.4) if sents[0]["start"] > 2.5 else 0.0
    b = min(total, sents[-1]["end"] + 1.2)
    keep = [(a, b)]
    if cut_middle and b - a >= min_len:
        lo_t, hi_t = a + (b - a) * 0.25, a + (b - a) * 0.80
        # pause after each sentence; long pause = end of a paragraph
        bounds = [((s["end"] + n["start"]) / 2, n["start"] - s["end"]) for s, n in zip(sents, sents[1:])
                  if lo_t <= s["end"] <= hi_t]
        lo, hi = cut_middle * 0.75, cut_middle * 1.25
        best = None
        for i, (t1, g1) in enumerate(bounds):
            for t2, g2 in bounds[i + 1:]:
                d = t2 - t1
                if d > hi:
                    break
                if d >= lo:
                    sc = min(g1, 1.5) + min(g2, 1.5) - abs(d - cut_middle) / 120
                    if best is None or sc > best[0]:
                        best = (sc, t1, t2)
        if best:
            _, t1, t2 = best
            keep = [(a, t1), (t2, b)]
            log(f"  cutting {t2 - t1:.0f} s of whole paragraphs at {t1 / 60:.1f}-{t2 / 60:.1f} min")
    gone = total - sum(y - x for x, y in keep)
    if gone > 0.5:
        log(f"  voice track: {total / 60:.1f} min -> {(total - gone) / 60:.1f} min "
            f"(silence/music at start {a:.0f} s, after last word {total - b:.0f} s)")
    return keep


def shift_segments(segments, keep):
    """Word/segment times re-mapped onto the shortened audio; words inside a removed part are dropped."""
    def mapt(t):
        off = 0.0
        for x, y in keep:
            if t < x:
                return None
            if t <= y:
                return off + t - x
            off += y - x
        return None

    out = []
    for s in segments:
        ws = []
        for w in s.get("words") or []:
            ns, ne = mapt(w["s"]), mapt(w["e"])
            if ns is not None and ne is not None:
                ws.append(dict(w, s=ns, e=ne))
        if s.get("words"):
            if not ws:
                continue
            text = s["text"] if len(ws) == len(s["words"]) else " ".join(w["w"] for w in ws)
            out.append(dict(s, start=ws[0]["s"], end=ws[-1]["e"], text=text, words=ws))
        else:
            ns, ne = mapt(s["start"]), mapt(s["end"])
            if ns is not None and ne is not None:
                out.append(dict(s, start=ns, end=ne))
    return out
