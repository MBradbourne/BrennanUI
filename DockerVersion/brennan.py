#!/usr/bin/env python3
"""
Brennan B3+ UI — SERVER / DOCKER EDITION.

Same app as the desktop launcher, but meant to run on an always-on machine (e.g. in Docker)
and be opened from any device on your network: phone, tablet, another computer.
Differences from the desktop version:
  * listens on all interfaces (--host, default 0.0.0.0) instead of 127.0.0.1 only
  * never opens a browser
  * every option can also be set with an environment variable (BRENNAN_*)
  * settings file location is configurable (BRENNAN_CONFIG, /data/brennan_ui.json in Docker)
  * optional password (BRENNAN_PASSWORD) using standard browser sign-in (HTTP Basic auth)
  * one shared status poll of the Brennan, however many devices have the page open
  * reads /proc/net/arp when the `arp` tool isn't installed (slim containers)

Brennan B3+ local UI launcher.

- Finds the B3+ on your network (remembers the last address, checks the ARP
  cache first, then sweeps your subnet).
- Serves the new UI at http://127.0.0.1:8765 and proxies its API calls to the
  unit, so the page never needs to know the unit's IP address.
- If the unit stops answering (e.g. it came back on a new address after a power
  cycle) it rescans automatically.

Standard library only. Run:  python3 brennan.py   (Ctrl+C to stop)
"""
from __future__ import annotations

import argparse
import base64
import hmac
import hashlib
import ipaddress
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artwork  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
UI_DIR = APP_DIR / "ui"
CONFIG_PATH = Path(os.environ.get("BRENNAN_CONFIG") or (Path.home() / ".brennan_ui.json"))
FCGI = "b2gci.fcgi"            # the endpoint the stock UI uses for everything
PROBE_TIMEOUT = 0.8            # seconds per host while scanning
PROXY_TIMEOUT = 12             # radio directory calls can be slow
MAX_SCAN_HOSTS = 1024          # cap: a /22 (TP-Link Deco default) or smaller
WORKERS = 128

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception as e:  # not fatal
        log(f"could not save config: {e}")


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def probe(ip: str, timeout: float = PROBE_TIMEOUT) -> dict | None:
    """Return device info if `ip` is a Brennan, else None."""
    url = f"http://{ip}/{FCGI}?status&{int(time.time() * 1000)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read(65536).decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(data, dict) or "source" not in data or "volume" not in data:
        return None
    info = {"ip": ip, "version": "", "tracks": None, "albums": None, "artists": None}
    try:
        url = f"http://{ip}/{FCGI}?getWebInfo&time={int(time.time() * 1000)}"
        with urllib.request.urlopen(url, timeout=2) as r:
            w = json.loads(r.read().decode("utf-8", "replace"))
            info.update(version=w.get("version", ""), tracks=w.get("tracks"),
                        albums=w.get("albums"), artists=w.get("artists"))
    except Exception:
        pass
    return info


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""


def local_networks() -> list[ipaddress.IPv4Interface]:
    """IPv4 interfaces of this machine (excluding loopback/link-local)."""
    found: list[ipaddress.IPv4Interface] = []
    if platform.system() == "Darwin" or sys.platform.startswith("freebsd"):
        out = _run(["ifconfig"])
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)", out):
            ip, mask = m.group(1), int(m.group(2), 16)
            prefix = bin(mask).count("1")
            found.append(ipaddress.IPv4Interface(f"{ip}/{prefix}"))
    elif sys.platform.startswith("linux"):
        out = _run(["ip", "-o", "-4", "addr", "show"])
        for m in re.finditer(r"^\d+:\s+(\S+)\s+inet (\d+\.\d+\.\d+\.\d+/\d+)", out, re.M):
            # skip Docker / VM / container bridges: the Brennan is never on those
            if re.match(r"(docker|br-|veth|virbr|cni|flannel|cali|tailscale|zt|lxc)", m.group(1)):
                continue
            found.append(ipaddress.IPv4Interface(m.group(2)))
    elif sys.platform.startswith("win"):
        out = _run(["ipconfig"])
        ips = re.findall(r"IPv4 Address[ .]*: (\d+\.\d+\.\d+\.\d+)", out)
        masks = re.findall(r"Subnet Mask[ .]*: (\d+\.\d+\.\d+\.\d+)", out)
        for ip, mask in zip(ips, masks):
            found.append(ipaddress.IPv4Interface(f"{ip}/{mask}"))

    # Fallback: the address used for the default route, assume /24
    if not found:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.0.2.1", 9))  # no packets are sent
            found.append(ipaddress.IPv4Interface(f"{s.getsockname()[0]}/24"))
            s.close()
        except Exception:
            pass

    keep = []
    for itf in found:
        a = itf.ip
        if a.is_loopback or a.is_link_local or not a.is_private:
            continue
        keep.append(itf)
    return keep


