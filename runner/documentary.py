"""Documentary mode: real archive photos & footage of the people/places/events in the narration.

For every sentence:
  1. AI (whole script) -> who is it about (main subject / other person / nobody), year, archive search words, stock words
  2. candidates from Wikimedia Commons (photos + film, with dates), Openverse (free CC photos), Pexels (generic b-roll)
  3. every candidate is judged: name in the file (for person sentences), CLIP picture-vs-sentence score,
     year of the photo vs year in the sentence, not used before
  4. photos get a slow Ken Burns move, film is cut to the sentence
Ideas taken from make_video.py (Wikimedia first, person priority, era matching, content blocklist) - rebuilt for speed.
"""
import json
import os
import random
import re
import time
import urllib.parse
import urllib.request

import numpy as np

UA = os.environ.get("MEDIA_USER_AGENT") or "FleetStudio/1.0 (open-source documentary video tool)"

BLOCKED = re.compile(r"\b(" + "|".join(re.escape(k) for k in [
    "bikini", "swimsuit", "swimwear", "lingerie", "underwear", "panties", "thong", "nude", "naked", "nudity", "topless",
    "sexy", "seductive", "erotic", "cleavage", "miniskirt", "mini skirt", "short skirt", "crop top", "beachwear",
    "pin-up", "pinup", "lad mag", "swim trunks", "bra", "boxer briefs", "underpants", "undressed", "provocative",
    "hot pants", "short shorts", "sensual", "glamour model", "corpse", "autopsy", "gore"]) + r")\b", re.I)
JUNK = re.compile(r"\b(logo|icon|signature|map of|diagram|chart|flag of|coat of arms|stamp|coin|poster template|"
                  r"screenshot|svg|seal of|emblem)\b", re.I)


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _clean_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _year(s):
    m = re.search(r"\b(1[89]\d\d|20[0-3]\d)\b", s or "")
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------ sources
class Sources:
    def __init__(self, log=print):
        self.log, self.cache = log, {}

    def commons(self, query, kind="image", limit=30):
        """Wikimedia Commons search. kind = image | video. Returns candidate dicts."""
        key = ("c", query.lower(), kind)
        if key in self.cache:
            return self.cache[key]
        ft = "filetype:bitmap" if kind == "image" else "filetype:video"
        params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": "6",
                  "gsrsearch": f"{query} {ft}", "gsrlimit": str(limit), "prop": "imageinfo",
                  "iiprop": "url|mime|size|extmetadata", "iiurlwidth": "1920",
                  "iiextmetadatafilter": "ImageDescription|DateTimeOriginal|ObjectName|Artist|LicenseShortName|Categories"}
        out = []
        for host in ("https://commons.wikimedia.org/w/api.php", "https://en.wikipedia.org/w/api.php"):
            try:
                j = _get(host + "?" + urllib.parse.urlencode(params))
                break
            except Exception as e:                       # noqa: BLE001
                j = {}
                self.log(f"  commons '{query}': {str(e)[:80]}")
                time.sleep(2)
        for p in (j.get("query", {}).get("pages", {}) or {}).values():
            ii = (p.get("imageinfo") or [{}])[0]
            md = ii.get("extmetadata") or {}
            title = p.get("title", "").replace("File:", "")
            desc = _clean_html((md.get("ImageDescription") or {}).get("value", ""))[:400]
            cats = _clean_html((md.get("Categories") or {}).get("value", ""))[:400]
            text = f"{title} {desc} {cats}"
            if BLOCKED.search(text) or JUNK.search(title):
                continue
            mime, w, h = ii.get("mime", ""), ii.get("width", 0), ii.get("height", 0)
            date = _clean_html((md.get("DateTimeOriginal") or {}).get("value", ""))
            name = re.sub(r"\.[a-z0-9]+$", "", title, flags=re.I)[:70]
            artist = _clean_html((md.get("Artist") or {}).get("value", ""))[:50] or "Wikimedia Commons"
            lic = (md.get("LicenseShortName") or {}).get("value", "see Commons")
            credit = f"{name} - {artist} ({lic})"
            if kind == "image":
                if mime not in ("image/jpeg", "image/png", "image/webp") or w < 400 or h < 300:
                    continue
                src = ii.get("thumburl") or ii.get("url")
                prev = re.sub(r"/\d+px-", "/400px-", src) if "/thumb/" in src else src
                out.append({"id": "c" + str(p.get("pageid")), "kind": "image", "src": src, "preview": [prev],
                            "text": text, "year": _year(date) or _year(title), "credit": credit, "w": w, "h": h,
                            "source": "commons"})
            else:
                if not mime.startswith(("video/", "application/ogg")):
                    continue
                dur = float(ii.get("duration") or 0) or 30.0
                if dur < 3:
                    continue
                prev = ii.get("thumburl")
                out.append({"id": "c" + str(p.get("pageid")), "kind": "video", "src": ii.get("url"),
                            "preview": [re.sub(r"/\d+px-", "/400px-", prev)] if prev else [], "dur": dur,
                            "text": text, "year": _year(date) or _year(title), "credit": credit, "source": "commons"})
        self.cache[key] = out
        return out

    def openverse(self, query, limit=20):
        """Free CC photos (Flickr etc.) usable commercially, with attribution."""
        key = ("o", query.lower())
        if key in self.cache:
            return self.cache[key]
        out = []
        try:
            j = _get("https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(
                {"q": query, "page_size": limit, "license_type": "commercial,modification", "mature": "false"}))
            for r in j.get("results", []):
                text = f"{r.get('title', '')} {' '.join(t.get('name', '') for t in r.get('tags') or [])}"
                if BLOCKED.search(text) or not r.get("url") or (r.get("width") or 999) < 400:
                    continue
                out.append({"id": "o" + r["id"], "kind": "image", "src": r["url"],
                            "preview": [r.get("thumbnail") or r["url"]], "text": text, "year": _year(text),
                            "credit": f"{r.get('title', '')[:70]} - {r.get('creator') or 'unknown'} "
                                      f"({(r.get('license') or '').upper()} {r.get('license_version') or ''}, {r.get('source')})",
                            "source": "openverse"})
        except Exception as e:                           # noqa: BLE001
            self.log(f"  openverse '{query}': {str(e)[:80]}")
        self.cache[key] = out
        return out


