"""Visual matching: picks the Pexels clip whose PICTURES best match the spoken sentence.

CLIP (image model) + its multilingual text twin: the Spanish/Hindi/... sentence and the clip's preview frames are put
in the same "meaning space" and compared. Runs on CPU, free, models are cached between runs."""
import io
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np


class Matcher:
    def __init__(self, log=print):
        from sentence_transformers import SentenceTransformer
        log("  loading the visual matching model (CLIP)...")
        self.img_model = SentenceTransformer("clip-ViT-B-32", device="cpu")
        self.txt_model = SentenceTransformer("sentence-transformers/clip-ViT-B-32-multilingual-v1", device="cpu")
        self.img_cache, self.log = {}, log

    def text(self, texts):
        return self.txt_model.encode([t[:300] for t in texts], normalize_embeddings=True, batch_size=64,
                                     show_progress_bar=False)

    @staticmethod
    def _frames(v):
        """2-3 small preview frames of a Pexels video (start / middle / poster)."""
        pics = [p.get("picture") for p in v.get("video_pictures") or [] if p.get("picture")]
        urls = []
        if pics:
            urls += [pics[len(pics) // 4], pics[(3 * len(pics)) // 4]]
        if v.get("image"):
            urls.append(v["image"].split("?")[0] + "?auto=compress&cs=tinysrgb&w=400")
        return list(dict.fromkeys(urls))[:3]

    @staticmethod
    def _get(url):
        from PIL import Image
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fleet-studio/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                im = Image.open(io.BytesIO(r.read())).convert("RGB")
            im.thumbnail((336, 336))
            return im
        except Exception:                                  # noqa: BLE001
            return None

    def images(self, vids):
        """Embedding per video id (average of its preview frames). Cached."""
        todo = [v for v in vids if v["id"] not in self.img_cache]
        if todo:
            jobs = [(v["id"], u) for v in todo for u in self._frames(v)]
            with ThreadPoolExecutor(16) as ex:
                ims = list(ex.map(lambda j: self._get(j[1]), jobs))
            ok = [(vid, im) for (vid, _), im in zip(jobs, ims) if im is not None]
            if ok:
                emb = self.img_model.encode([im for _, im in ok], normalize_embeddings=True, batch_size=32,
                                            show_progress_bar=False)
                by = {}
                for (vid, _), e in zip(ok, emb):
                    by.setdefault(vid, []).append(e)
                for vid, es in by.items():
                    m = np.mean(es, axis=0)
                    self.img_cache[vid] = m / (np.linalg.norm(m) + 1e-9)
            for v in todo:
                self.img_cache.setdefault(v["id"], None)
        return {v["id"]: self.img_cache.get(v["id"]) for v in vids}


def slug(v):
    """Pexels page URL -> human description, e.g. '.../video/a-man-running-on-the-beach-855123/' -> 'a man running on the beach'."""
    m = re.search(r"/video/([^/]+?)-?\d*/?$", v.get("url") or "")
    return m.group(1).replace("-", " ").strip() if m else ""
