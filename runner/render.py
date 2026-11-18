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


# ------------------------------------------------------------------ viral thumbnail v2 (reference style)
def _serif(size):
    for f in (os.path.join(HERE, "PlayfairDisplay-Black.ttf"), r"C:\Windows\Fonts\georgiab.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", r"C:\Windows\Fonts\timesbd.ttf"):
        if os.path.isfile(f):
            return ImageFont.truetype(f, size)
    return _font(size)


def _split_lines(words, n):
    """n lines with about the same number of letters (keeps short words like DE/LO with the next one)."""
    total = sum(len(w) for w in words) + len(words)
    out, cur, cur_len = [], [], 0
    for i, w in enumerate(words):
        cur.append(w)
        cur_len += len(w) + 1
        left_lines = n - len(out) - 1
        if left_lines > 0 and cur_len >= total / n and len(words) - i - 1 >= left_lines:
            out.append(cur)
            cur, cur_len = [], 0
    if cur:
        out.append(cur)
    return out


def thumbnail_v2(work, text, avatar=None, name=None, out=None, log=print, seed=""):
    """Reference style: big avatar LEFT with a warm glow, a whole hook sentence RIGHT in 4-6 full-width lines with
    alternating colours (white / black on yellow bar / white on red bar / yellow), the name in serif under it."""
    W, H = 1280, 720
    out = out or os.path.join(work, "thumbnail.jpg")
    themes = [((10, 8, 6), (255, 130, 10)), ((14, 6, 6), (255, 60, 20)), ((12, 10, 4), (255, 190, 0)),
              ((6, 10, 24), (40, 140, 255)), ((16, 6, 4), (255, 95, 0))]
    base, glow_col = themes[sum(map(ord, seed or text or "x")) % len(themes)]
    bg = Image.new("RGB", (W, H), base)
    col_x = int(W * 0.47)                              # text column starts here
    # warm light behind the avatar (no picture, just light)
    glow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glow).ellipse([-W * 0.12, -H * 0.1, W * 0.58, H * 1.15], fill=200)
    bg = Image.composite(Image.new("RGB", (W, H), glow_col), bg, glow.filter(ImageFilter.GaussianBlur(140)))
    if avatar and os.path.isfile(avatar):
        sp = Image.open(avatar).convert("RGBA")
        box = sp.getchannel("A").getbbox()
        sp = sp.crop(box) if box else sp
        h = int(H * 1.02)
        sp = sp.resize((max(1, round(sp.width * h / sp.height)), h), Image.LANCZOS)
        maxw = int(W * 0.52)
        if sp.width > maxw:                            # wide photo: keep the face, cut the sides
            l = (sp.width - maxw) // 2
            sp = sp.crop((l, 0, l + maxw, sp.height))
        a = sp.getchannel("A")
        rim = a.filter(ImageFilter.MaxFilter(13))
        halo = a.filter(ImageFilter.MaxFilter(41)).filter(ImageFilter.GaussianBlur(26))
        x = max(-20, int(col_x - sp.width + 30))
        y = H - sp.height + 10
        bg.paste(Image.new("RGB", sp.size, glow_col), (x, y), halo)
        bg.paste(Image.new("RGB", sp.size, (255, 255, 255)), (x, y), rim)
        bg.paste(sp, (x, y), sp)
    d = ImageDraw.Draw(bg)
    words = re.sub(r"[^\w\s¿?¡!.,'’-]", " ", text or "").upper().split()[:14]
    name_h = 84 if name else 0
    if words:
        col_w = W - col_x - 34
        n = max(2, min(6, -(-len(words) // 2)))        # about 2 words per line -> big letters
        lines = _lines(words, min(n, len(words)))
        for k in range(len(lines) - 1, 0, -1):           # no line that is only a tiny word ("QUE", "DE", "LA")
            if len(lines[k]) == 1 and len(lines[k][0]) <= 3:
                lines[k - 1] = lines[k - 1] + lines[k]
                lines.pop(k)
            elif len(lines[k - 1]) == 1 and len(lines[k - 1][0]) <= 3:
                lines[k] = lines[k - 1] + lines[k]
                lines.pop(k - 1)
        styles = ["white", "yellowbar", "white", "redbar", "yellow", "white"]
        # every line as big as the column allows (like the reference), then all scaled to fit the height
        sizes = []
        for l in lines:
            s, t = 40, " ".join(l)
            while s < 190 and d.textlength(t, font=_font(s + 4)) <= col_w - 24:
                s += 4
            sizes.append(s)
        med = sorted(sizes)[len(sizes) // 2]
        sizes = [min(sz, int(med * 1.25)) for sz in sizes]   # a lone short word must not become huge
        gap = 10
        total = sum(sizes) * 1.04 + gap * (len(lines) - 1)
        avail = H - 40 - name_h - 20
        if total > avail:
            k = avail / total
            sizes = [max(36, int(s * k)) for s in sizes]
            total = sum(sizes) * 1.04 + gap * (len(lines) - 1)
        y = 24 + max(0, (avail - total) / 2)
        for i, (l, s) in enumerate(zip(lines, sizes)):
            f, t = _font(s), " ".join(l)
            tw = d.textlength(t, font=f)
            x = col_x + (col_w - tw) / 2
            st = styles[i % len(styles)]
            sw = max(4, s // 11)
            bx = d.textbbox((x, y), t, font=f)
            if st in ("yellowbar", "redbar"):
                pad = max(8, s // 10)
                d.rectangle([bx[0] - pad, bx[1] - pad // 2, bx[2] + pad, bx[3] + pad // 2],
                            fill=(255, 214, 0) if st == "yellowbar" else (210, 16, 32))
                d.text((x, y), t, font=f, fill=(0, 0, 0) if st == "yellowbar" else (255, 255, 255),
                       stroke_width=0 if st == "yellowbar" else 2, stroke_fill=(0, 0, 0))
            else:
                d.text((x + 5, y + 6), t, font=f, fill=(0, 0, 0), stroke_width=sw, stroke_fill=(0, 0, 0))
                d.text((x, y), t, font=f, fill=(255, 214, 0) if st == "yellow" else (255, 255, 255),
                       stroke_width=sw, stroke_fill=(0, 0, 0))
            y += s * 1.04 + gap
    if name:                                           # "Walter" white + "Riso" yellow, serif, yellow underline
        parts = name.split()
        first, last = " ".join(parts[:-1]) or parts[0], parts[-1] if len(parts) > 1 else ""
        f = _serif(64)
        tw = d.textlength(first + (" " + last if last else ""), font=f)
        x = col_x + (W - col_x - 34 - tw) / 2
        y = H - name_h - 8
        d.rectangle([x - 14, y + 70, x + tw + 14, y + 78], fill=(255, 214, 0))
        d.text((x, y), first, font=f, fill=(255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0))
        if last:
            d.text((x + d.textlength(first + " ", font=f), y), last, font=f, fill=(255, 214, 0),
                   stroke_width=3, stroke_fill=(0, 0, 0))
    bg.save(out, "JPEG", quality=92)
    log("thumbnail made")
    return out


def audio_length(path, log=print):
    """Real length of the audio, measured by decoding it (very fast). Some MP3s claim a wrong length in their header
    (e.g. '44 min' while only 9 min of sound are inside) - trusting the header made empty pieces and wasted time."""
    claimed = duration(path)
    try:
        p = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0", "-f", "null", "-",
                            "-progress", "pipe:1"], capture_output=True, text=True, timeout=900, creationflags=NO_WINDOW)
        ms = [int(x) for x in re.findall(r"out_time_us=(\d+)", p.stdout)]
        real = (max(ms) / 1e6) if ms else 0.0
    except Exception:                                  # noqa: BLE001
        real = 0.0
    if real > 1 and abs(real - claimed) > 5:
        log(f"  NOTE: the audio file says {claimed / 60:.1f} min but really contains {real / 60:.1f} min - "
            f"using {real / 60:.1f} min (check your MP3 if this is not what you expected)")
    return real if real > 1 else claimed
