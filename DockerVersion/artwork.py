"""
Album-artwork helpers for the Brennan UI launcher.

- search(): look for cover art for an artist/album in several free sources
  (Apple/iTunes, Deezer, MusicBrainz + Cover Art Archive) at once.
- set_art(): tell the Brennan to use an image. First asks the unit to download
  the URL itself (what the stock UI does). If the art didn't change — e.g. the
  unit can't fetch that HTTPS host — the image is downloaded here and handed to
  the unit from a short-lived local "relay" web server instead. The relay is
  also how you set art from a file on your computer.
- MissingScan: walks the whole library and lists albums with no artwork.

Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FCGI = "b2gci.fcgi"
UA = "BrennanUI/1.0 (personal Brennan B3 library tool)"
HTTP_TIMEOUT = 8


# --------------------------------------------------------------------------- #
# small HTTP helpers
# --------------------------------------------------------------------------- #

def _get(url: str, timeout: float = HTTP_TIMEOUT, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_json(url: str, timeout: float = HTTP_TIMEOUT):
    return json.loads(_get(url, timeout, "application/json").decode("utf-8", "replace"))


def _final_url(url: str, timeout: float = HTTP_TIMEOUT) -> str | None:
    """Follow redirects (Cover Art Archive → archive.org) and return the real image URL."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status == 200 and r.headers.get("Content-Type", "").startswith("image/"):
                return r.geturl()
    except Exception:
        return None
    return None


def device_call(ip: str, query: str, timeout: float = 15) -> bytes:
    with urllib.request.urlopen(f"http://{ip}/{FCGI}?{query}", timeout=timeout) as r:
        return r.read()


def album_art_bytes(ip: str, album_id: int) -> bytes:
    try:
        return device_call(ip, f"getAlbumArt&id={album_id}&time={int(time.time()*1000)}", timeout=10)
    except Exception:
        return b""


def sniff(body: bytes) -> str | None:
    if body[:3] == b"\xff\xd8\xff":
        return "jpg"
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return None


