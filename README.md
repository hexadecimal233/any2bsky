# any2bsky

> **NOTE**: currently most of the codebase is AI generated!!

A pipeline that migrates your social-media history to Bluesky — **from any
export format**:

```
export dir (read-only) ──convert──▶ data/<source>/events.json
                                    ──plan──▶ data/<source>/tasks.json (+ compressed/)
                                              ──dry / live──▶ checkpoint written back
```

The converter only knows one thing per source: **turn an export directory
into the generic event stream** (`shared.event`). Everything else — PDS
limits, tweetstorms, AVIF compression, dependency-scheduled posting,
rollback — is source-agnostic and shared by all datasources. QZone is the
first, fully baked-in datasource; adding another platform is a small,
well-defined job (see *Datasource guide*).

## Features

- **Datasource registry**: `source_type + build_events(root)` is all a new
  platform needs; datasources register at import time
  (`datasource/__init__.py`). Not limited to QZone in any way.
- **PDS-compliant planning** (limits all sourced/annotated):
  - text ≤ 300 graphemes/post; overlong text auto-splits tweetstorm-style
    (`1/N` prefixes + reply chain)
  - images ≤ 4 per post (read from the atproto lexicon), each ≤ 4000px /
    ≤ 2MB; oversized images are resized by the long edge and encoded to
    **AVIF** (lossless → step-10 quality until within limits)
  - videos failing ≥ 10 min / > 300MB are hard-failed; compliant videos are
    uploaded via `uploadBlob` (the library's `send_video` path — the server
    transcodes)
  - share/repost with a URL → appended + **link facet**; without a URL →
    degrades to plain text
  - album photo captions land in per-image **alt text**, not the post body
- **Original publish time is restored**: `createdAt` keeps the source
  timestamp, so your history is dated as it was originally
- **DAG executor**: `reply_to` is the only dependency edge; any task (media
  or text) waits for its parent; media tasks are concurrency-limited by
  `--heavy`, text tasks run serially; chains therefore stay naturally ordered
- **Session caching**: interactive `login` (password hidden via getpass),
  session persisted in `data/session.json`, auto-refreshed on expiry
- **Everything lands in `./data`** — the source export is never written to
- **Rollback**: `undo` deletes published posts (child-first, own-account-only)

## How your migrated posts appear on Bluesky

Bluesky keeps two timestamps, and the client uses them for different
surfaces:

| Surface | Timestamp used | What readers see |
|---|---|---|
| Feed cards (author/following) — sort **and** display | `indexedAt` | posts show as "posted just now" and are ordered by the migration moment |
| Post thread page | `createdAt` vs `indexedAt` | if `createdAt` is **≥ 24h** before `indexedAt`, the client shows a pill **「Archived from &lt;original date&gt;」**; tapping it opens: *"This post claims to have been created on &lt;createdAt&gt;, but was first seen by Bluesky on &lt;indexedAt&gt;."* |
| Chronological feeds (per official docs) | `sortAt` = earlier of createdAt/indexedAt | backfilled posts rank by original time (future outliers fall back to indexedAt) |

In practice: a 2017 QZone post is migrated in 2026, shows in feeds as
"posted today" (indexedAt), and only inside its thread reads as
"Archived from 2017-02-03". This is the platform's standard indication for
imported/backfilled content — a design feature, not a penalty. Our 208-post
migration was accepted with zero record-level failures.

Note: comments/replies *on* the source posts are not part of the migration
(only post bodies, media, alt text, and repost links are converted).

## Installation

```bash
uv sync            # or: pip install -e .
```

System tools: `ffmpeg` / `ffprobe` (AVIF encoding, dimension/duration
probing — with them there is no Python image library dependency).

## Quick start

```bash
# 1. login (interactive; password hidden; session cached to data/session.json)
python cli.py login

# 2. convert + (optional) manually filter events
python cli.py convert <export-dir>
python cli.py filter <export-dir>      # browser editor to keep/drop events

# 3. plan (reads the filtered events.json; dropped events are skipped)
python cli.py plan <export-dir>

# 4. preview (dry-run on a tasks.dry.json copy)
python cli.py dry <export-dir> --heavy 3

# 5. actually post (reuses the cached session only)
python cli.py live <export-dir> --heavy 2
```

## CLI reference (subcommands, no boolean switches)