def arp_candidates() -> list[str]:
    out = _run(["arp", "-an"]) if sys.platform != "win32" else _run(["arp", "-a"])
    if not out and Path("/proc/net/arp").exists():          # slim Linux images have no `arp`
        try:
            out = Path("/proc/net/arp").read_text()
        except Exception:
            out = ""
    ips = re.findall(r"\(?(\d+\.\d+\.\d+\.\d+)\)?", out)
    seen, res = set(), []
    for ip in ips:
        try:
            a = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if a.is_private and not a.is_loopback and ip not in seen and not ip.endswith(".255"):
            seen.add(ip)
            res.append(ip)
    return res


def scan_order(subnet: str | None, last_ip: str | None) -> list[str]:
    nets: list[tuple[ipaddress.IPv4Network, ipaddress.IPv4Address | None]] = []
    if subnet:
        nets.append((ipaddress.IPv4Network(subnet, strict=False), None))
    else:
        for itf in local_networks():
            net = itf.network
            if net.num_addresses > MAX_SCAN_HOSTS:
                # narrow to the block of MAX_SCAN_HOSTS that contains our address
                new_prefix = 32 - (MAX_SCAN_HOSTS.bit_length() - 1)
                net = ipaddress.IPv4Network(f"{itf.ip}/{new_prefix}", strict=False)
            nets.append((net, itf.ip))

    order: list[str] = []
    seen: set[str] = set()

    def add(ip: str):
        if ip not in seen:
            seen.add(ip)
            order.append(ip)

    in_scope = lambda ip: any(ipaddress.IPv4Address(ip) in n for n, _ in nets) if nets else True
    if last_ip:
        add(last_ip)
    for ip in arp_candidates():
        if in_scope(ip):
            add(ip)
    for net, mine in nets:
        hosts = [str(h) for h in net.hosts() if h != mine]
        if mine is not None:
            # same /24 first, then outward
            m = int(mine)
            hosts.sort(key=lambda h: (int(ipaddress.IPv4Address(h)) >> 8 != m >> 8,
                                      abs(int(ipaddress.IPv4Address(h)) - m)))
        for h in hosts:
            add(h)
    return order


def discover(subnet: str | None, last_ip: str | None, progress=None) -> dict | None:
    candidates = scan_order(subnet, last_ip)
    if not candidates:
        log("no local network found to scan")
        return None
    log(f"scanning {len(candidates)} addresses for a Brennan…")
    # quick check of the remembered address with a longer timeout
    if last_ip:
        hit = probe(last_ip, timeout=2.0)
        if hit:
            return hit
    done = 0
    ex = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        futures = {ex.submit(probe, ip): ip for ip in candidates}
        for fut in as_completed(futures):
            done += 1
            if progress and done % 32 == 0:
                progress(done, len(candidates))
            res = fut.result()
            if res:
                return res
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return None


# --------------------------------------------------------------------------- #
# Device state
# --------------------------------------------------------------------------- #

