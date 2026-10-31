#!/usr/bin/env python3
"""
lyrics-sync — give every track in a music library a synced .lrc sidecar.
https://github.com/ayumpo/lyrics-sync

For each audio file without a synced .lrc next to it:
  1. embedded  : export synced lyrics already inside the file (ID3 SYLT, or LRC text
                 in USLT / LYRICS / ©lyr) — no network needed
  2. lrclib    : LRCLIB  /api/get (exact match incl. duration) -> /api/search
  3. lrcmux    : lrcmux LRCLIB-compatible API (Deezer, YouTube Music, KuGou, ...)

Result is written as <same name>.lrc (UTF-8) next to the audio file, which is what
Navidrome (0.63+ reads sidecars first), Plex (sidecar only), Jellyfin and Emby expect.
A plain-text .lrc is upgraded when synced lyrics are found (the old file is backed up
under STATE_DIR/backup). Misses are remembered in SQLite and retried after RETRY_DAYS
(backing off further on repeated misses), so nightly runs only hit the network for
new or due tracks.

Only dependency: mutagen.   Python 3.10+.

Config (env vars, CLI flags override — see README for the full list):
  MUSIC_DIR=/music  STATE_DIR=/config  PROVIDERS=embedded,lrclib,lrcmux
  LRCLIB_URL=https://lrclib.net  LRCMUX_URL=https://api.lrcmux.dev/compat/lrclib
  RETRY_DAYS=14  UPGRADE_PLAIN=true  WRITE_PLAIN_TXT=false  MAX_RUNTIME_MIN=0
  EXCLUDE=@eaDir,#recycle,#snapshot,lost+found   (folders starting with "." are always skipped)
  PUID / PGID (chown new files)   FILE_MODE=664
"""
from __future__ import annotations

import argparse
import csv
import difflib
import fcntl
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:
    import mutagen
except ImportError:  # pragma: no cover
    sys.exit("mutagen is required:  pip install mutagen")

VERSION = "1.1.0"
PROJECT_URL = "https://github.com/ayumpo/lyrics-sync"
USER_AGENT = f"lyrics-sync/{VERSION} (+{PROJECT_URL})"
LRCLIB_MIN_INTERVAL = 1.0    # seconds between requests (be polite to the free LRCLIB service)
LRCMUX_MIN_INTERVAL = 1.05   # lrcmux allows 60 requests/minute
RETRY_DELAYS = (2.0, 5.0)    # 3 attempts per request; a busy provider costs ~7 s, not minutes
PREFLIGHT_TRACK = ("Elevation Worship", "Graves Into Gardens")  # widely available, synced on both services
DEFAULT_EXCLUDE = "@eaDir,#recycle,#snapshot,lost+found"
AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif", ".wav"}
TS_RE = re.compile(r"^\s*\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]", re.M)
LOG = logging.getLogger("lyrics-sync")
STOP = False


def env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


# ----------------------------------------------------------------------------- LRC helpers

def timed_lines(text: str | None) -> list[float]:
    """Return timestamps (seconds) of every timed line in an LRC string."""
    out = []
    for m, s, frac in TS_RE.findall(text or ""):
        f = int(frac) / (10 ** len(frac)) if frac else 0.0
        out.append(int(m) * 60 + int(s) + f)
    return out


def is_synced(text: str | None, duration: float | None = None) -> bool:
    ts = timed_lines(text)
    if len(ts) < 3:
        return False
    if duration and duration > 0 and max(ts) > duration + 15:
        return False  # timings run past the end of this recording -> wrong version
    return True


def sylt_to_lrc(frame) -> str | None:
    """Convert an ID3 SYLT frame (list of (text, time)) to LRC. format 2 = milliseconds."""
    if getattr(frame, "format", 2) != 2 or not frame.text:
        return None
    lines = []
    for txt, ms in frame.text:
        txt = (txt or "").strip().lstrip("\n")
        m, rem = divmod(int(ms), 60000)
        s, cs = divmod(rem, 1000)
        lines.append(f"[{m:02d}:{s:02d}.{cs // 10:02d}]{txt}")
    return "\n".join(lines)


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s.lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


FEAT_RE = re.compile(r"\s*[\(\[]\s*(feat\.?|ft\.?|featuring|with|con|part\.?)\s[^\)\]]*[\)\]]", re.I)
SUFFIX_RE = re.compile(r"\s*[-–]\s*(remaster(ed)?|\d{4} remaster(ed)?|radio edit|single version|album version).*$", re.I)


