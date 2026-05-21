#!/usr/bin/env python3
"""scrobble-say — show your Last.fm top albums in the terminal.

Phase 1: top-albums-as-image. Fetches user.gettopalbums from Last.fm,
downloads cover art (cached), composes an NxN grid, renders via chafa.

Usage:
    scrobble-say                       # default: top 9 (3x3) for the last 7 days
    scrobble-say --period 1month
    scrobble-say --grid 4 --size 60x30
    scrobble-say --json                # raw album list, no rendering

Config: ~/.config/scrobble-say/config.toml (see config.example.toml).
Credentials: read from 1Password via `op`. NEVER stored in config or repo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import requests
from PIL import Image

try:  # Python 3.11+ has tomllib in stdlib
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[import-not-found]


CONFIG_PATH = Path.home() / ".config" / "scrobble-say" / "config.toml"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "scrobble-say/0.1 (+https://github.com/jeanluciradukunda/scrobble-say)"
VALID_PERIODS = {"7day", "1month", "3month", "6month", "12month", "overall"}


@dataclass(frozen=True)
class Album:
    name: str
    artist: str
    playcount: int
    mbid: str
    image_url: str  # largest available

    @property
    def cache_key(self) -> str:
        # mbid is more stable when present, but Last.fm often omits it for
        # albums that don't have a MusicBrainz mapping. Fall back to a
        # content-hash of artist+name.
        if self.mbid:
            return self.mbid
        return hashlib.sha1(f"{self.artist}::{self.name}".encode()).hexdigest()


@dataclass(frozen=True)
class RecentTrack:
    artist: str
    name: str
    album: str
    now_playing: bool       # True if Last.fm marks it as currently playing
    timestamp: int          # unix seconds; 0 if now_playing


# --- Config -------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"missing config: {CONFIG_PATH}\n"
            f"copy config.example.toml from the repo and edit."
        )
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def read_secret_via_op(vault: str, item_id: str, field: str) -> str:
    r = subprocess.run(
        ["op", "read", f"op://{vault}/{item_id}/{field}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.exit(f"op read failed for field {field}: {r.stderr.strip()}")
    return r.stdout.strip()


def get_api_key(cfg: dict[str, Any]) -> str:
    src = cfg["lastfm"].get("secrets_source", "op")
    if src == "env":
        v = os.environ.get("LASTFM_API_KEY")
        if not v:
            sys.exit("LASTFM_API_KEY env var is empty")
        return v
    if src == "op":
        return read_secret_via_op(
            cfg["lastfm"]["op_vault"],
            cfg["lastfm"]["op_item_id"],
            "LASTFM_API_KEY",
        )
    sys.exit(f"unknown secrets_source: {src}")


# --- Last.fm ------------------------------------------------------------------

def parse_grid(spec: str) -> tuple[int, int]:
    """Accept 'N' (= NxN) or 'WxH'. Returns (cols, rows)."""
    s = spec.strip().lower()
    if "x" in s:
        a, b = s.split("x", 1)
        cols, rows = int(a), int(b)
    else:
        cols = rows = int(s)
    if cols < 1 or rows < 1:
        sys.exit(f"grid dimensions must be positive, got {spec!r}")
    return cols, rows

def _fetch_top_albums_raw(user: str, api_key: str, period: str, limit: int) -> list[Album]:
    if period not in VALID_PERIODS:
        sys.exit(f"period must be one of {VALID_PERIODS}, got {period!r}")
    params = {
        "method": "user.gettopalbums",
        "user": user,
        "api_key": api_key,
        "period": period,
        "limit": limit * 3,   # over-fetch to absorb cover-less albums
        "format": "json",
    }
    r = requests.get(LASTFM_API, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    data = r.json()
    raw = data.get("topalbums", {}).get("album", [])
    out: list[Album] = []
    for a in raw:
        images = {img["size"]: img["#text"] for img in a.get("image", [])}
        url = images.get("extralarge") or images.get("large") or images.get("medium") or ""
        if not url:
            continue
        out.append(Album(
            name=a.get("name", "?"),
            artist=a.get("artist", {}).get("name", "?"),
            playcount=int(a.get("playcount", 0)),
            mbid=a.get("mbid", "") or "",
            image_url=url,
        ))
        if len(out) >= limit:
            break
    return out

def fetch_now_playing(user: str, api_key: str) -> RecentTrack | None:
    """Return the currently-playing track, or the most recent scrobble if
    nothing is playing right now. Returns None on transport failure or empty
    history."""
    params = {
        "method": "user.getrecenttracks",
        "user": user,
        "api_key": api_key,
        "limit": 1,
        "format": "json",
    }
    try:
        r = requests.get(LASTFM_API, params=params, headers={"User-Agent": USER_AGENT}, timeout=10)
        r.raise_for_status()
    except Exception:
        return None
    tracks = r.json().get("recenttracks", {}).get("track", [])
    if not tracks:
        return None
    t = tracks[0] if isinstance(tracks, list) else tracks
    attr = t.get("@attr") or {}
    now = bool(attr.get("nowplaying") == "true")
    ts = int(t.get("date", {}).get("uts", 0)) if not now else 0
    return RecentTrack(
        artist=t.get("artist", {}).get("#text") or "?",
        name=t.get("name") or "?",
        album=t.get("album", {}).get("#text") or "",
        now_playing=now,
        timestamp=ts,
    )


def _humanise_ago(ts: int) -> str:
    """'2m ago', '3h ago', '4d ago' style relative time."""
    if ts <= 0:
        return ""
    delta = int(time.time()) - ts
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def fetch_top_albums(
    user: str, api_key: str, period: str, limit: int,
    cache: Path | None, ttl_seconds: int,
) -> list[Album]:
    """Cached wrapper. On-disk cache keyed by (user, period, limit). When the
    cache file is fresh (mtime within ttl_seconds), return its contents
    without hitting Last.fm. Set ttl_seconds=0 to disable caching."""
    if not cache or ttl_seconds <= 0:
        return _fetch_top_albums_raw(user, api_key, period, limit)
    cache_dir = cache / "api"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{user}::{period}::{limit}".encode()).hexdigest()[:16]
    cache_file = cache_dir / f"topalbums-{user}-{period}-{limit}-{key}.json"
    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < ttl_seconds:
            try:
                data = json.loads(cache_file.read_text())
                return [Album(**a) for a in data]
            except Exception:
                cache_file.unlink(missing_ok=True)  # bad cache, refetch
    albums = _fetch_top_albums_raw(user, api_key, period, limit)
    try:
        cache_file.write_text(json.dumps([asdict(a) for a in albums], indent=2))
    except Exception:
        pass  # caching is best-effort
    return albums


# --- Cover cache + grid --------------------------------------------------------

def cache_dir(cfg: dict[str, Any]) -> Path | None:
    raw = cfg.get("cache", {}).get("dir", "")
    if not raw:
        return None
    p = Path(os.path.expanduser(raw))
    (p / "covers").mkdir(parents=True, exist_ok=True)
    return p


# ---- Cache hygiene ---------------------------------------------------------

def _dir_size_bytes(d: Path) -> int:
    if not d.exists():
        return 0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())

def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n = n / 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"

def evict_covers_lru(covers_dir: Path, max_bytes: int) -> int:
    """Delete oldest-by-mtime cover files until covers_dir is under max_bytes.
    Returns the number of files deleted. Triggered after fetch_cover writes
    a new file. No-op if dir is already under cap."""
    if not covers_dir.exists() or max_bytes <= 0:
        return 0
    files = sorted(
        (f for f in covers_dir.iterdir() if f.is_file()),
        key=lambda f: f.stat().st_mtime,
    )
    total = sum(f.stat().st_size for f in files)
    deleted = 0
    for f in files:
        if total <= max_bytes:
            break
        size = f.stat().st_size
        try:
            f.unlink()
            total -= size
            deleted += 1
        except OSError:
            pass
    return deleted

def cache_info(cache: Path | None) -> str:
    if cache is None:
        return "cache: disabled"
    lines = [f"cache dir: {cache}"]
    for sub in ("covers", "api"):
        d = cache / sub
        n = sum(1 for _ in d.glob("*")) if d.exists() else 0
        sz = _dir_size_bytes(d)
        lines.append(f"  {sub:<8} {n:>4} files   {_human_bytes(sz)}")
    grid_tmp = GRID_TMP_PATH
    if grid_tmp.exists():
        lines.append(f"  /tmp grid: {_human_bytes(grid_tmp.stat().st_size)}")
    return "\n".join(lines)

def cache_clear(cache: Path | None) -> str:
    removed_bytes = 0
    files_removed = 0
    targets = []
    if cache is not None:
        targets.extend([cache / "covers", cache / "api"])
    for d in targets:
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file():
                removed_bytes += f.stat().st_size
                try:
                    f.unlink()
                    files_removed += 1
                except OSError:
                    pass
    # Also wipe any stray /tmp grid files (current stable + any legacy
    # PID-suffixed leftovers from before the fix)
    for p in Path("/tmp").glob("scrobble-say-grid*.png"):
        try:
            removed_bytes += p.stat().st_size
            p.unlink()
            files_removed += 1
        except OSError:
            pass
    return f"removed {files_removed} files, freed {_human_bytes(removed_bytes)}"


def fetch_cover(album: Album, cache: Path | None, cover_cap_bytes: int = 0) -> Image.Image | None:
    if not album.image_url:
        return None
    if cache:
        f = cache / "covers" / f"{album.cache_key}.png"
        if f.exists():
            try:
                # Touch mtime so LRU eviction treats recently-used covers as fresh
                os.utime(f, None)
                return Image.open(f).convert("RGB")
            except Exception:
                f.unlink(missing_ok=True)
    try:
        r = requests.get(album.image_url, headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
    except Exception:
        return None
    from io import BytesIO
    img = Image.open(BytesIO(r.content)).convert("RGB")
    if cache:
        covers_dir = cache / "covers"
        img.save(covers_dir / f"{album.cache_key}.png", format="PNG")
        if cover_cap_bytes > 0:
            evict_covers_lru(covers_dir, cover_cap_bytes)
    return img


GRID_TMP_PATH = Path("/tmp") / "scrobble-say-grid.png"

def compose_grid(
    albums: list[Album], cols: int, rows: int, cache: Path | None,
    cell_px: int = 220, cover_cap_bytes: int = 0,
) -> Path:
    """Returns path to a PNG of the composed grid (cols x rows).

    Writes to a single stable path (/tmp/scrobble-say-grid.png) so we don't
    leak a new file per invocation. chafa reads it once; the file is
    overwritten on the next call.

    cover_cap_bytes > 0 triggers LRU eviction of the covers cache after a new
    cover is downloaded (oldest-mtime files deleted until under cap)."""
    canvas = Image.new("RGB", (cell_px * cols, cell_px * rows), color=(20, 20, 20))
    placeholder = Image.new("RGB", (cell_px, cell_px), color=(40, 40, 40))
    needed = cols * rows
    for i in range(needed):
        col = i % cols
        row = i // cols
        if i < len(albums):
            img = fetch_cover(albums[i], cache, cover_cap_bytes) or placeholder
        else:
            img = placeholder
        img = img.resize((cell_px, cell_px), Image.LANCZOS)
        canvas.paste(img, (col * cell_px, row * cell_px))
    canvas.save(GRID_TMP_PATH, format="PNG")
    return GRID_TMP_PATH


# --- Render -------------------------------------------------------------------

VALID_FORMATS = {"symbols", "iterm", "kitty", "sixels"}
VALID_POSITIONS = {"left", "center", "right"}
POSITION_TO_CHAFA = {"left": "left", "center": "center", "right": "right"}

def render_with_chafa(png: Path, size: str, fmt: str, position: str) -> int:
    """fmt:
        symbols  — chafa's stylised mix of block characters (broad terminal
                   support; works in plain xterm, screen, tmux)
        iterm    — iTerm2 inline-image protocol; raw pixel-perfect PNG
        kitty    — kitty graphics protocol; raw pixel-perfect PNG
        sixels   — sixel-compatible terminals (mlterm, foot, recent xterm)

    position:
        left     — default, image at column 0
        center   — image horizontally centred in the terminal
        right    — image at the right edge of the terminal

    For center/right, we expand the chafa size to the full terminal width and
    pass --align so chafa places the actual image accordingly; the grid art
    is not scaled — it just shifts horizontally within the wider canvas.
    """
    chafa = shutil.which("chafa")
    if not chafa:
        sys.exit("chafa not found on PATH — `brew install chafa`")
    if fmt not in VALID_FORMATS:
        sys.exit(f"format must be one of {VALID_FORMATS}, got {fmt!r}")
    if position not in VALID_POSITIONS:
        sys.exit(f"position must be one of {VALID_POSITIONS}, got {position!r}")

    cmd = [chafa, f"--format={fmt}", "--animate=off"]
    if position == "left":
        cmd.append(f"--size={size}")
    else:
        # Expand horizontal axis to terminal width so --align has room to shift.
        try:
            term_cols = os.get_terminal_size().columns
        except OSError:
            term_cols = 100
        # Keep user's row count from the original size; widen cols only.
        rows_part = size.split("x")[1] if "x" in size else size
        cmd.append(f"--size={term_cols}x{rows_part}")
        cmd.append(f"--align={POSITION_TO_CHAFA[position]},top")
    cmd.append(str(png))
    return subprocess.call(cmd)


# --- CLI ----------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--period", help="7day | 1month | 3month | 6month | 12month | overall")
    ap.add_argument("--grid", help="N (= NxN) or WxH (e.g. 3, 4x4, 10x2)")
    ap.add_argument("--size", help="chafa terminal size, e.g. 60x30")
    ap.add_argument("--format", dest="fmt",
                    help=f"output mode: one of {sorted(VALID_FORMATS)}. "
                         "'symbols' is the stylised default; 'iterm'/'kitty'/'sixels' "
                         "embed the raw PNG via the matching terminal protocol "
                         "(pixel-perfect, slower). Also settable via $SCROBBLE_SAY_FORMAT.")
    ap.add_argument("--raw", action="store_const", const="iterm", dest="fmt",
                    help="shortcut for --format=iterm")
    ap.add_argument("--position", choices=sorted(VALID_POSITIONS),
                    help="horizontal position in the terminal (default: left)")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass the on-disk top-albums cache (forces a fresh API call)")
    ap.add_argument("--cache-info", action="store_true",
                    help="print cache directory + size + file count; exit")
    ap.add_argument("--cache-clear", action="store_true",
                    help="delete cached covers, API responses, and the /tmp grid; exit")
    ap.add_argument("--now", action="store_true",
                    help="print currently-playing track (or last scrobble) as a one-line greeting; exit")
    ap.add_argument("--json", action="store_true", help="dump album list as JSON, skip render")
    args = ap.parse_args()

    cfg = load_config()

    # --cache-info / --cache-clear / --now short-circuit before doing anything else
    if args.cache_info:
        print(cache_info(cache_dir(cfg)))
        return
    if args.cache_clear:
        print(cache_clear(cache_dir(cfg)))
        return
    if args.now:
        api_key = get_api_key(cfg)
        user = cfg["lastfm"]["username"]
        rt = fetch_now_playing(user, api_key)
        if rt is None:
            print("(no recent scrobbles)")
            return
        marker = "♪" if rt.now_playing else "♫"
        suffix = "" if rt.now_playing else f"  ({_humanise_ago(rt.timestamp)})"
        album = f"  · {rt.album}" if rt.album else ""
        print(f"{marker} {rt.artist} — {rt.name}{album}{suffix}")
        return

    period = args.period or cfg["render"].get("period", "7day")
    cols, rows = parse_grid(args.grid or str(cfg["render"].get("grid", 3)))
    size = args.size or cfg["render"].get("size", "60x30")
    fmt = args.fmt or os.environ.get("SCROBBLE_SAY_FORMAT") or cfg["render"].get("format", "iterm")
    position = args.position or cfg["render"].get("position", "left")
    user = cfg["lastfm"]["username"]
    ttl = 0 if args.no_cache else int(cfg.get("cache", {}).get("ttl_seconds", 86400))
    cover_cap_mb = int(cfg.get("cache", {}).get("covers_max_mb", 50))
    cover_cap_bytes = cover_cap_mb * 1024 * 1024

    cache = cache_dir(cfg)
    api_key = get_api_key(cfg)
    albums = fetch_top_albums(user, api_key, period, cols * rows, cache, ttl)

    if args.json:
        print(json.dumps(
            [{"name": a.name, "artist": a.artist, "playcount": a.playcount} for a in albums],
            indent=2,
        ))
        return

    png = compose_grid(albums, cols, rows, cache, cover_cap_bytes=cover_cap_bytes)
    code = render_with_chafa(png, size, fmt, position)

    # Caption line under the grid
    if albums:
        top = albums[0]
        print(f"— top {period}: {top.artist} — {top.name} ({top.playcount} plays)")
    sys.exit(code)


if __name__ == "__main__":
    main()
