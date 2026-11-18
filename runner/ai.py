"""Free text AI for Fleet Studio: titles, descriptions, tags, hashtags, thumbnail words, Pexels search words.
Order: Gemini (if secret GEMINI_API_KEY, free tier) -> Pollinations (no key) -> simple rules (never fails)."""
import json
import os
import re
import time
import urllib.request

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")


def _post(url, body, headers=None, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "fleet-studio/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _gemini(prompt, key):
    j = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}",
              {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.8, "responseMimeType": "application/json"}})
    return j["candidates"][0]["content"]["parts"][0]["text"]


def _pollinations(prompt):
    j = _post("https://text.pollinations.ai/openai",
              {"model": "openai", "messages": [{"role": "system", "content": "You answer with valid JSON only."},
                                               {"role": "user", "content": prompt}], "temperature": 0.8})
    return j["choices"][0]["message"]["content"]


def _parse(txt):
    txt = txt.strip()
    m = re.search(r"\{.*\}|\[.*\]", txt, re.S)
    return json.loads(m.group(0) if m else txt)


def llm_json(prompt, secrets, log=print, tries=3):
    key = secrets.get("GEMINI_API_KEY")
    for n in range(tries):
        for name, fn in (("gemini", lambda: _gemini(prompt, key)) if key else (None, None),
                         ("pollinations", lambda: _pollinations(prompt))):
            if not name:
                continue
            try:
                return _parse(fn())
            except Exception as e:                      # noqa: BLE001
                log(f"  AI ({name}) try {n + 1}: {str(e)[:120]}")
        time.sleep(20 * (n + 1))                         # free Pollinations allows about 1 request every 15 s
    return None


# ------------------------------------------------------------------ metadata
def _ts(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def metadata(ref, transcript, segments, duration, lang, secrets, log=print):
    """ref = reference video {title, description, tags}. Returns new title/description/tags/hashtags/thumb text..."""
    outline = []                                       # transcript with times, for chapters
    step = max(1, len(segments) // 60)
    for s in segments[::step]:
        outline.append(f"[{_ts(s['start'])}] {s['text'][:140]}")
    prompt = f"""You are a YouTube growth expert. Write NEW metadata for a video, in the SAME LANGUAGE as the transcript ({lang or 'auto'}).
Reference video title: {ref.get('title', '')}
Reference tags: {', '.join(ref.get('tags', [])[:20])}
Video length: {_ts(duration)}
Transcript (start): {transcript[:5000]}
Timed outline:
{chr(10).join(outline)[:5000]}

Return JSON with keys:
"title": viral, curiosity, emotional, max 70 characters, similar topic to the reference but different words,
"thumbnail_text": 4 to 8 powerful words for the thumbnail (no hashtags, no emojis),
"highlight_words": the 1-2 most powerful words of thumbnail_text,
"description": 3 short paragraphs (hook, what they will learn, call to subscribe) - no hashtags inside,
"chapters": list of {{"time":"m:ss","title":"..."}} with first time "0:00", 5-12 chapters, only if length > 3 minutes, else [],
"tags": 20 search tags (mix broad + specific, total under 450 characters),
"hashtags": 4 hashtags with #,
"short_titles": 2 viral titles for vertical Shorts made from this video, max 70 characters,
"thumb_query": 2-4 ENGLISH words for a stock photo matching the thumbnail emotion."""
    j = llm_json(prompt, secrets, log) or {}
    title = (j.get("title") or ref.get("title") or transcript[:60] or "Video").strip()[:100]
    words = re.findall(r"\w[\w'’]*", title)
    meta = {
        "title": title,
        "thumbnail_text": (j.get("thumbnail_text") or " ".join(words[:6])).strip(),
        "highlight": [w.upper() for w in (j.get("highlight_words") or [])][:2],
        "tags": [t.strip("# ").strip() for t in (j.get("tags") or ref.get("tags") or words)][:30],
        "hashtags": [h if h.startswith("#") else "#" + h for h in (j.get("hashtags") or [])][:5],
        "short_titles": (j.get("short_titles") or [])[:2],
        "thumb_query": " ".join(j["thumb_query"][:1]) if isinstance(j.get("thumb_query"), list) else (j.get("thumb_query") or ""),
    }
    while sum(len(t) + 3 for t in meta["tags"]) > 480:
        meta["tags"].pop()
    desc = (j.get("description") or ref.get("description") or "").strip()
    ch = j.get("chapters") or []
    if duration > 180 and len(ch) >= 3:
        ch[0]["time"] = "0:00"
        desc += "\n\n" + "\n".join(f"{c.get('time')} {c.get('title')}" for c in ch)
    if meta["hashtags"]:
        desc += "\n\n" + " ".join(meta["hashtags"])
    meta["description"] = desc
    return meta


# ------------------------------------------------------------------ stock-video search words for each scene
STOP = set("""a an the and or but of to in on at for with from by is are was were be been it this that these those you your
we our they their he she his her i me my not no yes so if then than as do does did have has had will would can could just
very more most all any some what when where who why how el la los las un una unos unas y o de del al en con por para es son
fue ser que se lo le les mi tu su sus nos no si ya muy mas más como cuando donde quien porque pero este esta esto ese esa""".split())


def _fallback_query(text):
    w = [x for x in re.findall(r"[^\W\d_]{4,}", text.lower()) if x not in STOP]
    w.sort(key=len, reverse=True)
    return " ".join(w[:2]) or "nature landscape"


def scene_queries(scenes, topic, secrets, log=print):
    """One short ENGLISH Pexels search per scene (2-4 concrete visual words). Returns (queries, english?)."""
    out, english = [None] * len(scenes), True
    for b in range(0, len(scenes), 40):
        chunk = scenes[b:b + 40]
        lines = "\n".join(f"{i + 1}. {s['text'][:200]}" for i, s in enumerate(chunk))
        prompt = f"""Video topic: {topic}
For EACH numbered sentence give a stock-video search query in ENGLISH (2-4 words) that shows something CONCRETE and
filmable matching the meaning (people, places, actions, objects, emotions). Avoid abstract words. Vary them.
Return JSON: {{"q": ["query 1", "query 2", ...]}} with exactly {len(chunk)} items.
{lines}"""
        j = llm_json(prompt, secrets, log) or {}
        qs = j.get("q") if isinstance(j, dict) else j
        for i in range(len(chunk)):
            q = qs[i] if isinstance(qs, list) and i < len(qs) and isinstance(qs[i], str) and qs[i].strip() else None
            if not q:
                english = False
            out[b + i] = (q or _fallback_query(chunk[i]["text"])).strip()[:60]
        time.sleep(3)
    return out, english
