#!/usr/bin/env python3
"""One list of every opencode and codex session on this machine.

Enter / left click on a row opens that session in a new terminal window
(opencode: `opencode -s ID`, codex: `codex resume ID`) in the session's own
working directory.

Stdlib only — python3 + curses.
"""

from __future__ import annotations

import argparse
import curses
import json
import locale
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

HOME = os.path.expanduser("~")
OPENCODE_DB = os.environ.get(
    "OPENCODE_DB", os.path.join(HOME, ".local/share/opencode/opencode.db")
)
CODEX_DB = os.environ.get("CODEX_STATE_DB", os.path.join(HOME, ".codex/state_5.sqlite"))

OPENCODE_BIN = shutil.which("opencode") or "opencode"
CODEX_BIN = shutil.which("codex") or "codex"
UWSM_APP = shutil.which("uwsm-app")  # launch windows as proper desktop apps

SOURCES = ["all", "opencode", "codex"]

PRINT_ONLY = False  # --print-cmd: report the launch command instead of spawning


# ---------------------------------------------------------------- data model


@dataclass
class Session:
    app: str  # "opencode" | "codex"
    id: str
    title: str
    cwd: str
    updated: float  # epoch seconds
    model: str = ""
    cost: float | None = None
    tokens: int | None = None
    archived: bool = False
    child: bool = False
    first: str = ""
    last: str = ""
    _preview_loaded: bool = field(default=False, repr=False)

    @property
    def project(self) -> str:
        base = os.path.basename(self.cwd.rstrip("/")) or self.cwd
        return base

    @property
    def resume_cmd(self) -> list[str]:
        if self.app == "opencode":
            return [OPENCODE_BIN, "-s", self.id]
        return [CODEX_BIN, "resume", self.id]

    @property
    def resume_str(self) -> str:
        cmd = " ".join(self.resume_cmd)
        return f"{cmd}   (cwd {self.cwd})"


WARNINGS: list[str] = []


def _row(db: str, sql: str) -> list[tuple]:
    if not os.path.exists(db):
        WARNINGS.append(f"missing db {db}")
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:  # db busy / schema drift — do not kill the TUI
        WARNINGS.append(f"{os.path.basename(db)}: {exc}")
        return []


def load_opencode() -> list[Session]:
    sql = """
        select id, title, directory, time_updated, model, cost,
               tokens_input + tokens_output, time_archived, parent_id
        from session_v2
    """
    out = []
    for sid, title, directory, tupd, model, cost, tokens, arch, parent in _row(
        OPENCODE_DB, sql
    ):
        mid = ""
        if model:
            try:
                mid = json.loads(model).get("id", "") or ""
            except (ValueError, AttributeError):
                mid = str(model)
        out.append(
            Session(
                app="opencode",
                id=sid,
                title=(title or "(untitled)").strip(),
                cwd=directory or "",
                updated=(tupd or 0) / 1000.0,
                model=mid,
                cost=cost,
                tokens=tokens,
                archived=bool(arch),
                child=bool(parent),
            )
        )
    return out


def _codex_db() -> str:
    """Prefer $CODEX_STATE_DB, else the newest ~/.codex/state_*.sqlite."""
    if os.environ.get("CODEX_STATE_DB"):
        return CODEX_DB
    if os.path.exists(CODEX_DB):
        return CODEX_DB
    import glob

    cands = sorted(glob.glob(os.path.join(HOME, ".codex/state_*.sqlite")))
    return cands[-1] if cands else CODEX_DB


def load_codex() -> list[Session]:
    sql = """
        select id, nullif(name, ''), title, cwd, updated_at, archived,
               tokens_used, model, first_user_message, preview
        from threads
    """
    out = []
    for sid, name, title, cwd, tupd, arch, tokens, model, first, preview in _row(
        _codex_db(), sql
    ):
        out.append(
            Session(
                app="codex",
                id=sid,
                title=(name or title or "(untitled)").strip(),
                cwd=cwd or "",
                updated=float(tupd or 0),
                model=model or "",
                tokens=tokens,
                archived=bool(arch),
                first=(first or preview or "").strip(),
                last=(preview or "").strip(),
            )
        )
    return out or _codex_from_index()


def _codex_from_index() -> list[Session]:
    """Fallback when the state db is missing: ~/.codex/session_index.jsonl."""
    path = os.path.join(HOME, ".codex/session_index.jsonl")
    out: list[Session] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                sid = obj.get("id")
                if not sid:
                    continue
                ts = obj.get("updated_at") or ""
                try:
                    upd = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    upd = 0.0
                out.append(
                    Session(
                        app="codex",
                        id=sid,
                        title=(obj.get("thread_name") or "(untitled)").strip(),
                        cwd="",
                        updated=upd,
                    )
                )
    except OSError:
        pass
    return out