def image_size(body: bytes) -> tuple[int, int] | None:
    """Width/height of a JPEG or PNG without any imaging library."""
    try:
        if body[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(body[16:20], "big"), int.from_bytes(body[20:24], "big")
        if body[:2] == b"\xff\xd8":
            i = 2
            while i < len(body) - 9:
                if body[i] != 0xFF:
                    i += 1
                    continue
                marker = body[i + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    h = int.from_bytes(body[i + 5:i + 7], "big")
                    w = int.from_bytes(body[i + 7:i + 9], "big")
                    return w, h
                seg = int.from_bytes(body[i + 2:i + 4], "big")
                i += 2 + seg
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

_DISC = re.compile(r"\s*[\(\[\-–:]?\s*(cd|disc|disk)\s*\d+\s*[\)\]]?\s*$", re.I)
_EXTRA = re.compile(r"\s*[\(\[][^\)\]]*(remaster|deluxe|edition|expanded|anniversary|bonus|mono|stereo|version)[^\)\]]*[\)\]]", re.I)


def clean_album(name: str) -> str:
    s = re.sub(r"<[^>]*>", "", name or "")
    s = _DISC.sub("", s)
    s = _EXTRA.sub("", s)
    return s.strip(" -–")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower().replace("&", "and"))


def _score(want_artist: str, want_album: str, artist: str, album: str) -> float:
    a, b = _norm(want_artist), _norm(artist)
    c, d = _norm(want_album), _norm(album)
    s = 0.0
    if a and b and (a == b or a in b or b in a):
        s += 1
    if c and d:
        if c == d:
            s += 1.5
        elif c in d or d in c:
            s += 0.8
    return s


def _itunes(artist: str, album: str) -> list[dict]:
    q = urllib.parse.quote_plus(f"{artist} {album}")
    data = _get_json(f"https://itunes.apple.com/search?term={q}&entity=album&limit=10")
    out = []
    for r in data.get("results", []):
        art = r.get("artworkUrl100")
        if not art:
            continue
        out.append({"source": "Apple Music", "artist": r.get("artistName", ""),
                    "album": r.get("collectionName", ""),
                    "url": art.replace("100x100bb", "600x600bb"),
                    "large": art.replace("100x100bb", "1200x1200bb"),
                    "thumb": art.replace("100x100bb", "300x300bb"),
                    "year": (r.get("releaseDate") or "")[:4]})
    return out


def _deezer(artist: str, album: str) -> list[dict]:
    q = urllib.parse.quote(f'artist:"{artist}" album:"{album}"')
    data = _get_json(f"https://api.deezer.com/search/album?q={q}&limit=10")
    items = data.get("data") or []
    if not items:  # fall back to a loose query
        q = urllib.parse.quote(f"{artist} {album}")
        items = (_get_json(f"https://api.deezer.com/search/album?q={q}&limit=10").get("data") or [])
    out = []
    for r in items:
        if not r.get("cover_big"):
            continue
        out.append({"source": "Deezer", "artist": (r.get("artist") or {}).get("name", ""),
                    "album": r.get("title", ""), "url": r["cover_big"],
                    "large": r.get("cover_xl") or r["cover_big"],
                    "thumb": r.get("cover_medium") or r["cover_big"], "year": ""})
    return out


def _musicbrainz(artist: str, album: str) -> list[dict]:
    query = f'releasegroup:"{album}" AND artist:"{artist}"'
    url = "https://musicbrainz.org/ws/2/release-group/?fmt=json&limit=6&query=" + urllib.parse.quote(query)
    data = _get_json(url)
    groups = data.get("release-groups") or []

    def one(g):
        gid = g.get("id")
        if not gid:
            return None
        real = _final_url(f"https://coverartarchive.org/release-group/{gid}/front-500")
        if not real:
            return None
        credit = (g.get("artist-credit") or [{}])[0]
        return {"source": "MusicBrainz", "artist": credit.get("name", ""), "album": g.get("title", ""),
                "url": real, "large": real, "thumb": real,
                "year": (g.get("first-release-date") or "")[:4]}

    with ThreadPoolExecutor(max_workers=6) as ex:
        return [r for r in ex.map(one, groups) if r]


SOURCES = {"Apple Music": _itunes, "Deezer": _deezer, "MusicBrainz": _musicbrainz}


def search(artist: str, album: str) -> dict:
    album_q = clean_album(album)
    results, errors = [], {}

    def run(name_fn):
        name, fn = name_fn
        try:
            return name, fn(artist, album_q)
        except Exception as e:
            return name, e

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        for name, res in ex.map(run, SOURCES.items()):
            if isinstance(res, Exception):
                errors[name] = str(res)
            else:
                results.extend(res)

    seen, uniq = set(), []
    for r in results:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        r["score"] = _score(artist, album_q, r["artist"], r["album"])
        uniq.append(r)
    order = {"Apple Music": 0, "Deezer": 1, "MusicBrainz": 2}
    uniq.sort(key=lambda r: (-r["score"], order.get(r["source"], 9)))
    return {"query": {"artist": artist, "album": album_q}, "results": uniq[:24], "errors": errors}


# --------------------------------------------------------------------------- #
# Relay: let the Brennan download an image from this computer
# --------------------------------------------------------------------------- #

def lan_ip_towards(ip: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, 80))  # no packets sent
        return s.getsockname()[0]
    finally:
        s.close()


class _Relay:
    """One-file web server on the LAN interface, alive for a few seconds."""

    def __init__(self, bind_ip: str, body: bytes, ext: str, port: int = 0, advertise: str | None = None):
        token = hashlib.sha1(body + str(time.time()).encode()).hexdigest()[:16]
        self.path = f"/{token}/cover.{ext}"
        self.hits = 0
        relay = self
        ctype = "image/png" if ext == "png" else "image/jpeg"

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.split("?")[0] != relay.path:
                    self.send_response(404)
                    self.end_headers()
                    return
                relay.hits += 1
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer((bind_ip, port), H)
        self.url = f"http://{advertise or bind_ip}:{self.httpd.server_address[1]}{self.path}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


def _ask_device(ip: str, album_id: int, url: str):
    # Same parameter order the stock UI uses: id, url, time.
    q = f"getArtFromURL&id={album_id}&url={url}&time={int(time.time()*1000)}"
    try:
        device_call(ip, q, timeout=30)
    except Exception:
        pass


def _wait_for_change(ip: str, album_id: int, before: bytes, seconds: float = 8) -> bytes | None:
    end = time.time() + seconds
    while time.time() < end:
        now = album_art_bytes(ip, album_id)
        if now and now != before:
            return now
        time.sleep(0.8)
    return None


