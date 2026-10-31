# lyrics-sync

[![tests](https://github.com/ayumpo/lyrics-sync/actions/workflows/tests.yml/badge.svg)](https://github.com/ayumpo/lyrics-sync/actions/workflows/tests.yml)
[![docker](https://github.com/ayumpo/lyrics-sync/actions/workflows/docker.yml/badge.svg)](https://github.com/ayumpo/lyrics-sync/actions/workflows/docker.yml)

Give every song in your music library **synced (time-stamped) lyrics**. lyrics-sync scans your library,
finds tracks that have no lyrics or only plain-text lyrics, and writes a `.lrc` file next to each one.
It runs unattended on a schedule, so new music gets lyrics automatically.

```
Artist/Album/01 - Song.flac
Artist/Album/01 - Song.lrc     <- [00:12.40] first line ...
```

Sidecar `.lrc` files work with **Navidrome, Plex, Jellyfin, Emby** and most music players. Plex in
particular only reads sidecar lyrics, not lyrics embedded in the file.

- **Multiple sources, in order**
  1. Synced lyrics already embedded in the file (ID3 `SYLT`, or LRC text in `USLT` / `LYRICS` / `©lyr`), with no network needed
  2. [LRCLIB](https://lrclib.net), the free community lyrics database
  3. [lrcmux](https://lrcmux.dev), which aggregates Deezer, YouTube Music, KuGou and others, with no key needed
- **Matches the right version.** Lookups use the track's duration, and anything more than 4 seconds off
  (a live version, remix or radio edit) is rejected.
- **Upgrades plain `.lrc` files** when synced lyrics are found. The original is backed up first.
- **Polite and resilient.** Requests are rate-limited, and if a source is down or overloaded it is skipped
  for a while instead of stalling the run.
- **Remembers misses.** Tracks with no lyrics anywhere are re-checked after 14 days, backing off to 56,
  so nightly runs only make network requests for new or due tracks. Misses are listed in `misses.csv`.
- **Safe.** Writes are atomic, `--dry-run` changes nothing (not even its own state), and files are written with the owner you choose.
- MP3, FLAC, M4A/ALAC, OGG, Opus, WMA, AIFF and WAV. One Python file; the only dependency is `mutagen`.

## Quick start (Docker)

Try it first without writing anything:

```bash
docker run --rm \
  -v /path/to/music:/music \
  -v "$PWD/config":/config \
  ghcr.io/ayumpo/lyrics-sync --dry-run --limit 200
```

The log shows what it would write. When you're happy with it, run it for real on a schedule, as in the next section.

Images are published for `linux/amd64` and `linux/arm64` (Synology, Proxmox, Unraid, Raspberry Pi and so on).

## Docker Compose

```bash
git clone https://github.com/ayumpo/lyrics-sync.git && cd lyrics-sync
cp .env.example .env        # set MUSIC_PATH, PUID, PGID
docker compose up -d
docker compose logs -f
```

The container runs a pass immediately, then one every `INTERVAL_HOURS` (default 24) after each pass ends.
Passes never overlap. The first pass over a large library can take many hours, because lookups are
rate-limited to a few seconds per track. After that, runs are quick.

## Synology (Container Manager)

1. In **File Station**, create a folder for the state files, for example `/volume1/docker/lyrics-sync`.
2. Find the user and group IDs that own your music. Over SSH, run `id yourname`. If you already run
   linuxserver.io containers (Lidarr, Sonarr and so on), you can reuse the `PUID`/`PGID` values from those.
3. **Container Manager → Project → Create**. Set the path to `/volume1/docker/lyrics-sync`, choose
   *Create docker-compose.yml*, and paste this, adjusting the paths and IDs:

   ```yaml
   services:
     lyrics-sync:
       image: ghcr.io/ayumpo/lyrics-sync:latest
       container_name: lyrics-sync
       restart: unless-stopped
       environment:
         TZ: America/New_York
         PUID: "1026"
         PGID: "100"
       volumes:
         - /volume1/music:/music
         - /volume1/docker/lyrics-sync:/config
       stop_grace_period: 3m
   ```
4. Build the project. Progress appears in the container log and in `/volume1/docker/lyrics-sync/lyrics-sync.log`.

`@eaDir` and `#recycle` folders are skipped automatically.

## Proxmox

**Option A: Docker in an LXC container or VM (simplest).** Install Docker in the guest, make the music
available inside it, then use the Docker Compose steps above.
- **LXC:** add a bind mount on the host, for example `pct set <id> -mp0 /tank/music,mp=/mnt/music`.
- **VM:** mount the share over NFS or SMB.

With an *unprivileged* LXC, IDs are shifted on the host. A file written as `PUID=1000` inside the
container is owned by `101000` on the host. Pick a PUID/PGID that maps to an ID with write access to
the music folder.

**Option B: native, without Docker** (in an LXC container or any Debian/Ubuntu machine):

```bash
apt install -y python3-venv git
git clone https://github.com/ayumpo/lyrics-sync.git /opt/lyrics-sync
python3 -m venv /opt/lyrics-sync/venv
/opt/lyrics-sync/venv/bin/pip install -r /opt/lyrics-sync/requirements.txt
useradd --system --no-create-home lyrics-sync    # needs write access to the music folder

cp /opt/lyrics-sync/examples/systemd/lyrics-sync.{service,timer} /etc/systemd/system/
# edit MUSIC_DIR in the .service file, then:
systemctl daemon-reload
systemctl enable --now lyrics-sync.timer
systemctl start --no-block lyrics-sync.service   # first run now, in the background
journalctl -u lyrics-sync -f
```

The timer runs it nightly at about 02:30. State is kept in `/var/lib/lyrics-sync`.

## Unraid

Add a container from the **Docker** tab:

| Setting | Value |
|---|---|
| Repository | `ghcr.io/ayumpo/lyrics-sync` |
| Path `/music` | `/mnt/user/media/music` (your music share) |
| Path `/config` | `/mnt/user/appdata/lyrics-sync` |
| Variable `PUID` / `PGID` | `99` / `100` |

## Without Docker (any OS with Python 3.9+)

```bash
pip install mutagen
python lyrics_sync.py --music-dir ~/Music --state-dir ~/.local/share/lyrics-sync --dry-run
python lyrics_sync.py --music-dir ~/Music --state-dir ~/.local/share/lyrics-sync
```

To schedule it, use cron (`30 2 * * * python /path/lyrics_sync.py --music-dir ... --state-dir ...`), a
systemd timer (see `examples/systemd`), launchd or Task Scheduler.

## Configuration

Every option can be set as an environment variable. Command-line flags override them.

| Variable | Flag | Default | What it does |
|---|---|---|---|
| `MUSIC_DIR` | `--music-dir` | `/music` | Library root |
| `STATE_DIR` | `--state-dir` | `/config` | State DB, log, `misses.csv`, backups |
| `PROVIDERS` | `--providers` | `embedded,lrclib,lrcmux` | Sources, tried in this order |
| `LRCLIB_URL` | `--lrclib-url` | `https://lrclib.net` | Point at a self-hosted LRCLIB |
| `LRCMUX_URL` | `--lrcmux-url` | `https://api.lrcmux.dev/compat/lrclib` | Any LRCLIB-compatible API |
| `RETRY_DAYS` | `--retry-days` | `14` | Re-check misses after this many days (×1–4 on repeat misses) |
| `UPGRADE_PLAIN` | `--no-upgrade-plain` | `true` | Replace plain-text `.lrc` files when synced lyrics are found |
| `WRITE_PLAIN_TXT` | `--write-plain-txt` | `false` | If only plain lyrics exist, save them as `.txt` (useful for Plex) |
| `MAX_RUNTIME_MIN` | `--max-runtime-min` | `0` | Stop a pass after N minutes (0 = no limit) |
| `EXCLUDE` | `--exclude` | `@eaDir,#recycle,#snapshot,lost+found` | Folder names to skip; folders starting with `.` are always skipped |
| `PUID` / `PGID` | | unset | Owner of new files (container runs as root, files are chowned) |
| `FILE_MODE` | | `664` | Permissions of new files |
| `INTERVAL_HOURS` | | `24` | Docker only: hours between passes; `0` = run once and exit |

One-off flags:

| Flag | What it does |
|---|---|
| `--dry-run` | Look everything up, change nothing |
| `--path "Artist Name"` | Only process this subfolder |
| `--since-hours 24` | Only files modified in the last N hours (handy after an import) |
| `--limit 100` | Stop after N files |
| `--force` | Ignore remembered misses and re-check everything |
| `-v` | Also log every miss and HTTP retry |

To run a one-off command in the running container while it's sleeping between passes:

```bash
docker exec lyrics-sync python /app/lyrics_sync.py --path "Some Artist" -v
```

(If a pass is already running, the second one exits immediately. Only one runs at a time.)

## Files it creates

| Where | File | Purpose |
|---|---|---|
| Next to each track | `Song.lrc` | Synced lyrics (UTF-8) |
| Next to each track | `Song.txt` | Plain lyrics, only with `WRITE_PLAIN_TXT=true` |
| `STATE_DIR` | `lyrics-sync.log` | Run log, rotated at 5 MB |
| `STATE_DIR` | `misses.csv` | Tracks with no synced lyrics anywhere yet |
| `STATE_DIR` | `backup/` | Original plain `.lrc` files that were replaced |
| `STATE_DIR` | `lyrics-sync.db` | What was checked and when (delete it to start fresh) |

## Media server notes

- **Navidrome** reads sidecar `.lrc` files and prefers them over embedded lyrics (the
  [`LyricsPriority`](https://www.navidrome.org/docs/usage/configuration/options/) option). New files
  are picked up on the next library scan.
- **Plex** needs the *Local Media Assets* source enabled for the music library. Refresh the artist's
  metadata after lyrics are added. Plex does not read embedded lyrics, which is why sidecars matter.
- **Jellyfin / Emby** pick up `.lrc` files next to the track on the next library scan.

## Troubleshooting

- **`lrclib is down/overloaded — skipping it for N min`.** LRCLIB is a free service and is often busy.
  The run continues with the other sources and tries LRCLIB again later. Tracks it couldn't check are
  retried on the next pass.
- **A track never gets lyrics.** Check `misses.csv`. Most misses are songs that simply aren't synced
  anywhere yet; they're re-checked automatically. Good tags help: artist, title, and an accurate
  duration. You can also search for and contribute lyrics with [LRCGET](https://github.com/tranxuanthang/lrcget).
- **Permission denied writing `.lrc`.** The container's user, or `PUID`/`PGID`, needs write access to the
  music folders.
- **Each pass starts with a `preflight` line** that shows whether each source answered. If one says
  *not usable right now*, that source is down or blocking requests.

## About the lyrics sources

LRCLIB and lrcmux are free services. lyrics-sync identifies itself with a `User-Agent`, waits about
1 second between requests (lrcmux allows 60 per minute) and backs off when a service is busy. Please
keep these defaults. If you process very large libraries often, consider self-hosting and pointing
`LRCLIB_URL` or `LRCMUX_URL` at your own instance.

Lyrics are the property of their rights holders. This tool is intended for personal music libraries.

## Development

```bash
pip install -r requirements.txt pytest pyflakes   # ffmpeg is also needed to generate test audio
pytest -v
```

The tests run against a local mock of both APIs. They cover embedded export, wrong-version
rejection, outages and blocked requests, dry runs, and repeat runs that make no network requests.

## License

[MIT](LICENSE)
