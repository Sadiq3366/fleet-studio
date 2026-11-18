"""Video production for Fleet Studio: 1080p main video (16:9 or 9:16), 2 vertical Shorts, the thumbnail."""
import os
import re
import subprocess

import numpy as np
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


def _lines(words, n):
    """Splits words into n balanced lines."""
    best, total = None, sum(len(w) for w in words)
    import itertools
    for cuts in itertools.combinations(range(1, len(words)), n - 1):
        parts = [words[a:b] for a, b in zip((0,) + cuts, cuts + (len(words),))]
        score = max(len(" ".join(p)) for p in parts)
        if best is None or score < best[0]:
            best = (score, parts)
    return best[1] if best else [words]


def _palette(img):
    small = img.convert("RGB").resize((64, 36))
    q = small.quantize(5)
    pal = q.getpalette()[:15]
    counts = sorted(q.getcolors(), reverse=True)
    cols = [tuple(pal[i * 3:i * 3 + 3]) for _, i in counts]
    # most colourful of the main colours
    return max(cols, key=lambda c: max(c) - min(c))


def thumbnail(work, text, bg_photo=None, ref_thumb=None, avatar=None, highlight=(), out=None, log=print, name=None,
              solid=False):
    """1280x720 viral thumbnail in the style/colours of the reference: photo, avatar with white outline, 4-8 big words."""
    from PIL import ImageEnhance, ImageOps
    W, H = 1280, 720
    out = out or os.path.join(work, "thumbnail.jpg")

    def cover(path):
        im = Image.open(path).convert("RGB")
        s = max(W / im.width, H / im.height)
        im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
        return im.crop(((im.width - W) // 2, (im.height - H) // 2, (im.width - W) // 2 + W, (im.height - H) // 2 + H))

    ref = cover(ref_thumb) if ref_thumb and os.path.isfile(ref_thumb) else None
    if solid:                                             # clean solid colour, no picture behind the text
        palette = [(14, 14, 18), (165, 0, 24), (8, 38, 105), (70, 12, 105), (0, 82, 66)]
        col = palette[sum(map(ord, text or "x")) % len(palette)] if solid is True else solid
        bg = Image.new("RGB", (W, H), col)
        ref = None
    elif bg_photo and os.path.isfile(bg_photo):
        bg = cover(bg_photo)
        if ref is not None:                               # take over the reference colour mood
            bg = Image.blend(bg, Image.new("RGB", (W, H), _palette(ref)), 0.22)
    elif ref is not None:
        bg = ImageOps.mirror(ref).filter(ImageFilter.GaussianBlur(3))
    else:
        bg = Image.new("RGB", (W, H), (25, 25, 40))
    if not solid:
        bg = ImageEnhance.Color(bg).enhance(1.35)
        bg = ImageEnhance.Contrast(bg).enhance(1.18)
        # vignette + dark text side
        mask = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(mask)
        for x in range(0, W, 4):
            d.rectangle([x, 0, x + 4, H], fill=int(175 * max(0.0, 1 - x / (W * 0.7))))
        bg = Image.composite(Image.new("RGB", (W, H)), bg, mask)
        vig = Image.new("L", (W, H), 0)
        ImageDraw.Draw(vig).ellipse([-W * 0.2, -H * 0.3, W * 1.2, H * 1.3], fill=255)
        bg = Image.composite(bg, Image.new("RGB", (W, H)), vig.filter(ImageFilter.GaussianBlur(120)))

    text_w = W * 0.92
    if avatar and os.path.isfile(avatar):
        sp = Image.open(avatar).convert("RGBA")
        box = sp.getchannel("A").getbbox()
        sp = sp.crop(box) if box else sp
        h = int(H * 0.98)
        sp = sp.resize((max(1, round(sp.width * h / sp.height)), h), Image.LANCZOS)
        if sp.width > W * 0.5:
            sp = sp.resize((int(W * 0.5), round(sp.height * W * 0.5 / sp.width)), Image.LANCZOS)
        a = sp.getchannel("A")
        outline = a.filter(ImageFilter.MaxFilter(17))
        glow = a.filter(ImageFilter.MaxFilter(31)).filter(ImageFilter.GaussianBlur(18))
        x, y = W - sp.width + int(sp.width * 0.04), H - sp.height
        bg.paste(Image.new("RGB", sp.size, _palette(ref) if ref is not None else (255, 200, 0)), (x, y), glow)
        bg.paste(Image.new("RGB", sp.size, (255, 255, 255)), (x, y), outline)
        bg.paste(sp, (x, y), sp)
        text_w = min(text_w, x - 60)
        if name:                                      # bold name tag on the avatar: "WALTER RISO"
            d0 = ImageDraw.Draw(bg)
            nf = _font(54)
            label = name.upper()
            tw = d0.textlength(label, font=nf)
            cx = min(W - tw / 2 - 30, x + sp.width / 2)
            bx = [cx - tw / 2 - 22, H - 104, cx + tw / 2 + 22, H - 30]
            d0.rounded_rectangle([bx[0] + 6, bx[1] + 6, bx[2] + 6, bx[3] + 6], radius=16, fill=(0, 0, 0))
            d0.rounded_rectangle(bx, radius=16, fill=(225, 25, 45), outline=(255, 255, 255), width=4)
            d0.text((cx - tw / 2, bx[1] + 8), label, font=nf, fill=(255, 255, 255))

    text = re.sub(r"[^\w\s¿?¡!.,'’-]", " ", text or "")      # no emojis / = / symbols on the thumbnail
    words = text.upper().split()[:7]
    if words:
        d = ImageDraw.Draw(bg)
        clean = lambda w: w.strip(",.!?¡¿:;\"'").upper()
        hl = {clean(x) for h in (highlight or []) for x in str(h).split()} & {clean(w) for w in words}
        hl = set(sorted(hl, key=len, reverse=True)[:1]) or {clean(max(words, key=lambda w: len(clean(w))))}
        n = 1 if len(words) <= 2 else 2 if len(words) <= 5 else 3
        lines = _lines(words, n)
        size = 190
        while size > 48:
            f = _font(size)
            if max(d.textlength(" ".join(l), font=f) for l in lines) <= text_w and len(lines) * size * 1.18 <= H * 0.86:
                break
            size -= 4
        f = _font(size)
        y = max(30, (H - len(lines) * size * 1.18) / 2 - 30)
        sw = max(6, size // 10)
        sh = Image.new("L", (W, H), 0)                # soft drop shadow behind the words
        ds = ImageDraw.Draw(sh)
        yy = y
        for l in lines:
            ds.text((52, yy + 8), " ".join(l), font=f, fill=200, stroke_width=sw + 4, stroke_fill=200)
            yy += size * 1.18
        bg.paste(Image.new("RGB", (W, H)), (0, 0), sh.filter(ImageFilter.GaussianBlur(10)))
        for l in lines:
            x = 46
            for w in l:
                tw = d.textlength(w, font=f)
                if clean(w) in hl:              # key word: yellow on a red box
                    bx = d.textbbox((x, y), w, font=f, stroke_width=sw)
                    d.rounded_rectangle([bx[0] - 8, bx[1] - 8, bx[2] + 8, bx[3] + 8], radius=14, fill=(220, 20, 40))
                    d.text((x, y), w, font=f, fill=(255, 230, 0), stroke_width=sw, stroke_fill=(0, 0, 0))
                else:
                    d.text((x, y), w, font=f, fill=(255, 255, 255), stroke_width=sw, stroke_fill=(0, 0, 0))
                x += tw + d.textlength(" ", font=f)
            y += size * 1.18
    bg.save(out, "JPEG", quality=92)
    if os.path.getsize(out) > 1_900_000:
        bg.save(out, "JPEG", quality=80)
    log("thumbnail made")
    return out


# ------------------------------------------------------------------ avatar cutout
def cutout(path, work, log=print):
    """Avatar photo with a background (brick wall, room...) -> transparent PNG of just the person (rembg, free).
    Files that are already transparent are kept as they are."""
    if not path or not os.path.isfile(path):
        return path
    im = Image.open(path).convert("RGBA")
    alpha = np.asarray(im.getchannel("A"))
    if (alpha < 200).mean() > 0.05:                 # already a real cutout
        return path
    out = os.path.join(work, "avatar_cutout.png")
    if os.path.isfile(out):
        return out
    try:
        from rembg import new_session, remove
        log("  removing the avatar background (rembg)...")
        res = remove(im.convert("RGB"), session=new_session("u2net_human_seg"), post_process_mask=True)
        a = np.asarray(res.getchannel("A"))
        if (a > 128).mean() < 0.03:                 # model found no person -> general model
            res = remove(im.convert("RGB"), session=new_session("isnet-general-use"), post_process_mask=True)
        box = res.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
        if box:
            res = res.crop(box)
        res.save(out)
        return out
    except Exception as e:                          # noqa: BLE001
        log(f"  background removal not available ({str(e)[:120]}) - using the avatar as it is")
        return path
