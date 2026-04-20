"""
Arrow-key CLI — curses UI with live execution panel, op detail view, tag filter.

Controls (menu):
  ↑/↓ or j/k   navigate
  Enter         select
  t             cycle tag filter
  p             pause/resume
  r             reset state
  q / Esc       quit

Controls (log/detail view):
  ↑/↓           scroll
  any other key return
"""

import curses, threading, time
from typing import List, Callable, Optional

# ── colour pair indices ───────────────────────────────────────────────────────
_C_SELECTED = 1
_C_TITLE    = 2
_C_STATUS   = 3
_C_DONE     = 4
_C_FAIL     = 5
_C_DIM      = 6
_C_WARN     = 7

_ICONS = {
    "run":   "▶", "done":  "✓", "failed": "✗",
    "skip":  "⊘", "retry": "↺", "fatal":  "☠",
    "dry":   "~", "info":  "·",
}


def _init_colors():
    curses.use_default_colors()
    curses.init_pair(_C_SELECTED, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(_C_TITLE,    curses.COLOR_YELLOW, -1)
    curses.init_pair(_C_STATUS,   curses.COLOR_CYAN,   -1)
    curses.init_pair(_C_DONE,     curses.COLOR_GREEN,  -1)
    curses.init_pair(_C_FAIL,     curses.COLOR_RED,    -1)
    curses.init_pair(_C_DIM,      curses.COLOR_WHITE,  -1)
    curses.init_pair(_C_WARN,     curses.COLOR_YELLOW, -1)


# ── Logger ────────────────────────────────────────────────────────────────────

class Logger:
    def __init__(self, max_lines: int = 500):
        self.lines: List[tuple] = []   # (event, op, msg, ts)
        self.max_lines = max_lines
        self._lock = threading.Lock()

    def add(self, event: str, op: str, msg: str = ""):
        with self._lock:
            self.lines.append((event, op, msg, time.time()))
            if len(self.lines) > self.max_lines:
                self.lines.pop(0)

    def _format(self, event, op, msg, ts) -> tuple[str, int]:
        icon = _ICONS.get(event, "·")
        text = f" {icon} {op}" + (f": {msg}" if msg else "")
        attr = {
            "done": _C_DONE, "fatal": _C_FAIL, "failed": _C_FAIL,
            "retry": _C_WARN, "dry": _C_DIM,
        }.get(event, 0)
        return text, attr

    def show(self, state_summary: dict):
        curses.wrapper(self._show_loop, state_summary)

    def _show_loop(self, stdscr, summary: dict):
        _init_colors()
        curses.curs_set(0)
        scroll = 0

        while True:
            stdscr.clear()
            h, w = stdscr.getmaxyx()

            s = summary
            header = (f" Task: {s.get('task','—')}  Status: {s.get('status','—')}  "
                      f"✓{s.get('done',0)} ✗{s.get('failed',0)} "
                      f"⊘{s.get('skipped',0)} ▶{s.get('pending',0)}  "
                      f"elapsed: {s.get('elapsed',0)}s")
            stdscr.addstr(0, 0, header[:w-1], curses.color_pair(_C_TITLE) | curses.A_BOLD)
            stdscr.addstr(1, 0, "─" * (w-1), curses.A_DIM)

            with self._lock:
                snapshot = list(self.lines)

            visible = snapshot[scroll: scroll + h - 4]
            for i, (ev, op, msg, ts) in enumerate(visible):
                text, attr = self._format(ev, op, msg, ts)
                pair = curses.color_pair(attr) if attr else curses.A_NORMAL
                stdscr.addstr(2 + i, 0, text[:w-1], pair)

            stdscr.addstr(h-1, 0, " ↑↓ scroll  any key to return "[:w-1], curses.A_DIM)
            stdscr.refresh()

            key = stdscr.getch()
            if key == curses.KEY_UP:
                scroll = max(0, scroll - 1)
            elif key == curses.KEY_DOWN:
                scroll = min(max(0, len(snapshot) - (h - 4)), scroll + 1)
            else:
                return


# ── Live execution panel ──────────────────────────────────────────────────────

class LivePanel:
    """
    Displays a live-updating execution view while ops run in a background thread.
    Call start() before launching executor, stop() after it finishes.
    """

    def __init__(self, logger: Logger, state_fn: Callable[[], dict]):
        self.logger = logger
        self.state_fn = state_fn
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=curses.wrapper, args=(self._loop,), daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)

    def _loop(self, stdscr):
        _init_colors()
        curses.curs_set(0)
        stdscr.nodelay(True)
        scroll = 0
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        tick = 0

        while self._running:
            stdscr.clear()
            h, w = stdscr.getmaxyx()
            s = self.state_fn()

            spin = spinner[tick % len(spinner)]
            tick += 1

            header = (f" {spin} {s.get('task','—')}  "
                      f"✓{s.get('done',0)} ✗{s.get('failed',0)} "
                      f"⊘{s.get('skipped',0)} ▶{s.get('pending',0)}  "
                      f"{s.get('elapsed',0)}s")
            stdscr.addstr(0, 0, header[:w-1], curses.color_pair(_C_TITLE) | curses.A_BOLD)
            stdscr.addstr(1, 0, "─" * (w-1), curses.A_DIM)

            with self.logger._lock:
                lines = list(self.logger.lines)

            # auto-scroll to bottom
            max_scroll = max(0, len(lines) - (h - 4))
            scroll = max_scroll

            visible = lines[scroll: scroll + h - 4]
            for i, (ev, op, msg, ts) in enumerate(visible):
                text, attr = self.logger._format(ev, op, msg, ts)
                pair = curses.color_pair(attr) if attr else curses.A_NORMAL
                try:
                    stdscr.addstr(2 + i, 0, text[:w-1], pair)
                except curses.error:
                    pass

            stdscr.addstr(h-1, 0, " live view — p pause  q abort "[:w-1], curses.A_DIM)
            stdscr.refresh()

            key = stdscr.getch()
            if key == ord('q'):
                self._running = False
            time.sleep(0.1)