def pexels_cands(px, q, orient):
    out = []
    for v in px.search(q, orient) if px else []:
        pics = [p.get("picture") for p in v.get("video_pictures") or [] if p.get("picture")]
        prev = ([pics[len(pics) // 3]] if pics else []) + ([v["image"].split("?")[0] + "?auto=compress&w=400"] if v.get("image") else [])
        out.append({"id": "p" + str(v["id"]), "kind": "video", "src": None, "pexels": v, "preview": prev,
                    "dur": v.get("duration", 10), "text": re.sub(r"[-/]", " ", v.get("url", "")), "year": None,
                    "credit": None, "source": "pexels"})
    return out


# ------------------------------------------------------------------ the plan
def _names_in(text, names):
    """Whole-word name match; all-caps nicknames (MJ) must match in capitals."""
    for n in names:
        if not n:
            continue
        flags = 0 if (n.isupper() and len(n) <= 4) else re.I
        if re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", text, flags):
            return True
    return False


def _aliases(subject, extra):
    """Full name + real nicknames. A bare surname ('Jackson') is NOT used alone - it matched 'Jackson Lake'."""
    parts = {p.lower() for p in subject.split()}
    out = [subject] if subject else []
    for a in extra or []:
        a = (a or "").strip()
        if not a or a.lower() == subject.lower():
            continue
        if " " not in a and a.lower() in parts:
            continue
        if len(a) >= 4 or (a.isupper() and len(a) >= 2):
            out.append(a)
    return out


def plan(scenes, info, per, px, aspect, log=print, report=None):
    """info = whole-script analysis (subject, aliases, people). per[i] = {who, year, archive, stock} for scene i."""
    import match
    m = match.Matcher(log)
    src = Sources(log)
    orient = "landscape" if aspect == "16:9" else "portrait"
    subject = info.get("subject") or ""
    aliases = _aliases(subject, info.get("aliases"))

    # person pools, built once (like the character prefetch in make_video.py)
    pools = {}

    def person_pool(name, names):
        if name in pools:
            return pools[name]
        c = []
        for q in (name, f"{name} portrait", f"{name} photograph", f"{name} concert", f"{name} interview"):
            c += [x for x in src.commons(q, "image") if _names_in(x["text"], names)]
        c += [x for x in src.commons(name, "video") if _names_in(x["text"], names)]
        c += [x for x in src.openverse(name) if _names_in(x["text"], names)]
        uniq = {x["id"]: x for x in c}
        pools[name] = list(uniq.values())
        log(f"  {name}: {sum(1 for x in pools[name] if x['kind'] == 'image')} real photos, "
            f"{sum(1 for x in pools[name] if x['kind'] == 'video')} real film clips found")
        return pools[name]

    if subject and info.get("subject_type", "person") == "person":
        person_pool(subject, aliases)
    sent_emb = m.text([s["text"] or subject for s in scenes])
    used, prev, lines, scores, credits = {}, None, [], [], {}
    for i, (s, p) in enumerate(zip(scenes, per)):
        who = (p.get("who") or "").strip()
        is_subject = who and subject and (who.lower() == "subject" or who.lower() == subject.lower())
        names = aliases if is_subject else ([who] if who and who.lower() not in ("none", "nobody") else [])
        cands = []
        if names:
            cands += person_pool(subject if is_subject else who, names if is_subject else _aliases(who, []))
            if p.get("archive"):
                cands += [x for x in src.commons(p["archive"], "image") if _names_in(x["text"], names)]
        if p.get("archive"):
            cands += src.commons(p["archive"], "image", 20)
            cands += src.commons(p["archive"], "video", 10)
            if not names:
                cands += src.openverse(p["archive"], 15)
        if p.get("stock") and (not names or len(cands) < 6):
            cands += pexels_cands(px, p["stock"], orient)
        if not cands and px:
            cands += pexels_cands(px, info.get("topic_stock") or "documentary", orient)
        cands = list({c["id"]: c for c in cands}.values())
        if not cands:
            s.update(clip=None, image=None)
            continue
        emb = m.images(cands)
        q_emb = m.text([x for x in (p.get("archive"), p.get("stock")) if x] or [s["text"]])
        year = p.get("year")
        best, best_sc = None, -9
        for c in cands:
            e = emb.get(c["id"])
            if e is None:
                continue
            sc = 0.55 * float(e @ sent_emb[i]) + 0.30 * float(np.max(q_emb @ e))
            if names and _names_in(c["text"], names):
                sc += 0.08                                # really shows the person named in the sentence
            if year and c.get("year"):
                gap = abs(int(year) - c["year"])
                sc += 0.05 if gap <= 2 else 0.02 if gap <= 6 else -0.04 if gap > 15 else 0
            if c["source"] != "pexels" and (names or p.get("archive")):
                sc += 0.02                                # real archive over stock for named things
            sc += 0.01 if c["kind"] == "video" else 0
            sc -= 0.03 * used.get(c["id"], 0) + (0.10 if c["id"] == prev else 0)
            if sc > best_sc:
                best, best_sc = c, sc
        c = best or cands[0]
        used[c["id"]] = used.get(c["id"], 0) + 1
        prev = c["id"]
        scores.append(best_sc)
        d = s["end"] - s["start"]
        if c["kind"] == "image":
            s.update(clip=None, image=c["src"], kb=random.choice(["in", "out", "left", "right"]))
        else:
            link = px.link(c["pexels"], 1920 if aspect == "16:9" else 1080) if c["source"] == "pexels" else c["src"]
            dur = c.get("dur") or 10
            s.update(clip=link, clip_dur=dur, pexels=c.get("pexels"), image=None,
                     offset=((used[c["id"]] - 1) * 5.0) % max(1.0, dur - d))
        if c.get("credit"):
            credits[c["id"]] = c["credit"]
        lines.append(f"{s['start']:7.1f}s [{best_sc:.3f}] {s['text'][:90]}\n"
                     f"         who={who or '-'} year={year or '-'} archive='{p.get('archive', '')}' stock='{p.get('stock', '')}'\n"
                     f"         -> {c['source']} {c['kind']}: {c['text'][:110]}\n")
        if (i + 1) % 25 == 0:
            log(f"  scenes {i + 1}/{len(scenes)}, match score {np.mean(scores[-25:]):.3f}")
    if report:
        with open(report, "w", encoding="utf-8") as f:
            f.write(f"Documentary media plan - subject: {subject}\n\n" + "\n".join(lines))
    if scores:
        log(f"  average match score {np.mean(scores):.3f}; "
            f"{sum(1 for s in scenes if s.get('image'))} photos, {sum(1 for s in scenes if s.get('clip'))} film clips")
    # best portrait of the subject for the thumbnail
    portrait = None
    pool = [c for c in pools.get(subject, []) if c["kind"] == "image"]
    if pool:
        pe = m.images(pool)
        t = m.text([f"close-up portrait photo of {subject}"])[0]
        ranked = sorted((c for c in pool if pe.get(c["id"]) is not None),
                        key=lambda c: float(pe[c["id"]] @ t) + (0.03 if (c.get("w") or 0) >= 1200 else 0), reverse=True)
        portrait = ranked[0]["src"] if ranked else None
    return scenes, list(credits.values()), portrait


def credits_text(credits, limit=1800):
    if not credits:
        return ""
    out, n = "Media credits (Wikimedia Commons / Openverse, free licenses):", 0
    for c in credits:
        line = "\n- " + c
        if len(out) + len(line) > limit:
            out += f"\n... and {len(credits) - n} more"
            break
        out += line
        n += 1
    return out
