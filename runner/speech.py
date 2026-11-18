"""Audio -> sentences with word times (faster-whisper, free, CPU) and viral word-by-word subtitles (ASS)."""
import os
import re


def transcribe(audio, duration, log=print):
    from faster_whisper import WhisperModel
    size = os.environ.get("WHISPER_MODEL") or ("small" if duration <= 20 * 60 else "base")
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
    """Whisper segments can hold 2-3 sentences -> split them at . ? ! using the exact word times."""
    out = []
    for seg in segments:
        ws = seg.get("words") or []
        if not ws:
            out.append(dict(seg))
            continue
        cur = []
        for w in ws:
            cur.append(w)
            if w["w"].endswith(END) and len(cur) >= 3:
                out.append({"start": cur[0]["s"], "end": cur[-1]["e"], "text": " ".join(x["w"] for x in cur), "words": cur})
                cur = []
        if cur:
            if out and len(cur) < 3 and out[-1]["words"] and out[-1]["words"][-1] in ws:   # tiny tail -> previous
                out[-1]["words"] += cur
                out[-1].update(end=cur[-1]["e"], text=out[-1]["text"] + " " + " ".join(x["w"] for x in cur))
            else:
                out.append({"start": cur[0]["s"], "end": cur[-1]["e"], "text": " ".join(x["w"] for x in cur), "words": cur})
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
