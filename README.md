# Brennan UI

A modern, local web interface for the [Brennan B3 / B3+](https://brennan.co.uk) music server.
It runs on your computer, finds the Brennan on your home network by itself, and talks to
it through the same HTTP interface the unit's built-in web page uses.

- **Now Playing** with large album art, seek, volume and a VU meter
- **Library**: artists (A–Z), albums (cover grid), album and artist pages, search
- **Playlists**: play, add, remove, create; queue anything
- **Radio**: your presets plus the internet radio directory and station search
- **Outputs**: switch between the Brennan's speakers and any Sonos room it sees
- **Library fixes**: rename artists, albums and tracks; move an album to another artist
- **Artwork**: search Apple Music, Deezer and MusicBrainz for covers; find albums with
  missing, placeholder or low-resolution art and fix them one by one
- **Settings**: shuffle, sort order, segue, bass/treble, EQ, rip format

> Not affiliated with or endorsed by Brennan. The unit's HTTP interface is undocumented,
> so a future firmware update could change it. Built and tested against firmware
> "B3 Aug 28 2025".

## Requirements

- A Brennan B3 or B3+ on the same network as your computer
- Python 3.9 or newer (macOS: `brew install python`, or the installer from python.org).
  No other packages are needed.
- Developed on macOS; Linux should work too. Windows is untested.

## Get it and run it

```
git clone https://github.com/<your-account>/<this-repo>.git
cd <this-repo>
python3 brennan.py
```

Or download the ZIP from GitHub, unzip it, and on a Mac double-click
**Start Brennan.command**. If macOS says it can't be opened, run this once in Terminal from
the folder: `chmod +x "Start Brennan.command"`, or just use `python3 brennan.py`.

Your browser opens at http://127.0.0.1:8765. Leave the terminal window open while you use
it; Ctrl+C (or closing the window) stops it.

The first time, macOS may ask to allow Terminal/Python to "find devices on your local
network". Choose **Allow**, otherwise the scan can't see the Brennan.

Options:

| flag | what it does |
|---|---|
| `--ip 192.168.1.50` | skip scanning and use this address |
| `--subnet 192.168.1.0/24` | scan this range instead of auto-detecting |
| `--port 8766` | use another local port |
| `--no-browser` | don't open a browser tab automatically |

The UI is only served to your own computer (127.0.0.1), not to the rest of the network.

## How it finds the Brennan

Brennans get their address from your router, and it can change after a power cycle. The
launcher:

1. tries the last address it found (saved in `~/.brennan_ui.json`),
2. checks addresses already in your computer's ARP cache,
3. sweeps your subnet (auto-detected from your network interface, up to 1,024
   addresses), nearest addresses first.

A device counts as a Brennan when `/b2gci.fcgi?status` returns its status JSON. If the unit
stops answering mid-session the launcher rescans automatically and the page reconnects on
its own. You can also press ↻ in the sidebar, or set the address by hand in Settings.

## Album artwork

Open an album and click its cover (or ⋯ → Find artwork…). The finder searches Apple Music,
Deezer and MusicBrainz/Cover Art Archive at once; click a result to use it. You can also
paste an image URL or choose a JPEG/PNG from your computer.

Albums → **Fix artwork** checks every album on the unit and lists those with no cover, a
generic placeholder (the same image on albums by 3+ different artists, or one you mark via
⋯ → Mark cover as placeholder), or a cover under 300px. "Fix these one by one" steps
through them. Placeholder fingerprints are remembered in `~/.brennan_ui.json`.

How a cover is applied: the Brennan is first asked to download the image itself (as the
stock UI does). If its artwork doesn't change (for example it can't fetch that HTTPS
site), the launcher downloads the image and serves it to the Brennan for a few seconds from
a temporary server on your computer's network address. If your firewall is on, allow
incoming connections for Python when asked.

## Files

- `brennan.py`: launcher (discovery, local web server, API proxy). Standard library only.
- `artwork.py`: artwork search, apply/relay, and the missing-art scan
- `ui/index.html`: the whole interface (single file, no build step)
- `Start Brennan.command`: double-click launcher for macOS

## Notes for tinkerers

- `/api?<query>` on the local server is passed straight through to
  `http://<brennan>/b2gci.fcgi?<query>`. The unit is order-sensitive: free-text parameters
  (`string=` for search, `name=` for renames) must come last.
- Item IDs are namespaced: albums 1,000,000+, tracks 2,000,000+, artists 3,000,000+,
  playlists 4,000,000+, presets 6,000,000+.
- Keyboard: Space = play/pause, `/` = search, Shift+←/→ = previous/next.

## License

MIT, see [LICENSE](LICENSE).