class Device:
    def __init__(self, subnet: str | None, fixed_ip: str | None):
        self.lock = threading.Lock()
        self.subnet = subnet
        self.cfg = load_config()
        self.info: dict | None = None
        self.state = "idle"          # idle | scanning | found | notfound
        self.progress = (0, 0)
        self.failures = 0
        self.last_scan = 0.0
        self.fixed_ip = fixed_ip

    @property
    def ip(self) -> str | None:
        with self.lock:
            return self.info["ip"] if self.info else None

    def snapshot(self) -> dict:
        with self.lock:
            return {"state": self.state, "device": self.info,
                    "progress": {"done": self.progress[0], "total": self.progress[1]}}

    def set_found(self, info: dict):
        with self.lock:
            self.info, self.state, self.failures = info, "found", 0
        self.cfg["last_ip"] = info["ip"]
        save_config(self.cfg)
        log(f"Brennan found at {info['ip']}  {info.get('version', '')}")

    def scan(self, force: bool = False):
        with self.lock:
            if self.state == "scanning":
                return
            gap = 20 if self.state == "notfound" else 5   # back off while the unit is off
            if not force and time.time() - self.last_scan < gap:
                return
            self.state, self.progress, self.last_scan = "scanning", (0, 0), time.time()

        def prog(done, total):
            with self.lock:
                self.progress = (done, total)

        def run():
            last = self.fixed_ip or self.cfg.get("last_ip")
            if self.fixed_ip:
                info = probe(self.fixed_ip, timeout=3)
            else:
                info = discover(self.subnet, last, prog)
            if info:
                self.set_found(info)
            else:
                with self.lock:
                    self.state, self.info = "notfound", None
                log("no Brennan found — is it switched on and on this network?")

        threading.Thread(target=run, daemon=True).start()

    def manual(self, ip: str) -> bool:
        info = probe(ip, timeout=3)
        if info:
            self.set_found(info)
            return True
        return False

    def report_failure(self):
        with self.lock:
            self.failures += 1
            n = self.failures
        if n >= 2:
            log("lost contact with the Brennan — rescanning")
            self.scan()

    def report_ok(self):
        with self.lock:
            self.failures = 0


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #

# Commands that can change the library (and therefore the unit's item IDs).
WRITE_CMDS = {"renameID", "moveID", "deleteID", "getArtFromURL", "artURL", "rip", "newRip",
              "reindex", "scanDisk", "upload", "USBImport", "mixPlaylists"}


class Library:
    """Tracks a 'generation' fingerprint of the Brennan's library.

    The unit's album/artist/track IDs are positions in its index, not permanent
    identifiers: renaming, moving, ripping or deleting can renumber them. The UI
    tags everything it caches (art URLs, lists, the artwork scan) with the
    generation, and drops it all when the generation changes.
    """

    def __init__(self, device: "Device"):
        self.device = device
        self.lock = threading.Lock()
        self.gen = "0"
        self.edits = 0
        self.sig = ""
        self.albums: list = []
        self._kick = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def bump(self):
        with self.lock:
            self.edits += 1
        self._kick.set()

    def refresh(self) -> str:
        ip = self.device.ip
        if not ip:
            return self.gen
        t = int(time.time() * 1000)
        try:
            albums = artwork.device_call(ip, f"search&artists=N&tracks=N&radio=N&video=N&offset=0&count=20000&time={t}&string=", 30)
            artists = artwork.device_call(ip, f"search&albums=N&tracks=N&radio=N&video=N&offset=0&count=20000&time={t}&string=", 30)
        except Exception:
            return self.gen
        sig = hashlib.sha1(albums + b"|" + artists).hexdigest()
        with self.lock:
            try:
                self.albums = json.loads(albums.decode("utf-8", "replace"))
            except Exception:
                pass
            self.sig = sig
            self.gen = hashlib.sha1(f"{sig}:{self.edits}".encode()).hexdigest()[:12]
            return self.gen

    def album_name(self, album_id: int) -> tuple[str, str] | None:
        with self.lock:
            for a in self.albums:
                if a.get("id") == album_id:
                    return a.get("album", ""), a.get("artist", "")
        return None

    def _loop(self):
        while True:
            self._kick.wait(timeout=2 if self.gen == "0" else 20)
            if self._kick.is_set():
                self._kick.clear()
                time.sleep(1.5)          # let the unit finish re-indexing
            old = self.gen
            new = self.refresh()
            if new != old and old != "0":
                log(f"library changed (generation {new})")


