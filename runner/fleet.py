"""Fleet Studio cloud production runner (runs inside GitHub Actions).

  python runner/fleet.py produce  --item <id>      make one queued video now
  python runner/fleet.py autopilot                  make the next videos for channels with autopilot on
  python runner/fleet.py import   --channel <id> --handle @name   put every video of a reference channel in the queue

Repo files:  fleet/channels.json  fleet/queue.json  (written by the website)
             fleet/state.json                       (written by this runner: results, schedule slots)
Secrets:     YT_TOKEN_<CHANNELID>  (Google refresh token JSON, saved by the website)   YT_COOKIES (optional)
"""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import ai  # noqa: E402
import broll  # noqa: E402
import render  # noqa: E402
import speech  # noqa: E402

FLEET = os.path.join(ROOT, "fleet")
OUT = os.path.join(ROOT, "out")
LOCALES = {"es": "es-ES", "pt": "pt-BR", "fr": "fr-FR", "de": "de-DE", "it": "it-IT", "hi": "hi-IN", "id": "id-ID",
           "ru": "ru-RU", "tr": "tr-TR", "pl": "pl-PL", "nl": "nl-NL", "ja": "ja-JP", "ko": "ko-KR", "zh": "zh-CN"}
GAPS = {"auto48": 48, "auto72": 72, "auto96": 96}
AHEAD = int(os.environ.get("FLEET_AHEAD", "3"))          # scheduled videos kept waiting per channel
PER_RUN = int(os.environ.get("FLEET_PER_RUN", "2"))      # videos per autopilot run (YouTube quota ~6 uploads/day)
SECRETS = {k: os.environ[k] for k in ("PEXELS_API_KEY", "GEMINI_API_KEY", "YT_COOKIES", "FLEET_VAULT_KEY") if os.environ.get(k)}
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


