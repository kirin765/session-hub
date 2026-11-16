# session-hub

One TUI list of every **opencode** and **codex** session on this machine. Pick a row and it
opens in a new terminal window, resumed in that session's own directory.

```
 session-hub  572 opencode · 281 codex · 853 active · 798 shown · updated 0s ago
 src:ALL  proj:ALL  filter:*
      updated       project                    title
  OC  11-16 12:49   cafe24-review              Korean food distributor prospects
  OC  11-16 12:49   llm-wiki                   TUI for OpenCode and Codex sessions
  CX  11-16 12:46   llm-wiki                   한국형 고가 B2B SaaS 아이디어 추천
  OC  11-16 12:32   reviewboost                SEO and GEO for Cafe24 Apps
 ─────────────────────────────────────────────────────────────────────────────────
 llm-wiki  ·  opencode  ·  deepseek-v4.1-flash  ·  $0.0544  ·  169.0k tok  ·  2026-11-16 12:49
 opencode -s ses_e19…    (cwd /home/giwan/Projects/cafe24-review)
 ▸ last ask
   Use $b2b-email-prospects to find 5 new Korean businesses for this idea: …
```

## What it reads

| tool | source |
|---|---|
| opencode | `~/.local/share/opencode/opencode.db` → `session_v2`, `session_message` |
| codex | `~/.codex/state_*.sqlite` → `threads` (falls back to `~/.codex/session_index.jsonl`) |

Both databases are opened **read-only**. Codex's filename is versioned, so the newest
`state_*.sqlite` is used when `state_5.sqlite` is absent.

## Opening a session

The row is launched through `xdg-terminal-exec` (wrapped in `uwsm-app` when present), so the
window follows the desktop's default terminal and app rules:

- opencode → `opencode -s <session-id>` with cwd = session directory
- codex → `codex resume <session-id>` with cwd = session directory

## Keys

| key | action |
|---|---|
| `↑` `↓` / `j` `k` | move |
| `Enter` / left click | open in a new window |
| `/` | filter (live, matches title, project, cwd, model, id) |
| `Tab` | cycle source: all → opencode → codex |
| `p` | project picker |
| `a` | toggle archived sessions |
| `c` | toggle child/subagent sessions |
| `y` | copy session id to the clipboard |
| `r` | reload |
| `Esc` | clear all filters |
| `q` | quit |

Wheel scrolling and terminal resizes are handled; the list refreshes itself every 10 seconds.

## Install

Stdlib only — python3 + curses, no dependencies.

```bash
git clone https://github.com/kirin765/session-hub.git ~/Projects/session-hub
ln -sf ~/Projects/session-hub/session_hub.py ~/.local/bin/session-hub
```

## Also usable from scripts

```bash
session-hub list [query] [--all]   # TSV: app, id, updated, age, project, model, title
session-hub open "thread name"     # open by id or title substring
session-hub --print-cmd            # print the window command instead of launching it
```

Override the data locations with `OPENCODE_DB` and `CODEX_STATE_DB`.
