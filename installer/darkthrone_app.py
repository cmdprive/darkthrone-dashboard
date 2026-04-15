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

# ── Path fix: works when run as .py (subfolder), frozen .exe, or from root ────
import os as _os, sys as _sys
if getattr(_sys, "frozen", False):
    _ROOT = _os.path.dirname(_sys.executable)
else:
    _ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_os.chdir(_ROOT)
_sys.path.insert(0, _ROOT)
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

# Must match STRATEGIES keys in optimizer.py
STRATEGY_LABELS = {
    "balanced": {"label": "⚖️  Balanced",   "desc": "Even spread — workers, soldiers, guards, spies, sentries"},
    "attack":   {"label": "⚔️  Attack",     "desc": "Heavy soldiers — max offense, light defense"},
    "defense":  {"label": "🛡️  Defense",    "desc": "Heavy guards — max defense, light offense"},
    "economy":  {"label": "💰  Economy",    "desc": "Max workers and income buildings, minimal army"},
    "spy":      {"label": "🗡️  Spy",        "desc": "Heavy spies and sentries, intelligence focused"},
    "hybrid":   {"label": "⚔️🛡️  Hybrid",  "desc": "Soldiers + guards only, skip spy units"},
}

C = {                         # colour palette
    "bg":      "#0d0d0d",
    "card":    "#161616",
    "border":  "#252525",
    "text":    "#cccccc",
    "dim":     "#555555",
    "gold":    "#e8c96d",
    "green":   "#5dba6f",
    "red":     "#d95f5f",
    "blue":    "#5090d0",
    "orange":  "#d08050",
    "btn":     "#222222",
    "btn_act": "#2e2e2e",
    "log_bg":  "#0a0a0a",
}

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
        self.root.title("DarkThrone Suite")
        self.root.geometry("980x720")
        self.root.minsize(900, 520)
        self.root.configure(bg=C["bg"])

        self._cfg        = self._load_config()
        self._opt_stop   = threading.Event()
        self._opt_thread = None

        # Battle-loop state (auto-attack / auto-spy).  Widgets created in _build_ui.
        self._battle_stop   = threading.Event()
        self._battle_thread = None

        self._build_ui()
        self._check_auth()
        self._first_run_warning()

    # ── Config ────────────────────────────────────────────────────────────────
    def _load_config(self):
        if os.path.isfile(CONFIG_FILE):
            try:
                _unhide_file(CONFIG_FILE)
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"race": "Human", "class": "Fighter"}

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
        # Header
        hdr = tk.Frame(self.root, bg=C["bg"])
        hdr.pack(fill="x", padx=18, pady=(14, 8))
        tk.Label(hdr, text="🛡️  DarkThrone Suite",
                 bg=C["bg"], fg=C["gold"],
                 font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Label(hdr, text="automated optimizer & dashboard",
                 bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(10, 0), pady=3)

        # Separator
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill="x")

        # Body: sidebar + log
        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=18, pady=14)

        # ── Scrollable sidebar ───────────────────────────────────────────
        # The sidebar has more content than a 720px window can show on
        # short laptop screens, so wrap it in a Canvas + Scrollbar.  The
        # inner `sb` frame is what every _section / _button / etc. packs
        # into, exactly as before — the scroll rig is transparent.
        sb_outer = tk.Frame(body, bg=C["bg"], width=226)
        sb_outer.pack(side="left", fill="y", padx=(0, 14))
        sb_outer.pack_propagate(False)

        sb_canvas = tk.Canvas(
            sb_outer, bg=C["bg"], highlightthickness=0, bd=0, width=210)
        sb_scroll = tk.Scrollbar(
            sb_outer, orient="vertical", command=sb_canvas.yview,
            bg=C["bg"], troughcolor=C["card"], activebackground=C["border"],
            highlightthickness=0, bd=0, width=10)
        sb_canvas.configure(yscrollcommand=sb_scroll.set)
        sb_scroll.pack(side="right", fill="y")
        sb_canvas.pack(side="left", fill="both", expand=True)

        sb = tk.Frame(sb_canvas, bg=C["bg"])
        sb_window = sb_canvas.create_window((0, 0), window=sb, anchor="nw")

        def _sb_configure(event=None):
            sb_canvas.configure(scrollregion=sb_canvas.bbox("all"))
            # Keep the inner frame width synced to the canvas width so
            # long labels don't wrap oddly or get clipped horizontally.
            sb_canvas.itemconfig(sb_window, width=sb_canvas.winfo_width())
        sb.bind("<Configure>",        _sb_configure)
        sb_canvas.bind("<Configure>", _sb_configure)

        # Mousewheel only scrolls when the cursor is over the sidebar so
        # the log widget keeps its own scroll behavior.
        def _sb_wheel(event):
            sb_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        def _sb_wheel_bind(e):   sb_canvas.bind_all("<MouseWheel>", _sb_wheel)
        def _sb_wheel_unbind(e): sb_canvas.unbind_all("<MouseWheel>")
        sb_canvas.bind("<Enter>", _sb_wheel_bind)
        sb_canvas.bind("<Leave>", _sb_wheel_unbind)
        sb.bind("<Enter>",        _sb_wheel_bind)
        sb.bind("<Leave>",        _sb_wheel_unbind)

        # ACCOUNT
        self._section(sb, "ACCOUNT")
        self._auth_lbl = self._label(sb, "○ Checking…", C["dim"])
        self._auth_lbl.pack(anchor="w", padx=4, pady=(0, 4))
        self._button(sb, "🔑  Login with Browser", self._do_login)

        self._divider(sb)

        # STRATEGY
        self._section(sb, "STRATEGY")
        self._strat_var = tk.StringVar(value=self._cfg.get("strategy", "balanced"))
        self._strat_desc_lbl = tk.Label(
            sb, text="", bg=C["bg"], fg=C["dim"],
            font=("Segoe UI", 7), anchor="w", wraplength=190, justify="left")
        self._strat_desc_lbl.pack(fill="x", pady=(0, 4))

        strat_frame = tk.Frame(sb, bg=C["bg"])
        strat_frame.pack(fill="x")
        self._strat_radios = {}
        for key, info in STRATEGY_LABELS.items():
            rb = tk.Radiobutton(
                strat_frame,
                text=info["label"],
                variable=self._strat_var,
                value=key,
                command=self._on_strategy_change,
                bg=C["bg"], fg=C["text"],
                selectcolor=C["card"],
                activebackground=C["bg"],
                activeforeground=C["gold"],
                font=("Segoe UI", 9),
                anchor="w", cursor="hand2",
            )
            rb.pack(fill="x", pady=1)
            self._strat_radios[key] = rb
        self._on_strategy_change()   # set initial description

        self._divider(sb)

        # OPTIMIZER
        self._section(sb, "OPTIMIZER")
        self._opt_lbl = self._label(sb, "○ Stopped", C["dim"])
        self._opt_lbl.pack(anchor="w", padx=4, pady=(0, 4))
        self._opt_start_btn = self._button(
            sb, "▶  Start Optimizer", self._start_opt, state="disabled")
        self._opt_stop_btn = self._button(
            sb, "■  Stop Optimizer", self._stop_opt,
            fg=C["dim"], state="disabled")

        self._divider(sb)

        # AUTO BATTLE
        self._section(sb, "AUTO BATTLE")
        self._battle_lbl = self._label(sb, "○ Stopped", C["dim"])
        self._battle_lbl.pack(anchor="w", padx=4, pady=(0, 4))

        # Mode radio — attack vs spy
        self._battle_mode = tk.StringVar(value=self._cfg.get("battle_mode", "attack"))
        mode_frame = tk.Frame(sb, bg=C["bg"])
        mode_frame.pack(fill="x", pady=(0, 2))
        for val, lbl in (("attack", "⚔ Attack"), ("spy", "🔍 Spy")):
            tk.Radiobutton(
                mode_frame, text=lbl, variable=self._battle_mode, value=val,
                command=self._on_battle_mode_change,
                bg=C["bg"], fg=C["text"], selectcolor=C["card"],
                activebackground=C["bg"], activeforeground=C["gold"],
                font=("Segoe UI", 9), anchor="w", cursor="hand2",
            ).pack(side="left", padx=(0, 8))

        # Turns per hit (1-10 spinbox)
        tf = tk.Frame(sb, bg=C["bg"])
        tf.pack(fill="x", pady=1)
        tk.Label(tf, text="Turns:", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 8), width=7, anchor="w").pack(side="left")
        self._battle_turns = tk.IntVar(value=int(self._cfg.get("battle_turns", 5)))
        tk.Spinbox(tf, from_=1, to=10, textvariable=self._battle_turns,
                   width=4, font=("Segoe UI", 9),
                   bg=C["card"], fg=C["text"],
                   buttonbackground=C["btn"], relief="flat", bd=0).pack(side="left")

        # Min ATK margin entry
        mf = tk.Frame(sb, bg=C["bg"])
        mf.pack(fill="x", pady=1)
        tk.Label(mf, text="Margin:", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 8), width=7, anchor="w").pack(side="left")
        self._battle_margin = tk.StringVar(value=str(self._cfg.get("battle_margin", 1.2)))
        tk.Entry(mf, textvariable=self._battle_margin, width=5,
                 font=("Segoe UI", 9),
                 bg=C["card"], fg=C["text"], relief="flat", bd=0,
                 insertbackground=C["text"]).pack(side="left")

        # Skip checkboxes
        self._battle_skip_friends = tk.BooleanVar(value=self._cfg.get("battle_skip_friends", True))
        self._battle_skip_clan    = tk.BooleanVar(value=self._cfg.get("battle_skip_clan",    True))
        self._battle_skip_bots    = tk.BooleanVar(value=self._cfg.get("battle_skip_bots",    False))
        for var, txt in (
            (self._battle_skip_friends, "Skip friends"),
            (self._battle_skip_clan,    "Skip clanmates"),
            (self._battle_skip_bots,    "Skip bots"),
        ):
            tk.Checkbutton(
                sb, text=txt, variable=var,
                command=self._save_battle_cfg,
                bg=C["bg"], fg=C["text"], selectcolor=C["card"],
                activebackground=C["bg"], activeforeground=C["gold"],
                font=("Segoe UI", 8), anchor="w", cursor="hand2",
            ).pack(fill="x", padx=2, pady=0)

        self._battle_start_btn = self._button(
            sb, "▶  Start Battle", self._start_battle, state="disabled")
        self._battle_stop_btn = self._button(
            sb, "■  Stop Battle", self._stop_battle,
            fg=C["dim"], state="disabled")

        self._divider(sb)

        # DASHBOARD
        self._section(sb, "VIEW")
        self._button(sb, "📊  Growth Chart",      self._open_chart)
        self._button(sb, "🌐  Public Dashboard",  self._open_dash)

        self._divider(sb)

        # SETTINGS
        self._section(sb, "SETTINGS")
        self._race_var  = self._dropdown(sb, "Race",  RACES,
                                         self._cfg.get("race",  "Human"))
        self._class_var = self._dropdown(sb, "Class", CLASSES,
                                         self._cfg.get("class", "Fighter"))
        self._button(sb, "💾  Save Settings", self._save_settings,
                     fg=C["dim"])

        # ── Log panel
        log_outer = tk.Frame(body, bg=C["card"],
                             highlightbackground=C["border"],
                             highlightthickness=1)
        log_outer.pack(side="left", fill="both", expand=True)

        log_hdr = tk.Frame(log_outer, bg=C["card"])
        log_hdr.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(log_hdr, text="📋  Activity Log",
                 bg=C["card"], fg=C["dim"],
                 font=("Segoe UI", 8, "bold")).pack(side="left")
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
            font=("Consolas", 9),
            relief="flat", bd=0,
            padx=12, pady=8,
            selectbackground="#2a2a2a",
        )
        self._log.pack(fill="both", expand=True, padx=6, pady=6)

        # Log colour tags
        for tag, color in [
            ("gold",   C["gold"]),   ("green", C["green"]),
            ("red",    C["red"]),    ("dim",   C["dim"]),
            ("orange", C["orange"]), ("blue",  C["blue"]),
            ("battle", "#67e8f9"),   # cyan — auto-battle activity
        ]:
            self._log.tag_config(tag, foreground=color)

        # Status bar
        self._statusbar = tk.Label(
            self.root, text="Ready.",
            bg=C["border"], fg=C["dim"],
            font=("Segoe UI", 8), anchor="w", padx=10, pady=3)
        self._statusbar.pack(fill="x", side="bottom")

        # Welcome message
        self._log_write(
            "Welcome to DarkThrone Suite.\n"
            "→ Click 'Login with Browser' to authenticate your account.\n"
            "→ Then click 'Start Optimizer' to begin.\n\n",
            "dim")

    # ── Sidebar helpers ───────────────────────────────────────────────────────
    def _section(self, parent, title):
        tk.Label(parent, text=title,
                 bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 7, "bold"),
                 anchor="w").pack(fill="x", pady=(6, 2))

    def _divider(self, parent):
        tk.Frame(parent, bg=C["border"], height=1).pack(
            fill="x", pady=8)

    def _label(self, parent, text, color):
        return tk.Label(parent, text=text,
                        bg=C["bg"], fg=color,
                        font=("Segoe UI", 9), anchor="w")

    def _button(self, parent, text, cmd,
                fg=None, state="normal", store=None):
        btn = tk.Button(
            parent, text=text, command=cmd,
            bg=C["btn"], fg=fg or C["text"],
            activebackground=C["btn_act"],
            activeforeground=C["gold"],
            relief="flat", bd=0,
            pady=6, padx=8,
            font=("Segoe UI", 9),
            cursor="hand2",
            state=state,
            anchor="w",
        )
        btn.pack(fill="x", pady=2)
        if store:
            setattr(self, store, btn)
        return btn

    def _dropdown(self, parent, label, options, default):
        f = tk.Frame(parent, bg=C["bg"])
        f.pack(fill="x", pady=2)
        tk.Label(f, text=label + ":", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 8), width=6, anchor="w").pack(side="left")
        var = tk.StringVar(value=default)
        cb = ttk.Combobox(f, textvariable=var, values=options,
                          state="readonly", width=11,
                          font=("Segoe UI", 9))
        cb.pack(side="left")
        return var

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
            messagebox.showwarning(
                "Auto-Battle running",
                "Stop auto-battle before starting the optimizer — "
                "they share the browser session.")
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

        try:
            while not self._opt_stop.is_set():
                try:
                    optimizer.run_tick()
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

                now  = datetime.datetime.now()
                wait = (30 * 60) - (now.minute % 30) * 60 - now.second
                nxt  = (now + datetime.timedelta(seconds=wait)).strftime("%H:%M")
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
        if self._opt_thread and self._opt_thread.is_alive():
            messagebox.showwarning(
                "Optimizer running",
                "Stop the optimizer before starting auto-battle — "
                "they share the browser session.")
            return
        if not os.path.isfile(AUTH_FILE):
            messagebox.showwarning("Not logged in", "Please log in first.")
            return

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
            f"turns/hit={self._battle_turns.get()} "
            f"margin={self._battle_margin.get()}\n", "battle")

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
        cfg = {
            "mode":          self._battle_mode.get(),
            "margin":        max(1.0, margin),
            "turns_per_hit": int(self._battle_turns.get()),
            "skip_friends":  bool(self._battle_skip_friends.get()),
            "skip_clan":     bool(self._battle_skip_clan.get()),
            "skip_bots":     bool(self._battle_skip_bots.get()),
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


if __name__ == "__main__":
    _check_deps()

    root = tk.Tk()

    # Style ttk combobox to match dark theme
    style = ttk.Style(root)
    style.theme_use("clam")
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

    # Window icon (if icon file present)
    ico = os.path.join(os.path.dirname(__file__), "icon.ico")
    if os.path.isfile(ico):
        try: root.iconbitmap(ico)
        except Exception: pass

    app = DarkThroneApp(root)  # noqa
    root.mainloop()