STATUS_CACHE: dict = {}
STATUS_LOCK = threading.Lock()
STATUS_FLIGHT: dict = {}
STATUS_TTL = 0.8   # seconds; pages poll every 1 s
# Commands that change what `status` reports (cache is dropped after them).
STATE_CMDS = {"play", "next", "back", "seek", "playID", "queueID", "startPlaylist", "setRandom",
              "setSorted", "setSegue", "setTone", "equaliser", "vTunerLink", "vTunerPlaySearchItem",
              "eject", "findAndPlay"}


MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".json": "application/json"}


def sniff_image(body: bytes) -> str | None:
    if body[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if body[:4] == b"GIF8":
        return "image/gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


class Handler(BaseHTTPRequestHandler):
    device: Device = None  # set at startup
    library: "Library" = None
    missing: artwork.MissingScan = None  # set at startup
    server_version = "BrennanUI/1.0"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # -- helpers -------------------------------------------------------------
    def send_bytes(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code: int, obj):
        self.send_bytes(code, json.dumps(obj).encode(), "application/json",
                        {"Cache-Control": "no-store"})

    # -- optional password ----------------------------------------------------
    password: str = ""

    def authorized(self) -> bool:
        if not self.password:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                user_pass = base64.b64decode(h[6:]).decode("utf-8", "replace")
                pw = user_pass.split(":", 1)[1] if ":" in user_pass else ""
                if hmac.compare_digest(pw, self.password):
                    return True
            except Exception:
                pass
        body = b"Sign in required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Brennan UI", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    # -- routes --------------------------------------------------------------
    def do_GET(self):
        if self.path == "/healthz":                     # for Docker's health check; no auth, no data
            return self.send_bytes(200, b"ok", "text/plain")
        if not self.authorized():
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/api":
            return self.proxy(parsed.query)
        if path == "/local/device":
            return self.send_json(200, {**self.device.snapshot(), "gen": self.library.gen})
        if path == "/local/art-search":
            q = urllib.parse.parse_qs(parsed.query)
            artist, album = q.get("artist", [""])[0], q.get("album", [""])[0]
            if not (artist or album):
                return self.send_json(400, {"error": "artist or album required"})
            return self.send_json(200, artwork.search(artist, album))
        if path == "/local/art-scan":
            return self.send_json(200, {**self.missing.snapshot(), "scan_gen": getattr(self.missing, "scan_gen", ""),
                                        "gen": self.library.gen})
        return self.static(path)

    do_HEAD = do_GET

    def do_POST(self):
        if not self.authorized():
            return
        path = urllib.parse.urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length > 25_000_000:
            return self.send_json(413, {"error": "file too large"})
        body = self.rfile.read(length) if length else b"{}"
        if path == "/local/art-upload":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            return self.art_set(qs.get("id", ["0"])[0], data=body, expect=qs.get("expect", [""])[0])
        try:
            data = json.loads(body or b"{}")
        except Exception:
            data = {}
        if path == "/local/art-set":
            return self.art_set(data.get("id"), url=str(data.get("url", "")).strip(), expect=str(data.get("expect", "")))
        if path in ("/local/art-placeholder", "/local/art-not-placeholder"):
            if not self.device.ip:
                return self.send_json(503, {"error": "device_unavailable"})
            try:
                aid = int(data.get("id"))
            except (TypeError, ValueError):
                return self.send_json(400, {"error": "bad album id"})
            fn = self.missing.mark_placeholder if path.endswith("/art-placeholder") else self.missing.unmark_placeholder
            res = fn(self.device.ip, aid)
            return self.send_json(200 if res.get("ok") else 400, res)
        if path == "/local/art-scan":
            if not self.device.ip:
                return self.send_json(503, {"error": "device_unavailable"})
            self.missing.scan_gen = self.library.gen
            self.missing.start(self.device.ip)
            return self.send_json(200, {**self.missing.snapshot(), "scan_gen": self.missing.scan_gen})
        if path == "/local/library-refresh":
            return self.send_json(200, {"gen": self.library.refresh()})
        if path == "/local/verify-album":
            # Is album `id` still the one called `expect`? (IDs can be renumbered.)
            ok, actual = self.verify_album(data.get("id"), str(data.get("expect", "")))
            return self.send_json(200 if ok else 409, {"ok": ok, "actual": actual})
        if path == "/local/rescan":
            self.device.scan(force=True)
            return self.send_json(200, self.device.snapshot())
        if path == "/local/device":
            ip = str(data.get("ip", "")).strip()
            try:
                ipaddress.IPv4Address(ip)
            except ValueError:
                return self.send_json(400, {"error": "invalid IP address"})
            ok = self.device.manual(ip)
            return self.send_json(200 if ok else 404,
                                  self.device.snapshot() if ok else {"error": f"No Brennan answered at {ip}"})
        self.send_json(404, {"error": "not found"})

    def verify_album(self, album_id, expect: str) -> tuple[bool, str]:
        """Check the unit still has `expect` (album name) at `album_id`."""
        ip = self.device.ip
        try:
            raw = artwork.device_call(ip, f"albumDetails&id={int(album_id)}&time={int(time.time()*1000)}", 10)
            name = json.loads(raw.decode("utf-8", "replace")).get("name", "")
        except Exception:
            return False, ""
        norm = lambda x: re.sub(r"<[^>]*>", "", x or "").strip().lower()
        return (not expect) or norm(name) == norm(expect), re.sub(r"<[^>]*>", "", name)

    def art_set(self, album_id, url: str | None = None, data: bytes | None = None, expect: str = ""):
        ip = self.device.ip
        if not ip:
            return self.send_json(503, {"error": "device_unavailable"})
        try:
            album_id = int(album_id)
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "bad album id"})
        if not (1_000_000 <= album_id < 2_000_000):
            return self.send_json(400, {"error": "not an album id"})
        ok, actual = self.verify_album(album_id, expect)
        if not ok:
            log(f"artwork NOT set: album {album_id} is now '{actual}', expected '{expect}'")
            return self.send_json(409, {"error": "library_changed", "actual": actual,
                                        "message": "The Brennan's library changed and that album moved. Refresh and try again."})
        if url and not re.match(r"^https?://", url):
            return self.send_json(400, {"error": "URL must start with http:// or https://"})
        if not url and not data:
            return self.send_json(400, {"error": "url or image required"})
        res = artwork.set_art(ip, album_id, url=url or None, data=data)
        if res.get("ok"):
            self.missing.mark_fixed(album_id, ip)
            self.library.bump()
            log(f"artwork set for album {album_id} ({res.get('method')})")
        else:
            log(f"artwork for album {album_id} failed: {res.get('error')}")
        return self.send_json(200 if res.get("ok") else 502, res)

    def static(self, path: str):
        if path in ("", "/"):
            path = "/index.html"
        target = (UI_DIR / path.lstrip("/")).resolve()
        if UI_DIR not in target.parents or not target.is_file():
            return self.send_bytes(404, b"Not found", "text/plain")
        ctype = MIME.get(target.suffix.lower(), "application/octet-stream")
        self.send_bytes(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    def proxy(self, query: str):
        ip = self.device.ip
        if not ip:
            self.device.scan()
            return self.send_json(503, {"error": "device_unavailable", **self.device.snapshot()})
        # Every open page polls `status` once a second. With several devices that adds up on the
        # Brennan's small web server, so share one recent answer between all of them.
        cmd0 = query.split("&", 1)[0]
        cache_key = None
        if cmd0 == "status":
            cache_key = "status"
        elif cmd0 == "sonosStatus":
            m = re.search(r"[?&]?sonos=(\d+)", query)
            cache_key = "sonosStatus:" + (m.group(1) if m else "")
        if cache_key:
            # one fetch at a time per key; everyone else waits briefly and reuses the answer
            with STATUS_LOCK:
                flight = STATUS_FLIGHT.setdefault(cache_key, threading.Lock())
            flight.acquire()
            try:
                with STATUS_LOCK:
                    hit = STATUS_CACHE.get(cache_key)
                if hit and time.time() - hit[0] < STATUS_TTL:
                    return self.send_bytes(200, hit[1], hit[2], {"Cache-Control": "no-store"})
                return self._proxy_fetch(ip, query, cache_key)
            finally:
                flight.release()
        return self._proxy_fetch(ip, query, None)

    def _proxy_fetch(self, ip: str, query: str, cache_key: str | None):
        # Pass the query through untouched: the unit is order-sensitive
        # (e.g. search needs `string=` last).
        url = f"http://{ip}/{FCGI}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=PROXY_TIMEOUT) as r:
                body = r.read()
                ctype = r.headers.get("Content-Type", "application/octet-stream")
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            self.device.report_failure()
            return self.send_json(502, {"error": "device_unreachable", "detail": str(e)})
        self.device.report_ok()
        cmd = query.split("&", 1)[0]
        if cmd in WRITE_CMDS or cmd in STATE_CMDS or cmd.startswith("vol"):
            with STATUS_LOCK:
                STATUS_CACHE.clear()          # make the next status read fresh after any command
        if cmd in WRITE_CMDS:
            self.library.bump()
        headers = {"Cache-Control": "no-store"}
        if cmd in ("getAlbumArt", "getCurrentArt", "getVideoThumbnail"):
            if not body:
                return self.send_bytes(404, b"", "text/plain", headers)
            ctype = sniff_image(body) or ctype
            # IDs can be renumbered, so only cache art requested with the current
            # library generation in the URL (&g=...). Anything else is always fresh.
            if cmd == "getAlbumArt" and f"&g={self.library.gen}" in query:
                headers["Cache-Control"] = "private, max-age=86400"
        elif ctype.startswith("text/") or ctype == "application/octet-stream":
            s = body.lstrip()[:1]
            if s in (b"{", b"["):
                ctype = "application/json"
        if cache_key:
            with STATUS_LOCK:
                STATUS_CACHE[cache_key] = (time.time(), body, ctype)
        self.send_bytes(200, body, ctype, headers)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description="Brennan B3+ UI — server edition (open it from any device)")
    ap.add_argument("--host", default=env("BRENNAN_HOST", "0.0.0.0"),
                    help="address to listen on (default 0.0.0.0 = all; 127.0.0.1 = this machine only)")
    ap.add_argument("--port", type=int, default=int(env("BRENNAN_PORT", "8765")))
    ap.add_argument("--ip", default=env("BRENNAN_IP") or None, help="skip scanning and use this address")
    ap.add_argument("--subnet", default=env("BRENNAN_SUBNET") or None,
                    help="scan this network instead, e.g. 192.168.1.0/24")
    ap.add_argument("--password", default=env("BRENNAN_PASSWORD", ""),
                    help="require this password (browser sign-in; any user name)")
    args = ap.parse_args()
    Handler.password = args.password

    if not (UI_DIR / "index.html").exists():
        sys.exit(f"UI not found at {UI_DIR}/index.html")

    dev = Device(args.subnet, args.ip)
    Handler.device = dev
    Handler.library = Library(dev)

    def save_placeholders(placeholders, allowed):
        dev.cfg["placeholder_hashes"] = placeholders
        dev.cfg["not_placeholder_hashes"] = allowed
        save_config(dev.cfg)
    Handler.missing = artwork.MissingScan(dev.cfg.get("placeholder_hashes", []), save_placeholders,
                                          dev.cfg.get("not_placeholder_hashes", []))
    dev.scan(force=True)

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        sys.exit(f"Can't listen on {args.host}:{args.port} ({e}). Is something else using that port?")
    log(f"config: {CONFIG_PATH}")
    if args.host in ("0.0.0.0", "::"):
        addrs = sorted({str(i.ip) for i in local_networks()}) or ["<this-machine's-IP>"]
        for a in addrs:
            log(f"Brennan UI running — open http://{a}:{args.port}/ from any device on your network")
    else:
        log(f"Brennan UI running at http://{args.host}:{args.port}/")
    if not args.password:
        log("no password set: anyone on your network can use this page (set BRENNAN_PASSWORD to require one)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("bye")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
