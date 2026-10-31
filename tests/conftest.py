"""Test fixtures: a generated music library and a local mock of the LRCLIB / lrcmux APIs."""
import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lyrics_sync  # noqa: E402


def lrc(n, step=4.0):
    return "\n".join(f"[00:{int(1 + i * step):02d}.00]line {i + 1}" for i in range(n))


class MockAPI:
    """Serves /lrclib/... and /mux/... with canned answers. mode: normal | lrclib_down | mux_blocked."""

    def __init__(self, mode="normal"):
        self.mode = mode
        self.requests = []
        self.lrclib_calls = 0
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj, headers=None):
                body = json.dumps(obj).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                api.requests.append((u.path, q, self.headers.get("User-Agent")))
                svc, _, ep = u.path.lstrip("/").partition("/")
                t, a = q.get("track_name", ""), q.get("artist_name", "")
                if svc == "lrclib":
                    return api.lrclib(self, ep, t, a)
                if svc == "mux":
                    return api.mux(self, ep, t, a)
                self.send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def lrclib(self, h, ep, t, a):
        self.lrclib_calls += 1
        if self.mode == "lrclib_down":
            return h.send(503, {"name": "ServerOverloaded"})
        if self.lrclib_calls <= 2:  # "server busy" on the first two calls, like the real thing
            return h.send(503, {"name": "ServerOverloaded"}, {"Retry-After": "0"})
        if ep == "api/get":
            if t == "Graves Into Gardens":
                return h.send(200, {"trackName": t, "artistName": a, "duration": 30, "instrumental": False,
                                    "plainLyrics": "x", "syncedLyrics": lrc(6)})
            if t == "Interludio":
                return h.send(200, {"trackName": t, "artistName": a, "duration": 15, "instrumental": True,
                                    "plainLyrics": None, "syncedLyrics": None})
            return h.send(404, {"code": 404, "name": "TrackNotFound"})
        if ep == "api/search":
            if t == "Renuévame":
                return h.send(200, [{"trackName": "Renuévame", "artistName": "Marcos Witt", "duration": 21,
                                     "plainLyrics": "p", "syncedLyrics": lrc(5)}])
            if t == "The Blessing":  # a 5-minute live version: must be rejected by the duration check
                return h.send(200, [{"trackName": "The Blessing (Live)", "artistName": "Elevation Worship",
                                     "duration": 300, "plainLyrics": "p", "syncedLyrics": lrc(40, 7)}])
            return h.send(200, [])
        h.send(404, {})

    def mux(self, h, ep, t, a):
        if self.mode == "mux_blocked":
            return h.send(403, {"error": "forbidden"})
        if ep == "api/get" and t.startswith("The Blessing"):  # exact match on a different-length version
            return h.send(200, {"trackName": "The Blessing (Live)", "artistName": "Elevation Worship",
                                "duration": 300, "plainLyrics": "p", "syncedLyrics": lrc(9)})
        if ep == "api/search" and t == "The Blessing":
            return h.send(200, [{"trackName": "The Blessing", "artistName": "Elevation Worship, Kari Jobe",
                                 "duration": 25, "plainLyrics": "p", "syncedLyrics": lrc(5)}])
        if ep == "api/search" and t == "Graves Into Gardens":
            return h.send(200, [{"trackName": "Graves Into Gardens", "artistName": "Elevation Worship",
                                 "duration": 30, "plainLyrics": "p", "syncedLyrics": lrc(6)}])
        if t == "No Hay Rewind":  # lrcmux flags this vocal song "instrumental" with no lyrics (real behaviour)
            item = {"trackName": t, "artistName": "Manny Montes", "duration": 15, "instrumental": True,
                    "plainLyrics": None, "syncedLyrics": None}
            return h.send(200, item if ep == "api/get" else [item])
        if ep == "api/search" and t == "Cancion Sin Tags":
            return h.send(200, [{"trackName": "Cancion Sin Tags", "artistName": "Untagged Artist",
                                 "duration": 10, "plainLyrics": "solo texto", "syncedLyrics": None}])
        return h.send(404, {"code": 404}) if ep == "api/get" else h.send(200, [])

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    """No politeness delays in tests."""
    monkeypatch.setattr(lyrics_sync, "LRCLIB_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(lyrics_sync, "LRCMUX_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(lyrics_sync, "RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(lyrics_sync, "STOP", False)


@pytest.fixture
def mock_api():
    apis = []

    def make(mode="normal"):
        api = MockAPI(mode)
        apis.append(api)
        return api

    yield make
    for a in apis:
        a.close()


def _silence(seconds, dest):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
                    "-t", str(seconds), str(dest)], check=True)