def title_variants(title: str) -> list[str]:
    out = [title.strip()]
    t = FEAT_RE.sub("", title)
    t = SUFFIX_RE.sub("", t)
    t = re.sub(r"\s*[\(\[](remaster(ed)?|\d{4} remaster(ed)?|explicit|clean)[^\)\]]*[\)\]]", "", t, flags=re.I).strip()
    if t and t not in out:
        out.append(t)
    return out


def artist_variants(artist: str) -> list[str]:
    out = [artist.strip()]
    primary = re.split(r"\s*(?:,|;|/|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|&)\s*", artist, flags=re.I)[0].strip()
    if primary and primary not in out:
        out.append(primary)
    return out


# ----------------------------------------------------------------------------- tag reading

@dataclass
class TrackInfo:
    artist: str = ""
    title: str = ""
    album: str = ""
    duration: float = 0.0
    embedded: str | None = None  # embedded lyrics text (synced or plain)


def _first(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "text"):  # ID3 frame
        v = v.text
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    return str(v).strip()


def read_track(path: Path) -> TrackInfo:
    info = TrackInfo()
    f = mutagen.File(path)
    if f is None:
        raise ValueError("unsupported/unreadable audio file")
    info.duration = float(getattr(f.info, "length", 0) or 0)
    tags = f.tags
    if tags is None:
        return info
    cls = type(tags).__name__
    if cls == "ID3" or hasattr(tags, "getall"):  # MP3 / AIFF / WAV with ID3
        info.artist = _first(tags.get("TPE1")) or _first(tags.get("TPE2"))
        info.title = _first(tags.get("TIT2"))
        info.album = _first(tags.get("TALB"))
        for fr in tags.getall("SYLT"):
            lrc = sylt_to_lrc(fr)
            if lrc and is_synced(lrc):
                info.embedded = lrc
                break
        if not info.embedded:
            for fr in tags.getall("USLT"):
                if fr.text and fr.text.strip():
                    info.embedded = fr.text
                    if is_synced(fr.text):
                        break
    elif cls in ("MP4Tags",):
        info.artist = _first(tags.get("\xa9ART")) or _first(tags.get("aART"))
        info.title = _first(tags.get("\xa9nam"))
        info.album = _first(tags.get("\xa9alb"))
        info.embedded = _first(tags.get("\xa9lyr")) or None
    else:  # Vorbis comments (FLAC, Ogg, Opus) and anything dict-like
        low = {k.lower(): v for k, v in tags.items()}
        info.artist = _first(low.get("artist")) or _first(low.get("albumartist")) or _first(low.get("album artist"))
        info.title = _first(low.get("title"))
        info.album = _first(low.get("album"))
        for key in ("syncedlyrics", "lyrics", "unsyncedlyrics", "unsynced lyrics"):
            val = low.get(key)
            if val:
                txt = "\n".join(map(str, val)) if isinstance(val, list) else str(val)
                if txt.strip():
                    info.embedded = txt
                    if is_synced(txt):
                        break
    return info


def guess_from_path(path: Path, root: Path, info: TrackInfo) -> TrackInfo:
    """Fill missing artist/title from Artist/Album/NN - Title.ext layout."""
    rel = path.relative_to(root).parts
    if not info.artist and len(rel) >= 2:
        info.artist = rel[0]
    if not info.title:
        stem = path.stem.split(" - ")[-1]
        info.title = re.sub(r"^\d{1,2}-\d{1,3}\s+|^\d{1,3}[.\s]+", "", stem).strip()
    return info


# ----------------------------------------------------------------------------- providers

@dataclass
class Found:
    synced: str | None
    plain: str | None
    instrumental: bool
    source: str