# ------------------------------------------------------------------ repo files
def rj(name, default):
    try:
        with open(os.path.join(FLEET, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def wj(name, data):
    with open(os.path.join(FLEET, name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)


def git(*a, check=True):
    return subprocess.run(["git", *a], cwd=ROOT, check=check, capture_output=True, text=True)


def commit(msg, paths):
    """Commit + push, re-applying our change if the website pushed meanwhile."""
    git("add", "-A", *paths)
    if not git("diff", "--cached", "--quiet", check=False).returncode:
        return
    git("commit", "-m", msg)
    for _ in range(6):
        if not git("push", check=False).returncode:
            return
        git("pull", "--rebase", "-X", "theirs", check=False)
        time.sleep(3)
    raise RuntimeError("could not push results to the repository")


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.isoformat(timespec="seconds") if t else None


def parse(s):
    return dt.datetime.fromisoformat(s) if s else None


def summary(md):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    print(md)


# ------------------------------------------------------------------ YouTube API
def youtube(channel_id):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    raw = vault_token(channel_id)
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def vault_token(channel_id):
    """YouTube login of a channel: fleet/tokens/<id>.txt, sealed by the website with the vault PUBLIC key.
    Only the private key (secret FLEET_VAULT_KEY) can open it - same method GitHub uses for its secrets."""
    import base64
    from nacl.public import PrivateKey, SealedBox
    p = os.path.join(FLEET, "tokens", f"{channel_id}.txt")
    if not os.path.isfile(p):
        raise RuntimeError("YouTube not connected for this channel (website -> Channels -> Connect YouTube)")
    if not SECRETS.get("FLEET_VAULT_KEY"):
        raise RuntimeError("secret FLEET_VAULT_KEY missing - connect YouTube again on the website")
    box = SealedBox(PrivateKey(base64.b64decode(SECRETS["FLEET_VAULT_KEY"])))
    with open(p, encoding="utf-8") as f:
        return box.decrypt(base64.b64decode(f.read().strip())).decode("utf-8")


def video_meta(yt, vid):
    items = yt.videos().list(part="snippet,contentDetails", id=vid).execute().get("items") or []
    if not items:
        return {"title": "", "description": "", "tags": []}
    s = items[0]["snippet"]
    return {"title": s.get("title", ""), "description": s.get("description", ""), "tags": (s.get("tags") or [])[:30],
            "lang": s.get("defaultAudioLanguage") or s.get("defaultLanguage")}


def channel_uploads(yt, handle):
    h = handle.strip()
    if "youtube.com/" in h:
        h = h.split("youtube.com/")[1].split("/")[0]
    if h.startswith("channel/") or h.startswith("UC"):
        req = {"id": h.replace("channel/", "")}
    else:
        req = {"forHandle": h if h.startswith("@") else "@" + h}
    ch = yt.channels().list(part="contentDetails,snippet", **req).execute().get("items") or []
    if not ch:
        raise RuntimeError(f"channel {handle} not found")
    pl = ch[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    out, tok = [], None
    while True:
        r = yt.playlistItems().list(part="contentDetails,snippet", playlistId=pl, maxResults=50, pageToken=tok).execute()
        for i in r.get("items", []):
            vid = i["contentDetails"]["videoId"]
            out.append({"id": vid, "title": i["snippet"].get("title", ""), "url": f"https://www.youtube.com/watch?v={vid}"})
        tok = r.get("nextPageToken")
        if not tok:
            return out


def upload(yt, path, title, desc, tags, publish_at, thumb, log, lang=None):
    from googleapiclient.http import MediaFileUpload
    status = {"selfDeclaredMadeForKids": False}
    if publish_at:
        status.update(privacyStatus="private", publishAt=publish_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    else:
        status["privacyStatus"] = "public"
    snip = {"title": title[:100], "description": desc[:4900], "tags": tags[:30], "categoryId": "22"}
    if lang:
        snip["defaultLanguage"] = snip["defaultAudioLanguage"] = lang
    req = yt.videos().insert(part="snippet,status", body={"snippet": snip, "status": status},
                             media_body=MediaFileUpload(path, chunksize=16 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        st, resp = req.next_chunk()
        if st:
            log(f"  upload {int(st.progress() * 100)}%")
    vid = resp["id"]
    log(f"  uploaded https://youtu.be/{vid} " + (f"(public at {publish_at:%Y-%m-%d %H:%M} UTC)" if publish_at else "(public)"))
    if thumb:
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb)).execute()
            log("  winning thumbnail attached")
        except Exception as e:                       # noqa: BLE001
            log(f"  thumbnail not attached: {str(e)[:200]}  (verify the channel at youtube.com/verify)")
    return vid


# ------------------------------------------------------------------ downloads
def ytdlp(url, work, what, log):
    """Cloud servers are often blocked by YouTube - that is why the website can send the MP3 itself."""
    import yt_dlp
    o = {"quiet": True, "no_warnings": True, "noprogress": True}
    if SECRETS.get("YT_COOKIES"):
        ck = os.path.join(work, "cookies.txt")
        with open(ck, "w", encoding="utf-8") as f:
            f.write(SECRETS["YT_COOKIES"])
        o["cookiefile"] = ck
    if what == "audio":
        o.update(format="bestaudio[ext=m4a]/bestaudio", outtmpl=os.path.join(work, "ref_audio.%(ext)s"))
    else:
        o.update(format="bestvideo[height<=1080][ext=mp4]/bestvideo[height<=1080]", outtmpl=os.path.join(work, "ref_video.%(ext)s"))
    with yt_dlp.YoutubeDL(o) as y:
        y.download([url])
    pre = "ref_audio." if what == "audio" else "ref_video."
    for n in os.listdir(work):
        if n.startswith(pre) and not n.endswith(".part"):
            log(f"downloaded reference {what}")
            return os.path.join(work, n)
    raise RuntimeError(f"reference {what} not downloaded")


def ref_thumb(vid, work):
    for q in ("maxresdefault", "sddefault", "hqdefault"):
        try:
            p = os.path.join(work, "ref_thumb.jpg")
            urllib.request.urlretrieve(f"https://i.ytimg.com/vi/{vid}/{q}.jpg", p)
            if os.path.getsize(p) > 2000:
                return p
        except OSError:
            pass
    return None


def repo_file(rel):
    if not rel:
        return None
    p = os.path.join(ROOT, rel)
    return p if os.path.isfile(p) else None


# ------------------------------------------------------------------ one video
def next_slot(st, gap_h):
    t = now() + dt.timedelta(minutes=30)
    last = parse(st.get("last_slot"))
    return (max(t, last + dt.timedelta(hours=gap_h)) if last else t).replace(second=0, microsecond=0)


def produce(it, c, state):
    cs = state["channels"].setdefault(c["id"], {})
    res = state["items"].setdefault(it["id"], {})
    work = os.path.join(OUT, it["id"])
    os.makedirs(work, exist_ok=True)
    logs = []

    def log(m):
        print(m, flush=True)
        logs.append(m)

    res.update(status="running", run=os.environ.get("GITHUB_RUN_ID"), started=iso(now()), error=None)
    try:
        yt = youtube(c["id"]) if (it.get("upload") or it.get("video_id")) else None
        aspect = it.get("aspect", "16:9")
        ref = {"title": it.get("title") or "", "description": "", "tags": []}
        if it.get("video_id") and yt:
            ref = video_meta(yt, it["video_id"])
        log(f"== reference: {ref['title'] or it['id']}")
        audio = repo_file(it.get("audio"))
        if not audio:
            log("== getting audio from the reference video")
            audio = ytdlp(it["ref_url"], work, "audio", log)
        total = render.duration(audio)
        if total < 1:
            raise RuntimeError("the audio file is empty or broken")
        ref_th = ref_thumb(it["video_id"], work) if it.get("video_id") else None
        avatar = repo_file(it.get("speaker")) or repo_file(c.get("avatar"))

        log("== 1. speech to text")
        segments, lang = speech.transcribe(audio, total, log)
        transcript = " ".join(s["text"] for s in segments)

        log("== 2. new title, description, tags, hashtags (AI)")
        meta = ai.metadata(ref, transcript, segments, total, lang, SECRETS, log)
        if it.get("title"):
            meta["title"] = it["title"]
        log(f"  title: {meta['title']}")
        log(f"  thumbnail text: {meta['thumbnail_text']}")

        mode = it.get("background") or "pexels"
        px = broll.Pexels(SECRETS["PEXELS_API_KEY"], os.path.join(work, "clips"), log) if SECRETS.get("PEXELS_API_KEY") else None
        if mode == "pexels" and not px:
            log("  no PEXELS_API_KEY secret -> background = reference thumbnail with motion (add the free key in Settings)")
        topic_q = meta.get("thumb_query") or "cinematic nature"
        scenes = speech.scenes_from(segments, total)
        still = repo_file(it.get("thumb")) or ref_th or render._blank(work, 1920, 1080)
        if mode == "video" and it.get("ref_url"):
            try:
                bgv = ytdlp(it["ref_url"], work, "video", log)
                for sc in scenes:
                    sc.update(clip=bgv, clip_dur=total * 10, offset=sc["start"])
            except Exception as e:                   # noqa: BLE001
                log(f"  reference video blocked ({str(e)[:120]}) - using Pexels/thumbnail")
                mode = "pexels"
        if mode == "pexels" and px:
            log(f"== 3. stock clips for {len(scenes)} scenes (Pexels)")
            queries, english = ai.scene_queries(scenes, meta["title"], SECRETS, log)
            if not english:
                px.locale = LOCALES.get(lang)
            broll.plan(scenes, queries, topic_q, px, aspect, log, report=os.path.join(work, "clip_matches.txt"))

        log(f"== 4. video {aspect} with subtitles + avatar ({total / 60:.1f} min)")
        main = broll.compose(work, scenes, segments, audio, os.path.join(work, "video.mp4"), aspect, still, avatar, log=log)

        log("== 5. thumbnail")
        thumb = repo_file(it.get("thumb"))
        if thumb:
            thumb = shutil.copy(thumb, os.path.join(work, "thumbnail" + os.path.splitext(thumb)[1]))
        else:
            photo = px.photo(topic_q) if px else None
            thumb = render.thumbnail(work, meta["thumbnail_text"], photo, ref_th, avatar, meta.get("highlight"), log=log)

        shorts = []
        if it.get("shorts"):
            log("== 6. vertical Shorts")
            for n, (a, b) in enumerate(broll.pick_short_windows(segments, total)):
                shorts.append(broll.compose(work, scenes, segments, audio, os.path.join(work, f"short_{n + 1}.mp4"),
                                            "9:16", still, avatar, a, b, log, tag=f"short{n + 1}"))
        shutil.rmtree(os.path.join(work, "clips"), ignore_errors=True)

        yt_ids = list(res.get("youtube") or [])            # a retry after the daily limit skips what is already up
        done = {y["kind"] for y in yt_ids}
        if it.get("upload"):
            log("== 7. uploading to YouTube")
            desc = ((c.get("credit") or "") + "\n\n" + meta["description"]).strip()
            gap = GAPS.get(it.get("schedule"))
            prev = next((y for y in yt_ids if y["kind"] == "video"), None)
            slot = parse(prev["publish"]) if prev and prev.get("publish") else (next_slot(cs, gap) if gap else None)
            if "video" not in done:
                vid = upload(yt, main, meta["title"], desc, meta["tags"], slot, thumb, log, lang)
                yt_ids.append({"kind": "video", "id": vid, "publish": iso(slot)})
                res["youtube"] = yt_ids
                if slot:
                    cs["last_slot"] = iso(slot)
                    cs["slots"] = [s for s in cs.get("slots", []) if parse(s) > now() - dt.timedelta(days=30)] + [iso(slot)]
            for n, sv in enumerate(shorts):
                kind = f"short {n + 1}"
                if kind in done:
                    continue
                when = slot + dt.timedelta(hours=gap * (n + 1) / 3) if slot else None
                st = (meta["short_titles"][n] if n < len(meta["short_titles"]) else meta["title"])[:88] + " #shorts"
                sid = upload(yt, sv, st, desc, meta["tags"], when, None, log, lang)
                yt_ids.append({"kind": kind, "id": sid, "publish": iso(when)})
                res["youtube"] = yt_ids
        res.update(status="done", finished=iso(now()), youtube=yt_ids, title=meta["title"])
        summary(f"### ✅ {meta['title']}\n" + "".join(f"- {y['kind']}: https://youtu.be/{y['id']} "
                                                       f"{'(public ' + y['publish'] + ')' if y['publish'] else '(public now)'}\n" for y in yt_ids))
        for k in ("audio", "thumb", "speaker"):          # inbox files are not needed any more
            p = repo_file(it.get(k))
            if p and "/inbox/" in p.replace("\\", "/"):
                os.remove(p)
        return True
    except Exception as e:                               # noqa: BLE001
        msg = str(e)
        traceback.print_exc()
        quota = "quota" in msg.lower() or "uploadLimitExceeded" in msg
        res.update(status="waiting" if quota else "error", finished=iso(now()), error=msg[:400],
                   retry_at=iso(now() + dt.timedelta(hours=24)) if quota else None)
        summary(f"### ❌ {it.get('title') or it['id']}\n{msg[:400]}")
        return False
    finally:
        res["log"] = logs[-40:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["produce", "autopilot", "import", "test"])
    ap.add_argument("--item")
    ap.add_argument("--channel")
    ap.add_argument("--handle")
    a = ap.parse_args()
    git("config", "user.name", "fleet-studio-bot")
    git("config", "user.email", "fleet-studio-bot@users.noreply.github.com")
    channels = {c["id"]: c for c in rj("channels.json", [])}
    state = rj("state.json", {})
    state.setdefault("channels", {})
    state.setdefault("items", {})

    if a.mode == "test":                                  # sample video, no upload: check clip matching quality
        text = a.handle or ("La disciplina es el puente entre tus metas y tus logros. Cada mañana un hombre sale a correr "
                            "por la playa antes del amanecer. Una mujer estudia en la biblioteca con sus libros abiertos. "
                            "Los grandes campeones entrenan en el gimnasio todos los días. Cuando sientas que quieres "
                            "rendirte, mira a tu familia y recuerda por qué empezaste. El éxito es como subir una montaña: "
                            "paso a paso hasta llegar a la cima.")
        os.makedirs(os.path.join(ROOT, "inbox"), exist_ok=True)
        voice = os.environ.get("TEST_VOICE") or "es-MX-JorgeNeural"
        subprocess.run([sys.executable, "-m", "edge_tts", "--voice", voice, "--text", text,
                        "--write-media", os.path.join(ROOT, "inbox", "test-audio.mp3")], check=True)
        it = {"id": "test", "channel_id": "test", "audio": "inbox/test-audio.mp3", "aspect": "16:9", "shorts": False,
              "upload": False, "background": "pexels", "title": ""}
        ok = produce(it, {"id": "test", "name": "test"}, state)
        log = state["items"]["test"].get("log", [])
        summary("Test video and clip_matches.txt are in this run's Artifacts.\n\n```\n" + "\n".join(log[-25:]) + "\n```")
        sys.exit(0 if ok else 1)

    if a.mode == "import":
        c = channels[a.channel]
        vids = channel_uploads(youtube(c["id"]), a.handle or c.get("reference", ""))
        queue = rj("queue.json", [])
        have = {q.get("video_id") for q in queue}
        added = 0
        for v in reversed(vids):
            if v["id"] in have:
                continue
            queue.append({"id": "i" + v["id"].replace("-", "x").replace("_", "y").lower()[:11] + c["id"][:4],
                          "channel_id": c["id"], "ref_url": v["url"], "video_id": v["id"], "title": v["title"],
                          "aspect": c.get("aspect", "16:9"), "shorts": c.get("shorts", True), "upload": True,
                          "schedule": c.get("schedule", "auto72"), "background": "pexels", "created": iso(now())})
            added += 1
        wj("queue.json", queue)
        state["channels"].setdefault(c["id"], {})["import_msg"] = f"{iso(now())[:16]} imported {added} new of {len(vids)} videos"
        wj("state.json", state)
        commit(f"import {added} videos for {c['name']}", ["fleet"])
        summary(f"Imported **{added}** new videos ({len(vids)} on the reference channel).")
        return

    queue = rj("queue.json", [])
    if a.mode == "produce":
        todo = [q for q in queue if q["id"] == a.item]
    else:
        todo, per_chan = [], {}
        for q in queue:
            r = state["items"].get(q["id"], {})
            c = channels.get(q["channel_id"])
            if not c or not c.get("autopilot") or r.get("status") in ("done", "error"):
                continue
            if r.get("retry_at") and parse(r["retry_at"]) > now():
                continue
            slots = state["channels"].get(c["id"], {}).get("slots", [])
            ahead = sum(1 for s in slots if parse(s) > now()) + per_chan.get(c["id"], 0)
            if ahead < AHEAD:
                todo.append(q)
                per_chan[c["id"]] = per_chan.get(c["id"], 0) + 1
            if len(todo) >= PER_RUN:
                break
    if not todo:
        summary("Nothing to do (queue empty, autopilot off, or enough videos already scheduled).")
        return
    for it in todo:
        c = channels.get(it["channel_id"])
        if not c:
            continue
        produce(it, c, state)
        wj("state.json", state)
        commit(f"run: {state['items'][it['id']].get('status')} {it['id']}", ["fleet", "inbox"])


if __name__ == "__main__":
    main()
