"""
GitHub Releases-backed auto-updater for the DarkThrone Suite.

Flow on every launch:
  1. `check_for_update(on_available)` spawns a background daemon thread.
  2. Thread hits the GitHub Releases API for the latest release.
  3. If the tag is newer than the bundled __version__, call on_available()
     with (new_version, download_url, release_notes). Caller shows a
     banner in the GUI.
  4. When the user clicks "Update Now", `download_and_launch(url, progress)`
     streams the installer to %TEMP%, launches it silently, exits the
     current process. Inno Setup handles the actual in-place upgrade.

All network calls are wrapped in try/except — a missing / failed update
check NEVER blocks the app. Silent on:
  - no internet
  - rate-limit hit (60 req/hr unauthenticated)
  - release has no matching asset
  - version parsing fails
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from urllib.error import URLError

try:
    # When imported from the installed app, _version lives at src/_version.py
    # (bundled inside _internal/ by PyInstaller).
    from _version import (__version__, __update_api__, __asset_prefix__)
except ImportError:
    # Fallback — dev mode path issue or standalone import for testing
    __version__     = "0.0.0"
    __update_api__  = "https://api.github.com/repos/cmdprive/darkthrone-suite/releases/latest"
    __asset_prefix__ = "DarkThroneSuite-Setup-"


# ───────────────────────────────────────────────────────────────────────
# Version comparison
# ───────────────────────────────────────────────────────────────────────

def _parse_version(v: str):
    """'1.2.3' → (1, 2, 3). Strips leading 'v' and any suffix like '-beta'."""
    v = str(v or "").lstrip("vV").split("-")[0].split("+")[0]
    out = []
    for part in v.split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    # Pad to length 3 so comparisons work consistently
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def _is_newer(remote: str, local: str) -> bool:
    """True iff remote version > local version."""
    try:
        return _parse_version(remote) > _parse_version(local)
    except Exception:
        return False


# ───────────────────────────────────────────────────────────────────────
# Update check (async)
# ───────────────────────────────────────────────────────────────────────

def check_for_update(on_available, on_error=None):
    """Fire-and-forget background update check.

    on_available(version, download_url, notes) — called on the UI thread
      via tkinter's `after()` if newer version is found. Caller is
      responsible for actually showing a banner.
    on_error(exception) — optional; called if the check itself failed.
      Silent by default (update checks must never annoy users).
    """
    def _worker():
        try:
            req = urllib.request.Request(
                __update_api__,
                headers={"User-Agent": f"DarkThroneSuite/{__version__}"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                release = json.loads(r.read().decode("utf-8"))

            tag = release.get("tag_name", "")
            if not _is_newer(tag, __version__):
                return   # already up to date

            # Find the first .exe asset matching our naming convention
            asset = None
            for a in release.get("assets", []):
                name = a.get("name", "")
                if name.startswith(__asset_prefix__) and name.endswith(".exe"):
                    asset = a
                    break
            if not asset:
                return   # release has no matching installer — skip

            on_available(
                tag.lstrip("vV"),
                asset["browser_download_url"],
                release.get("body", "") or "",
            )
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            if on_error:
                try: on_error(e)
                except Exception: pass
        except Exception as e:
            # Never let an update check crash the app
            if on_error:
                try: on_error(e)
                except Exception: pass

    t = threading.Thread(target=_worker, daemon=True, name="UpdateCheck")
    t.start()
    return t


# ───────────────────────────────────────────────────────────────────────
# Download + launch installer
# ───────────────────────────────────────────────────────────────────────

def download_and_launch(url: str, progress_cb=None, on_error=None):
    """Download Setup.exe to %TEMP% and launch it silently.

    progress_cb(percent: float, downloaded: int, total: int) — optional
      called periodically during download so the GUI can show a progress
      bar. Runs on the worker thread.
    on_error(exception) — if anything fails, caller is notified. The
      current app stays running (user can retry or dismiss the banner).

    On success: launches `<installer>.exe /SILENT` and calls sys.exit(0).
    The installer is Inno Setup, which handles the in-place upgrade
    natively — uninstalls the old version, installs the new one, and
    offers to re-launch.
    """
    def _worker():
        try:
            # Deterministic name so re-downloads replace the previous file
            filename = os.path.basename(url.split("?")[0]) or "DarkThroneSuite-Setup.exe"
            dest = os.path.join(tempfile.gettempdir(), filename)

            req = urllib.request.Request(
                url, headers={"User-Agent": f"DarkThroneSuite/{__version__}"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                total = int(r.headers.get("Content-Length", 0))
                downloaded = 0
                chunk = 64 * 1024
                with open(dest, "wb") as f:
                    while True:
                        data = r.read(chunk)
                        if not data:
                            break
                        f.write(data)
                        downloaded += len(data)
                        if progress_cb and total > 0:
                            try:
                                progress_cb(downloaded / total * 100.0,
                                            downloaded, total)
                            except Exception:
                                pass

            # Launch installer silently — Inno Setup understands /SILENT
            # (shows progress dialog, no wizard) and /VERYSILENT (no UI).
            # We use /SILENT so the user sees SOMETHING is happening.
            # After install, Inno Setup's postinstall task re-launches
            # the app automatically (see darkthrone_suite.iss [Run]).
            try:
                subprocess.Popen(
                    [dest, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                    creationflags=0x00000010 if sys.platform == "win32" else 0,
                    # DETACHED_PROCESS so the installer survives our exit
                )
            except Exception as e:
                if on_error:
                    try: on_error(e)
                    except Exception: pass
                return

            # Give the installer a moment to lock files, then exit so it
            # can overwrite us.
            import time as _time
            _time.sleep(1.0)
            os._exit(0)
        except Exception as e:
            if on_error:
                try: on_error(e)
                except Exception: pass

    t = threading.Thread(target=_worker, daemon=True, name="UpdateDownload")
    t.start()
    return t