# ── Menu ──────────────────────────────────────────────────────────────────────

class Menu:
    def __init__(self, title: str, items: List[str],
                 status_fn: Callable[[], str] = None,
                 tags: List[str] = None):
        self.title = title
        self._all_items = items
        self.items = items
        self.status_fn = status_fn or (lambda: "")
        self.tags = ["all"] + (tags or [])
        self._tag_idx = 0
        self.cursor = 0

    def _active_tag(self) -> str:
        return self.tags[self._tag_idx % len(self.tags)]

    def _filter(self, tag: str):
        if tag == "all":
            self.items = self._all_items
        else:
            self.items = [i for i in self._all_items if i.startswith("──") or tag in i.lower()]
        self.cursor = min(self.cursor, max(0, len(self.items) - 1))

    def run(self) -> Optional[str]:
        return curses.wrapper(self._loop)

    def _loop(self, stdscr) -> Optional[str]:
        _init_colors()
        curses.curs_set(0)

        while True:
            stdscr.clear()
            h, w = stdscr.getmaxyx()

            tag = self._active_tag()
            title_line = f" {self.title}  [tag: {tag}]"
            stdscr.addstr(0, 0, title_line[:w-1], curses.color_pair(_C_TITLE) | curses.A_BOLD)

            status = self.status_fn()
            if status:
                stdscr.addstr(1, 0, (" " + status)[:w-1], curses.color_pair(_C_STATUS))

            stdscr.addstr(2, 0, "─" * (w-1), curses.A_DIM)

            offset = 3
            for i, item in enumerate(self.items):
                if offset + i >= h - 1:
                    break
                is_sep = item.startswith("──")
                if is_sep:
                    attr = curses.A_DIM
                elif i == self.cursor:
                    attr = curses.color_pair(_C_SELECTED)
                else:
                    attr = curses.A_NORMAL
                stdscr.addstr(offset + i, 2, item[:w-3], attr)

            help_bar = " ↑↓ nav  Enter select  t filter  p pause  r reset  q quit"
            stdscr.addstr(h-1, 0, help_bar[:w-1], curses.A_DIM)
            stdscr.refresh()

            key = stdscr.getch()
            if key in (curses.KEY_UP, ord('k')):
                self.cursor = max(0, self.cursor - 1)
                # skip separators
                while self.cursor > 0 and self.items[self.cursor].startswith("──"):
                    self.cursor -= 1
            elif key in (curses.KEY_DOWN, ord('j')):
                self.cursor = min(len(self.items) - 1, self.cursor + 1)
                while self.cursor < len(self.items)-1 and self.items[self.cursor].startswith("──"):
                    self.cursor += 1
            elif key in (curses.KEY_ENTER, 10, 13):
                if self.items:
                    return self.items[self.cursor]
            elif key in (ord('q'), 27):
                return None
            elif key == ord('t'):
                self._tag_idx += 1
                self._filter(self._active_tag())
            elif key == ord('p'):
                return "__pause__"
            elif key == ord('r'):
                return "__reset__"
