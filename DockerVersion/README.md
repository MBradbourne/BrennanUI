# Brennan UI — Server / Docker edition

The same interface as the desktop version of Brennan UI, packaged to run on an always-on
machine (a home server, NAS, Raspberry Pi or a spare Mac) so you can open it from **any
phone, tablet or computer on your home network** while the server is running.

> Not affiliated with or endorsed by Brennan. The unit's HTTP interface is undocumented, so a
> future firmware update could change it. Built and tested against firmware "B3 Aug 28 2025".

## What's different from the desktop version

| | Desktop (`BrennanUI`) | Server (`BrennanUI-docker`) |
|---|---|---|
| Who can open it | only the computer it runs on | any device on your network |
| Opens a browser on start | yes | no |
| Settings file | `~/.brennan_ui.json` | `/data/brennan_ui.json` (a Docker volume) |
| Password | none | optional (`BRENNAN_PASSWORD`) |
| Many devices at once | one browser | one shared status poll for every open page |
| Phone layout | basic | tab bar, compact player, touch-friendly rows, home-screen icon |

Everything else (discovery, artwork tools, queue, Sonos, themes) is the same code.

## Run it with Docker (recommended)

On a Linux server with Docker:

```
cd BrennanUI-docker
docker compose up -d --build
docker compose logs -f        # shows the address to open
```

Then on your phone, tablet or another computer open:

```
http://<server-ip>:8765
```

The log prints the exact address(es). On an iPhone/iPad you can use **Share → Add to Home
Screen** for an app-style icon that opens full-screen.

### Settings (environment variables in `docker-compose.yml`)

| variable | default | what it does |
|---|---|---|
| `BRENNAN_PORT` | `8765` | port to serve on — change it if something else uses 8765 |
| `BRENNAN_PASSWORD` | *(none)* | ask for a password in the browser (any user name). **Recommended.** |
| `BRENNAN_IP` | *(scan)* | skip scanning and use this Brennan address |
| `BRENNAN_SUBNET` | *(auto)* | scan this range, e.g. `192.168.68.0/22` |
| `BRENNAN_HOST` | `0.0.0.0` | address to listen on (`127.0.0.1` = this machine only) |
| `BRENNAN_CONFIG` | `/data/brennan_ui.json` | where settings are saved |
| `BRENNAN_RELAY_HOST` | *(unset)* | bridge networking only — see below |
| `BRENNAN_RELAY_PORT` | `8766` | bridge networking only — see below |

### Networking: host mode vs. bridge mode

**Host networking (`network_mode: host`, the default in the compose file)** is the simple,
fully-featured choice on Linux. The container sees your home network directly, so it can
scan for the Brennan, and the Brennan can reach back to it when it needs to download a cover
image from the server.

**Bridge networking** (Docker's default private network, or Docker Desktop on a Mac without
host networking) works with two adjustments, both shown commented-out in
`docker-compose.yml`:

- Discovery can't see your LAN from inside Docker's private network: set `BRENNAN_SUBNET` (or
  `BRENNAN_IP`) and publish the port (`8765:8765`).
- When the Brennan can't download a cover itself, the server hands it the image from a short
  -lived web server. In bridge mode that needs a published port (`8766:8766`) and the
  server's LAN address in `BRENNAN_RELAY_HOST`. Without these, artwork still works whenever
  the Brennan can fetch the image directly, but "Choose file…" uploads won't.

## Run it without Docker

Python 3.9+ and no other packages:

```
python3 brennan.py                       # listens on all interfaces, port 8765
python3 brennan.py --port 9000 --password secret
BRENNAN_SUBNET=192.168.68.0/22 python3 brennan.py
```

## Security

There's no account system — anyone who can reach the port can play music, change settings,
rename albums and change artwork. Keep it on your home network, set `BRENNAN_PASSWORD`, and
**never** forward the port to the internet. (The password uses standard browser sign-in over
plain HTTP, which is fine on a home network but isn't encryption.)

`/healthz` answers without a password (it returns only `ok`) so Docker can check the service.

## Several devices at once

Every open page asks for the Brennan's status once a second. The server answers all of them
from one shared reading (refreshed at most every 0.8 s, and immediately after any command),
so ten open tabs cost the Brennan about the same as one. Each device keeps its own choices —
theme, playback output (Brennan speakers or a Sonos room), the page it's on — because those
live in that browser. Two people controlling the same Brennan will, of course, affect each
other's playback, just as with the Brennan's own web page.

## Files

- `brennan.py` — server: discovery, web server, API proxy, shared status cache, optional password
- `artwork.py` — artwork search, apply/relay (with bridge-mode relay support), missing-art scan
- `ui/index.html` — the interface (single file); `ui/icon-180.png` — home-screen icon
- `Dockerfile`, `docker-compose.yml`, `.dockerignore`

See the desktop edition's README for how discovery, artwork, the Queue and Sonos work — the
behaviour is identical.

## License

MIT, see [LICENSE](LICENSE).
