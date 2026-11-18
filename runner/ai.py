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
    """For every scene 3 ENGLISH stock-video searches: very specific -> general. Returns (list of lists, english?)."""
    out, english = [None] * len(scenes), True
    for b in range(0, len(scenes), 30):
        chunk = scenes[b:b + 30]
        lines = "\n".join(f"{i + 1}. {s['text'][:220]}" for i, s in enumerate(chunk))
        prompt = f"""You choose stock footage (Pexels) for a narrated video. Topic: {topic}
For EACH numbered sentence write 3 ENGLISH search queries for a video clip that SHOWS what the sentence means:
 - query 1: very specific and literal (who / what / action / place), 3-5 words, e.g. "man running on beach at sunrise"
 - query 2: simpler, 2-3 words, e.g. "man running"
 - query 3: the emotion or concept as a visible scene, 2-3 words, e.g. "sunrise motivation"
Rules: only things a camera can film; for abstract ideas use a concrete visual metaphor (success -> "climber reaching summit");
keep the person's gender/age if the sentence says it; use the sentences around for context; never use names or brands.
Return JSON: {{"q": [["q1","q2","q3"], ...]}} with exactly {len(chunk)} items, same order.
{lines}"""
        j = llm_json(prompt, secrets, log) or {}
        qs = j.get("q") if isinstance(j, dict) else j
        for i in range(len(chunk)):
            item = qs[i] if isinstance(qs, list) and i < len(qs) else None
            if isinstance(item, str):
                item = [item]
            item = [x.strip()[:60] for x in (item or []) if isinstance(x, str) and x.strip()]
            if not item:
                english = False
                item = [_fallback_query(chunk[i]["text"])]
            out[b + i] = item
        time.sleep(3)
    return out, english


# ------------------------------------------------------------------ documentary
PRONOUNS = re.compile(r"\b(he|him|his|she|her|hers|él|ella|su|sus|lui|elle|er|sie|ele|ela|वह|उसने|उसका|اس|وہ)\b", re.I)


def documentary_info(transcript, subject_hint, secrets, log=print):
    """Who/what the documentary is about."""
    prompt = f"""Read this documentary narration and identify what it is about.
{('The user says the main subject is: ' + subject_hint) if subject_hint else ''}
Narration (start): {transcript[:6000]}
Return JSON: {{"subject": "full name of the main person/thing as written in English Wikipedia",
"aliases": ["other names used for the subject, e.g. nickname, last name"],
"subject_type": "person | place | event | topic",
"people": ["other real people named in the narration, full names"],
"years": "e.g. 1958-2009",
"topic_stock": "2-3 English words for generic stock footage matching the mood"}}"""
    j = llm_json(prompt, secrets, log) or {}
    if subject_hint:
        j["subject"] = subject_hint
    if not j.get("subject"):                      # no AI: most frequent capitalised name
        names = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b", transcript)
        j["subject"] = max(set(names), key=names.count) if names else ""
    j.setdefault("aliases", [])
    j.setdefault("subject_type", "person")
    return j


def documentary_scenes(scenes, info, secrets, log=print):
    """Per sentence: who is shown, which year, archive search words, stock search words."""
    out = [None] * len(scenes)
    subject = info.get("subject", "")
    for b in range(0, len(scenes), 30):
        chunk = scenes[b:b + 30]
        lines = "\n".join(f"{i + 1}. {s['text'][:240]}" for i, s in enumerate(chunk))
        prompt = f"""Documentary about: {subject} ({info.get('subject_type', 'person')}, {info.get('years', '')}).
Other people: {', '.join(info.get('people', [])[:15])}
For EACH numbered narration sentence decide what the picture must show. Return JSON:
{{"s": [{{"who": "subject" | "<full name of another real person shown>" | "none",
         "year": year the sentence is about (number) or null,
         "archive": "English search words for real archive photos on Wikimedia Commons: names + event/place/object + year, 2-6 words",
         "stock": "English stock-footage words WITHOUT names for a generic matching shot, 2-4 words"}}, ...]}}
Use the sentences around for context (he/she/they usually means the subject). Exactly {len(chunk)} items, same order.
{lines}"""
        j = llm_json(prompt, secrets, log) or {}
        items = j.get("s") if isinstance(j, dict) else j
        prev_year = None
        for i, s in enumerate(chunk):
            it = items[i] if isinstance(items, list) and i < len(items) and isinstance(items[i], dict) else {}
            if not it:                                # fallback rules
                about = subject and (subject.split()[-1].lower() in s["text"].lower() or PRONOUNS.search(s["text"]))
                it = {"who": "subject" if about else "none",
                      "archive": f"{subject} {_fallback_query(s['text'])}" if about else _fallback_query(s["text"]),
                      "stock": _fallback_query(s["text"])}
            y = it.get("year")
            try:
                y = int(str(y)[:4]) if y else None
            except ValueError:
                y = None
            m = re.search(r"\b(1[89]\d\d|20[0-3]\d)\b", s["text"])
            it["year"] = int(m.group(1)) if m else (y or prev_year)
            prev_year = it["year"]
            out[b + i] = it
        time.sleep(2)
    return out