class LrclibLikeProvider:
    """Client for LRCLIB and LRCLIB-compatible APIs (lrcmux /compat/lrclib)."""

    def __init__(self, name: str, base: str, min_interval: float, timeout: float = 30):
        self.name, self.base = name, base.rstrip("/")
        self.min_interval, self.timeout = min_interval, timeout
        self._last = 0.0
        self.fail_streak = 0
        self.paused_until = 0.0
        self.pause_minutes = 10.0  # doubles each time the breaker trips, resets on success
        self.calls = 0
        self.http_errors: dict[int, int] = {}

    @property
    def available(self) -> bool:
        if self.paused_until and time.time() >= self.paused_until:
            LOG.info("%s: pause over, trying it again", self.name)
            self.paused_until = 0.0
        return time.time() >= self.paused_until

    def _ok(self):
        self.fail_streak = 0
        self.pause_minutes = 10.0

    def _request(self, path: str, params: dict) -> tuple[int, object]:
        """GET with short retries. Returns (status, json); status -1 = provider down/overloaded."""
        url = f"{self.base}{path}?{urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, '')})}"
        delays = RETRY_DELAYS
        for attempt in range(len(delays) + 1):
            if STOP:
                return 0, None
            wait = self._last + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            retry_in = delays[attempt] if attempt < len(delays) else None
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    self._ok()
                    return r.status, json.loads(r.read().decode("utf-8") or "null")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    self._ok()
                    return 404, None
                if e.code not in (429, 500, 502, 503, 504):
                    n = self.http_errors[e.code] = self.http_errors.get(e.code, 0) + 1
                    if n <= 3 or n % 100 == 0:
                        LOG.warning("%s %s -> HTTP %s (seen %d times)", self.name, path, e.code, n)
                    if e.code in (401, 403):  # blocked: treat as outage so the breaker trips
                        break
                    return e.code, None
                ra = e.headers.get("Retry-After")
                if retry_in is not None and ra and ra.isdigit():
                    retry_in = min(float(ra), 30.0)
                LOG.debug("%s %s -> HTTP %s", self.name, path, e.code)
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError, OSError) as e:
                LOG.debug("%s %s -> %s", self.name, path, e)
            if retry_in is None:
                break
            time.sleep(retry_in)
        # gave up on this request: circuit breaker so an outage doesn't stall the whole run
        self.fail_streak += 1
        if self.fail_streak >= 3:
            self.paused_until = time.time() + self.pause_minutes * 60
            LOG.warning("%s is down/overloaded — skipping it for %.0f min (other providers continue)",
                        self.name, self.pause_minutes)
            self.pause_minutes = min(self.pause_minutes * 2, 120.0)
            self.fail_streak = 0
        return -1, None

    @staticmethod
    def _to_found(item: dict, source: str) -> Found:
        return Found(item.get("syncedLyrics") or None, item.get("plainLyrics") or None,
                     bool(item.get("instrumental")), source)

    def lookup(self, t: TrackInfo) -> Found | None:
        """Return the best hit (synced preferred). None on miss; raises ConnectionError on outage."""
        best_plain: Found | None = None
        dur = round(t.duration) if t.duration else None
        # 1) exact signature lookup with the file's own tags (needs duration; album helps)
        if dur:
            code, data = self._request("/api/get", {"artist_name": t.artist, "track_name": t.title,
                                                    "album_name": t.album, "duration": dur})
            if code == -1:
                raise ConnectionError(f"{self.name} unavailable")
            if code == 200 and isinstance(data, dict):
                f = self._to_found(data, self.name)
                rdur = float(data.get("duration") or 0)
                if rdur and t.duration and abs(rdur - t.duration) > 4:
                    f = None  # matched a different version (remix/live/edit) — don't trust it
                elif f.instrumental or is_synced(f.synced, t.duration):
                    return f
                best_plain = f if (f and f.plain) else None
        # 2) fuzzy search with cleaned title, full artist then primary artist
        title = title_variants(t.title)[-1]
        for artist in artist_variants(t.artist):
            code, data = self._request("/api/search", {"track_name": title, "artist_name": artist})
            if code == -1:
                raise ConnectionError(f"{self.name} unavailable")
            if code == 200 and isinstance(data, list) and data:
                hit = self._pick(data, t, title)
                if hit and (hit.instrumental or is_synced(hit.synced, t.duration)):
                    return hit
                best_plain = best_plain or hit
                if hit:
                    break  # track is known here, just not synced — don't burn more calls
        return best_plain

    def _pick(self, items: list, t: TrackInfo, title: str) -> Found | None:
        scored = []
        for it in items:
            if not isinstance(it, dict):
                continue
            ts = similarity(title, it.get("trackName") or it.get("name") or "")
            ars = similarity(t.artist, it.get("artistName") or "")
            if ts < 0.75 or ars < 0.5:
                continue
            idur = float(it.get("duration") or 0)
            ddiff = abs(idur - t.duration) if (idur and t.duration) else 999
            if t.duration and idur and ddiff > 4:
                continue
            synced = is_synced(it.get("syncedLyrics"), t.duration)
            scored.append((0 if synced else 1, ddiff, -(ts + ars), it))
        if not scored:
            return None
        scored.sort(key=lambda x: x[:3])
        return self._to_found(scored[0][3], self.name)


