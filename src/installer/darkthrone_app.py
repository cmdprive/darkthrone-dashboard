#!/usr/bin/env python3
"""
DarkThrone Suite — GUI Launcher
================================
Double-click this file (or run: python darkthrone_app.py) to start.

First time:
  1. Click "Login with Browser" — a browser window opens
  2. Log in to your DarkThrone account
  3. The window closes automatically once you reach the game
  4. Click "Start Optimizer" — it runs every 30 minutes on game ticks

Requirements:  pip install playwright && playwright install chromium
"""

# ── Path fix ────────────────────────────────────────────────────────────────
# Post-reorg layout:
#   <darkthrone>/
#     ├── data/                         runtime data (cwd lives here)
#     ├── src/
#     │   ├── optimizer.py
#     │   ├── index.html                dashboard template
#     │   └── installer/darkthrone_app.py
#     └── release/DarkThrone Suite/
#         └── DarkThrone Suite.exe      frozen entry point
#
# Both the frozen exe and the source .py are 3 dirnames away from the
# darkthrone root, so the path math is symmetric:
#   .py:   src/installer/darkthrone_app.py  → installer → src → darkthrone
#   .exe:  release/DarkThrone Suite/DT Suite.exe → Suite dir → release → darkthrone
#
# We chdir into darkthrone/data/ so every bare filename reference inside
# optimizer.py ("private_latest.json", "auth.json", etc.) resolves there.
# The dashboard TEMPLATE lives at src/index.html and is copied into data/
# on first run; after that update_dashboard() rewrites data/index.html in
# place.  optimizer.py has no hardcoded absolute paths, so moving source
# doesn't require any edits inside optimizer.py itself.
import os as _os, sys as _sys, shutil as _shutil
if getattr(_sys, "frozen", False):
    # Installed / frozen build — ALWAYS use per-user AppData. The exe
    # may live in Program Files (read-only for non-admins) so we can't
    # write next to it. %LOCALAPPDATA% is a standard per-user writable
    # location that survives uninstall + reinstall (keeps auth.json).
    _exe_dir    = _os.path.dirname(_sys.executable)
    _DATA_DIR   = _os.path.join(
        _os.environ.get("LOCALAPPDATA") or _os.path.expanduser("~"),
        "DarkThroneSuite",
    )
    _src_dir    = ""   # no source dir at runtime; modules come from _internal/
else:
    # Dev / source mode — portable data/ next to the repo as before.
    _here       = _os.path.dirname(_os.path.abspath(__file__))   # src/installer/
    _src_dir    = _os.path.dirname(_here)                        # src/
    _darkthrone = _os.path.dirname(_src_dir)                     # darkthrone/
    _exe_dir    = ""
    _DATA_DIR   = _os.path.join(_darkthrone, "data")

_os.makedirs(_DATA_DIR, exist_ok=True)
_os.chdir(_DATA_DIR)

# Bootstrap dashboard template: if index.html doesn't exist yet in the
# data dir, seed it from the template shipped with the source or bundled
# alongside the exe (src/index.html is copied into the release root).
_dashboard_out = _os.path.join(_DATA_DIR, "index.html")
if not _os.path.isfile(_dashboard_out):
    _candidates = []
    if _src_dir: _candidates.append(_os.path.join(_src_dir, "index.html"))
    if _exe_dir: _candidates.append(_os.path.join(_exe_dir, "index.html"))
    for _tpl in _candidates:
        if _os.path.isfile(_tpl):
            try:
                _shutil.copy2(_tpl, _dashboard_out)
            except Exception as _e:
                print(f"⚠️  dashboard template copy failed: {_e}")
            break

# So `import optimizer` finds the source module when running as .py.
# (The frozen exe has optimizer.pyc bundled in _internal/; this insert is a
# no-op there but harmless.)
if _src_dir and _src_dir not in _sys.path:
    _sys.path.insert(0, _src_dir)
_ROOT = _DATA_DIR   # backward-compat alias for anything downstream
# ─────────────────────────────────────────────────────────────────────────────

# ── Playwright browser cache redirect (frozen bundle only) ───────────────────
# When PyInstaller bundles Playwright, the driver looks for Chromium at
#   <bundle>\_internal\playwright\driver\package\.local-browsers\chromium-<ver>\
# but we don't ship the 150MB browser binaries.  At runtime, point Playwright
# at the user's default cache where `install_browser.bat` installs them via
# `python -m playwright install chromium`.  Must run BEFORE playwright imports.
if getattr(_sys, "frozen", False) and _sys.platform == "win32":
    _local = _os.environ.get("LOCALAPPDATA", "")
    if _local:
        _cache = _os.path.join(_local, "ms-playwright")
        _os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _cache)
# ─────────────────────────────────────────────────────────────────────────────

# ── Suppress console windows for ALL child processes on Windows ───────────────
# Prevents Playwright/Chromium subprocess flashes when optimizer runs
if _sys.platform == "win32":
    import subprocess as _sp
    _CREATE_NO_WINDOW = 0x08000000
    _orig_popen = _sp.Popen.__init__
    def _popen_no_window(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
        _orig_popen(self, *args, **kwargs)
    _sp.Popen.__init__ = _popen_no_window
# ─────────────────────────────────────────────────────────────────────────────

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import sys
import os
import json
import datetime
import time
import io
import webbrowser

# Version info — imported from src/_version.py (bundled with the exe via
# PyInstaller). Fallback defaults keep the app runnable during dev even if
# the module lookup fails for some reason.
try:
    from _version import __version__, __update_repo__
except ImportError:
    __version__ = "dev"
    __update_repo__ = "cmdprive/darkthrone-suite"

# Auto-updater — lazy-imported in __init__ so an import failure doesn't
# block the GUI from launching.

def _unhide_file(path):
    """Remove hidden/read-only Windows attribute so the file can be written."""
    if sys.platform == "win32" and os.path.isfile(path):
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)  # NORMAL
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────────────────────
AUTH_FILE   = "auth.json"
CONFIG_FILE = "user_config.json"
CHART_FILE  = "optimizer_chart.html"
DASH_FILE   = "index.html"

RACES   = ["Human", "Goblin", "Elf", "Undead"]
CLASSES = ["Fighter", "Cleric", "Thief", "Assassin"]

# Must match STRATEGY_WEIGHTS keys in optimizer.py (decide_v2 engine).
# Legacy keys (balanced/attack/defense/economy/spy/hybrid) are auto-migrated
# to the new 3-profile set at config-load time via _LEGACY_STRAT_MAP below.
STRATEGY_LABELS = {
    "grow":   {"label": "📈  Grow",   "desc": "Net worth focus — income + cheap army + XP for unlocks"},
    "combat": {"label": "⚔️  Combat", "desc": "ATK-heavy — maximize offensive power for farming + PvP"},
    "defend": {"label": "🛡️  Defend", "desc": "DEF-heavy — survive attacks, protect bank + rank"},
}