@pytest.fixture
def library(tmp_path):
    """A small tagged library covering every code path. Requires ffmpeg."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, SYLT, TALB, TIT2, TPE1, USLT

    m = tmp_path / "music"
    files = {
        "gig": m / "Elevation Worship/Graves Into Gardens (2020)/01 - Graves Into Gardens.flac",
        "blessing": m / "Elevation Worship/Graves Into Gardens (2020)/02 - The Blessing (feat. Kari Jobe).flac",
        "sana": m / "Marcos Witt/Sana Nuestra Tierra (2001)/01 - Sana Nuestra Tierra.mp3",
        "renuevame": m / "Marcos Witt/Sana Nuestra Tierra (2001)/02 - Renuévame.mp3",
        "cambio": m / "Funky/Rewind/05 - Cambio De Plan.flac",
        "interludio": m / "Instrumentals/Piano/01 - Interludio.flac",
        "untagged": m / "Untagged Artist/Some Album/03 - Cancion Sin Tags.mp3",
        "hidden": m / ".trash/ignored.mp3",
        "excluded": m / "@eaDir/thumb.mp3",
    }
    lengths = {"gig": 30, "blessing": 25, "sana": 25, "renuevame": 21, "cambio": 20, "interludio": 15,
               "untagged": 10, "hidden": 5, "excluded": 5}
    for k, p in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        _silence(lengths[k], p)

    def flac(key, artist, title, album, **extra):
        f = FLAC(files[key])
        f["artist"], f["title"], f["album"] = artist, title, album
        for k, v in extra.items():
            f[k] = v
        f.save()

    def mp3(key, artist, title, album, frames=()):
        tags = ID3()
        tags.add(TIT2(encoding=3, text=title))
        tags.add(TPE1(encoding=3, text=artist))
        tags.add(TALB(encoding=3, text=album))
        for fr in frames:
            tags.add(fr)
        tags.save(files[key])

    flac("gig", "Elevation Worship", "Graves Into Gardens", "Graves Into Gardens")
    flac("blessing", "Elevation Worship", "The Blessing (feat. Kari Jobe)", "Graves Into Gardens")
    flac("cambio", "Funky", "Cambio De Plan", "Rewind",
         lyrics="[00:01.00]Linea uno\n[00:05.00]Linea dos\n[00:09.50]Linea tres\n[00:14.00]Linea cuatro")
    flac("interludio", "Piano Guy", "Interludio", "Piano")
    mp3("sana", "Marcos Witt", "Sana Nuestra Tierra", "Sana Nuestra Tierra",
        [SYLT(encoding=3, lang="spa", format=2, type=1,
              text=[("Si mi pueblo", 1000), ("se humillare", 6000), ("y orare", 12000), ("sana nuestra tierra", 20000)])])
    mp3("renuevame", "Marcos Witt", "Renuévame", "Sana Nuestra Tierra",
        [USLT(encoding=3, lang="spa", desc="", text="Renuévame Señor Jesús\nya no quiero ser igual")])
    files["renuevame"].with_suffix(".lrc").write_text("Renuévame Señor Jesús\nya no quiero ser igual\n", encoding="utf-8")
    return m, files


@pytest.fixture
def run(tmp_path):
    """run(api, *flags) -> (stats, syncer): one pass over tmp_path/music with the mock API."""

    def _run(api, *flags):
        args = lyrics_sync.parse_args([
            "--music-dir", str(tmp_path / "music"), "--state-dir", str(tmp_path / "config"),
            "--lrclib-url", f"{api.base}/lrclib", "--lrcmux-url", f"{api.base}/mux", *flags])
        syncer = lyrics_sync.Syncer(args)
        return syncer.run(), syncer

    return _run
