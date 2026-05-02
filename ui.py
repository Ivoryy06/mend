"""
Arrow-key CLI — curses UI: split-pane control system.

Layout:
  ┌─ mend ──────────────────────────────────────────────────────┐
  │ [status bar: task / health / lkg / time]                    │
  ├─────────────────────────┬───────────────────────────────────┤
  │  MENU (left pane)       │  LOG / DETAIL (right pane)        │
  │  ↑↓ navigate            │  auto-scrolls during execution    │
  │  Enter select           │  ↑↓ scroll when focused           │
  ├─────────────────────────┴───────────────────────────────────┤
  │ [key hints]                                                  │
  └─────────────────────────────────────────────────────────────┘

Keys (menu focused):
  ↑/↓  j/k    navigate
  Enter        select / run task
  Tab          switch focus to log pane
  t            cycle tag filter
  w            run one watchdog cycle
  s            show snapshots
  p            pause / resume
  r            reset state
  q / Esc      quit

Keys (log focused):
  ↑/↓          scroll
  Tab / Esc    return focus to menu
"""

import curses, threading, time, os
from typing import List, Callable, Optional

_C_SELECTED = 1
_C_TITLE    = 2
_C_STATUS   = 3
_C_DONE     = 4
_C_FAIL     = 5
_C_DIM      = 6
_C_WARN     = 7
_C_BORDER   = 8

_ICONS = {
    "run":   "▶", "done":  "✓", "failed": "✗",
    "skip":  "⊘", "retry": "↺", "fatal":  "☠",
    "dry":   "~", "info":  "·",
}

_FC_LABEL = {
    "system":      ("SYS",  _C_FAIL),
    "external":    ("EXT",  _C_WARN),
    "recoverable": ("REC",  _C_DIM),
    "fatal":       ("SYS",  _C_FAIL),   
}


def _init_colors():
    curses.use_default_colors()
    curses.init_pair(_C_SELECTED, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(_C_TITLE,    curses.COLOR_CYAN,   -1)
    curses.init_pair(_C_STATUS,   curses.COLOR_WHITE,  -1)
    curses.init_pair(_C_DONE,     curses.COLOR_GREEN,  -1)
    curses.init_pair(_C_FAIL,     curses.COLOR_RED,    -1)
    curses.init_pair(_C_DIM,      8,                   -1)   
    curses.init_pair(_C_WARN,     curses.COLOR_YELLOW, -1)
    curses.init_pair(_C_BORDER,   curses.COLOR_CYAN,   -1)


def _safe_addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    text = text[:w - x - 1]
    if not text:
        return
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass




class Logger:
    def __init__(self, max_lines: int = 500):
        self.lines: List[tuple] = []
        self.max_lines = max_lines
        self._lock = threading.Lock()

    def add(self, event: str, op: str, msg: str = ""):
        with self._lock:
            self.lines.append((event, op, msg, time.time()))
            if len(self.lines) > self.max_lines:
                self.lines.pop(0)

    def _format(self, event, op, msg, ts) -> tuple[str, int]:
        icon = _ICONS.get(event, "·")
        text = f" {icon} {op}" + (f"  {msg}" if msg else "")
        color = {
            "done":  _C_DONE, "fatal": _C_FAIL, "failed": _C_FAIL,
            "retry": _C_WARN, "dry":   _C_DIM,  "skip":   _C_DIM,
        }.get(event, 0)
        return text, color

    def show(self, state_summary: dict):
        """Standalone scrollable log viewer (used from menu)."""
        curses.wrapper(self._show_loop, state_summary)

    def _show_loop(self, stdscr, summary: dict):
        _init_colors()
        curses.curs_set(0)
        scroll = 0
        while True:
            stdscr.clear()
            h, w = stdscr.getmaxyx()
            s = summary
            header = (f" {s.get('task','—')}  {s.get('status','—')}  "
                      f"✓{s.get('done',0)} ✗{s.get('failed',0)} "
                      f"⊘{s.get('skipped',0)} ▶{s.get('pending',0)}  {s.get('elapsed',0)}s")
            _safe_addstr(stdscr, 0, 0, header, curses.color_pair(_C_TITLE) | curses.A_BOLD)
            _safe_addstr(stdscr, 1, 0, "─" * (w - 1), curses.color_pair(_C_DIM))
            with self._lock:
                snap = list(self.lines)
            for i, (ev, op, msg, ts) in enumerate(snap[scroll: scroll + h - 4]):
                text, color = self._format(ev, op, msg, ts)
                _safe_addstr(stdscr, 2 + i, 0, text,
                             curses.color_pair(color) if color else curses.A_NORMAL)
            _safe_addstr(stdscr, h - 1, 0, " ↑↓ scroll   any key to return ", curses.color_pair(_C_DIM))
            stdscr.refresh()
            key = stdscr.getch()
            if key == curses.KEY_UP:
                scroll = max(0, scroll - 1)
            elif key == curses.KEY_DOWN:
                scroll = min(max(0, len(snap) - (h - 4)), scroll + 1)
            else:
                return




class LivePanel:
    def __init__(self, logger: Logger, state_fn: Callable[[], dict]):
        self.logger   = logger
        self.state_fn = state_fn
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=curses.wrapper, args=(self._loop,), daemon=True)
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
            s    = self.state_fn()
            spin = spinner[tick % len(spinner)]
            tick += 1
            header = (f" {spin} {s.get('task','—')}  "
                      f"✓{s.get('done',0)} ✗{s.get('failed',0)} "
                      f"⊘{s.get('skipped',0)} ▶{s.get('pending',0)}  {s.get('elapsed',0)}s")
            _safe_addstr(stdscr, 0, 0, header, curses.color_pair(_C_TITLE) | curses.A_BOLD)
            _safe_addstr(stdscr, 1, 0, "─" * (w - 1), curses.color_pair(_C_DIM))
            with self.logger._lock:
                lines = list(self.logger.lines)
            scroll = max(0, len(lines) - (h - 4))
            for i, (ev, op, msg, ts) in enumerate(lines[scroll: scroll + h - 4]):
                text, color = self.logger._format(ev, op, msg, ts)
                _safe_addstr(stdscr, 2 + i, 0, text,
                             curses.color_pair(color) if color else curses.A_NORMAL)
            _safe_addstr(stdscr, h - 1, 0, " live  p pause  q abort ", curses.color_pair(_C_DIM))
            stdscr.refresh()
            key = stdscr.getch()
            if key == ord('q'):
                self._running = False
            time.sleep(0.1)




