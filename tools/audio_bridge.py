"""Local Audio Bridge - downloads reference audio on YOUR internet (no cloud bot blocks) for the Fleet Studio website.
Listens only on 127.0.0.1:7871. The website asks it for the audio of a YouTube URL and sends the file to the cloud."""
import glob
import os
import re
import shutil
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yt_dlp

PORT = 7871
OK_URL = re.compile(r"^https://(www\.|m\.)?(youtube\.com|youtu\.be)/")


class H(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "X-Filename")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def _text(self, code, t):
        b = t.encode()
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "text/plain; charset=utf-8"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/ping":
            return self._text(200, "ok")
        if u.path != "/audio":
            return self._text(404, "not found")
        url = (parse_qs(u.query).get("url") or [""])[0]
        if not OK_URL.match(url):
            return self._text(400, "only YouTube links")
        tmp = tempfile.mkdtemp(prefix="bridge_")
        try:
            print("downloading audio:", url, flush=True)
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": "bestaudio[ext=m4a]/bestaudio",
                                   "outtmpl": os.path.join(tmp, "audio.%(ext)s")}) as y:
                y.download([url])
            f = [p for p in glob.glob(os.path.join(tmp, "audio.*")) if not p.endswith(".part")][0]
            size = os.path.getsize(f)
            self.send_response(200); self._cors()
            self.send_header("Content-Type", "application/octet-stream"); self.send_header("Content-Length", str(size))
            self.send_header("X-Filename", os.path.basename(f)); self.end_headers()
            with open(f, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile)
            print(f"  sent {size / 1048576:.1f} MB", flush=True)
        except Exception as e:  # noqa: BLE001
            self._text(500, str(e)[:300])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Local Audio Bridge running on http://127.0.0.1:{PORT}  - keep this window open while you use Fleet Studio.")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