def set_art(ip: str, album_id: int, url: str | None = None, data: bytes | None = None) -> dict:
    """Make `url` (or raw image `data`) the cover for album_id. Returns a result dict."""
    before = album_art_bytes(ip, album_id)

    if url and not data:
        _ask_device(ip, album_id, url)
        after = _wait_for_change(ip, album_id, before, 6)
        if after:
            return {"ok": True, "method": "direct", "bytes": len(after), "size": image_size(after)}
        try:
            data = _get(url, timeout=15)
        except Exception as e:
            return {"ok": False, "error": f"Couldn't download that image ({e})"}

    if not data:
        return {"ok": False, "error": "No image"}
    ext = sniff(data)
    if not ext:
        return {"ok": False, "error": "That file isn't a JPEG or PNG image"}

    try:
        # Docker bridge networking: the container's own address isn't reachable from the
        # Brennan, so bind a fixed, published port and advertise the host's LAN address.
        adv = os.environ.get("BRENNAN_RELAY_HOST", "").strip()
        if adv:
            relay = _Relay("0.0.0.0", data, ext, port=int(os.environ.get("BRENNAN_RELAY_PORT", "8766")), advertise=adv)
        else:
            relay = _Relay(lan_ip_towards(ip), data, ext)
    except Exception as e:
        return {"ok": False, "error": f"Couldn't start the local relay ({e})"}
    try:
        _ask_device(ip, album_id, relay.url)
        after = _wait_for_change(ip, album_id, before, 8)
    finally:
        time.sleep(0.3)
        relay.close()
    if after:
        return {"ok": True, "method": "relay", "bytes": len(after), "size": image_size(after)}
    if relay.hits == 0:
        return {"ok": False, "error": "The Brennan never fetched the image from this computer — "
                                      "check that the Mac's firewall allows incoming connections for Python."}
    return {"ok": False, "error": "The Brennan downloaded the image but the artwork didn't change."}


# --------------------------------------------------------------------------- #
# Missing-art scan
# --------------------------------------------------------------------------- #

LOWRES_PX = 300            # covers smaller than this (longest side) are flagged
PLACEHOLDER_ARTISTS = 3    # same image on albums by this many different artists = placeholder