def pick_snapshot(snapshots: list) -> Optional[str]:
    """Arrow-key picker for rollback snapshots. Returns chosen path or None."""
    if not snapshots:
        return None
    return curses.wrapper(_snapshot_loop, snapshots)


def _snapshot_loop(stdscr, snapshots: list) -> Optional[str]:
    _init_colors()
    curses.curs_set(0)
    cursor = 0
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        _safe_addstr(stdscr, 0, 0, " Rollback Snapshots", curses.color_pair(_C_TITLE) | curses.A_BOLD)
        _safe_addstr(stdscr, 1, 0, "─" * (w - 1), curses.color_pair(_C_DIM))
        for i, snap in enumerate(snapshots[:h - 4]):
            size_mb = snap.get("size", 0) // (1024 * 1024)
            label   = f"  {snap['name']}  ({size_mb} MB)"
            attr    = curses.color_pair(_C_SELECTED) if i == cursor else curses.A_NORMAL
            _safe_addstr(stdscr, 2 + i, 0, label, attr)
        _safe_addstr(stdscr, h - 1, 0, " ↑↓ navigate   Enter select   Esc cancel ",
                     curses.color_pair(_C_DIM))
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord('k')):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_DOWN, ord('j')):
            cursor = min(len(snapshots) - 1, cursor + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            return snapshots[cursor]["path"]
        elif key in (27, ord('q')):
            return None




def failure_menu(op_name: str, failure_class: str, error: str, output: str) -> str:
    """
    Full-screen failure recovery picker.
    Returns: 'autofix' | 'manual' | 'skip' | 'abort'
    Skip is hidden for 'system' class.
    """
    return curses.wrapper(_failure_loop, op_name, failure_class, error, output)


def _failure_loop(stdscr, op_name, failure_class, error, output) -> str:
    _init_colors()
    curses.curs_set(0)

    fc_label, fc_color = _FC_LABEL.get(failure_class, ("???", _C_WARN))

    if failure_class == "system":
        options = [("Manual fix  (open shell)", "manual"),
                   ("Abort  (preserve state for resume)", "abort")]
    elif failure_class == "external":
        options = [("Auto-fix  — AI diagnoses and fixes  (recommended)", "autofix"),
                   ("Manual fix  (open shell)", "manual"),
                   ("Skip  (continue without this step)", "skip"),
                   ("Abort  (preserve state for resume)", "abort")]
    else:
        options = [("Auto-fix  — AI diagnoses and fixes  (recommended)", "autofix"),
                   ("Manual fix  (open shell)", "manual"),
                   ("Skip  (continue without this step)", "skip"),
                   ("Abort  (preserve state for resume)", "abort")]

    cursor = 0
    scroll = 0

    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()

        
        _safe_addstr(stdscr, 0, 0,
                     f" ❌ [{fc_label}] {op_name}",
                     curses.color_pair(fc_color) | curses.A_BOLD)
        _safe_addstr(stdscr, 1, 0, "─" * (w - 1), curses.color_pair(_C_DIM))

        
        lines = []
        for ln in error.splitlines():
            lines.append((" Error: " + ln, _C_FAIL))
        if output:
            lines.append((" Output:", _C_DIM))
            for ln in output[-800:].splitlines():
                lines.append(("   " + ln, _C_DIM))

        detail_h = min(len(lines), h - len(options) - 6)
        for i, (ln, color) in enumerate(lines[scroll: scroll + detail_h]):
            _safe_addstr(stdscr, 2 + i, 0, ln, curses.color_pair(color))

        sep_y = 2 + detail_h
        _safe_addstr(stdscr, sep_y, 0, "─" * (w - 1), curses.color_pair(_C_DIM))

        for i, (label, _) in enumerate(options):
            y = sep_y + 1 + i
            if y >= h - 1:
                break
            attr = curses.color_pair(_C_SELECTED) if i == cursor else curses.A_NORMAL
            _safe_addstr(stdscr, y, 2, label, attr)

        hint = " ↑↓ navigate   Enter select"
        if len(lines) > detail_h:
            hint += "   PgUp/PgDn scroll error"
        _safe_addstr(stdscr, h - 1, 0, hint, curses.color_pair(_C_DIM))
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord('k')):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_DOWN, ord('j')):
            cursor = min(len(options) - 1, cursor + 1)
        elif key == curses.KEY_PPAGE:
            scroll = max(0, scroll - (detail_h // 2))
        elif key == curses.KEY_NPAGE:
            scroll = min(max(0, len(lines) - detail_h), scroll + (detail_h // 2))
        elif key in (curses.KEY_ENTER, 10, 13):
            return options[cursor][1]




class Menu:
    """
    Split-pane interactive menu.
    Left: task list with tag filter.
    Right: live log / status detail.
    Tab switches focus between panes.
    """

    def __init__(self, title: str, items: List[str],
                 status_fn: Callable[[], str] = None,
                 tags: List[str] = None,
                 logger: Optional[Logger] = None,
                 watchdog_fn: Callable[[], list] = None):
        self.title       = title
        self._all_items  = items
        self.items       = list(items)
        self.status_fn   = status_fn or (lambda: "")
        self.tags        = ["all"] + (tags or [])
        self._tag_idx    = 0
        self.cursor      = 0
        self.logger      = logger or Logger()
        self.watchdog_fn = watchdog_fn  
        self._focus      = "menu"       
        self._log_scroll = 0

    def _active_tag(self) -> str:
        return self.tags[self._tag_idx % len(self.tags)]

    def _filter(self, tag: str):
        if tag == "all":
            self.items = list(self._all_items)
        else:
            self.items = [i for i in self._all_items
                          if i.startswith("──") or tag in i.lower()]
        self.cursor = min(self.cursor, max(0, len(self.items) - 1))

    def run(self) -> Optional[str]:
        return curses.wrapper(self._loop)

    def _draw_status_bar(self, stdscr, w: int):
        status = self.status_fn()
        ts     = time.strftime("%H:%M:%S")
        bar    = f" {status}  {ts}"
        _safe_addstr(stdscr, 1, 0, bar[:w - 1], curses.color_pair(_C_STATUS))

    def _draw_menu_pane(self, win, focused: bool):
        h, w = win.getmaxyx()
        tag   = self._active_tag()
        title = f" Tasks  [t:{tag}]"
        attr  = curses.color_pair(_C_TITLE) | curses.A_BOLD
        _safe_addstr(win, 0, 0, title, attr)
        _safe_addstr(win, 1, 0, ("═" if focused else "─") * (w - 1),
                     curses.color_pair(_C_BORDER if focused else _C_DIM))

        
        visible_h = h - 3
        start = max(0, self.cursor - visible_h + 1)

        for i, item in enumerate(self.items[start: start + visible_h]):
            idx  = start + i
            y    = 2 + i
            is_sep = item.startswith("──")
            if is_sep:
                _safe_addstr(win, y, 1, item[:w - 2], curses.color_pair(_C_DIM))
            elif idx == self.cursor and focused:
                _safe_addstr(win, y, 0, f" {item}"[:w - 1], curses.color_pair(_C_SELECTED))
            elif idx == self.cursor:
                _safe_addstr(win, y, 0, f">{item}"[:w - 1], curses.color_pair(_C_WARN))
            else:
                _safe_addstr(win, y, 1, item[:w - 2])

    def _draw_log_pane(self, win, focused: bool):
        h, w = win.getmaxyx()
        _safe_addstr(win, 0, 0, " Log",
                     curses.color_pair(_C_TITLE) | curses.A_BOLD)
        _safe_addstr(win, 1, 0, ("═" if focused else "─") * (w - 1),
                     curses.color_pair(_C_BORDER if focused else _C_DIM))

        with self.logger._lock:
            lines = list(self.logger.lines)

        visible_h = h - 3
        if not focused:
            
            self._log_scroll = max(0, len(lines) - visible_h)

        for i, (ev, op, msg, ts) in enumerate(lines[self._log_scroll: self._log_scroll + visible_h]):
            text, color = self.logger._format(ev, op, msg, ts)
            _safe_addstr(win, 2 + i, 0, text[:w - 1],
                         curses.color_pair(color) if color else curses.A_NORMAL)

        if focused and len(lines) > visible_h:
            pct = int(100 * self._log_scroll / max(1, len(lines) - visible_h))
            _safe_addstr(win, h - 1, w - 6, f"{pct:3d}%", curses.color_pair(_C_DIM))

    def _loop(self, stdscr) -> Optional[str]:
        _init_colors()
        curses.curs_set(0)
        stdscr.nodelay(False)

        while True:
            stdscr.clear()
            h, w = stdscr.getmaxyx()

            
            _safe_addstr(stdscr, 0, 0, f" {self.title} — mend",
                         curses.color_pair(_C_TITLE) | curses.A_BOLD)
            self._draw_status_bar(stdscr, w)
            _safe_addstr(stdscr, 2, 0, "─" * (w - 1), curses.color_pair(_C_DIM))

            
            split   = max(24, w * 2 // 5)
            pane_h  = h - 5

            left_win  = stdscr.derwin(pane_h, split,         3, 0)
            right_win = stdscr.derwin(pane_h, w - split - 1, 3, split + 1)

            
            for row in range(3, 3 + pane_h):
                _safe_addstr(stdscr, row, split, "│", curses.color_pair(_C_DIM))

            self._draw_menu_pane(left_win,  self._focus == "menu")
            self._draw_log_pane(right_win,  self._focus == "log")

            
            if self._focus == "menu":
                hint = " ↑↓ nav  Enter run  Tab→log  t filter  w check  s snaps  p pause  r reset  q quit"
            else:
                hint = " ↑↓ scroll  Tab→menu  Esc back"
            _safe_addstr(stdscr, h - 1, 0, hint[:w - 1], curses.color_pair(_C_DIM))

            stdscr.refresh()

            key = stdscr.getch()

            
            if self._focus == "log":
                with self.logger._lock:
                    total = len(self.logger.lines)
                visible_h = pane_h - 3
                if key in (curses.KEY_UP, ord('k')):
                    self._log_scroll = max(0, self._log_scroll - 1)
                elif key in (curses.KEY_DOWN, ord('j')):
                    self._log_scroll = min(max(0, total - visible_h), self._log_scroll + 1)
                elif key == curses.KEY_PPAGE:
                    self._log_scroll = max(0, self._log_scroll - visible_h)
                elif key == curses.KEY_NPAGE:
                    self._log_scroll = min(max(0, total - visible_h), self._log_scroll + visible_h)
                elif key in (9, 27):   
                    self._focus = "menu"
                continue

            
            if key in (curses.KEY_UP, ord('k')):
                self.cursor = max(0, self.cursor - 1)
                while self.cursor > 0 and self.items[self.cursor].startswith("──"):
                    self.cursor -= 1
            elif key in (curses.KEY_DOWN, ord('j')):
                self.cursor = min(len(self.items) - 1, self.cursor + 1)
                while self.cursor < len(self.items) - 1 and self.items[self.cursor].startswith("──"):
                    self.cursor += 1
            elif key in (curses.KEY_ENTER, 10, 13):
                if self.items:
                    return self.items[self.cursor]
            elif key in (ord('q'), 27):
                return None
            elif key == 9:   
                self._focus = "log"
            elif key == ord('t'):
                self._tag_idx += 1
                self._filter(self._active_tag())
            elif key == ord('p'):
                return "__pause__"
            elif key == ord('r'):
                return "__reset__"
            elif key == ord('w'):
                return "__watch_cycle__"
            elif key == ord('s'):
                return "__snapshots__"
