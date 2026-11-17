"""Video production for Fleet Studio: 1080p main video (16:9 or 9:16), 2 vertical Shorts, the thumbnail."""
import os
import re
import subprocess

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _exe(name):
    local = os.path.join(ROOT, "ffmpeg", name + (".exe" if os.name == "nt" else ""))
    return local if os.path.isfile(local) else name


FFMPEG, FFPROBE = _exe("ffmpeg"), _exe("ffprobe")


def _run(cmd, log):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=NO_WINDOW)
    if p.returncode:
        log(p.stderr[-1500:])
        raise RuntimeError("ffmpeg failed")
    return p


def duration(path):
    p = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def _speaker_png(src, out, max_h):
    """Trims the empty border of the transparent cutout and sizes it."""
    im = Image.open(src).convert("RGBA")
    box = im.getchannel("A").getbbox()
    if box:
        im = im.crop(box)
    im = im.resize((max(1, round(im.width * max_h / im.height)), max_h), Image.LANCZOS)
    im.save(out)
    return out


def main_video(work, audio, out, aspect="16:9", bg_video=None, bg_image=None, speaker=None, log=print):
    """Background (reference video muted & looped, or the image with a slow pan) + full audio + speaker bottom-left."""
    W, H = (1920, 1080) if aspect == "16:9" else (1080, 1920)
    cmd = [FFMPEG, "-y", "-hide_banner"]
    if bg_video:
        cmd += ["-stream_loop", "-1", "-i", bg_video]
        bg = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=30[bg]"
              if aspect == "16:9" else
              f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"boxblur=24:2,eq=brightness=-0.08[bl];[b]scale={W}:-2[fg];[bl][fg]overlay=0:(H-h)/2,setsar=1,fps=30[bg]")
    else:
        img = bg_image or _blank(work, W, H)
        cmd += ["-loop", "1", "-framerate", "30", "-i", img]
        bw, bh = int(W * 1.12) // 2 * 2, int(H * 1.12) // 2 * 2
        bg = (f"[0:v]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
              f"crop={W}:{H}:x='(iw-ow)/2+(iw-ow)/2*sin(t/17)':y='(ih-oh)/2+(ih-oh)/2*cos(t/23)',setsar=1[bg]")
    cmd += ["-i", audio]
    graph, last = bg, "bg"
    if speaker:
        sp = _speaker_png(speaker, os.path.join(work, "speaker_sized.png"), int(H * (0.62 if aspect == "16:9" else 0.36)))
        cmd += ["-loop", "1", "-i", sp]
        graph += f";[{last}][2:v]overlay=x={int(W * 0.02)}:y=H-h:shortest=1[v]"
        last = "v"
    cmd += ["-filter_complex", graph, "-map", f"[{last}]", "-map", "1:a", "-shortest",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    log(f"rendering {W}x{H} ({aspect}) ...")
    _run(cmd, log)
    return out


def _blank(work, W, H):
    p = os.path.join(work, "blank.png")
    Image.new("RGB", (W, H), (18, 18, 24)).save(p)
    return p


def _silences(audio):
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", audio, "-af", "silencedetect=n=-32dB:d=0.35", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return [float(m) for m in re.findall(r"silence_end: ([\d.]+)", p.stderr)]


def shorts(work, main, audio, count=2, length=58, log=print):
    """Cuts `count` vertical Shorts (<60 s) out of the main video, starting right after a pause in the speech."""
    total = duration(main)
    if total < 20:
        log("video too short for Shorts - skipped")
        return []
    length = min(length, total - 1)
    pauses = _silences(audio)
    outs = []
    for i in range(count):
        want = total * (0.12 + 0.76 * i / max(1, count - 1)) if count > 1 else total * 0.15
        want = min(want, total - length)
        near = [s for s in pauses if abs(s - want) < 20 and s + length <= total]
        start = min(near, key=lambda s: abs(s - want)) if near else max(0.0, want)
        # end at a pause too, so the Short does not stop mid-word
        ends = [s for s in pauses if start + 30 <= s <= start + length]
        dur = (max(ends) - start) if ends else length
        out = os.path.join(work, f"short_{i + 1}.mp4")
        vf = ("split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2,"
              "eq=brightness=-0.08[bl];[b]scale=1080:-2[fg];[bl][fg]overlay=0:(H-h)/2,setsar=1")
        log(f"short {i + 1}: {start:.0f}s -> {start + dur:.0f}s")
        _run([FFMPEG, "-y", "-hide_banner", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", main, "-vf", vf,
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out], log)
        outs.append(out)
    return outs


# ------------------------------------------------------------------ thumbnail
def _font(size):
    for f in (os.path.join(HERE, "Anton-Regular.ttf"), r"C:\Windows\Fonts\impact.ttf", r"C:\Windows\Fonts\arialbd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.isfile(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def _hook(title, max_chars=26):
    """Short, punchy text for the thumbnail from the video title (whole words, no dangling 'the/of/te...')."""
    t = re.sub(r"[\(\[].*?[\)\]]|#\S+|\|.*$", "", title).strip(" -:|")
    out = []
    for w in t.split():
        if len(" ".join(out + [w])) > max_chars:
            break
        out.append(w)
    while len(out) > 2 and len(out[-1].strip(",.;:!?¿¡")) <= 3:
        out.pop()
    return " ".join(out).upper()


def thumbnail(work, title, base_image=None, speaker=None, out=None, log=print):
    """1280x720 high-contrast thumbnail: background image, big 2-line text with stroke, speaker cutout on the right."""
    W, H = 1280, 720
    out = out or os.path.join(work, "thumbnail.jpg")
    if base_image and os.path.isfile(base_image):
        bg = Image.open(base_image).convert("RGB")
        s = max(W / bg.width, H / bg.height)
        bg = bg.resize((round(bg.width * s), round(bg.height * s)), Image.LANCZOS)
        bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2, (bg.width - W) // 2 + W, (bg.height - H) // 2 + H))
    else:
        bg = Image.new("RGB", (W, H), (20, 20, 30))
    bg = bg.filter(ImageFilter.GaussianBlur(2))
    shade = Image.new("L", (W, H))
    ImageDraw.Draw(shade).rectangle([0, 0, W, H], fill=0)
    for x in range(W):                                   # dark on the text side (left) -> readable text
        ImageDraw.Draw(shade).line([(x, 0), (x, H)], fill=int(170 * max(0, 1 - x / (W * 0.75))))
    bg = Image.composite(Image.new("RGB", (W, H)), bg, shade)
    if speaker and os.path.isfile(speaker):
        sp = Image.open(speaker).convert("RGBA")
        box = sp.getchannel("A").getbbox()
        sp = sp.crop(box) if box else sp
        h = int(H * 0.95)
        sp = sp.resize((max(1, round(sp.width * h / sp.height)), h), Image.LANCZOS)
        glow = sp.getchannel("A").filter(ImageFilter.GaussianBlur(14))
        x = W - sp.width + int(sp.width * 0.08)
        bg.paste(Image.new("RGB", sp.size, (255, 255, 255)), (x, H - h), glow)
        bg.paste(sp, (x, H - h), sp)
    text = _hook(title)
    if text:
        d = ImageDraw.Draw(bg)
        words, lines = text.split(), []
        half = (len(words) + 1) // 2 if len(words) > 2 else len(words)
        lines = [" ".join(words[:half]), " ".join(words[half:])] if len(words) > 2 else [text]
        lines = [l for l in lines if l]
        size = 150
        while size > 50:
            f = _font(size)
            if max(d.textlength(l, font=f) for l in lines) <= W * 0.58:
                break
            size -= 6
        f = _font(size)
        y = (H - len(lines) * size * 1.1) / 2
        for n, l in enumerate(lines):
            d.text((48, y), l, font=f, fill=(255, 221, 0) if n == 0 else (255, 255, 255),
                   stroke_width=max(4, size // 14), stroke_fill=(0, 0, 0))
            y += size * 1.1
    bg.save(out, "JPEG", quality=90)
    log("thumbnail made")
    return out
