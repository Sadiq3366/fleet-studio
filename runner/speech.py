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


def _groq(audio, duration, key, log):
    """Whisper large-v3-turbo on Groq (free key): ~20 s for 20 min of audio. Sent in 20-minute pieces."""
    import json
    import subprocess
    import tempfile
    import urllib.request
    import uuid

    import render
    log(f"speech to text (Groq whisper-large-v3-turbo, {duration / 60:.1f} min audio)...")
    piece, out, lang = 20 * 60, [], None
    tmp = tempfile.mkdtemp()
    for n, t0 in enumerate(range(0, int(duration) + 1, piece)):
        if duration - t0 < 0.5:
            break
        p = os.path.join(tmp, f"p{n}.mp3")                 # small mono file (Groq free limit is 25 MB)
        subprocess.run([render.FFMPEG, "-v", "error", "-y", "-ss", str(t0), "-t", str(piece), "-i", audio,
                        "-ac", "1", "-ar", "16000", "-b:a", "48k", p], check=True)
        bnd = uuid.uuid4().hex
        fields = [("model", "whisper-large-v3-turbo"), ("response_format", "verbose_json"),
                  ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment"), ("temperature", "0")]
        body = b"".join(f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields)
        with open(p, "rb") as f:
            body += (f'--{bnd}\r\nContent-Disposition: form-data; name="file"; filename="a.mp3"\r\n'
                     f'Content-Type: audio/mpeg\r\n\r\n').encode() + f.read() + f"\r\n--{bnd}--\r\n".encode()
        req = urllib.request.Request("https://api.groq.com/openai/v1/audio/transcriptions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {key}", "User-Agent": "fleet-studio/1.0",
                                              "Content-Type": f"multipart/form-data; boundary={bnd}"})
        with urllib.request.urlopen(req, timeout=300) as r:
            j = json.loads(r.read())
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
            out.append({"start": a, "end": b, "text": sg["text"].strip(), "words": ws})
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
        out.append({"start": s.start, "end": s.end, "text": s.text.strip(), "words": words})
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
    for s in segments:
        words = [w for w in s["words"] if w["e"] > t0 and w["s"] < t1]
        if not words and s["text"] and s["end"] > t0 and s["start"] < t1:     # no word times -> whole sentence
            words = [{"w": x, "s": s["start"] + i * (s["end"] - s["start"]) / max(1, len(s["text"].split())),
                      "e": s["start"] + (i + 1) * (s["end"] - s["start"]) / max(1, len(s["text"].split()))}
                     for i, x in enumerate(s["text"].split())]
        groups, sent = [], []                         # split per sentence, then into balanced lines of <= N words
        for j, w in enumerate(words):
            sent.append(w)
            if w["w"].endswith(END) or j == len(words) - 1:
                n = -(-len(sent) // max_words)
                size = -(-len(sent) // n)
                groups += [sent[x:x + size] for x in range(0, len(sent), size)]
                sent = []
        for grp in groups:
            for k, w in enumerate(grp):
                a = w["s"] if k else grp[0]["s"]
                b = grp[k + 1]["s"] if k + 1 < len(grp) else grp[-1]["e"] + 0.15
                a, b = max(a, t0) - t0, min(b, t1) - t0
                if b <= a:
                    continue
                txt = " ".join(("{\\c&H00E5FF&\\fscx108\\fscy108}" + _clean(x["w"]).upper() + "{\\r}") if j == k
                               else _clean(x["w"]).upper() for j, x in enumerate(grp))
                pop = "{\\fad(40,0)}" if k == 0 else ""
                lines.append(f"Dialogue: 0,{_t(a)},{_t(b)},S,,0,0,0,,{pop}{txt}")
    with open(out, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + "\n")
    return out


def ff_path(p):
    """Path escaped for the ffmpeg subtitles= filter."""
    p = os.path.abspath(p).replace("\\", "/")
    return re.sub(r"([:'\[\],;])", r"\\\1", p)
