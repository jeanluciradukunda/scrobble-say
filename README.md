# scrobble-say

> Your Last.fm top albums in the terminal, rendered as a chafa image grid.
> A musical companion to cowsay for your shell greeting.

Sister project to [scrobble-now](https://github.com/jeanluciradukunda/scrobble-now)
(the macOS menu-bar app) — same data sources, terminal output.

## What it does

Fetches `user.gettopalbums` from Last.fm, downloads cover art (with
on-disk caching), composes an NxN grid, and prints it to the terminal via
chafa. Useful as a periodic shell greeting alongside cowsay.

```
scrobble-say                    # top 9 (3x3) over last 7 days
scrobble-say --period 1month    # last month
scrobble-say --grid 4           # 4x4 = 16 covers
scrobble-say --json             # data only, no render
```

## Install

```bash
# Dependencies
brew install chafa gitleaks pre-commit
git clone git@github.com:jeanluciradukunda/scrobble-say ~/myspace/scrobble-say
cd ~/myspace/scrobble-say

# Python deps in a project-local venv
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Install the gitleaks pre-commit hook
pre-commit install

# Config
mkdir -p ~/.config/scrobble-say
cp config.example.toml ~/.config/scrobble-say/config.toml
# edit ~/.config/scrobble-say/config.toml: set lastfm.username + op_item_id
```

## Secrets

API keys live in **1Password**, never on disk in this repo.

The default `secrets_source = "op"` config option reads the Last.fm API key
from 1Password at runtime. The 1Password item ID goes in
`~/.config/scrobble-say/config.toml` (the config file, NOT the repo).

If you'd rather use environment variables:

```toml
secrets_source = "env"
```

then export `LASTFM_API_KEY` in your shell.

## Pre-commit / secret scanning

[gitleaks](https://github.com/gitleaks/gitleaks) is wired up via pre-commit.
On every `git commit`, staged diffs are scanned for credentials. The
ruleset extends gitleaks' upstream defaults with Last.fm + Discogs patterns
specific to this project (see `.gitleaks.toml`).

Run manually any time:

```bash
pre-commit run --all-files          # scan the working tree
gitleaks protect --staged --config .gitleaks.toml --verbose   # what the hook runs
gitleaks detect --config .gitleaks.toml --verbose             # scan git history
```

If you ever accidentally commit a real key:
1. `git reset --soft HEAD~1` (uncommit)
2. Revoke the key at last.fm/api/account (rotate it)
3. Update 1Password with the new key
4. Re-commit the rest

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Top-albums-grid image in terminal | ✅ |
| 2 | "Now playing" text greeting (track + artist) | todo |
| 3 | `scrobble week\|month\|year` helper for shell | todo |
| 4 | Currently-playing synced lyric line (LRClib) | todo |
| 5 | Mix into cowsay greeting rotation | todo |

## License

MIT — see [LICENSE](LICENSE).
