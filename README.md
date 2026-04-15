# DarkThrone Suite

Automated optimizer + auto-battle bot + dashboard for the browser game DarkThrone.

## How to run

Double-click one of:

| File                                   | What it does |
| -------------------------------------- | ------------ |
| `release\DarkThrone Suite.bat`         | Launches the bundled Suite GUI. **This is the normal way to start.** |
| `release\install_browser.bat`          | Run ONCE on first use. Downloads Chromium (~150 MB) for Playwright. |
| `Start DarkThrone.vbs`                 | Silent launcher that runs the source `.py` directly (no console). Useful for development. |
| `src\debug_launch.bat`                 | Runs the source `.py` with a console window for error diagnosis. |

The Suite GUI exposes three independent loops — **Optimizer** (30-minute ticks that train troops, buy gear, upgrade buildings), **Auto Battle** (farms XP + gold via attack/spy reports, learns from intel), and **Dashboard** (live HTML view of every scraped player). All three are gated behind a Start button; nothing runs until you click.

## Folder layout

```
<darkthrone>/
├── README.md               ← you are here
├── Start DarkThrone.vbs    ← silent launcher (runs src/)
├── .gitignore
│
├── data/                   ← RUNTIME — the Suite writes here
│   ├── auth.json                          (login session cookies)
│   ├── user_config.json                   (GUI settings)
│   ├── index.html                         (dashboard output)
│   ├── optimizer_chart.html               (growth chart)
│   ├── darkthrone_server_data.csv         (attack list scrape history)
│   ├── private_latest.json                (live stat snapshot)
│   ├── private_rankings_snapshot.json     (rank cache)
│   ├── private_optimizer_state.json       (tick counter)
│   ├── private_optimizer_growth.json      (time-series for chart)
│   ├── private_optimizer_log.csv          (optimizer audit trail)
│   ├── private_battle_log.csv             (auto-battle audit trail)
│   ├── private_intel.csv                  (harvested from attack/spy reports)
│   ├── private_player_estimates.csv       (estimator output per player)
│   ├── private_player_profiles.csv        (profile scrape history)
│   ├── private_rankings.csv               (rank history)
│   └── private_* (more CSVs)              (various historical data)
│
├── release/                ← BUILT SUITE — ship this to end users
│   ├── DarkThrone Suite.bat               (launcher shortcut)
│   ├── install_browser.bat                (first-run Playwright setup)
│   └── DarkThrone Suite/
│       ├── DarkThrone Suite.exe           (~3.8 MB)
│       ├── index.html                     (bundled dashboard template)
│       └── _internal/                     (~170 MB of Python+playwright+tkinter)
│
└── src/                    ← SOURCE CODE — edit here
    ├── optimizer.py                       (the whole bot: scrape + estimate + act)
    ├── index.html                         (dashboard template — edit here, auto-copied to data/ on first run)
    ├── build.bat                          (rebuilds release/ from src/)
    ├── debug_launch.bat                   (runs darkthrone_app.py with a visible console)
    ├── installer/
    │   ├── darkthrone_app.py              (tkinter GUI launcher)
    │   ├── install.bat
    │   ├── start.bat
    │   └── requirements.txt
    └── tests/
        ├── test_battle_pick.py            (unit test: target filter + sort)
        └── test_calibration.py            (diagnostic: rank model fit)
```

## Rebuilding the Suite

After editing anything under `src/`:

```bat
cd src
build.bat
```

That recompiles `optimizer.py` + `installer/darkthrone_app.py` to bytecode, bundles everything via PyInstaller, and overwrites `release/DarkThrone Suite/`. You do NOT need to re-run `install_browser.bat` — Chromium stays at the user cache.

## Running the tests

```bat
python src\tests\test_battle_pick.py
python src\tests\test_calibration.py
```

Both are offline (no browser), both chdir into `data/` automatically, and both pick up the current `optimizer.py` via `sys.path`.

## Git tracking

Only `src/` files are tracked in version control. `data/`, `release/`, build artifacts, `.lock` files, and Claude Code session state (`.claude/`) are all gitignored.