def load_all() -> list[Session]:
    sessions = load_opencode() + load_codex()
    sessions.sort(key=lambda s: s.updated, reverse=True)
    return sessions


def load_detail(s: Session) -> None:
    """Fetch the first/last user message for one session (opencode only)."""
    if s._preview_loaded or s.app != "opencode":
        return
    s._preview_loaded = True
    if not os.path.exists(OPENCODE_DB):
        return
    try:
        con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=5.0)
        try:
            rows = con.execute(
                "select type, data from session_message "
                "where session_id=? and type in ('user','assistant') "
                "order by seq",
                (s.id,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return

    for mtype, data in rows:
        text = _msg_text(data)
        if not text:
            continue
        if mtype == "user":
            s.first = s.first or text
            s.last = text

    if not s.first and rows:
        s.first = _msg_text(rows[0][1])


def _msg_text(data: str) -> str:
    try:
        obj = json.loads(data)
    except ValueError:
        return ""
    if isinstance(obj.get("text"), str):
        return _squash(obj["text"])
    parts = []
    for part in obj.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(part.get("text") or "")
    return _squash(" ".join(parts))


def _squash(text: str) -> str:
    return " ".join((text or "").split())


# ------------------------------------------------------------ display utils


def dwidth(s: str) -> int:
    total = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return total


def trunc(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if dwidth(s) <= width:
        return s
    out, used = [], 0
    for ch in s:
        w = 0 if unicodedata.combining(ch) else (
            2 if unicodedata.east_asian_width(ch) in "WF" else 1
        )
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def pad(s: str, width: int) -> str:
    s = trunc(s, width)
    return s + " " * max(0, width - dwidth(s))


def wrap(text: str, width: int, limit: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        trial = word if not cur else cur + " " + word
        if dwidth(trial) <= width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = trunc(word, width)
        if len(lines) >= limit:
            break
    if cur and len(lines) < limit:
        lines.append(cur)
    return lines[:limit]


def ago(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs / 60)}m"
    if secs < 86400:
        return f"{int(secs / 3600)}h"
    return f"{int(secs / 86400)}d"


def human_tokens(n: int | None) -> str:
    if not n:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def launch(s: Session) -> str:
    cwd = s.cwd if s.cwd and os.path.isdir(s.cwd) else HOME
    cmd = ["xdg-terminal-exec", f"--dir={cwd}", "--"] + s.resume_cmd
    if UWSM_APP:
        cmd = [UWSM_APP, "--"] + cmd
    if PRINT_ONLY:
        return " ".join(cmd)
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return f"launch failed: {exc}"
    return f"opened {s.app} · {s.project}"


def copy_to_clipboard(text: str) -> str:
    for tool in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(tool[0]):
            try:
                subprocess.run(
                    tool, input=text.encode(), check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return "copied to clipboard"
            except (OSError, subprocess.CalledProcessError):
                continue
    return "no clipboard tool (wl-copy / xclip)"


# ---------------------------------------------------------------------- TUI


class App:
    def __init__(self, stdscr, sessions: list[Session]):
        self.scr = stdscr
        self.sessions = sessions
        self.source = 0  # index into SOURCES
        self.project = ""  # "" = every project
        self.query = ""
        self.show_archived = False
        self.show_children = False
        self.sel = 0  # index into self.rows
        self.top = 0
        self.status = ""
        self.loaded_at = time.time()
        self.rowmap: dict[int, int] = {}
        self._init_colors()

    # -- colors

    def _init_colors(self) -> None:
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_MAGENTA, -1)
            curses.init_pair(3, curses.COLOR_BLUE, -1)
            curses.init_pair(4, curses.COLOR_YELLOW, -1)
            curses.init_pair(5, curses.COLOR_GREEN, -1)
        except curses.error:
            pass

    def attr(self, pair: int, extra: int = 0) -> int:
        return curses.color_pair(pair) | extra

    # -- filtering

    def projects(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for s in self.sessions:
            if s.archived and not self.show_archived:
                continue
            counts[s.project] = counts.get(s.project, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def rebuild(self) -> None:
        src = SOURCES[self.source]
        q = self.query.lower()
        rows = []
        for s in self.sessions:
            if src != "all" and s.app != src:
                continue
            if s.archived and not self.show_archived:
                continue
            if s.child and not self.show_children:
                continue
            if self.project and s.project != self.project:
                continue
            if q and q not in (
                f"{s.title} {s.project} {s.cwd} {s.model} {s.id}".lower()
            ):
                continue
            rows.append(s)
        self.rows = rows
        self.sel = max(0, min(self.sel, len(rows) - 1))
        self.clamp()

    def clamp(self) -> None:
        height = self.list_height()
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + height:
            self.top = max(0, self.sel - height + 1)

    def list_height(self) -> int:
        h, _ = self.scr.getmaxyx()
        return max(1, h - 3 - self.preview_height() - 1)

    def preview_height(self) -> int:
        h, w = self.scr.getmaxyx()
        if h < 16 or w < 50:
            return 4
        return 7

    # -- drawing

    def put(self, y: int, x: int, text: str, attr: int = 0, limit: int | None = None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        room = w - x if limit is None else min(limit, w - x)
        if room <= 0:
            return
        try:
            self.scr.addstr(y, x, trunc(text, room), attr)
        except curses.error:
            pass

    def draw(self) -> None:
        self.rebuild()
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        pv = self.preview_height()

        # header
        bar = " session-hub "
        self.put(0, 0, bar, self.attr(3, curses.A_BOLD))
        active = sum(1 for s in self.sessions if not s.archived)
        oc = sum(1 for s in self.sessions if s.app == "opencode" and not s.archived)
        cx = sum(1 for s in self.sessions if s.app == "codex" and not s.archived)
        self.put(
            0, len(bar),
            f"{oc} opencode · {cx} codex · {active} active · {len(self.rows)} shown"
            f" · updated {ago(time.time() - self.loaded_at)} ago",
            self.attr(3),
            limit=w - len(bar) - 1,
        )

        # filter line
        src = SOURCES[self.source].upper()
        chips = [
            f"src:{src}",
            f"proj:{self.project or 'ALL'}",
            f"filter:{(self.query or '*')}",
        ]
        if self.show_archived:
            chips.append("archived")
        if self.show_children:
            chips.append("children")
        self.put(1, 1, "  ".join(chips), self.attr(4), limit=w - 2)

        # column header
        pw = self.proj_width()
        hdr = f"{'':2}  {'updated':<11}  {pad('project', pw)}  title"
        self.put(2, 1, hdr, curses.A_BOLD, limit=w - 2)

        # rows
        height = self.list_height()
        self.rowmap = {}
        self.clamp()
        now = time.time()
        for i in range(height):
            idx = self.top + i
            y = 3 + i
            if idx >= len(self.rows):
                break
            s = self.rows[idx]
            self.rowmap[y] = idx
            stamp = datetime.fromtimestamp(s.updated).strftime("%m-%d %H:%M")
            tag = "OC" if s.app == "opencode" else "CX"
            line = f"{tag}  {stamp}  {pad(s.project, pw)}  {s.title}"
            if idx == self.sel:
                self.scr.move(y, 0)
                self.scr.clrtoeol()
                self.put(y, 1, pad(line, w - 2), curses.A_REVERSE)
            else:
                dim = curses.A_DIM if s.archived else 0
                self.put(y, 1, tag, self.attr(1 if s.app == "opencode" else 2, dim))
                self.put(
                    y, 4, f"{stamp}  {pad(s.project, pw)}  {s.title}",
                    dim, limit=w - 5,
                )

        # preview panel
        pv_top = 3 + height
        self.draw_preview(pv_top, h, w, pv)

        # footer
        keys = "↑↓ move · Enter/click open · / filter · Tab source · p project · a archived · c children · y yank · r refresh · q quit"
        self.put(h - 1, 0, keys, self.attr(3) | curses.A_BOLD, limit=w)
        if self.status:
            msg = f" {self.status} "
            x = max(0, w - dwidth(msg) - 1)
            self.put(h - 1, x, " " * (w - x), 0)
            self.put(h - 1, x, msg, self.attr(5, curses.A_BOLD), limit=w - x)
        self.scr.refresh()

    def proj_width(self) -> int:
        _, w = self.scr.getmaxyx()
        return max(10, min(26, w // 4))

    def draw_preview(self, y0: int, h: int, w: int, pv: int) -> None:
        line = "─" * max(0, w - 2)
        self.put(y0 - 1, 1, line, curses.A_DIM, limit=w - 2)
        if not self.rows:
            self.put(y0, 1, "no session matches — Esc clears filters", curses.A_DIM)
            return
        s = self.rows[self.sel]
        load_detail(s)

        meta = [s.app, s.model or "?"]
        if s.cost:
            meta.append(f"${s.cost:.4f}")
        tok = human_tokens(s.tokens)
        if tok:
            meta.append(f"{tok} tok")
        meta.append(datetime.fromtimestamp(s.updated).strftime("%Y-%m-%d %H:%M"))
        self.put(y0, 1, f"{s.project}  ·  {'  ·  '.join(meta)}", curses.A_BOLD, limit=w - 2)
        self.put(y0 + 1, 1, s.resume_str, self.attr(4), limit=w - 2)

        avail = pv - 3
        text = s.last or s.first or "(no message text)"
        label = "last ask" if s.app == "opencode" else "first ask"
        self.put(y0 + 2, 1, f"▸ {label}", self.attr(5), limit=w - 2)
        body = wrap(text, max(20, w - 6), max(1, avail))
        for i, ln in enumerate(body):
            self.put(y0 + 3 + i, 3, ln, curses.A_DIM, limit=w - 4)

    # -- input

    def prompt(self, label: str, initial: str = "") -> str | None:
        h, w = self.scr.getmaxyx()
        buf = initial
        curses.curs_set(1)
        try:
            while True:
                self.put(h - 1, 0, " " * (w - 1), 0)
                self.put(h - 1, 1, f"{label}: {buf}", self.attr(4), limit=w - 2)
                self.put(
                    h - 1, max(0, w - 20), "Enter ok · Esc cancel", curses.A_DIM,
                )
                self.scr.move(h - 1, min(w - 2, 3 + dwidth(label) + dwidth(buf)))
                self.scr.refresh()
                ch = self.scr.get_wch()
                if ch in ("\n", "\r", curses.KEY_ENTER):
                    return buf
                if ch == "\x1b":
                    return None
                if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    buf = buf[:-1]
                elif ch == curses.KEY_RESIZE:
                    h, w = self.scr.getmaxyx()
                elif isinstance(ch, str) and ch.isprintable():
                    buf += ch
                else:
                    continue
                if label == "filter":
                    self.query = buf
                    self.sel = 0
                    self.draw()
        finally:
            curses.curs_set(0)

    def menu(self, title: str, items: list[tuple[str, int]]) -> str | None:
        """Centered picker; returns the chosen label, "" for all, or None."""
        h, w = self.scr.getmaxyx()
        rows = [("ALL projects", 0)] + items
        width = min(max(30, max(dwidth(t) for t, _ in rows) + 8), w - 6)
        view = min(len(rows), max(5, h - 8))
        top = max(1, (h - view) // 2)
        left = max(1, (w - width) // 2)
        pos = offset = 0
        curses.curs_set(0)
        while True:
            if pos < offset:
                offset = pos
            elif pos >= offset + view:
                offset = pos - view + 1
            win = curses.newwin(view + 2, width, top, left)
            win.keypad(True)
            win.erase()
            try:
                win.addnstr(0, 0, pad(f" {title}", width - 1), width - 1,
                            self.attr(3, curses.A_BOLD))
            except curses.error:
                pass
            for i in range(view):
                idx = offset + i
                if idx >= len(rows):
                    break
                label, count = rows[idx]
                line = pad(f" {label}", width - 8) + f"{count:>5} "
                try:
                    win.addnstr(1 + i, 0, line, width - 1,
                                curses.A_REVERSE if idx == pos else 0)
                except curses.error:
                    pass
            win.refresh()
            ch = win.getch()
            if ch in (curses.KEY_UP, ord("k")) and pos > 0:
                pos -= 1
            elif ch in (curses.KEY_DOWN, ord("j")) and pos < len(rows) - 1:
                pos += 1
            elif ch in (10, 13, curses.KEY_ENTER):
                return rows[pos][0] if pos else ""
            elif ch in (27, curses.KEY_RESIZE):
                return None

    def open_selected(self) -> None:
        if not self.rows:
            return
        s = self.rows[self.sel]
        self.status = launch(s)
        self.loaded_at = time.time()

    def handle_mouse(self, ch: int) -> None:
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return
        if bstate & (curses.BUTTON4_PRESSED | curses.BUTTON4_RELEASED):
            self.sel = max(0, self.sel - 3)
            return
        if bstate & (curses.BUTTON5_PRESSED | curses.BUTTON5_RELEASED):
            self.sel = min(len(self.rows) - 1, self.sel + 3)
            return
        if not (bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED)):
            return
        idx = self.rowmap.get(my)
        if idx is None:
            return
        self.sel = idx
        # a click opens the session in a new window (the whole point of the list)
        self.open_selected()

    def run(self) -> None:
        curses.set_escdelay(25)
        self.scr.keypad(True)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(120)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.scr.timeout(500)
        self.draw()
        while True:
            try:
                ch = self.scr.get_wch()
            except curses.error:  # timeout
                if time.time() - self.loaded_at > 10:
                    self.sessions = load_all()
                    self.loaded_at = time.time()
                    self.draw()
                continue

            if ch == curses.KEY_RESIZE:
                self.draw()
                continue
            if ch == curses.KEY_MOUSE:
                self.handle_mouse(ch)
                self.draw()
                continue

            if ch in ("q", "Q"):
                return
            if ch in (curses.KEY_UP, "k"):
                self.sel = max(0, self.sel - 1)
            elif ch in (curses.KEY_DOWN, "j"):
                self.sel = min(len(self.rows) - 1, self.sel + 1)
            elif ch == curses.KEY_PPAGE:
                self.sel = max(0, self.sel - self.list_height())
            elif ch == curses.KEY_NPAGE:
                self.sel = min(len(self.rows) - 1, self.sel + self.list_height())
            elif ch in (curses.KEY_HOME, "g"):
                self.sel = 0
            elif ch in (curses.KEY_END, "G"):
                self.sel = max(0, len(self.rows) - 1)
            elif ch in ("\n", "\r", curses.KEY_ENTER, "o", "O"):
                self.open_selected()
            elif ch == "\t":
                self.source = (self.source + 1) % len(SOURCES)
                self.sel = 0
            elif ch == "p":
                choice = self.menu("project", self.projects())
                if choice is not None:
                    self.project = choice
                    self.sel = 0
            elif ch in ("a", "A"):
                self.show_archived = not self.show_archived
                self.sel = 0
            elif ch in ("c", "C"):
                self.show_children = not self.show_children
                self.sel = 0
            elif ch == "/":
                got = self.prompt("filter", self.query)
                if got is not None:
                    self.query = got
                    self.sel = 0
            elif ch == "y":
                if self.rows:
                    self.status = copy_to_clipboard(self.rows[self.sel].id)
            elif ch in ("r", "R"):
                self.sessions = load_all()
                self.loaded_at = time.time()
                self.status = "refreshed"
            elif ch == "\x1b":
                self.query = ""
                self.project = ""
                self.source = 0
                self.sel = 0
                self.status = "filters cleared"

            self.draw()


# ------------------------------------------------------------------- entry


def cmd_list(args) -> int:
    sessions = load_all()
    q = (args.query or "").lower()
    now = time.time()
    for s in sessions:
        if s.archived and not args.all:
            continue
        if q and q not in f"{s.title} {s.cwd} {s.id}".lower():
            continue
        stamp = datetime.fromtimestamp(s.updated).strftime("%Y-%m-%d %H:%M")
        print(
            "\t".join(
                [
                    s.app,
                    s.id,
                    stamp,
                    ago(now - s.updated),
                    s.project,
                    s.model or "-",
                    s.title.replace("\t", " "),
                ]
            )
        )
    return 0


def cmd_open(args) -> int:
    sessions = load_all()
    needle = args.target.lower()
    hits = [s for s in sessions if needle in s.id.lower()]
    if not hits:
        hits = [s for s in sessions if needle in f"{s.title} {s.project}".lower()]
    if not hits:
        print(f"session-hub: no session matches {args.target!r}", file=sys.stderr)
        return 1
    print(launch(hits[0]))
    return 0


def cmd_tui(_args) -> int:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    sessions = load_all()

    def run(stdscr):
        app = App(stdscr, sessions)
        if WARNINGS:
            app.status = " · ".join(WARNINGS)
        app.run()

    if not sessions:
        print(
            "session-hub: no opencode or codex sessions found\n"
            f"  opencode db: {OPENCODE_DB}\n  codex db:    {CODEX_DB}",
            file=sys.stderr,
        )
        return 1
    curses.wrapper(run)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="session-hub",
        description="All opencode + codex sessions in one TUI list. "
        "Selecting a row opens that session in a new terminal window.",
    )
    sub = parser.add_subparsers(dest="cmd")
    parser.add_argument(
        "--print-cmd",
        action="store_true",
        help="print the launch command instead of opening a window (testing)",
    )
    p_tui = sub.add_parser("tui", help="interactive list (default)")
    p_list = sub.add_parser("list", help="plain-text listing for scripts")
    p_list.add_argument("query", nargs="?", help="substring filter")
    p_list.add_argument("--all", action="store_true", help="include archived")
    p_open = sub.add_parser("open", help="open a session by id or title substring")
    p_open.add_argument("target")
    args = parser.parse_args(argv)

    global PRINT_ONLY
    PRINT_ONLY = args.print_cmd

    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "open":
        return cmd_open(args)
    return cmd_tui(args)


if __name__ == "__main__":
    sys.exit(main())