# Legacy → new strategy key migration.  Any old user_config.json with
# strategy="balanced" is silently rewritten to "grow" on first load.
_LEGACY_STRAT_MAP = {
    "balanced": "grow",
    "economy":  "grow",
    "attack":   "combat",
    "spy":      "combat",
    "defense":  "defend",
    "hybrid":   "defend",
}

C = {                         # colour palette
    "bg":       "#0f0f14",
    "card":     "#1a1a24",
    "card_alt": "#14141e",
    "border":   "#2a2a3a",
    "text":     "#e0e0e0",
    "dim":      "#666680",
    "gold":     "#ffd700",
    "green":    "#44cc66",
    "red":      "#ff5555",
    "blue":     "#5599ff",
    "orange":   "#ff9933",
    "cyan":     "#00e5ff",
    "purple":   "#bb77ff",
    "btn":      "#252535",
    "btn_act":  "#353545",
    "log_bg":   "#0a0a10",
}


def _fmt_num(n):
    """Format a number with k/M suffix for the player card."""
    if n is None:
        return "—"
    n = int(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)

# ─────────────────────────────────────────────────────────────────────────────
class _Capture(io.StringIO):
    """Redirect print() from background threads to the GUI log."""
    def __init__(self, cb):
        super().__init__()
        self.cb = cb
        self._last = ""
    def write(self, s):
        if s and s.strip():
            self._last = s.rstrip()
            self.cb(self._last)
        return len(s)
    def flush(self): pass


