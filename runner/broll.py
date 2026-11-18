"""Stock clips from Pexels for every scene, cut to the speech, joined into one timeline (any length: 1 h, 3 h ...)."""
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

import numpy as np

import render
import speech

API = "https://api.pexels.com/videos/search"


class Pexels:
    def __init__(self, key, cache, log=print):
        self.key, self.cache, self.log, self.locale = key, cache, log, None
        self.results, self.used, self.calls = {}, {}, 0
        os.makedirs(cache, exist_ok=True)

    def search(self, q, orientation):
        k = (q.lower(), orientation)
        if k in self.results:
            return self.results[k]
        if self.calls >= int(os.environ.get("PEXELS_MAX_CALLS", "180")):    # free plan: 200 requests / hour
            return []
        args = {"query": q, "orientation": orientation, "per_page": 15, "size": "medium"}
        if self.locale:                               # search words are not English -> Pexels searches in that language
            args["locale"] = self.locale
        url = API + "?" + urllib.parse.urlencode(args)
        for n in range(3):
            try:
                req = urllib.request.Request(url, headers={"Authorization": self.key, "User-Agent": "fleet-studio/1.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    vids = json.loads(r.read()).get("videos", [])
                self.calls += 1
                break
            except Exception as e:                    # noqa: BLE001
                self.log(f"  pexels '{q}': {str(e)[:80]}")
                time.sleep(5 * (n + 1))
        else:
            vids = []
        self.results[k] = [v for v in vids if v.get("duration", 0) >= 3]
        return self.results[k]

    def photo(self, q):
        """A big stock photo for the thumbnail."""
        try:
            url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode({"query": q, "orientation": "landscape", "per_page": 5})
            req = urllib.request.Request(url, headers={"Authorization": self.key, "User-Agent": "fleet-studio/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                photos = json.loads(r.read()).get("photos", [])
            if not photos:
                return None
            p = os.path.join(self.cache, "..", "thumb_photo.jpg")
            req = urllib.request.Request(photos[0]["src"]["large2x"], headers={"User-Agent": "fleet-studio/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r, open(p, "wb") as o:
                o.write(r.read())
            return p
        except Exception as e:                        # noqa: BLE001
            self.log(f"  thumbnail photo: {str(e)[:80]}")
            return None

    def pick(self, queries, orientation, need_w):
        """First clip not used yet (else the least used one) for the first query with results."""
        for q in queries:
            vids = self.search(q, orientation)
            if not vids:
                continue
            vids = sorted(vids, key=lambda v: self.used.get(v["id"], 0))
            v = vids[0]
            self.used[v["id"]] = self.used.get(v["id"], 0) + 1
            return self.download(v, need_w), v.get("duration", 10), self.used[v["id"]]
        return None, 0, 0

    def download(self, v, need_w):
        files = [f for f in v.get("video_files", []) if f.get("link") and f.get("width") and f.get("file_type") == "video/mp4"]
        if not files:
            return None
        big = [f for f in files if min(f["width"], f["height"]) >= min(need_w, 1080) and max(f["width"], f["height"]) <= 2560]
        f = min(big, key=lambda f: f["width"]) if big else max(files, key=lambda f: f["width"])
        p = os.path.join(self.cache, f"{v['id']}_{f['width']}.mp4")
        if not os.path.isfile(p):
            for n in range(3):
                try:
                    req = urllib.request.Request(f["link"], headers={"User-Agent": "fleet-studio/1.0"})
                    with urllib.request.urlopen(req, timeout=120) as r, open(p + ".part", "wb") as o:
                        while True:
                            b = r.read(1 << 20)
                            if not b:
                                break
                            o.write(b)
                    os.replace(p + ".part", p)
                    break
                except Exception as e:                # noqa: BLE001
                    self.log(f"  clip download: {str(e)[:80]}")
                    time.sleep(3)
        return p if os.path.isfile(p) else None


def plan(scenes, queries, topic_q, px, aspect, log=print, report=None):
    """Attach the best-matching clip to every scene.
    queries[i] = 1-3 search phrases (specific -> general). With the CLIP matcher every candidate clip is judged by its
    preview frames against the spoken sentence; without it the first unused search result is taken."""
    orient = "landscape" if aspect == "16:9" else "portrait"
    need = 1920 if aspect == "16:9" else 1080
    try:
        import match
        m = match.Matcher(log)
    except Exception as e:                            # noqa: BLE001
        m = None
        log(f"  visual matching not available ({str(e)[:100]}) - using search order")
    seen = {}                                         # every clip found so far (pool for when the API budget is used up)
    prev_id, scores, lines = None, [], []
    sent_emb = m.text([s["text"] or topic_q for s in scenes]) if m else None
    for i, (s, qs) in enumerate(zip(scenes, queries)):
        qs = [q for q in (qs if isinstance(qs, list) else [qs]) if q] or [topic_q]
        pool = {}
        budget = int(os.environ.get("PEXELS_MAX_CALLS", "180")) - px.calls
        k = max(1, min(3, budget // max(1, len(scenes) - i)))      # spread the free API budget over all scenes
        for q in qs[:k] + [topic_q]:
            for v in px.search(q, orient):
                pool.setdefault(v["id"], v)
                seen.setdefault(v["id"], v)
            if len(pool) >= 30 or (q != topic_q and len(pool) >= 15 and not m):
                break
        if len(pool) < 8:                             # API budget used up -> best of everything found so far
            pool.update(dict(list(seen.items())[-80:]))
        if not pool:
            s.update(clip=None, clip_dur=0, query=qs[0], offset=0)
            continue
        cands = list(pool.values())
        d = s["end"] - s["start"]
        if m:
            emb = m.images(cands)
            q_emb = m.text(qs)
            slugs = [match.slug(v) for v in cands]
            sl_emb = m.text([x or "video" for x in slugs])
            best, best_sc = None, -9
            for v, sl, se in zip(cands, slugs, sl_emb):
                e = emb.get(v["id"])
                if e is None:
                    continue
                sc = 0.45 * float(e @ sent_emb[i]) + 0.35 * float(np.max(q_emb @ e)) + 0.20 * float(se @ sent_emb[i])
                sc -= 0.015 * px.used.get(v["id"], 0) + (0.05 if v["id"] == prev_id else 0)
                sc += 0.01 if v.get("duration", 0) >= d else 0
                if sc > best_sc:
                    best, best_sc = v, sc
            v = best or cands[0]
            scores.append(best_sc)
        else:
            v = sorted(cands, key=lambda c: (px.used.get(c["id"], 0), c["id"] == prev_id))[0]
        px.used[v["id"]] = px.used.get(v["id"], 0) + 1
        clip = px.download(v, need)
        dur = v.get("duration", 10)
        s.update(clip=clip, clip_dur=dur, query=qs[0],
                 offset=((px.used[v["id"]] - 1) * 4.0) % max(1.0, dur - d) if dur else 0)
        prev_id = v["id"]
        lines.append(f"{s['start']:7.1f}s  [{(scores[-1] if m and scores else 0):.3f}]  {s['text'][:90]}\n"
                     f"           search: {' | '.join(qs)}\n           clip:   {match.slug(v) if m else v.get('url')}  {v.get('url')}\n")
        if (i + 1) % 25 == 0:
            log(f"  clips {i + 1}/{len(scenes)} (pexels calls {px.calls}"
                + (f", match score {np.mean(scores[-25:]):.3f})" if scores else ")"))
    if report:
        with open(report, "w", encoding="utf-8") as f:
            f.write("Which clip was chosen for which sentence (score: higher = better match)\n\n" + "\n".join(lines))
    if scores:
        log(f"  average match score {np.mean(scores):.3f}")
    return scenes


def _seg(s, out, W, H, still, log, slow=False):
    d = s["end"] - s["start"]
    zoom = f",scale={int(W * 1.06)}:{int(H * 1.06)},crop={W}:{H}:'(iw-ow)/2+(iw-ow)/2*sin(t/3)':'(ih-oh)/2'" if slow else ""
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}{zoom},setsar=1,fps=30,format=yuv420p"
    if s.get("clip"):
        inp = (["-stream_loop", "-1"] if s.get("clip_dur", 0) < d + s.get("offset", 0) + 0.5 else []) + \
              ["-ss", f"{s.get('offset', 0):.2f}", "-i", s["clip"]]
    else:
        inp = ["-loop", "1", "-i", still]
        vf = (f"scale={int(W * 1.12)}:{int(H * 1.12)}:force_original_aspect_ratio=increase,crop={int(W * 1.12)}:{int(H * 1.12)},"
              f"crop={W}:{H}:'(iw-ow)/2+(iw-ow)/2*sin(t/9)':'(ih-oh)/2+(ih-oh)/2*cos(t/11)',setsar=1,fps=30,format=yuv420p")
    render._run([render.FFMPEG, "-y", "-hide_banner", *inp, "-t", f"{d:.3f}", "-vf", vf, "-an",
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "19", "-r", "30", out], log)


def compose(work, scenes, segments, audio, out, aspect, still, avatar, t0=0.0, t1=None, log=print, tag="main"):
    """Clips for [t0,t1] + audio + subtitles + avatar bottom-left -> final mp4."""
    W, H = (1920, 1080) if aspect == "16:9" else (1080, 1920)
    t1 = t1 if t1 is not None else scenes[-1]["end"]
    d = os.path.join(work, f"seg_{tag}")
    os.makedirs(d, exist_ok=True)
    parts = []
    for i, s in enumerate(scenes):
        a, b = max(s["start"], t0), min(s["end"], t1)
        if b - a < 0.05:
            continue
        p = os.path.join(d, f"{len(parts):05d}.mp4")
        _seg(dict(s, start=a, end=b), p, W, H, still, log)
        parts.append(p)
        if len(parts) % 40 == 0:
            log(f"  {tag}: {len(parts)} scenes cut")
    lst = os.path.join(d, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.writelines(f"file '{p.replace(os.sep, '/')}'\n" for p in parts)
    base = os.path.join(d, "base.mp4")
    render._run([render.FFMPEG, "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", base], log)

    av_h = int(H * (0.42 if aspect == "16:9" else 0.24))
    av_w = 0
    cmd = [render.FFMPEG, "-y", "-hide_banner", "-i", base, "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", audio]
    graph, last = "[0:v]null[v0]", "v0"
    if avatar:
        sp = render._speaker_png(avatar, os.path.join(work, f"avatar_{tag}.png"), av_h)
        from PIL import Image
        av_w = Image.open(sp).width
        cmd += ["-loop", "1", "-i", sp]
        graph += f";[{last}][2:v]overlay=x={int(W * 0.015)}:y=H-h:shortest=1[v1]"
        last = "v1"
    if segments:
        sub = speech.ass(segments, os.path.join(work, f"subs_{tag}.ass"), W, H, t0, t1,
                         left_margin=av_w if aspect == "16:9" else 0)
        graph += f";[{last}]subtitles='{speech.ff_path(sub)}':fontsdir='{speech.ff_path(render.HERE)}'[v2]"
        last = "v2"
    long_video = (t1 - t0) > 40 * 60
    cmd += ["-filter_complex", graph, "-map", f"[{last}]", "-map", "1:a", "-shortest",
            "-c:v", "libx264", "-preset", "superfast" if long_video else "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    log(f"  {tag}: final {W}x{H}, {(t1 - t0) / 60:.1f} min ...")
    render._run(cmd, log)
    for p in parts:
        os.remove(p)
    return out


def pick_short_windows(segments, duration, count=2, length=58):
    """Best moments for Shorts: start at a sentence start, end at a sentence end, 30-58 s, spread over the video."""
    if duration < 35:
        return []
    wins = []
    for i in range(count):
        want = duration * (0.10 + 0.70 * i / max(1, count - 1)) if count > 1 else duration * 0.1
        starts = [s["start"] for s in segments if s["start"] + 30 <= duration]
        if not starts:
            break
        a = min(starts, key=lambda x: abs(x - want))
        ends = [s["end"] for s in segments if a + 25 <= s["end"] <= a + length]
        b = max(ends) if ends else min(duration, a + length)
        if all(b <= x or a >= y for x, y in wins):
            wins.append((a, b))
    return wins


def probe_ok(path):
    p = subprocess.run([render.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return p.returncode == 0