class MissingScan:
    """Walks the library and flags albums whose cover is missing, a generic
    placeholder, or very low resolution.

    Placeholder detection: the Brennan (or the CD lookup it uses) sometimes
    assigns the same stock image to unrelated albums. Any image shared by
    albums from PLACEHOLDER_ARTISTS or more different artists is treated as a
    placeholder (multi-disc sets by one artist sharing a cover are fine). You
    can also mark a cover as a placeholder by hand; those image fingerprints
    are remembered in the launcher's config file.
    """

    def __init__(self, known_placeholders=None, save_placeholders=None, not_placeholders=None):
        self.lock = threading.Lock()
        self.state = "idle"
        self.done = 0
        self.total = 0
        self.records: dict[int, dict] = {}     # album id -> {id, album, artist, hash, w, h}
        self.issues: list[dict] = []
        self.auto_placeholders: set[str] = set()
        self.known = set(known_placeholders or [])      # remembered placeholder fingerprints
        self.allowed = set(not_placeholders or [])      # fingerprints you said are fine
        self._save = save_placeholders
        self.finished_at = 0.0

    # -- public ---------------------------------------------------------------
    def snapshot(self) -> dict:
        with self.lock:
            counts = {"missing": 0, "placeholder": 0, "lowres": 0}
            for it in self.issues:
                counts[it["reason"]] += 1
            return {"state": self.state, "done": self.done, "total": self.total,
                    "missing": list(self.issues), "counts": counts,
                    "placeholders": len(self.known),
                    "finished_at": self.finished_at}

    def mark_fixed(self, album_id: int, ip: str | None = None):
        """Called after new art is applied: re-check that album."""
        rec = None
        if ip:
            body = album_art_bytes(ip, album_id)
            with self.lock:
                rec = self.records.get(album_id)
                if rec is not None:
                    rec.update(self._fingerprint(body))
        with self.lock:
            if rec is None:
                self.issues = [m for m in self.issues if m["id"] != album_id]
                return
            changed = self._classify()
        if changed:
            self._persist()

    def _persist(self):
        if self._save:
            with self.lock:
                known, allowed = sorted(self.known), sorted(self.allowed)
            self._save(known, allowed)

    def mark_placeholder(self, ip: str, album_id: int) -> dict:
        body = album_art_bytes(ip, album_id)
        if not sniff(body):
            return {"ok": False, "error": "That album has no artwork to mark"}
        h = hashlib.sha1(body).hexdigest()
        with self.lock:
            self.known.add(h)
            self.allowed.discard(h)
            if album_id in self.records:
                self.records[album_id].update(self._fingerprint(body))
            self._classify()
            affected = sum(1 for r in self.records.values() if r.get("hash") == h)
        self._persist()
        return {"ok": True, "hash": h, "albums_with_this_image": affected}

    def unmark_placeholder(self, ip: str, album_id: int) -> dict:
        body = album_art_bytes(ip, album_id)
        h = hashlib.sha1(body).hexdigest() if body else None
        if not h:
            return {"ok": False, "error": "That album has no artwork"}
        with self.lock:
            self.known.discard(h)
            self.allowed.add(h)
            self._classify()
        self._persist()
        return {"ok": True}

    def is_placeholder(self, body: bytes) -> bool:
        if not body:
            return False
        h = hashlib.sha1(body).hexdigest()
        with self.lock:
            return h in self.known or h in self.auto_placeholders

    def start(self, ip: str):
        with self.lock:
            if self.state == "running":
                return
            self.state, self.done, self.total = "running", 0, 0
            self.records, self.issues = {}, []
        threading.Thread(target=self._run, args=(ip,), daemon=True).start()

    # -- internals --------------------------------------------------------------
    @staticmethod
    def _fingerprint(body: bytes) -> dict:
        if not sniff(body):
            return {"hash": None, "w": 0, "h": 0}
        size = image_size(body) or (0, 0)
        return {"hash": hashlib.sha1(body).hexdigest(), "w": size[0], "h": size[1]}

    def _classify(self) -> bool:
        """(lock held) Rebuild auto-placeholders and the issues list from records.
        Newly spotted placeholders are remembered, so fixing some of the albums
        that share one doesn't make the rest drop off the list. Returns True if
        the remembered set changed."""
        by_hash: dict[str, set] = {}
        for r in self.records.values():
            if r.get("hash"):
                by_hash.setdefault(r["hash"], set()).add(_norm(r.get("artist", "")))
        self.auto_placeholders = {h for h, artists in by_hash.items()
                                  if len(artists) >= PLACEHOLDER_ARTISTS and h not in self.allowed}
        new = self.auto_placeholders - self.known
        self.known |= new
        placeholders = self.known
        issues = []
        for r in self.records.values():
            if not r.get("hash"):
                reason = "missing"
            elif r["hash"] in placeholders:
                reason = "placeholder"
            elif r.get("w") and max(r["w"], r["h"]) < LOWRES_PX:
                reason = "lowres"
            else:
                continue
            issues.append({"id": r["id"], "album": r["album"], "artist": r["artist"], "reason": reason,
                           "w": r.get("w", 0), "h": r.get("h", 0)})
        order = {"missing": 0, "placeholder": 1, "lowres": 2}
        issues.sort(key=lambda m: (order[m["reason"]], m["artist"].lower(), m["album"].lower()))
        self.issues = issues
        return bool(new)

    def _run(self, ip: str):
        try:
            q = (f"search&artists=N&tracks=N&radio=N&video=N&offset=0&count=10000"
                 f"&time={int(time.time()*1000)}&string=")
            albums = json.loads(device_call(ip, q, timeout=30).decode("utf-8", "replace"))
        except Exception:
            with self.lock:
                self.state = "error"
            return
        with self.lock:
            self.total = len(albums)

        def check(a):
            fp = self._fingerprint(album_art_bytes(ip, a["id"]))
            with self.lock:
                self.records[a["id"]] = {"id": a["id"], "album": a.get("album", ""), "artist": a.get("artist", ""), **fp}
                self.done += 1
                if self.done % 25 == 0:
                    self._classify()

        with ThreadPoolExecutor(max_workers=4) as ex:   # be gentle with the unit
            list(ex.map(check, albums))
        with self.lock:
            self._classify()
            self.state, self.finished_at = "done", time.time()
        self._persist()