# ─────────────────────────────────────────────────────────────────────────────
class DarkThroneApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"DarkThrone Suite  v{__version__}")
        self.root.geometry("1400x900")
        self.root.minsize(1200, 700)
        self.root.configure(bg=C["bg"])

        self._cfg        = self._load_config()
        self._opt_stop   = threading.Event()
        self._opt_thread = None

        # Battle-loop state (auto-attack / auto-spy).  Widgets created in _build_ui.
        self._battle_stop   = threading.Event()
        self._battle_thread = None

        # Settings vars (used by _save_settings, created before UI so
        # they exist even before the settings popup is opened)
        self._race_var  = tk.StringVar(value=self._cfg.get("race", "Human"))
        self._class_var = tk.StringVar(value=self._cfg.get("class", "Fighter"))

        # Update-banner state — populated when a newer release is found
        # on GitHub. Widgets created in _build_ui().
        self._update_pending = None   # dict(version, url, notes) or None

        self._build_ui()
        self._check_auth()
        self._update_player_card()      # load existing stats on startup
        self._first_run_warning()
        self._start_update_check()

    # ── Config ────────────────────────────────────────────────────────────────
    def _load_config(self):
        cfg = {"race": "Human", "class": "Fighter", "strategy": "grow"}
        if os.path.isfile(CONFIG_FILE):
            try:
                _unhide_file(CONFIG_FILE)
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                pass
        # Migrate legacy strategy keys (balanced/attack/etc.) to the new
        # 3-profile names used by decide_v2.
        legacy = cfg.get("strategy")
        if legacy in _LEGACY_STRAT_MAP:
            cfg["strategy"] = _LEGACY_STRAT_MAP[legacy]
            try:
                _unhide_file(CONFIG_FILE)
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
            except Exception:
                pass
        elif legacy not in STRATEGY_LABELS:
            cfg["strategy"] = "grow"
        return cfg

    def _save_config_file(self):
        try:
            _unhide_file(CONFIG_FILE)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._cfg, f, indent=2)
        except Exception as e:
            print(f"[warn] Could not save config: {e}")

    # ── First-run warning ─────────────────────────────────────────────────────
    def _first_run_warning(self):
        warned = self._cfg.get("_warned", False)
        if not warned:
            msg = (
                "⚠️  Important — please read before using:\n\n"
                "The optimizer logs in to YOUR DarkThrone account and makes\n"
                "REAL in-game decisions every 30 minutes (trains troops,\n"
                "buys gear, upgrades buildings).\n\n"
                "Make sure you understand what it does before starting it.\n"
                "You can stop it at any time with the Stop button.\n\n"
                "This message only appears once."
            )
            messagebox.showinfo("DarkThrone Suite — Welcome", msg)
            self._cfg["_warned"] = True
            self._save_config_file()

    # ── Auto-updater ──────────────────────────────────────────────────────────
    def _start_update_check(self):
        """Kick off the background GitHub-Releases check.  If a newer
        release is found, `_show_update_banner` is called via after() to
        bounce back onto the UI thread safely."""
        try:
            from updater import check_for_update
        except ImportError as e:
            print(f"  ⚠️  updater module unavailable: {e}")
            return

        def _available(version, url, notes):
            # Called from worker thread — marshal to UI via after()
            self.root.after(0, lambda: self._show_update_banner(version, url, notes))

        check_for_update(_available)

    def _show_update_banner(self, version, url, notes):
        """Reveal the update-available banner at the top of the window."""
        if getattr(self, "_update_pending", None):
            return   # banner already showing (don't stomp ongoing download)
        self._update_pending = {"version": version, "url": url, "notes": notes}
        self._update_msg.config(
            text=f"\u2b06  Update available: v{version}  —  click Update now to install.")
        # Pack AFTER the header but BEFORE the separator. We know the
        # separator is currently the 2nd child; insert banner before it.
        self._update_banner.pack(fill="x", padx=12, pady=(0, 6),
                                 before=self.root.pack_slaves()[1])

    def _dismiss_update(self):
        """Hide the banner; user can still update next launch."""
        try:
            self._update_banner.pack_forget()
        except Exception:
            pass

    def _do_update(self):
        """Start the download + install flow."""
        if not getattr(self, "_update_pending", None):
            return
        url = self._update_pending["url"]
        ver = self._update_pending["version"]

        # Confirm with user
        if not messagebox.askokcancel(
                "Install update?",
                f"DarkThrone Suite v{ver} will be downloaded and installed.\n\n"
                f"The current app will close during installation and\n"
                f"relaunch automatically afterwards.\n\n"
                f"Continue?"):
            return

        try:
            from updater import download_and_launch
        except ImportError as e:
            messagebox.showerror("Update error", f"Updater unavailable: {e}")
            return

        # Disable button + show progress
        self._update_btn.config(text="Downloading…", state="disabled")
        self._update_msg.config(text=f"\u2b07  Downloading v{ver}…  0%")

        def _progress(pct, done, total):
            # Marshal to UI thread
            self.root.after(0, lambda: self._update_msg.config(
                text=f"\u2b07  Downloading v{ver}…  {pct:.0f}%  ({done//1024//1024} / {total//1024//1024} MB)"))

        def _error(e):
            self.root.after(0, lambda: messagebox.showerror(
                "Update failed",
                f"Could not install the update: {e}\n\n"
                f"Please download the new version manually from:\n"
                f"https://github.com/{__update_repo__}/releases/latest"))
            self.root.after(0, lambda: self._update_btn.config(
                text="\u25bc Update now", state="normal"))

        download_and_launch(url, progress_cb=_progress, on_error=_error)

    # ── Auth ──────────────────────────────────────────────────────────────────
    def _check_auth(self):
        if os.path.isfile(AUTH_FILE):
            self._auth_lbl.config(text="● Logged in", fg=C["green"])
            self._opt_start_btn.config(state="normal")
            # Battle button only enabled if we have an auth session.
            if hasattr(self, "_battle_start_btn"):
                self._battle_start_btn.config(state="normal")
        else:
            self._auth_lbl.config(text="○ Not logged in", fg=C["dim"])
            self._opt_start_btn.config(state="disabled")
            if hasattr(self, "_battle_start_btn"):
                self._battle_start_btn.config(state="disabled")

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # ── Header bar ───────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=C["bg"])
        hdr.pack(fill="x", padx=18, pady=(10, 6))
        tk.Label(hdr, text="\U0001f3f0  DarkThrone Suite",
                 bg=C["bg"], fg=C["gold"],
                 font=("Segoe UI", 14, "bold")).pack(side="left")
        tk.Label(hdr, text="automated optimizer & battle dashboard",
                 bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(10, 0), pady=3)

        # Quick-access icons: chart + dashboard + settings (top-right)
        tk.Button(hdr, text="\u2699", command=self._open_settings_popup,
                  bg=C["bg"], fg=C["dim"], relief="flat", bd=0,
                  font=("Segoe UI", 14), cursor="hand2",
                  activebackground=C["bg"], activeforeground=C["gold"]).pack(
                      side="right", padx=(4, 0))
        tk.Button(hdr, text="\U0001f310", command=self._open_dash,
                  bg=C["bg"], fg=C["dim"], relief="flat", bd=0,
                  font=("Segoe UI", 12), cursor="hand2",
                  activebackground=C["bg"], activeforeground=C["gold"]).pack(
                      side="right", padx=(4, 0))
        tk.Button(hdr, text="\U0001f4c8", command=self._open_chart,
                  bg=C["bg"], fg=C["dim"], relief="flat", bd=0,
                  font=("Segoe UI", 12), cursor="hand2",
                  activebackground=C["bg"], activeforeground=C["gold"]).pack(
                      side="right", padx=(4, 0))

        # Update banner (hidden until an update is found on GitHub Releases)
        self._update_banner = tk.Frame(
            self.root, bg="#2d1f00",
            highlightbackground=C["gold"], highlightthickness=1,
        )
        # Intentionally NOT packed yet — `_show_update_banner()` packs it
        # when an update is actually available.
        self._update_msg = tk.Label(
            self._update_banner, text="", bg="#2d1f00", fg=C["gold"],
            font=("Segoe UI", 9, "bold"), padx=14, pady=6, anchor="w")
        self._update_msg.pack(side="left", fill="x", expand=True)
        self._update_btn = tk.Button(
            self._update_banner, text="\u25bc Update now",
            command=self._do_update, bg=C["gold"], fg="#000000",
            activebackground="#ffe680", activeforeground="#000000",
            relief="flat", bd=0, pady=3, padx=12,
            font=("Segoe UI", 9, "bold"), cursor="hand2")
        self._update_btn.pack(side="right", padx=(6, 10), pady=4)
        tk.Button(
            self._update_banner, text="\u2715",
            command=self._dismiss_update, bg="#2d1f00", fg=C["dim"],
            activebackground="#2d1f00", activeforeground=C["text"],
            relief="flat", bd=0, font=("Segoe UI", 10), cursor="hand2").pack(
                side="right", padx=(0, 6))

        # Separator
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill="x")

        # ── Status bar (packed FIRST so it sits at very bottom) ──────────
        self._statusbar = tk.Label(
            self.root, text="Ready.",
            bg=C["border"], fg=C["dim"],
            font=("Segoe UI", 8), anchor="w", padx=10, pady=3)
        self._statusbar.pack(fill="x", side="bottom")

        # ── Body: left column (player card + tabs) | right (activity log)
        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=12, pady=10)

        # ── LEFT COLUMN — Player Card on top, Tabs below ────────────────
        left_col = tk.Frame(body, bg=C["bg"], width=300)
        left_col.pack(side="left", fill="y", padx=(0, 8))
        left_col.pack_propagate(False)

        # Player card (top of left column)
        card = tk.Frame(left_col, bg=C["card"],
                        highlightbackground=C["border"], highlightthickness=1)
        card.pack(fill="x", pady=(0, 6))
        self._build_player_card(card)

        # Tabbed controls (bottom of left column, fills remaining height)
        nb = ttk.Notebook(left_col, style="Dark.TNotebook")
        nb.pack(fill="both", expand=True)

        opt_tab    = tk.Frame(nb, bg=C["card"])
        battle_tab = tk.Frame(nb, bg=C["card"])
        nb.add(opt_tab,      text="Optimizer")
        nb.add(battle_tab,   text="Battle")

        self._build_optimizer_tab(opt_tab)
        self._build_battle_tab(battle_tab)

        # ── RIGHT PANEL — Activity Log (takes all remaining space) ───
        log_outer = tk.Frame(body, bg=C["card"],
                             highlightbackground=C["border"],
                             highlightthickness=1)
        log_outer.pack(side="left", fill="both", expand=True)

        log_hdr = tk.Frame(log_outer, bg=C["card"])
        log_hdr.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(log_hdr, text="\U0001f4cb  Activity Log",
                 bg=C["card"], fg=C["dim"],
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        self._clear_btn = tk.Button(
            log_hdr, text="clear", command=self._clear_log,
            bg=C["card"], fg=C["dim"], relief="flat", bd=0,
            font=("Segoe UI", 8), cursor="hand2",
            activebackground=C["card"], activeforeground=C["text"])
        self._clear_btn.pack(side="right")

        self._log = scrolledtext.ScrolledText(
            log_outer,
            state="disabled", wrap="word",
            bg=C["log_bg"], fg=C["text"],
            font=("Consolas", 10),
            relief="flat", bd=0,
            padx=12, pady=8,
            selectbackground="#2a2a3a",
        )
        self._log.pack(fill="both", expand=True, padx=6, pady=6)

        # Log colour tags
        for tag, color in [
            ("gold",   C["gold"]),   ("green", C["green"]),
            ("red",    C["red"]),    ("dim",   C["dim"]),
            ("orange", C["orange"]), ("blue",  C["blue"]),
            ("battle", C["cyan"]),
        ]:
            self._log.tag_config(tag, foreground=color)

        # Welcome message
        self._log_write(
            "Welcome to DarkThrone Suite.\n"
            "\u2192 Click 'Login with Browser' to authenticate your account.\n"
            "\u2192 Then click 'Start Optimizer' to begin.\n\n",
            "dim")

    # ── Player Card (left panel) ─────────────────────────────────────────────
    def _build_player_card(self, parent):
        pad = {"padx": 14, "pady": 0}

        # Header
        tk.Label(parent, text="PLAYER", bg=C["card"], fg=C["gold"],
                 font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=14, pady=(14, 6))

        # Auth status
        self._auth_lbl = tk.Label(
            parent, text="\u25cb Checking\u2026", bg=C["card"], fg=C["dim"],
            font=("Segoe UI", 9), anchor="w")
        self._auth_lbl.pack(fill="x", **pad)

        # Optimizer status
        self._opt_lbl = tk.Label(
            parent, text="\u25cb Optimizer stopped", bg=C["card"], fg=C["dim"],
            font=("Segoe UI", 9), anchor="w")
        self._opt_lbl.pack(fill="x", **pad)

        # Battle status
        self._battle_lbl = tk.Label(
            parent, text="\u25cb Battle stopped", bg=C["card"], fg=C["dim"],
            font=("Segoe UI", 9), anchor="w")
        self._battle_lbl.pack(fill="x", padx=14, pady=(0, 8))

        # Divider
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", padx=10, pady=4)

        # Stat grid
        self._stat_frame = tk.Frame(parent, bg=C["card"])
        self._stat_frame.pack(fill="x", padx=14, pady=(4, 2))

        # Level label (standalone, not a bar)
        self._pc_level = tk.Label(
            parent, text="Level  \u2014", bg=C["card"], fg=C["gold"],
            font=("Segoe UI", 11, "bold"), anchor="w")
        self._pc_level.pack(fill="x", padx=14, pady=(0, 4))

        # Stat bars — placeholders created here, updated by _update_player_card
        self._pc_bars = {}
        stats_grid = tk.Frame(parent, bg=C["card"])
        stats_grid.pack(fill="x", padx=14, pady=(0, 4))
        stats_grid.columnconfigure(1, weight=1)
        for i, (key, label, color) in enumerate([
            ("atk",     "ATK",    C["red"]),
            ("def",     "DEF",    C["blue"]),
            ("spy_off", "SpyOff", C["purple"]),
            ("spy_def", "SpyDef", C["purple"]),
        ]):
            tk.Label(stats_grid, text=label, bg=C["card"], fg=C["dim"],
                     font=("Segoe UI", 9), anchor="w", width=6).grid(
                         row=i, column=0, sticky="w", pady=1)
            canvas = tk.Canvas(stats_grid, width=100, height=14,
                               bg=C["card_alt"], highlightthickness=0)
            canvas.grid(row=i, column=1, sticky="we", padx=(4, 4), pady=1)
            val_lbl = tk.Label(stats_grid, text="\u2014", bg=C["card"],
                               fg=C["text"], font=("Segoe UI", 10), anchor="e")
            val_lbl.grid(row=i, column=2, sticky="e", pady=1)
            self._pc_bars[key] = {"canvas": canvas, "val": val_lbl, "color": color}

        # Divider
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", padx=10, pady=6)

        # Gold / Income
        self._pc_gold = tk.Label(
            parent, text="\U0001f4b0 Gold  \u2014", bg=C["card"], fg=C["gold"],
            font=("Segoe UI", 10), anchor="w")
        self._pc_gold.pack(fill="x", padx=14, pady=1)
        self._pc_income = tk.Label(
            parent, text="\U0001f4c8 Income  \u2014 / tick", bg=C["card"], fg=C["green"],
            font=("Segoe UI", 10), anchor="w")
        self._pc_income.pack(fill="x", padx=14, pady=(1, 6))

        # Divider
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", padx=10, pady=4)

        # Quick-action buttons
        btn_pad = {"fill": "x", "padx": 14, "pady": 2}

        self._start_all_btn = tk.Button(
            parent, text="\u25b6 Start All", command=self._start_all,
            bg=C["green"], fg="#000000",
            activebackground="#55dd77", activeforeground="#000000",
            relief="flat", bd=0, pady=3, padx=6,
            font=("Segoe UI", 8, "bold"), cursor="hand2")
        self._start_all_btn.pack(**btn_pad)

        self._stop_all_btn = tk.Button(
            parent, text="\u25a0 Stop All", command=self._stop_all,
            bg=C["red"], fg="#ffffff",
            activebackground="#ff7777", activeforeground="#ffffff",
            relief="flat", bd=0, pady=3, padx=6,
            font=("Segoe UI", 8, "bold"), cursor="hand2")
        self._stop_all_btn.pack(**btn_pad)

        login_btn = tk.Button(
            parent, text="\U0001f511 Login", command=self._do_login,
            bg=C["btn"], fg=C["text"],
            activebackground=C["btn_act"], activeforeground=C["gold"],
            relief="flat", bd=0, pady=3, padx=6,
            font=("Segoe UI", 8), cursor="hand2")
        login_btn.pack(**btn_pad)

    # ── Optimizer Tab ────────────────────────────────────────────────────────
    def _build_optimizer_tab(self, parent):
        pad_section = {"padx": 12, "pady": (10, 2)}

        # Section header
        tk.Label(parent, text="STRATEGY", bg=C["card"], fg=C["gold"],
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(
                     fill="x", **pad_section)

        # Strategy description (updates when radio selection changes)
        self._strat_desc_lbl = tk.Label(
            parent, text="", bg=C["card"], fg=C["dim"],
            font=("Segoe UI", 7), anchor="w", wraplength=280, justify="left")
        self._strat_desc_lbl.pack(fill="x", padx=12, pady=(0, 4))

        # Strategy radios
        self._strat_var = tk.StringVar(value=self._cfg.get("strategy", "grow"))
        strat_frame = tk.Frame(parent, bg=C["card"])
        strat_frame.pack(fill="x", padx=12, pady=(0, 6))
        self._strat_radios = {}
        for key, info in STRATEGY_LABELS.items():
            rb = tk.Radiobutton(
                strat_frame,
                text=info["label"],
                variable=self._strat_var,
                value=key,
                command=self._on_strategy_change,
                bg=C["card"], fg=C["text"],
                selectcolor=C["card_alt"],
                activebackground=C["card"],
                activeforeground=C["gold"],
                font=("Segoe UI", 9),
                anchor="w", cursor="hand2",
            )
            rb.pack(fill="x", pady=1)
            self._strat_radios[key] = rb
        self._on_strategy_change()   # set initial description

        # Divider
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", padx=10, pady=3)

        # Control buttons
        btn_frame = tk.Frame(parent, bg=C["card"])
        btn_frame.pack(fill="x", padx=20, pady=(10, 10))

        self._opt_start_btn = tk.Button(
            btn_frame, text="\u25b6 Start", command=self._start_opt,
            bg=C["green"], fg="#000000",
            activebackground="#55dd77", activeforeground="#000000",
            relief="flat", bd=0, pady=3, padx=10,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
            state="disabled")
        self._opt_start_btn.pack(side="left", padx=(0, 6))

        self._opt_stop_btn = tk.Button(
            btn_frame, text="\u25a0 Stop", command=self._stop_opt,
            bg=C["btn"], fg=C["dim"],
            activebackground=C["btn_act"], activeforeground=C["red"],
            relief="flat", bd=0, pady=3, padx=10,
            font=("Segoe UI", 9), cursor="hand2",
            state="disabled")
        self._opt_stop_btn.pack(side="left")

    # ── Battle Tab ───────────────────────────────────────────────────────────
    def _build_battle_tab(self, parent):
        pad = {"padx": 12}

        # ---- Mode row ----
        tk.Label(parent, text="MODE", bg=C["card"], fg=C["gold"],
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(
                     fill="x", padx=12, pady=(10, 2))

        self._battle_mode = tk.StringVar(value=self._cfg.get("battle_mode", "attack"))
        mode_frame = tk.Frame(parent, bg=C["card"])
        mode_frame.pack(fill="x", **pad)
        for val, lbl in (("attack", "\u2694 Attack"), ("spy", "\U0001f50d Spy")):
            tk.Radiobutton(
                mode_frame, text=lbl, variable=self._battle_mode, value=val,
                command=self._on_battle_mode_change,
                bg=C["card"], fg=C["text"], selectcolor=C["card_alt"],
                activebackground=C["card"], activeforeground=C["gold"],
                font=("Segoe UI", 8), anchor="w", cursor="hand2",
            ).pack(side="left", padx=(0, 10))

        # ---- Farm mode row ----
        tk.Label(parent, text="FARM MODE", bg=C["card"], fg=C["gold"],
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(
                     fill="x", padx=12, pady=(6, 2))

        self._battle_farm_mode = tk.StringVar(
            value=self._cfg.get("battle_farm_mode", "gold"))
        ff = tk.Frame(parent, bg=C["card"])
        ff.pack(fill="x", **pad)
        for val, lbl in (("gold", "\U0001f4b0 Gold"), ("xp", "\u2b50 XP"), ("match", "\U0001f3af Match")):
            tk.Radiobutton(
                ff, text=lbl, variable=self._battle_farm_mode, value=val,
                command=self._save_battle_cfg,
                bg=C["card"], fg=C["text"], selectcolor=C["card_alt"],
                activebackground=C["card"], activeforeground=C["gold"],
                font=("Segoe UI", 8), anchor="w", cursor="hand2",
            ).pack(side="left", padx=(0, 6))

        # ---- Numeric fields (grid layout — 2 rows) ----
        fields_frame = tk.Frame(parent, bg=C["card"])
        fields_frame.pack(fill="x", padx=12, pady=(6, 2))

        # Row 0: Turns + Margin
        tk.Label(fields_frame, text="Turns:", bg=C["card"], fg=C["dim"],
                 font=("Segoe UI", 8), anchor="w").grid(
                     row=0, column=0, sticky="w", pady=2)
        self._battle_turns = tk.IntVar(value=int(self._cfg.get("battle_turns", 5)))
        tk.Spinbox(fields_frame, from_=1, to=10, textvariable=self._battle_turns,
                   width=4, font=("Segoe UI", 8),
                   bg=C["card_alt"], fg=C["text"],
                   buttonbackground=C["btn"], relief="flat", bd=1).grid(
                       row=0, column=1, sticky="w", padx=(4, 12), pady=2)

        tk.Label(fields_frame, text="Margin:", bg=C["card"], fg=C["dim"],
                 font=("Segoe UI", 8), anchor="w").grid(
                     row=0, column=2, sticky="w", pady=2)
        self._battle_margin = tk.StringVar(value=str(self._cfg.get("battle_margin", 1.2)))
        tk.Entry(fields_frame, textvariable=self._battle_margin, width=5,
                 font=("Segoe UI", 8),
                 bg=C["card_alt"], fg=C["text"], relief="flat", bd=1,
                 insertbackground=C["text"]).grid(
                     row=0, column=3, sticky="w", padx=(4, 0), pady=2)

        # Row 1: Min gold
        tk.Label(fields_frame, text="Min gold:", bg=C["card"], fg=C["dim"],
                 font=("Segoe UI", 8), anchor="w").grid(
                     row=1, column=0, sticky="w", pady=2)
        self._battle_min_gold = tk.StringVar(
            value=str(self._cfg.get("battle_min_gold", 0)))
        tk.Entry(fields_frame, textvariable=self._battle_min_gold, width=10,
                 font=("Segoe UI", 8),
                 bg=C["card_alt"], fg=C["text"], relief="flat", bd=1,
                 insertbackground=C["text"]).grid(
                     row=1, column=1, columnspan=3, sticky="w", padx=(4, 0), pady=2)

        # ---- Skip checkboxes ----
        chk_frame = tk.Frame(parent, bg=C["card"])
        chk_frame.pack(fill="x", padx=12, pady=(4, 2))

        self._battle_skip_friends = tk.BooleanVar(value=self._cfg.get("battle_skip_friends", True))
        self._battle_skip_clan    = tk.BooleanVar(value=self._cfg.get("battle_skip_clan",    True))
        self._battle_skip_bots    = tk.BooleanVar(value=self._cfg.get("battle_skip_bots",    False))

        for var, txt in (
            (self._battle_skip_friends, "Skip friends"),
            (self._battle_skip_clan,    "Skip clanmates"),
            (self._battle_skip_bots,    "Skip bots"),
        ):
            tk.Checkbutton(
                chk_frame, text=txt, variable=var,
                command=self._save_battle_cfg,
                bg=C["card"], fg=C["text"], selectcolor=C["card_alt"],
                activebackground=C["card"], activeforeground=C["gold"],
                font=("Segoe UI", 8), anchor="w", cursor="hand2",
            ).pack(fill="x", pady=0)

        # ---- Control buttons ----
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", padx=10, pady=3)

        bbtn_frame = tk.Frame(parent, bg=C["card"])
        bbtn_frame.pack(fill="x", padx=20, pady=(6, 10))

        self._battle_start_btn = tk.Button(
            bbtn_frame, text="\u25b6 Start", command=self._start_battle,
            bg=C["green"], fg="#000000",
            activebackground="#55dd77", activeforeground="#000000",
            relief="flat", bd=0, pady=3, padx=10,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
            state="disabled")
        self._battle_start_btn.pack(side="left", padx=(0, 6))

        self._battle_stop_btn = tk.Button(
            bbtn_frame, text="\u25a0 Stop", command=self._stop_battle,
            bg=C["btn"], fg=C["dim"],
            activebackground=C["btn_act"], activeforeground=C["red"],
            relief="flat", bd=0, pady=3, padx=10,
            font=("Segoe UI", 9), cursor="hand2",
            state="disabled")
        self._battle_stop_btn.pack(side="left")

    # ── Settings popup (opened via ⚙ in header) ──────────────────────────────
    def _open_settings_popup(self):
        """Open a small modal window for Race/Class settings."""
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.configure(bg=C["card"])
        win.geometry("300x220")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="CHARACTER", bg=C["card"], fg=C["gold"],
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(
                     fill="x", padx=16, pady=(12, 6))

        grid = tk.Frame(win, bg=C["card"])
        grid.pack(fill="x", padx=16)

        tk.Label(grid, text="Race:", bg=C["card"], fg=C["dim"],
                 font=("Segoe UI", 9), anchor="w").grid(
                     row=0, column=0, sticky="w", pady=4)
        if not hasattr(self, "_race_var"):
            self._race_var = tk.StringVar(value=self._cfg.get("race", "Human"))
        race_cb = ttk.Combobox(grid, textvariable=self._race_var,
                               values=RACES, state="readonly", width=12,
                               font=("Segoe UI", 9))
        race_cb.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=4)

        tk.Label(grid, text="Class:", bg=C["card"], fg=C["dim"],
                 font=("Segoe UI", 9), anchor="w").grid(
                     row=1, column=0, sticky="w", pady=4)
        if not hasattr(self, "_class_var"):
            self._class_var = tk.StringVar(value=self._cfg.get("class", "Fighter"))
        class_cb = ttk.Combobox(grid, textvariable=self._class_var,
                                values=CLASSES, state="readonly", width=12,
                                font=("Segoe UI", 9))
        class_cb.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=4)

        tk.Frame(win, bg=C["border"], height=1).pack(fill="x", padx=12, pady=10)

        def _save_and_close():
            self._save_settings()
            win.destroy()

        tk.Button(win, text="\U0001f4be Save", command=_save_and_close,
                  bg=C["btn"], fg=C["text"],
                  activebackground=C["btn_act"], activeforeground=C["gold"],
                  relief="flat", bd=0, pady=4, padx=12,
                  font=("Segoe UI", 9), cursor="hand2").pack(padx=16, anchor="w")

    # ── Stat bar helper ──────────────────────────────────────────────────────
    def _stat_bar(self, parent, label, value, max_val, color, row):
        """Draw a labeled stat bar in the player card grid."""
        tk.Label(parent, text=label, bg=C["card"], fg=C["dim"],
                 font=("Segoe UI", 9), anchor="w", width=6).grid(
                     row=row, column=0, sticky="w", pady=1)
        bar = tk.Canvas(parent, width=120, height=14,
                        bg=C["card_alt"], highlightthickness=0)
        bar.grid(row=row, column=1, sticky="we", padx=(4, 4), pady=1)
        pct = min(1.0, value / max(max_val, 1))
        bar.create_rectangle(0, 0, int(120 * pct), 14, fill=color, outline="")
        val_lbl = tk.Label(parent, text=_fmt_num(value), bg=C["card"],
                           fg=C["text"], font=("Segoe UI", 10), anchor="e")
        val_lbl.grid(row=row, column=2, sticky="e", pady=1)

    # ── Player card update ───────────────────────────────────────────────────
    def _update_player_card(self, stats=None):
        """Refresh the player-card labels from *stats* dict or
        private_latest.json on disk."""
        if stats is None:
            try:
                with open("private_latest.json", encoding="utf-8") as f:
                    stats = json.load(f)
            except Exception:
                return   # no data yet — leave placeholders

        # Level
        level = stats.get("level", "?")
        self._pc_level.config(text=f"Level  {level}")

        # Stat bars — compute max for relative sizing
        stat_keys = ["atk", "def", "spy_off", "spy_def"]
        values = [stats.get(k, 0) for k in stat_keys]
        max_val = max(values) if values else 1

        for key in stat_keys:
            info = self._pc_bars.get(key)
            if not info:
                continue
            v = stats.get(key, 0)
            pct = min(1.0, v / max(max_val, 1))
            canvas = info["canvas"]
            canvas.delete("all")
            # Redraw after canvas has rendered so winfo_width is accurate
            w = canvas.winfo_width() or 100
            canvas.create_rectangle(0, 0, int(w * pct), 14,
                                    fill=info["color"], outline="")
            info["val"].config(text=_fmt_num(v))

        # Gold / income
        gold = stats.get("gold_on_hand", stats.get("gold", 0))
        income = stats.get("income", 0)
        self._pc_gold.config(text=f"\U0001f4b0 Gold  {_fmt_num(gold)}")
        self._pc_income.config(text=f"\U0001f4c8 Income  {_fmt_num(income)} / tick")

    # ── Quick actions ────────────────────────────────────────────────────────
    def _start_all(self):
        """Start both optimizer and battle as independent loops."""
        self._start_opt()
        # Only auto-start battle if authenticated — it'll silently
        # no-op during login state.
        if os.path.isfile(AUTH_FILE):
            self._start_battle()

    def _stop_all(self):
        """Stop both optimizer and battle."""
        self._stop_opt()
        if self._battle_thread and self._battle_thread.is_alive():
            self._stop_battle()

    # ── Actions ───────────────────────────────────────────────────────────────
    def _do_login(self):
        self._log_write("Opening browser — log in and the window will close automatically.\n", "gold")
        self._status("Waiting for login…")

        def _run():
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=False)
                    ctx = browser.new_context()
                    page = ctx.new_page()
                    page.goto("https://darkthronegame.com/login")
                    # Wait until user reaches /game (dashboard) or any sub-page
                    page.wait_for_url("**/game*", timeout=180_000)
                    # Give the page a moment to fully settle cookies
                    page.wait_for_load_state("networkidle", timeout=10_000)
                    _unhide_file(AUTH_FILE)
                    ctx.storage_state(path=AUTH_FILE)
                    browser.close()
                self._ui(self._log_write,
                         "✅ Login successful! Session saved.\n", "green")
                self._ui(self._check_auth)
                self._ui(self._status, "Logged in.")
            except Exception as e:
                self._ui(self._log_write, f"❌ Login error: {e}\n", "red")
                self._ui(self._status, "Login failed.")

        threading.Thread(target=_run, daemon=True).start()

    def _start_opt(self):
        if self._opt_thread and self._opt_thread.is_alive():
            return
        if self._battle_thread and self._battle_thread.is_alive():
            if not messagebox.askokcancel(
                    "Auto-Battle running",
                    "A standalone auto-battle session is active.  The "
                    "optimizer and battle will coordinate via a lockfile "
                    "— if a tick fires while battle is mid-action it may "
                    "be skipped for this cycle.\n\nTip: stop the standalone "
                    "battle and check 'Battle during optimizer wait' "
                    "instead so the optimizer coordinates both "
                    "automatically.\n\nProceed anyway?"):
                return
        if not os.path.isfile(AUTH_FILE):
            messagebox.showwarning("Not logged in", "Please log in first.")
            return

        self._opt_stop.clear()
        self._opt_thread = threading.Thread(
            target=self._run_optimizer, daemon=True)
        self._opt_thread.start()

        self._opt_lbl.config(text="● Running", fg=C["green"])
        self._opt_start_btn.config(state="disabled")
        self._opt_stop_btn.config(state="normal")
        self._log_write("▶ Optimizer started — will act on every game tick.\n", "green")

    def _stop_opt(self):
        self._opt_stop.set()
        self._opt_lbl.config(text="◌ Stopping…", fg=C["orange"])
        self._opt_start_btn.config(state="normal")
        self._opt_stop_btn.config(state="disabled")
        self._log_write("■ Optimizer will stop after the current tick completes.\n", "dim")

    def _run_optimizer(self):
        import optimizer

        old_out = sys.stdout
        sys.stdout = _Capture(lambda msg: self._ui(self._log_write, msg + "\n"))

        # next_tick_dt is set at the end of each cycle. On the second+
        # iteration the battle phase uses it to know when to stop.
        next_tick_dt = None
        try:
            while not self._opt_stop.is_set():
                # ── Run the optimizer tick (SPENDS all gold on strategy)
                # Runs right after each game tick fires. The battle loop
                # is deliberately NOT coupled to this loop — they run as
                # independent threads (use Start Battle separately) to
                # keep the optimizer's gold-spending logic uncontested.
                try:
                    optimizer.run_tick()
                    self._ui(self._update_player_card)
                except Exception as e:
                    self._ui(self._log_write, f"❌ Tick error: {e}\n", "red")

                # Detect session expiry from captured output
                import io as _io
                _captured = sys.stdout
                if isinstance(_captured, _Capture) and "Session expired" in getattr(_captured, "_last", ""):
                    self._ui(self._log_write,
                             "⚠️  Session expired — click 'Login with Browser' to re-authenticate.\n", "orange")
                    self._ui(self._auth_lbl.config, text="○ Session expired", fg=C["red"])

                if self._opt_stop.is_set():
                    break

                # ── Sleep until next tick ──────────────────────────────
                now          = datetime.datetime.now()
                wait         = (30 * 60) - (now.minute % 30) * 60 - now.second
                next_tick_dt = now + datetime.timedelta(seconds=wait)
                nxt          = next_tick_dt.strftime("%H:%M")
                self._ui(self._log_write,
                         f"⏳  Next tick at {nxt}  ({wait // 60}m {wait % 60}s)\n", "dim")
                self._ui(self._status, f"Sleeping until {nxt}…")

                for _ in range(wait):
                    if self._opt_stop.is_set():
                        break
                    time.sleep(1)
        finally:
            sys.stdout = old_out
            self._ui(self._opt_lbl.config, text="○ Stopped", fg=C["dim"])
            self._ui(self._log_write, "■ Optimizer stopped.\n", "dim")
            self._ui(self._status, "Optimizer stopped.")

    # ── Auto-Battle (attack + spy) ────────────────────────────────────────────
    def _on_battle_mode_change(self):
        self._cfg["battle_mode"] = self._battle_mode.get()
        self._save_config_file()

    def _save_battle_cfg(self):
        """Persist battle panel values to user_config.json on any change."""
        try:
            self._cfg["battle_mode"]         = self._battle_mode.get()
            self._cfg["battle_turns"]        = int(self._battle_turns.get())
            self._cfg["battle_margin"]       = float(self._battle_margin.get() or 1.2)
            self._cfg["battle_skip_friends"] = bool(self._battle_skip_friends.get())
            self._cfg["battle_skip_clan"]    = bool(self._battle_skip_clan.get())
            self._cfg["battle_skip_bots"]    = bool(self._battle_skip_bots.get())
            self._cfg["battle_farm_mode"]    = self._battle_farm_mode.get()
            # min_gold: accept integer, silently clamp bad input to 0
            try:
                self._cfg["battle_min_gold"] = max(0, int(self._battle_min_gold.get() or 0))
            except (TypeError, ValueError):
                self._cfg["battle_min_gold"] = 0
            self._save_config_file()
        except Exception:
            pass

    def _battle_log(self, msg: str, tag: str = "battle"):
        """Callback passed to optimizer.battle_loop.  Pushes to the shared log
        widget on the UI thread.  No stdout redirect — callers can freely
        interleave battle logs with optimizer logs."""
        self._ui(self._log_write, msg + "\n", tag)

    def _start_battle(self):
        if self._battle_thread and self._battle_thread.is_alive():
            return
        if not os.path.isfile(AUTH_FILE):
            messagebox.showwarning("Not logged in", "Please log in first.")
            return
        # When the optimizer is also running, the two loops share the
        # browser session via a lockfile — if a tick fires while battle
        # is mid-action, the tick briefly waits its turn. No prompt
        # needed; this is the normal "run both at once" mode.

        self._save_battle_cfg()
        self._battle_stop.clear()
        self._battle_thread = threading.Thread(
            target=self._run_battle, daemon=True)
        self._battle_thread.start()

        self._battle_lbl.config(text="● Running", fg=C["green"])
        self._battle_start_btn.config(state="disabled")
        self._battle_stop_btn.config(state="normal")
        self._log_write(
            f"▶ Auto-Battle started — mode={self._battle_mode.get()} "
            f"farm={self._battle_farm_mode.get()} "
            f"turns/hit={self._battle_turns.get()} "
            f"margin={self._battle_margin.get()} "
            f"min_gold={self._battle_min_gold.get() or 0}\n", "battle")

    def _stop_battle(self):
        self._battle_stop.set()
        self._battle_lbl.config(text="◌ Stopping…", fg=C["orange"])
        self._battle_start_btn.config(state="normal")
        self._battle_stop_btn.config(state="disabled")
        self._log_write("■ Auto-Battle will stop after the current action.\n", "dim")

    def _run_battle(self):
        import optimizer
        try:
            margin = float(self._battle_margin.get() or 1.2)
        except Exception:
            margin = 1.2
        try:
            min_gold = max(0, int(self._battle_min_gold.get() or 0))
        except (TypeError, ValueError):
            min_gold = 0
        cfg = {
            "mode":          self._battle_mode.get(),
            "margin":        max(1.0, margin),
            "turns_per_hit": int(self._battle_turns.get()),
            "skip_friends":  bool(self._battle_skip_friends.get()),
            "skip_clan":     bool(self._battle_skip_clan.get()),
            "skip_bots":     bool(self._battle_skip_bots.get()),
            "farm_mode":     self._battle_farm_mode.get(),
            "min_gold":      min_gold,
            "max_per_pass":  20,
            "max_total":     200,
            "scrape_pages":  10,
            "dry_run":       False,
        }
        try:
            optimizer.battle_loop(self._battle_stop, cfg, log_fn=self._battle_log)
        except Exception as e:
            self._ui(self._log_write, f"❌ Battle error: {e}\n", "red")
        finally:
            self._ui(self._battle_lbl.config, text="○ Stopped", fg=C["dim"])
            self._ui(self._battle_start_btn.config, state="normal")
            self._ui(self._battle_stop_btn.config, state="disabled")
            self._ui(self._status, "Auto-Battle stopped.")

    def _open_chart(self):
        path = os.path.abspath(CHART_FILE)
        if os.path.isfile(path):
            webbrowser.open(f"file:///{path}")
        else:
            messagebox.showinfo(
                "No chart yet",
                "No growth chart found.\n"
                "Run the optimizer for at least one tick to generate it.")

    def _open_dash(self):
        path = os.path.abspath(DASH_FILE)
        if os.path.isfile(path):
            webbrowser.open(f"file:///{path}")
        else:
            webbrowser.open("https://cmdprive.github.io/darkthrone-dashboard/")

    def _on_strategy_change(self):
        key  = self._strat_var.get()
        info = STRATEGY_LABELS.get(key, {})
        self._strat_desc_lbl.config(text=info.get("desc", ""))
        # Highlight selected radio
        for k, rb in self._strat_radios.items():
            rb.config(fg=C["gold"] if k == key else C["text"])
        # Auto-save immediately
        self._cfg["strategy"] = key
        self._save_config_file()

    def _save_settings(self):
        self._cfg["race"]     = self._race_var.get()
        self._cfg["class"]    = self._class_var.get()
        self._cfg["strategy"] = self._strat_var.get()
        self._save_config_file()
        self._log_write(
            f"✅ Settings saved — Race: {self._cfg['race']}  "
            f"Class: {self._cfg['class']}  "
            f"Strategy: {self._cfg['strategy']}\n",
            "green")

    # ── Log helpers ───────────────────────────────────────────────────────────
    def _log_write(self, msg, tag=None):
        """Write to log — must be called on the main thread."""
        ts  = datetime.datetime.now().strftime("%H:%M:%S")
        # Prefix each new line (not blank separators) with timestamp
        lines = msg.split("\n")
        self._log.config(state="normal")
        for i, line in enumerate(lines):
            sep = "\n" if i < len(lines) - 1 else ""
            if line.strip():
                self._log.insert("end", f"[{ts}] ", "dim")
                if tag:
                    self._log.insert("end", line + sep, tag)
                else:
                    self._log.insert("end", line + sep)
            else:
                self._log.insert("end", sep)
        self._log.see("end")
        self._log.config(state="disabled")

    def _clear_log(self):
        self._log.config(state="normal")
        self._log.delete("1.0", "end")
        self._log.config(state="disabled")

    def _status(self, msg):
        self._statusbar.config(text=f"  {msg}")

    # ── Thread-safe UI update helper ─────────────────────────────────────────
    def _ui(self, fn, *args, **kwargs):
        """Schedule fn(*args, **kwargs) on the Tk main thread."""
        self.root.after(0, fn, *args, **kwargs)


# ── Entry point ───────────────────────────────────────────────────────────────
def _check_deps():
    missing = []
    try:
        import playwright  # noqa
    except ImportError:
        missing.append("playwright")
    if missing:
        print("Missing dependencies:", ", ".join(missing))
        print("Run:  pip install playwright  &&  playwright install chromium")
        sys.exit(1)


def _chromium_installed() -> bool:
    """Check whether Playwright's chromium browser has already been
    downloaded to the cache dir. Returns False for a fresh install where
    first-run setup is still needed."""
    cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ms-playwright")
    if not os.path.isdir(cache):
        return False
    # Playwright installs into `chromium-{version}/chrome-win/chrome.exe`.
    # Finding any chrome.exe deep under the cache is a good signal.
    for root_dir, _dirs, files in os.walk(cache):
        if "chrome.exe" in files:
            return True
    return False


def _install_chromium_with_splash():
    """Show a modal Tk splash + progress while Playwright downloads Chromium.

    Invoked only on first launch when _chromium_installed() is False.
    Uses the bundled playwright module (no external python needed).
    """
    import tkinter as _tk
    splash = _tk.Tk()
    splash.title("DarkThrone Suite — First-time setup")
    splash.configure(bg="#0f0f14")
    splash.geometry("460x200")
    splash.resizable(False, False)

    _tk.Label(splash, text="\U0001f3f0  Welcome to DarkThrone Suite",
              bg="#0f0f14", fg="#ffd700",
              font=("Segoe UI", 13, "bold")).pack(pady=(20, 6))
    _tk.Label(splash,
              text="Downloading browser automation (~150 MB)\n"
                   "This happens once — future launches are instant.",
              bg="#0f0f14", fg="#e0e0e0",
              font=("Segoe UI", 9), justify="center").pack(pady=4)
    status = _tk.Label(splash, text="Starting download…",
                       bg="#0f0f14", fg="#666680",
                       font=("Consolas", 8))
    status.pack(pady=(10, 6))
    pb = ttk.Progressbar(splash, mode="indeterminate", length=340)
    pb.pack(pady=(0, 8))
    pb.start(10)
    splash.update()

    import subprocess
    done = {"ok": False, "error": None}

    def _run_install():
        try:
            # Invoke Playwright's CLI module directly via the bundled
            # Python runtime. PyInstaller-frozen apps don't have a
            # standalone python.exe, so we use the current process's
            # interpreter via runpy.
            import runpy, sys as _sys
            _old_argv = _sys.argv
            _sys.argv = ["playwright", "install", "chromium"]
            try:
                runpy.run_module("playwright", run_name="__main__")
            except SystemExit as se:
                # playwright's CLI calls sys.exit(0) on success
                if se.code not in (0, None):
                    raise
            finally:
                _sys.argv = _old_argv
            done["ok"] = True
        except Exception as e:
            done["error"] = e

    t = threading.Thread(target=_run_install, daemon=True)
    t.start()

    # Pump the Tk event loop until the worker finishes
    while t.is_alive():
        splash.update()
        time.sleep(0.1)

    pb.stop()
    splash.destroy()

    if not done["ok"]:
        err = done.get("error") or "unknown error"
        messagebox.showerror(
            "First-time setup failed",
            f"Could not download the browser:\n\n{err}\n\n"
            f"You can retry by restarting the Suite. If it keeps failing,\n"
            f"check your internet connection or firewall.")
        sys.exit(1)


if __name__ == "__main__":
    _check_deps()

    # First-run: download Chromium (~150 MB) with a progress splash.
    # After this completes, subsequent launches go straight to the main GUI.
    if not _chromium_installed():
        _install_chromium_with_splash()

    root = tk.Tk()

    # Style ttk widgets to match dark theme
    style = ttk.Style(root)
    style.theme_use("clam")

    # Combobox
    style.configure("TCombobox",
                    fieldbackground=C["btn"],
                    background=C["btn"],
                    foreground=C["text"],
                    selectbackground=C["btn_act"],
                    selectforeground=C["gold"],
                    arrowcolor=C["dim"])
    style.map("TCombobox",
              fieldbackground=[("readonly", C["btn"])],
              foreground=[("readonly", C["text"])])

    # Notebook (tabbed pane)
    style.configure("Dark.TNotebook",
                    background=C["bg"],
                    borderwidth=0,
                    tabmargins=[0, 0, 0, 0])
    style.configure("Dark.TNotebook.Tab",
                    background=C["btn"],
                    foreground=C["dim"],
                    padding=[8, 3],
                    font=("Segoe UI", 9),
                    borderwidth=0,
                    width=8)
    style.map("Dark.TNotebook.Tab",
              background=[("selected", C["card"]),
                          ("active",   C["btn_act"])],
              foreground=[("selected", C["gold"]),
                          ("active",   C["text"])],
              expand=[("selected", [0, 0, 0, 0])])

    # Window icon (if icon file present)
    ico = os.path.join(os.path.dirname(__file__), "icon.ico")
    if os.path.isfile(ico):
        try: root.iconbitmap(ico)
        except Exception: pass

    app = DarkThroneApp(root)  # noqa
    root.mainloop()