| Command | Description |
|---|---|
| `sources` | list registered datasources |
| `login [--handle H] [--password P] [--session F]` | interactive login + session cache |
| `convert <root> [--source qzone]` | datasource → `data/<source>/events.json` |
| `filter <root> [--port P]` | mini local server + browser editor: keep/drop events (backend applies) |
| `plan <root>` | filtered events → `tasks.json` (+ `compressed/` AVIF) |
| `dry <root> [--heavy N]` | dry-run the DAG executor on a `tasks.dry.json` copy |
| `undo <root> [--dry] [--yes] [--session F]` | rollback: delete published posts (child-first; resets tasks to pending) |
| `live <root> [--heavy N] [--session F]` | real posting (needs a cached login and a prior `plan`) |

Credentials fall back to env vars `BSKY_HANDLE` / `BSKY_APP_PASSWORD`
(app password) for the `login` command.

`--heavy N`: global concurrency limit for **media** tasks (only
mutually-independent media tasks truly parallelize; reply chains and text
tasks are serial by dependency).

## Output layout (everything in `./data`, gitignored)

```
data/
├── session.json                  # login session cache
└── <source-name>/
    ├── events.json               # generic event stream (v1)
    ├── tasks.json                # task array + executor checkpoint (resume)
    ├── tasks.dry.json            # dry-run demo copy
    └── compressed/               # AVIF outputs for oversized images
```

## Tasks & resume

- `tasks.json` top-level is only `tasks: []` — a flat linear array (the only
  arrays inside a task are `medias`/`alts`)
- every task carries
  `text/medias(alts)/reply_to/link_url/created_at/post_uri/post_cid/parent_uri/state/fail_reason`
- `state ∈ pending|done|skipped|failed`; the executor rewrites the whole file
  after every task, so an interrupted run resumes from the first non-pending
  task
- all media paths are ABSOLUTE; `created_at` preserves the original publish
  time; `post_uri`/`post_cid`/`parent_uri` are backfilled after a successful
  post (this is how replies always resolve their parent)

## Conversion rules (QZone datasource)

- `Boards` (留言板) is **not converted** — visitor comments/spam, not your
  content; real posts come from `Messages/json/messages.json`
- album photos merge within a 10-minute window (videos never merge); photo
  captions → per-image `alt`
- repost: with URL → URL appended + `link_url` for link-faceting; without →
  title/source degrade into plain text
- link facets use UTF-8 byte offsets
- anything else already described under *Features*

## Datasources

| Datasource | Export format handled | Source / export tool |
|---|---|---|
| `qzone` | QQ空间 backup — a `QQ空间备份_<qq>/` directory tree (`Messages/` (说说), `Boards/` (留言板, ignored), `Albums/`, `Videos/`, `Shares/`, `Common/`) | [aqiongbei/qzone_helper](https://github.com/aqiongbei/qzone_helper) |

Datasources register at import time (`datasource/__init__.py`); run
`python cli.py sources` to list the ones available in your checkout.

## Datasource guide (beyond QZone)

Any platform export can be plugged in — nothing is QZone-specific outside
`datasource/qzone/`:

```python
# datasource/my_source/convert.py
from datasource.base import BaseDataSource

class MySource(BaseDataSource):
    source_type = "my_source"

    def build_events(self, root):
        ...             # parse the export, produce list[shared.event.Event]
        return events
```

then register at startup in `datasource/__init__.py`:

```python
from datasource.my_source import MySource
register(MySource)
```

`convert/plan/filter/dry/live/undo` all work unchanged.

## Project structure

```
cli.py                  # terminal entry (subcommands)
datasource/
  base.py               # BaseDataSource abstraction
  __init__.py           # import-time registry
  qzone/convert.py      # QZone parsing (the reference datasource)
shared/
  event.py              # generic event model (v1) + load_events
  planner.py            # events → PDS-compliant tasks (limits/tweetstorm/AVIF)
  executor.py           # DAG-scheduling executor (dry/live, checkpointing)
  undoer.py             # rollback ("后悔药")
  auth.py               # session-cached login
  filter_server.py      # local editor server (token-protected)
  paths.py              # all artifact paths (./data)
tools/editor.html       # browser filter UI served by `filter`
```

## Dependencies

- Python ≥ 3.13; `atproto` (AsyncClient + lexicon models)
- system `ffmpeg` / `ffprobe`
- dev: `ruff` (lint + format via `uv add --dev ruff`; `ruff check --fix` and
  `ruff format`)
