@echo off
:: Hides all non-essential files in the darkthrone folder.
:: Run once after first setup. Files are NOT deleted — just hidden.
:: To unhide everything: attrib -h -h /s /d  (or Explorer > View > Hidden items)

cd /d "%~dp0.."

:: ── Dev / debug tools ─────────────────────────────────────────────────────────
for %%f in (
    profile_dumper.py dumper.py diagnose.py setup_estimates_repo.py
) do if exist "%%f" attrib +h "%%f"

:: ── HTML page dumps ───────────────────────────────────────────────────────────
for %%f in (dump_*.html dump_profile_*.html) do attrib +h "%%f" 2>nul

:: ── Debug / notes / old scheduler ────────────────────────────────────────────
for %%f in (
    debug_*.txt run_log.txt
    "Stopzetten schedule.txt"
    schedule_tasks.bat run_scheduled.bat
    gitignore build.bat
) do if exist "%%f" attrib +h "%%f"

:: ── Private / sensitive data ──────────────────────────────────────────────────
:: auth.json and user_config.json are NOT hidden — the app needs to read/write them
for %%f in (pyarmor.bug.log) do (
    if exist "%%f" attrib +h "%%f"
)
:: Hide static private data — NOT files the optimizer writes to every tick
for %%f in (
    private_army_snapshot.json private_rankings_snapshot.json
    private_player_estimates.html private_dashboard.html
    private_player_profiles.csv private_player_estimates.csv
    private_rankings.csv private_fort_stats.csv private_fort_attacks.csv
) do if exist "%%f" attrib +h "%%f"

:: ── Build artefacts ────────────────────────────────────────────────────────────
if exist "build_protected" attrib +h /s /d "build_protected" >nul 2>&1
if exist "release"         attrib +h /s /d "release"         >nul 2>&1

:: ── Python / git cache ────────────────────────────────────────────────────────
if exist "__pycache__" attrib +h /s /d "__pycache__" >nul 2>&1
if exist ".git"        attrib +h /s /d ".git"        >nul 2>&1

echo Done — folder is clean.
timeout /t 2 >nul
