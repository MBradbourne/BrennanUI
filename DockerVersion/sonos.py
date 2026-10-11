"""Talk to Sonos speakers directly (UPnP/SOAP on port 1400) so the UI can show and edit a
room's queue.

When the Brennan plays to a Sonos room it loads tracks into that room's own Sonos queue
(the one the Sonos app shows), and `queueID&sonos=n` appends to it. The Brennan can't list
that queue, so we read it from the speaker. Standard library only.
"""
from __future__ import annotations

import html
import re
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

PORT = 1400
_AVT = ("/MediaRenderer/AVTransport/Control", "urn:schemas-upnp-org:service:AVTransport:1")
_CD = ("/MediaServer/ContentDirectory/Control", "urn:schemas-upnp-org:service:ContentDirectory:1")
_ZGT = ("/ZoneGroupTopology/Control", "urn:schemas-upnp-org:service:ZoneGroupTopology:1")
NS = {"didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
      "dc": "http://purl.org/dc/elements/1.1/",
      "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/"}


class SonosError(Exception):
    pass


def norm_room(name: str) -> str:
    """Room names differ in apostrophes/case between the Brennan and Sonos."""
    s = unicodedata.normalize("NFKC", name or "").replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", s).strip().lower()


def soap(ip: str, svc: tuple[str, str], action: str, args: dict, timeout: float = 6) -> dict:
    path, stype = svc
    body = "".join(f"<{k}>{html.escape(str(v), quote=False)}</{k}>" for k, v in args.items())
    env = ('<?xml version="1.0" encoding="utf-8"?>'
           '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           f'<u:{action} xmlns:u="{stype}">{body}</u:{action}></s:Body></s:Envelope>')
    req = urllib.request.Request(f"http://{ip}:{PORT}{path}", data=env.encode("utf-8"), method="POST",
                                 headers={"Content-Type": 'text/xml; charset="utf-8"',
                                          "SOAPACTION": f'"{stype}#{action}"'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        code = re.search(r"<errorCode>(\d+)</errorCode>", detail)
        raise SonosError(f"{action} failed ({e.code}{', UPnP error ' + code.group(1) if code else ''})")
    except OSError as e:
        raise SonosError(f"{action}: can't reach the speaker at {ip} ({e})")
    root = ET.fromstring(raw)
    out = {}
    for el in root.iter():
        if el.tag.endswith(action + "Response"):
            for child in el:
                out[child.tag.split("}")[-1]] = child.text or ""
    return out



# ---------------------------------------------------------------- discovery
class Sonos:
    """Finds speakers (SSDP, then known addresses) and maps room name -> group coordinator."""

    def __init__(self, extra_candidates=lambda: []):
        self.extra_candidates = extra_candidates
        self.lock = threading.Lock()
        # any speaker IPs we've seen; BRENNAN_SONOS_HOSTS seeds it (e.g. Docker bridge mode,
        # where multicast discovery can't see the LAN)
        import os
        self.known: set[str] = {h.strip() for h in os.environ.get("BRENNAN_SONOS_HOSTS", "").split(",") if h.strip()}
        self.rooms: dict[str, dict] = {}   # norm name -> {name, coordinator, members}
        self.at = 0.0

    def _ssdp(self, wait: float = 2.0) -> set[str]:
        msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
               "MX: 1\r\nST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n").encode()
        found: set[str] = set()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.settimeout(0.3)
            for _ in range(2):
                s.sendto(msg, ("239.255.255.250", 1900))
            end = time.time() + wait
            while time.time() < end:
                try:
                    data, addr = s.recvfrom(4096)
                except socket.timeout:
                    continue
                if b"Sonos" in data or b"ZonePlayer" in data:
                    found.add(addr[0])
        except OSError:
            pass
        finally:
            s.close()
        return found

    def _is_sonos(self, ip: str) -> bool:
        try:
            with urllib.request.urlopen(f"http://{ip}:{PORT}/xml/device_description.xml", timeout=0.8) as r:
                return b"Sonos" in r.read(4000)
        except Exception:
            return False

    def _topology(self, ip: str) -> dict[str, dict]:
        res = soap(ip, _ZGT, "GetZoneGroupState", {})
        root = ET.fromstring(res.get("ZoneGroupState", ""))
        rooms: dict[str, dict] = {}
        for g in root.iter("ZoneGroup"):
            coord_uuid = g.get("Coordinator")
            members = [m for m in g.iter("ZoneGroupMember") if m.get("Invisible") != "1"]
            ips = {m.get("UUID"): re.sub(r"^https?://([^:/]+).*$", r"\1", m.get("Location", "")) for m in g.iter("ZoneGroupMember")}
            coord_ip = ips.get(coord_uuid)
            if not coord_ip:
                continue
            for m in members:
                name = m.get("ZoneName", "")
                rooms[norm_room(name)] = {"name": name, "coordinator": coord_ip, "ip": ips.get(m.get("UUID")),
                                          "group": sorted({x.get("ZoneName", "") for x in members})}
        return rooms

    def refresh(self, force: bool = False) -> dict[str, dict]:
        with self.lock:
            if not force and self.rooms and time.time() - self.at < 60:
                return self.rooms
            def probe():
                yield list(self.known)
                yield sorted(self._ssdp() - self.known)
                from concurrent.futures import ThreadPoolExecutor
                ips = [ip for ip in list(self.extra_candidates())[:1024] if ip not in self.known]
                with ThreadPoolExecutor(64) as ex:
                    yield [ip for ip, ok in zip(ips, ex.map(self._is_sonos, ips)) if ok]
            for cands in probe():
                for ip in cands:
                    try:
                        rooms = self._topology(ip)
                    except Exception:
                        continue
                    if rooms:
                        self.rooms, self.at = rooms, time.time()
                        self.known |= {r["ip"] for r in rooms.values() if r.get("ip")} | {r["coordinator"] for r in rooms.values()}
                        return self.rooms
            raise SonosError("No Sonos speakers found on the network")

    def coordinator(self, room: str) -> str:
        key = norm_room(room)
        rooms = self.refresh()
        if key not in rooms:
            rooms = self.refresh(force=True)
        if key not in rooms:
            raise SonosError(f"Sonos room '{room}' not found")
        return rooms[key]["coordinator"]

    # ------------------------------------------------------------ queue
    def queue(self, room: str) -> dict:
        ip = self.coordinator(room)
        media = soap(ip, _AVT, "GetMediaInfo", {"InstanceID": 0})
        pos = soap(ip, _AVT, "GetPositionInfo", {"InstanceID": 0})
        state = soap(ip, _AVT, "GetTransportInfo", {"InstanceID": 0}).get("CurrentTransportState", "")
        uri = media.get("CurrentURI", "")
        from_queue = uri.startswith("x-rincon-queue:")
        if uri.startswith("x-rincon:"):        # grouped: should not happen for a coordinator
            from_queue = False
        items, start, total = [], 0, None
        while True:
            r = soap(ip, _CD, "Browse", {"ObjectID": "Q:0", "BrowseFlag": "BrowseDirectChildren",
                                          "Filter": "dc:title,dc:creator,upnp:album,upnp:albumArtURI,res",
                                          "StartingIndex": start, "RequestedCount": 200, "SortCriteria": ""}, timeout=10)
            total = int(r.get("TotalMatches") or 0)
            got = int(r.get("NumberReturned") or 0)
            items += self._didl(r.get("Result", ""), ip, start)
            start += got
            if got == 0 or start >= total or start >= 1000:
                break
        cur = int(pos.get("Track") or 0) if from_queue else 0
        return {"room": room, "coordinator": ip, "fromQueue": from_queue, "current": cur,
                "state": state, "total": total or len(items), "items": items}

    @staticmethod
    def _didl(xml: str, ip: str, offset: int) -> list[dict]:
        if not xml:
            return []
        root = ET.fromstring(xml)
        out = []
        for i, it in enumerate(root.findall("didl:item", NS)):
            def t(path):
                el = it.find(path, NS)
                return (el.text or "") if el is not None else ""
            art = t("upnp:albumArtURI")
            if art.startswith("/"):
                art = f"http://{ip}:{PORT}{art}"
            out.append({"n": offset + i + 1, "title": t("dc:title"), "artist": t("dc:creator"),
                        "album": t("upnp:album"), "art": art})
        return out

    def move(self, room: str, start: int, count: int, before: int) -> None:
        """Move `count` tracks starting at 1-based `start` so they sit before 1-based `before`."""
        ip = self.coordinator(room)
        soap(ip, _AVT, "ReorderTracksInQueue", {"InstanceID": 0, "StartingIndex": start,
                                                 "NumberOfTracks": count, "InsertBefore": before, "UpdateID": 0})

    def remove(self, room: str, n: int) -> None:
        ip = self.coordinator(room)
        soap(ip, _AVT, "RemoveTrackRangeFromQueue", {"InstanceID": 0, "UpdateID": 0,
                                                     "StartingIndex": n, "NumberOfTracks": 1})

    def play_track(self, room: str, n: int) -> None:
        ip = self.coordinator(room)
        media = soap(ip, _AVT, "GetMediaInfo", {"InstanceID": 0})
        if not media.get("CurrentURI", "").startswith("x-rincon-queue:"):
            raise SonosError("That room isn't playing from its queue")
        soap(ip, _AVT, "Seek", {"InstanceID": 0, "Unit": "TRACK_NR", "Target": n})
        soap(ip, _AVT, "Play", {"InstanceID": 0, "Speed": 1})

    # ------------------------------------------------------------ position
    @staticmethod
    def _secs(t: str) -> int:
        try:
            h, m, s = (t or "0:0:0").split(":")
            return int(h) * 3600 + int(m) * 60 + int(float(s))
        except ValueError:
            return 0

    def position(self, room: str) -> dict:
        """Elapsed / total seconds of what the room is playing (the Brennan only knows its own)."""
        ip = self.coordinator(room)
        pos = soap(ip, _AVT, "GetPositionInfo", {"InstanceID": 0})
        state = soap(ip, _AVT, "GetTransportInfo", {"InstanceID": 0}).get("CurrentTransportState", "")
        dur = self._secs(pos.get("TrackDuration", ""))
        if not dur:   # tracks the Brennan hands Sonos carry no length; read it from the file
            meta = pos.get("TrackMetaData", "")
            m = re.search(r'duration="(\d+:\d+:\d+)', meta)
            dur = self._secs(m.group(1)) if m else 0
        if not dur:
            dur = file_duration(pos.get("TrackURI", ""))
        mode = soap(ip, _AVT, "GetTransportSettings", {"InstanceID": 0}).get("PlayMode", "NORMAL")
        return {"into": self._secs(pos.get("RelTime", "")), "duration": dur, "state": state,
                "playing": state in ("PLAYING", "TRANSITIONING"), "track": int(pos.get("Track") or 0),
                "playMode": mode, "shuffle": mode.startswith("SHUFFLE")}

    # Sonos keeps shuffle and repeat in one "play mode"; flip shuffle and keep the repeat setting
    _SHUF = {"NORMAL": "SHUFFLE_NOREPEAT", "REPEAT_ALL": "SHUFFLE", "REPEAT_ONE": "SHUFFLE_REPEAT_ONE"}

    def set_shuffle(self, room: str, on: bool) -> str:
        ip = self.coordinator(room)
        mode = soap(ip, _AVT, "GetTransportSettings", {"InstanceID": 0}).get("PlayMode", "NORMAL")
        unshuf = {v: k for k, v in self._SHUF.items()}
        new = self._SHUF.get(mode, mode) if on else unshuf.get(mode, mode)
        if new != mode:
            soap(ip, _AVT, "SetPlayMode", {"InstanceID": 0, "NewPlayMode": new})
        return new

    def seek(self, room: str, seconds: int) -> None:
        ip = self.coordinator(room)
        s = max(0, int(seconds))
        soap(ip, _AVT, "Seek", {"InstanceID": 0, "Unit": "REL_TIME", "Target": f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"})


# ---------------------------------------------------------------- track length from the file
_DUR_CACHE: dict[str, int] = {}


def _fetch_range(url: str, rng: str, timeout: float = 5) -> tuple[bytes, int]:
    req = urllib.request.Request(url, headers={"Range": f"bytes={rng}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        total = 0
        cr = r.headers.get("Content-Range", "")
        if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
            total = int(cr.rsplit("/", 1)[1])
        elif r.status == 200:
            total = int(r.headers.get("Content-Length") or 0)
        return r.read(4 << 20), total


def _mp4_duration(buf: bytes) -> float:
    i = buf.find(b"mvhd")
    if i < 4 or i + 28 > len(buf):
        return 0
    v = buf[i + 4]
    if v == 1:
        ts = int.from_bytes(buf[i + 24:i + 28], "big"); du = int.from_bytes(buf[i + 28:i + 36], "big")
    else:
        ts = int.from_bytes(buf[i + 16:i + 20], "big"); du = int.from_bytes(buf[i + 20:i + 24], "big")
    return du / ts if ts else 0


def _mp3_duration(head: bytes, size: int) -> float:
    i = 0
    if head[:3] == b"ID3":
        i = 10 + ((head[6] & 0x7f) << 21 | (head[7] & 0x7f) << 14 | (head[8] & 0x7f) << 7 | (head[9] & 0x7f))
    while i + 4 < len(head) and not (head[i] == 0xFF and head[i + 1] & 0xE0 == 0xE0):
        i += 1
    if i + 4 >= len(head):
        return 0
    b1, b2 = head[i + 1], head[i + 2]
    ver = (b1 >> 3) & 3          # 3 = MPEG1
    rates = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
    sr = rates.get(ver, [0, 0, 0])[(b2 >> 2) & 3] if (b2 >> 2) & 3 < 3 else 0
    kbps1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    kbps2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
    br = (kbps1 if ver == 3 else kbps2)[b2 >> 4]
    spf = 1152 if ver == 3 else 576
    for tag in (b"Xing", b"Info"):            # VBR header: exact frame count
        j = head.find(tag, i, i + 200)
        if j > 0 and head[j + 7] & 1 and sr:
            return int.from_bytes(head[j + 8:j + 12], "big") * spf / sr
    return (size - i) * 8 / (br * 1000) if br and size else 0


def file_duration(url: str) -> int:
    """Length in seconds of an audio file served over HTTP (FLAC, WAV, MP3, M4A/AAC)."""
    if not url.startswith("http"):
        return 0
    if url in _DUR_CACHE:
        return _DUR_CACHE[url]
    secs = 0.0
    try:
        head, size = _fetch_range(url, "0-262143")
        if head[:4] == b"fLaC":
            si = head[8:26]                      # STREAMINFO body
            rate = int.from_bytes(si[10:13], "big") >> 4
            total = int.from_bytes(si[13:18], "big") & 0xFFFFFFFFF
            secs = total / rate if rate else 0
        elif head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            i, byte_rate, data = 12, 0, 0
            while i + 8 <= len(head):
                cid, ln = head[i:i + 4], int.from_bytes(head[i + 4:i + 8], "little")
                if cid == b"fmt ":
                    byte_rate = int.from_bytes(head[i + 16:i + 20], "little")
                elif cid == b"data":
                    data = ln if ln not in (0, 0xFFFFFFFF) else max(0, size - i - 8)
                    break
                i += 8 + ln + (ln & 1)
            secs = data / byte_rate if byte_rate else 0
        elif head[4:8] == b"ftyp":
            secs = _mp4_duration(head)
            if not secs and size > len(head):    # moov at the end of the file
                tail, _ = _fetch_range(url, f"-{min(size, 2 << 20)}")
                secs = _mp4_duration(tail)
        else:
            secs = _mp3_duration(head, size)
    except Exception:
        secs = 0
    if secs > 0:
        if len(_DUR_CACHE) > 500:
            _DUR_CACHE.clear()
        _DUR_CACHE[url] = int(round(secs))
    return int(round(secs))
