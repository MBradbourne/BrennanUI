#!/usr/bin/env python3
"""
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
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artwork  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
UI_DIR = APP_DIR / "ui"
CONFIG_PATH = Path.home() / ".brennan_ui.json"
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
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", out):
            found.append(ipaddress.IPv4Interface(m.group(1)))
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

    # -- routes --------------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/api":
            return self.proxy(parsed.query)
        if path == "/local/device":
            return self.send_json(200, self.device.snapshot())
        if path == "/local/art-search":
            q = urllib.parse.parse_qs(parsed.query)
            artist, album = q.get("artist", [""])[0], q.get("album", [""])[0]
            if not (artist or album):
                return self.send_json(400, {"error": "artist or album required"})
            return self.send_json(200, artwork.search(artist, album))
        if path == "/local/art-scan":
            return self.send_json(200, self.missing.snapshot())
        return self.static(path)

    do_HEAD = do_GET

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length > 25_000_000:
            return self.send_json(413, {"error": "file too large"})
        body = self.rfile.read(length) if length else b"{}"
        if path == "/local/art-upload":
            return self.art_set(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("id", ["0"])[0], data=body)
        try:
            data = json.loads(body or b"{}")
        except Exception:
            data = {}
        if path == "/local/art-set":
            return self.art_set(data.get("id"), url=str(data.get("url", "")).strip())
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
            self.missing.start(self.device.ip)
            return self.send_json(200, self.missing.snapshot())
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

    def art_set(self, album_id, url: str | None = None, data: bytes | None = None):
        ip = self.device.ip
        if not ip:
            return self.send_json(503, {"error": "device_unavailable"})
        try:
            album_id = int(album_id)
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "bad album id"})
        if not (1_000_000 <= album_id < 2_000_000):
            return self.send_json(400, {"error": "not an album id"})
        if url and not re.match(r"^https?://", url):
            return self.send_json(400, {"error": "URL must start with http:// or https://"})
        if not url and not data:
            return self.send_json(400, {"error": "url or image required"})
        res = artwork.set_art(ip, album_id, url=url or None, data=data)
        if res.get("ok"):
            self.missing.mark_fixed(album_id, ip)
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
        headers = {"Cache-Control": "no-store"}
        if cmd in ("getAlbumArt", "getCurrentArt", "getVideoThumbnail"):
            if not body:
                return self.send_bytes(404, b"", "text/plain", headers)
            ctype = sniff_image(body) or ctype
            if cmd == "getAlbumArt":
                headers["Cache-Control"] = "private, max-age=86400"
        elif ctype.startswith("text/") or ctype == "application/octet-stream":
            s = body.lstrip()[:1]
            if s in (b"{", b"["):
                ctype = "application/json"
        self.send_bytes(200, body, ctype, headers)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Local UI for the Brennan B3+")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--ip", help="skip scanning and use this address")
    ap.add_argument("--subnet", help="scan this network instead, e.g. 192.168.1.0/24")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()

    if not (UI_DIR / "index.html").exists():
        sys.exit(f"UI not found at {UI_DIR}/index.html")

    dev = Device(args.subnet, args.ip)
    Handler.device = dev

    def save_placeholders(placeholders, allowed):
        dev.cfg["placeholder_hashes"] = placeholders
        dev.cfg["not_placeholder_hashes"] = allowed
        save_config(dev.cfg)
    Handler.missing = artwork.MissingScan(dev.cfg.get("placeholder_hashes", []), save_placeholders,
                                          dev.cfg.get("not_placeholder_hashes", []))
    dev.scan(force=True)

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"Port {args.port} is busy ({e}). Is the UI already running? Try --port 8766")
    url = f"http://127.0.0.1:{args.port}/"
    log(f"Brennan UI running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("bye")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
