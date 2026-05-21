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
import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
    try:
        r = subprocess.run(
            ["op", "read", f"op://{vault}/{item_id}/{field}"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        sys.exit(
            "the 1Password CLI (`op`) is not installed but the config sets\n"
            "  [lastfm] secrets_source = \"op\".\n"
            "Install it with:  brew install --cask 1password-cli\n"
            "Or switch to env-var auth in ~/.config/scrobble-say/config.toml:\n"
            "  [lastfm] secrets_source = \"env\"\n"
            "and `export LASTFM_API_KEY=...` in your shell."
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

MAX_GRID_CELLS = 200  # 14x14 ish — covers ~30MB of PIL canvas at cell_px=220

def parse_grid(spec: str) -> tuple[int, int]:
    """Accept 'N' (= NxN) or 'WxH'. Returns (cols, rows).

    Validates against MAX_GRID_CELLS to prevent a typo like --grid 1000
    from allocating a 145GB canvas before crashing."""
    s = spec.strip().lower()
    try:
        if "x" in s:
            a, b = s.split("x", 1)
            if not a.strip() or not b.strip():
                raise ValueError("missing dimension")
            cols, rows = int(a), int(b)
        else:
            cols = rows = int(s)
    except ValueError:
        sys.exit(f"--grid: must be 'N' or 'WxH' with positive integers, got {spec!r}")
    if cols < 1 or rows < 1:
        sys.exit(f"--grid: dimensions must be positive, got {spec!r}")
    cells = cols * rows
    if cells > MAX_GRID_CELLS:
        sys.exit(
            f"--grid: {cols}x{rows} = {cells} cells exceeds limit of "
            f"{MAX_GRID_CELLS}. Each cell is a ~50KB PNG; large grids "
            "balloon memory and overrun Last.fm's per-call limits."
        )
    return cols, rows

class LastFmError(Exception):
    """Last.fm returned a JSON error payload like {'error': 6, 'message': '...'}.
    See https://www.last.fm/api/errorcodes for codes."""


def _lastfm_call(params: dict, timeout: int = 20) -> dict:
    """Single GET to the Last.fm API. Raises LastFmError on a JSON error
    payload so callers can surface a real message instead of silently
    returning empty data (e.g. a wrong username would look like 'no
    albums')."""
    r = requests.get(LASTFM_API, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "error" in data:
        code = data.get("error")
        msg = data.get("message", "(no message)")
        raise LastFmError(f"Last.fm API error {code}: {msg}")
    return data


def _fetch_top_albums_raw(user: str, api_key: str, period: str, limit: int) -> list[Album]:
    if period not in VALID_PERIODS:
        sys.exit(f"period must be one of {VALID_PERIODS}, got {period!r}")
    data = _lastfm_call({
        "method": "user.gettopalbums",
        "user": user,
        "api_key": api_key,
        "period": period,
        "limit": limit * 3,   # over-fetch to absorb cover-less albums
        "format": "json",
    })
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


def _fetch_now_playing_raw(user: str, api_key: str) -> RecentTrack | None:
    """Returns None ONLY when there are genuinely no scrobbles. Network
    failures and Last.fm API errors propagate so the caller can distinguish
    'user has no history' from 'something is broken'."""
    data = _lastfm_call({
        "method": "user.getrecenttracks",
        "user": user,
        "api_key": api_key,
        "limit": 1,
        "format": "json",
    }, timeout=10)
    tracks = data.get("recenttracks", {}).get("track", [])
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


def fetch_now_playing(
    user: str, get_api_key_fn,
    cache: Path | None, ttl_seconds: int = 30,
) -> RecentTrack | None:
    """Cached now-playing lookup. Default TTL 30s (currently-playing track
    changes every few minutes; 30s is fresh enough). get_api_key_fn is only
    called on cache miss — important to avoid 1Password Touch ID prompts on
    every invocation."""
    if not cache or ttl_seconds <= 0:
        return _fetch_now_playing_raw(user, get_api_key_fn())
    cache_dir = cache / "api"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"nowplaying-{user}.json"
    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < ttl_seconds:
            try:
                data = json.loads(cache_file.read_text())
                return RecentTrack(**data) if data else None
            except Exception:
                cache_file.unlink(missing_ok=True)
    rt = _fetch_now_playing_raw(user, get_api_key_fn())
    try:
        cache_file.write_text(json.dumps(asdict(rt) if rt else None))
    except Exception:
        pass
    return rt


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
    user: str, get_api_key_fn, period: str, limit: int,
    cache: Path | None, ttl_seconds: int,
) -> list[Album]:
    """Cached wrapper. On-disk cache keyed by (user, period, limit). When
    the cache file is fresh (mtime within ttl_seconds), return its contents
    without hitting Last.fm AND without calling get_api_key_fn — important
    because get_api_key_fn typically triggers a 1Password Touch ID prompt,
    and we want zero prompts on cache hits."""
    if not cache or ttl_seconds <= 0:
        return _fetch_top_albums_raw(user, get_api_key_fn(), period, limit)
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
    # Cache miss → now we need the key
    albums = _fetch_top_albums_raw(user, get_api_key_fn(), period, limit)
    try:
        cache_file.write_text(json.dumps([asdict(a) for a in albums], indent=2))
    except Exception:
        pass
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
    lines = []
    if cache is not None:
        lines.append(f"cache dir: {cache}")
        for sub in ("covers", "api"):
            d = cache / sub
            n = sum(1 for _ in d.glob("*")) if d.exists() else 0
            sz = _dir_size_bytes(d)
            lines.append(f"  {sub:<8} {n:>4} files   {_human_bytes(sz)}")
    else:
        lines.append("cache: disabled")
    # Runtime dir (grid PNGs + debug log) is independent of the cache config
    runtime = _runtime_dir()
    n = sum(1 for _ in runtime.iterdir() if _.is_file())
    lines.append(f"runtime dir: {runtime}")
    lines.append(f"  files     {n:>4}        {_human_bytes(_dir_size_bytes(runtime))}")
    return "\n".join(lines)

def cache_clear(cache: Path | None) -> str:
    removed_bytes = 0
    files_removed = 0
    targets: list[Path] = []
    if cache is not None:
        targets.extend([cache / "covers", cache / "api"])
    # Also wipe ~/.cache/scrobble-say/runtime (grid PNGs from crashed runs,
    # debug log) — except the CURRENT process's grid file which atexit
    # handles itself.
    targets.append(_runtime_dir())
    for d in targets:
        if not d.exists():
            continue
        for f in d.iterdir():
            if not f.is_file():
                continue
            if f == GRID_TMP_PATH:
                continue   # leave this process's own grid alone
            removed_bytes += f.stat().st_size
            try:
                f.unlink()
                files_removed += 1
            except OSError:
                pass
    # Sweep legacy /tmp grid files from older scrobble-say versions.
    for p in Path("/tmp").glob("scrobble-say-grid*.png"):
        try:
            removed_bytes += p.stat().st_size
            p.unlink()
            files_removed += 1
        except OSError:
            pass
    # Legacy /tmp debug log too
    legacy_log = Path("/tmp/scrobble-say-debug.log")
    if legacy_log.exists():
        try:
            removed_bytes += legacy_log.stat().st_size
            legacy_log.unlink()
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
    try:
        # A 200 response with HTML/error body raises UnidentifiedImageError —
        # fall back to the placeholder instead of crashing the whole render.
        img = Image.open(BytesIO(r.content)).convert("RGB")
    except Exception as e:
        _log(f"fetch_cover: decode failed for {album.artist}/{album.name}: {e!s}")
        return None
    if cache:
        covers_dir = cache / "covers"
        try:
            img.save(covers_dir / f"{album.cache_key}.png", format="PNG")
            if cover_cap_bytes > 0:
                evict_covers_lru(covers_dir, cover_cap_bytes)
        except OSError as e:
            _log(f"fetch_cover: cache save failed: {e!s}")
    return img


def _runtime_dir() -> Path:
    """User-private runtime dir for ephemeral state (grid PNG, debug log).
    Lives under ~/.cache/scrobble-say/runtime so we avoid world-writable
    /tmp entirely — that was a symlink-clobber risk and concurrent runs
    of the previous stable path could overwrite each other mid-render."""
    d = Path.home() / ".cache" / "scrobble-say" / "runtime"
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


# Per-PID grid file so concurrent invocations don't collide. Cleaned up on
# process exit so we don't leak files.
GRID_TMP_PATH = _runtime_dir() / f"grid-{os.getpid()}.png"
atexit.register(lambda: GRID_TMP_PATH.unlink(missing_ok=True))


def compose_grid(
    albums: list[Album], cols: int, rows: int, cache: Path | None,
    cell_px: int = 220, cover_cap_bytes: int = 0,
) -> Path:
    """Returns path to a PNG of the composed grid (cols x rows).

    Writes to a per-PID path under ~/.cache/scrobble-say/runtime/ via
    atomic write (write-then-rename), then atexit-cleans the file. This
    avoids both the stable-path concurrent-overwrite race and the
    world-writable /tmp symlink-clobber risk.

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
    # Atomic write: temp file in same dir + rename
    fd, tmp = tempfile.mkstemp(
        dir=str(GRID_TMP_PATH.parent),
        prefix=f".{GRID_TMP_PATH.name}.",
        suffix=".tmp",
    )
    os.close(fd)
    try:
        canvas.save(tmp, format="PNG")
        os.replace(tmp, GRID_TMP_PATH)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return GRID_TMP_PATH


# --- Render -------------------------------------------------------------------

VALID_FORMATS = {"symbols", "iterm", "kitty", "sixels"}
VALID_POSITIONS = {"left", "center", "right"}

import re as _re

DEBUG_LOG = _runtime_dir() / "debug.log"

def _log(msg: str) -> None:
    """Append to the per-user debug log under ~/.cache/scrobble-say/runtime/.
    Best-effort; never raises. Multi-process append is safe (atomic writes
    for short messages on Unix)."""
    try:
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass
    if os.environ.get("SCROBBLE_DEBUG") == "1":
        print(f"[scrobble-say] {msg}", file=sys.stderr)

def _get_term_cols() -> int:
    """Detect terminal width robustly.

    Tries in order:
      1. $COLUMNS env var (must be exported by zsh; precmd helper does this)
      2. os.get_terminal_size() (reliable when stdout is a tty)
      3. `tput cols` (last resort, slow)
      4. 100 (arbitrary fallback)

    $COLUMNS goes first because in zsh precmd contexts, the env var (when
    explicitly exported by the caller via `COLUMNS=$COLUMNS scrobble-say`)
    is more trustworthy than os.get_terminal_size which depends on stdout
    being a fully-initialised TTY — not always true during shell startup."""
    env = os.environ.get("COLUMNS", "").strip()
    if env.isdigit() and int(env) > 0:
        c = int(env)
        _log(f"term_cols={c} via $COLUMNS")
        return c
    try:
        c = os.get_terminal_size().columns
        if c > 0:
            _log(f"term_cols={c} via os.get_terminal_size")
            return c
    except OSError as e:
        _log(f"os.get_terminal_size failed: {e}")
    try:
        r = subprocess.run(["tput", "cols"], capture_output=True, text=True, timeout=1)
        out = r.stdout.strip()
        if out.isdigit() and int(out) > 0:
            c = int(out)
            _log(f"term_cols={c} via tput")
            return c
    except (subprocess.SubprocessError, ValueError, FileNotFoundError) as e:
        _log(f"tput failed: {e}")
    _log("term_cols=100 (fallback — could not detect)")
    return 100

def _measure_image_cols(output: str, fmt: str, fallback: int) -> int:
    """How many terminal cells wide is the rendered image?

    For iterm/kitty: parsed from the protocol header (width=N).
    For sixels: there's no protocol-level width, fall back to user's --size cols.
    For symbols: max display width across all lines (after stripping ANSI)."""
    if fmt == "iterm":
        m = _re.search(r'width=(\d+)', output)
        return int(m.group(1)) if m else fallback
    if fmt == "kitty":
        m = _re.search(r'c=(\d+)', output)
        return int(m.group(1)) if m else fallback
    if fmt == "symbols":
        cleaned = _re.sub(r'\x1b\[[0-9;]*[mGKH]', '', output)
        widths = [len(line) for line in cleaned.split("\n")]
        return max(widths) if widths else fallback
    return fallback


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

    Position is implemented as a post-render shift. We always render at the
    user's exact --size, then for non-left positions, we measure the
    rendered image's width and either pad each output line with spaces
    (symbols mode) or emit an absolute cursor-move escape before the image
    (iterm/kitty/sixels modes). This avoids chafa's --align which only
    shifts within whatever --size you give it — useless for our case where
    the user's --size IS the image size and shifting needs to happen
    within the full terminal width.
    """
    chafa = shutil.which("chafa")
    if not chafa:
        sys.exit("chafa not found on PATH — `brew install chafa`")
    if fmt not in VALID_FORMATS:
        sys.exit(f"format must be one of {VALID_FORMATS}, got {fmt!r}")
    if position not in VALID_POSITIONS:
        sys.exit(f"position must be one of {VALID_POSITIONS}, got {position!r}")

    cmd = [chafa, f"--format={fmt}", "--animate=off", f"--size={size}", str(png)]

    if position == "left":
        return subprocess.call(cmd)

    # Capture chafa output to inject our own positioning.
    # We capture stderr too so we can surface chafa's error messages on
    # failure — capture_output=True swallows them otherwise, leaving users
    # with a bare non-zero exit and no clue what went wrong.
    r = subprocess.run(cmd, capture_output=True, text=True)
    output = r.stdout
    if r.returncode != 0 or not output:
        sys.stdout.write(output)
        if r.stderr:
            sys.stderr.write(r.stderr)
        return r.returncode

    term_cols = _get_term_cols()
    fallback_cols = int(size.split("x")[0]) if "x" in size else int(size)
    image_cols = _measure_image_cols(output, fmt, fallback_cols)
    slack = term_cols - image_cols
    _log(f"render fmt={fmt} size={size} position={position} term_cols={term_cols} image_cols={image_cols} slack={slack}")
    if slack <= 0:
        sys.stdout.write(output)
        return 0

    pad = slack if position == "right" else slack // 2

    if fmt == "symbols":
        # Per-line space padding so each row of art starts at the right column
        spaces = " " * pad
        for line in output.split("\n"):
            sys.stdout.write(spaces + line + "\n")
        return 0

    # iterm / kitty / sixels: a single escape blob. Move cursor first.
    sys.stdout.write(f"\x1b[{pad+1}G")
    sys.stdout.write(output)
    return 0


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
        user = cfg["lastfm"]["username"]
        cache = cache_dir(cfg)
        # --no-cache disables the now-playing cache too (Codex #4)
        now_ttl = 0 if args.no_cache else int(cfg.get("cache", {}).get("now_ttl_seconds", 30))
        try:
            rt = fetch_now_playing(user, lambda: get_api_key(cfg), cache, now_ttl)
        except LastFmError as e:
            sys.exit(str(e))
        except requests.RequestException as e:
            sys.exit(f"network error: {e}")
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
    # Lazy: get_api_key is only invoked on cache miss, so warm calls skip
    # the 1Password Touch ID prompt entirely.
    try:
        albums = fetch_top_albums(user, lambda: get_api_key(cfg), period, cols * rows, cache, ttl)
    except LastFmError as e:
        sys.exit(str(e))
    except requests.RequestException as e:
        sys.exit(f"network error: {e}")

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