# ----------------------------------------------------------------------------- state

class State:
    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(db_path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS tracks(
            path TEXT PRIMARY KEY, mtime REAL, status TEXT, source TEXT,
            artist TEXT, title TEXT, checked REAL, attempts INTEGER DEFAULT 0)""")
        self.db.commit()

    def get(self, rel: str):
        return self.db.execute("SELECT mtime,status,checked,attempts FROM tracks WHERE path=?", (rel,)).fetchone()

    def put(self, rel, mtime, status, source, t: TrackInfo | None, bump_attempts: bool):
        prev = self.get(rel)
        attempts = ((prev[3] or 0) + 1) if (bump_attempts and prev) else (1 if bump_attempts else 0)
        self.db.execute("INSERT OR REPLACE INTO tracks VALUES(?,?,?,?,?,?,?,?)",
                        (rel, mtime, status, source, t.artist if t else "", t.title if t else "", time.time(), attempts))

    def commit(self):
        self.db.commit()

    def export_misses(self, out: Path):
        rows = self.db.execute("SELECT path,artist,title,status,attempts,datetime(checked,'unixepoch','localtime') "
                               "FROM tracks WHERE status IN ('none','plain_only','error') ORDER BY path").fetchall()
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["path", "artist", "title", "status", "attempts", "last_checked"])
            w.writerows(rows)
        return len(rows)


# ----------------------------------------------------------------------------- main work

class Syncer:
    def __init__(self, args):
        self.a = args
        self.root = Path(args.music_dir)
        self.state_dir = Path(args.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state = State(self.state_dir / "lyrics-sync.db")
        self.providers: list[LrclibLikeProvider] = []
        for p in [x.strip().lower() for x in args.providers.split(",") if x.strip()]:
            if p == "lrclib":
                self.providers.append(LrclibLikeProvider("lrclib", args.lrclib_url, LRCLIB_MIN_INTERVAL))
            elif p == "lrcmux":
                self.providers.append(LrclibLikeProvider("lrcmux", args.lrcmux_url, LRCMUX_MIN_INTERVAL))
        self.use_embedded = "embedded" in args.providers.lower()
        self.excludes = {x.strip() for x in args.exclude.split(",") if x.strip()}
        self.uid = int(os.environ["PUID"]) if os.environ.get("PUID") else None
        self.gid = int(os.environ["PGID"]) if os.environ.get("PGID") else None
        self.mode = int(os.environ.get("FILE_MODE", "664"), 8)
        self.stats: dict[str, int] = {}

    def bump(self, key):
        self.stats[key] = self.stats.get(key, 0) + 1

    def record(self, *args):
        """Persist a track's result — skipped in dry-run so a trial run leaves no trace."""
        if not self.a.dry_run:
            self.state.put(*args)

    def iter_audio(self):
        start = self.root / self.a.path if self.a.path else self.root
        cutoff = time.time() - self.a.since_hours * 3600 if self.a.since_hours else None
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = sorted(d for d in dirnames if d not in self.excludes and not d.startswith("."))
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                if p.suffix.lower() in AUDIO_EXTS and not fn.startswith("."):
                    if cutoff:
                        try:
                            if p.stat().st_mtime < cutoff:
                                continue
                        except OSError:
                            continue
                    yield p

    def write_file(self, dest: Path, text: str):
        if self.a.dry_run:
            return
        tmp = dest.with_name(f".{dest.name}.tmp")
        tmp.write_text(text.strip() + "\n", encoding="utf-8")
        os.chmod(tmp, self.mode)
        if self.uid is not None or self.gid is not None:
            try:
                os.chown(tmp, self.uid if self.uid is not None else -1, self.gid if self.gid is not None else -1)
            except PermissionError:
                pass
        os.replace(tmp, dest)

    def backup(self, lrc: Path):
        if self.a.dry_run:
            return
        dest = self.state_dir / "backup" / lrc.relative_to(self.root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lrc, dest)

    def due(self, rel: str, mtime: float) -> bool:
        if self.a.force:
            return True
        row = self.state.get(rel)
        if not row:
            return True
        prev_mtime, status, checked, attempts = row
        if abs((prev_mtime or 0) - mtime) > 1:
            return True  # file changed (retagged/replaced)
        if status in ("none", "plain_only", "instrumental", "error"):
            backoff_days = self.a.retry_days * min(max(attempts, 1), 4)
            if status == "instrumental":
                backoff_days = self.a.retry_days * 8
            return time.time() - (checked or 0) > backoff_days * 86400
        return True

    def process(self, audio: Path):
        rel = str(audio.relative_to(self.root))
        lrc = audio.with_suffix(".lrc")
        try:
            mtime = audio.stat().st_mtime
        except OSError:
            return
        existing = None
        if lrc.exists():
            try:
                existing = lrc.read_text(encoding="utf-8", errors="replace")
            except OSError:
                existing = None
            if is_synced(existing):
                self.bump("already_synced")
                return
            if not self.a.upgrade_plain:
                self.bump("plain_kept")
                return
        if not self.due(rel, mtime):
            self.bump("skipped_recent_miss")
            return

        try:
            t = guess_from_path(audio, self.root, read_track(audio))
        except Exception as e:  # noqa: BLE001
            LOG.warning("unreadable: %s (%s)", rel, e)
            self.record(rel, mtime, "error", "", None, True)
            self.bump("unreadable")
            return

        # 1) embedded synced lyrics -> sidecar, no network
        if self.use_embedded and t.embedded and is_synced(t.embedded, t.duration):
            if existing:
                self.backup(lrc)
            self.write_file(lrc, t.embedded)
            self.record(rel, mtime, "synced", "embedded", t, False)
            LOG.info("EMBED  %s", rel)
            self.bump("from_embedded")
            return

        if not t.title or not t.artist:
            self.record(rel, mtime, "error", "", t, True)
            self.bump("missing_tags")
            return

        plain_hit: Found | None = None
        outage = False
        for prov in self.providers:
            if STOP:
                return
            if not prov.available:
                outage = True
                continue
            try:
                hit = prov.lookup(t)
            except ConnectionError:
                outage = True
                continue
            if not hit:
                continue
            if hit.instrumental:
                self.record(rel, mtime, "instrumental", hit.source, t, True)
                LOG.info("INSTR  %s [%s]", rel, hit.source)
                self.bump("instrumental")
                return
            if is_synced(hit.synced, t.duration):
                if existing:
                    self.backup(lrc)
                self.write_file(lrc, hit.synced)
                self.record(rel, mtime, "synced", hit.source, t, False)
                LOG.info("%-6s %s%s", hit.source.upper(), rel, "  (upgraded plain .lrc)" if existing else "")
                self.bump(f"from_{hit.source}")
                return
            plain_hit = plain_hit or hit

        if outage and not plain_hit:
            # don't record a miss we never really checked; try again next run
            self.bump("deferred_provider_down")
            return
        if plain_hit and plain_hit.plain and self.a.write_plain_txt:
            txt = audio.with_suffix(".txt")
            if not txt.exists() and not existing and not (t.embedded and t.embedded.strip()):
                self.write_file(txt, plain_hit.plain)
                self.bump("wrote_plain_txt")
        self.record(rel, mtime, "plain_only" if plain_hit else "none", plain_hit.source if plain_hit else "", t, True)
        LOG.debug("MISS   %s (%s / %s)", rel, t.artist, t.title)
        self.bump("plain_only" if plain_hit else "not_found")

    def preflight(self):
        """One known-good lookup per provider, so the log shows whether each source is reachable."""
        for p in self.providers:
            code, data = p._request("/api/search", {"track_name": PREFLIGHT_TRACK[1], "artist_name": PREFLIGHT_TRACK[0]})
            items = data if isinstance(data, list) else []
            synced = sum(1 for d in items if isinstance(d, dict) and d.get("syncedLyrics"))
            LOG.info("preflight %-6s %s — %d results, %d synced%s", p.name,
                     "unreachable/busy" if code == -1 else f"HTTP {code}", len(items), synced,
                     "" if code == 200 and synced else "   <-- not usable right now")

    def run(self) -> dict:
        """Process the library once. Returns the counters (also logged)."""
        started = time.time()
        deadline = started + self.a.max_runtime_min * 60 if self.a.max_runtime_min else None
        if not self.a.dry_run or self.a.verbose:
            self.preflight()
        last_progress = time.time()
        n = 0
        for audio in self.iter_audio():
            if STOP or (deadline and time.time() > deadline):
                LOG.info("stopping early (%s)", "signal" if STOP else "max runtime reached")
                break
            if self.a.limit and n >= self.a.limit:
                break
            self.process(audio)
            n += 1
            if n % 50 == 0:
                self.state.commit()
            if n % 250 == 0 or time.time() - last_progress >= 300:
                last_progress = time.time()
                LOG.info("progress: %d files scanned (now in %s)  %s", n,
                         str(audio.relative_to(self.root)).split(os.sep)[0], self.summary())
        self.state.commit()
        misses = 0 if self.a.dry_run else self.state.export_misses(self.state_dir / "misses.csv")
        calls = ", ".join(f"{p.name}={p.calls}" for p in self.providers)
        LOG.info("done in %.1f min — %d files scanned — %s — API calls: %s — misses.csv rows: %d%s",
                 (time.time() - started) / 60, n, self.summary(), calls or "none", misses,
                 "  [DRY RUN: nothing written]" if self.a.dry_run else "")
        return dict(self.stats)

    def summary(self) -> str:
        return " ".join(f"{k}={v}" for k, v in sorted(self.stats.items()))


def parse_args(argv=None):
    e = os.environ.get
    ap = argparse.ArgumentParser(description="Fetch synced .lrc lyrics for a music library.")
    ap.add_argument("--music-dir", default=e("MUSIC_DIR", "/music"))
    ap.add_argument("--state-dir", default=e("STATE_DIR", "/config"))
    ap.add_argument("--providers", default=e("PROVIDERS", "embedded,lrclib,lrcmux"),
                    help="comma list, in order: embedded,lrclib,lrcmux")
    ap.add_argument("--lrclib-url", default=e("LRCLIB_URL", "https://lrclib.net"))
    ap.add_argument("--lrcmux-url", default=e("LRCMUX_URL", "https://api.lrcmux.dev/compat/lrclib"))
    ap.add_argument("--retry-days", type=float, default=float(e("RETRY_DAYS", "14")))
    ap.add_argument("--max-runtime-min", type=float, default=float(e("MAX_RUNTIME_MIN", "0")))
    ap.add_argument("--exclude", default=e("EXCLUDE", DEFAULT_EXCLUDE),
                    help="comma list of folder names to skip (dot-folders are always skipped)")
    ap.add_argument("--path", default="", help="only process this subfolder (relative to music dir)")
    ap.add_argument("--since-hours", type=float, default=0, help="only files modified in the last N hours")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files (testing)")
    ap.add_argument("--force", action="store_true", help="ignore remembered misses")
    ap.add_argument("--dry-run", action="store_true", help="look up but write nothing")
    ap.add_argument("--no-upgrade-plain", dest="upgrade_plain", action="store_false",
                    default=env_bool("UPGRADE_PLAIN", True), help="leave existing plain .lrc files alone")
    ap.add_argument("--write-plain-txt", action="store_true", default=env_bool("WRITE_PLAIN_TXT", False),
                    help="when only plain lyrics exist, write them to .txt (Plex/Navidrome fallback)")
    ap.add_argument("-v", "--verbose", action="store_true", help="also log every miss and HTTP retry")
    ap.add_argument("--version", action="version", version=f"lyrics-sync {VERSION}")
    return ap.parse_args(argv)


def setup_logging(state_dir: Path, verbose: bool):
    for h in list(LOG.handlers):
        LOG.removeHandler(h)
        h.close()
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    LOG.addHandler(sh)
    state_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(state_dir / "lyrics-sync.log", maxBytes=5_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    LOG.addHandler(fh)


def main(argv=None):
    args = parse_args(argv)
    setup_logging(Path(args.state_dir), args.verbose)

    def _stop(*_):
        global STOP
        STOP = True
        LOG.info("signal received — finishing current track")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    lock = open(Path(args.state_dir) / "lyrics-sync.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG.info("another run is in progress — exiting")
        return 0
    if not Path(args.music_dir).is_dir():
        LOG.error("music dir not found: %s", args.music_dir)
        return 2
    LOG.info("lyrics-sync %s — music=%s providers=%s%s", VERSION, args.music_dir, args.providers,
             " (dry run)" if args.dry_run else "")
    Syncer(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
