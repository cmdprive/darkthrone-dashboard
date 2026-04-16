# Release Checklist

Concise steps for cutting a new public release of the DarkThrone Suite.

## One-time setup (first release only)

1. **Install Inno Setup 6** — https://jrsoftware.org/isinfo.php (free, next-next-finish)
2. **Create GitHub repo** `cmdprive/darkthrone-suite` (public), enable Releases
3. **Install `gh` CLI** (optional but recommended) — https://cli.github.com/
   then `gh auth login`

Everything else (PyInstaller, Python deps) is already wired up in `src/build.bat`.

## Per-release workflow

### 1. Bump the version

Edit `src/_version.py`:
```python
__version__     = "1.1.0"       # semver: MAJOR.MINOR.PATCH
__release_tag__ = "v1.1.0"      # must match the GitHub tag exactly
```

Edit `src/installer/darkthrone_suite.iss`:
```ini
#define MyAppVersion   "1.1.0"
```

Both files must reference the same version string, or the auto-updater will keep prompting users to install the version they already have.

### 2. Build the installer

```bat
cd src
build.bat
```

Produces `release/installers/DarkThroneSuite-Setup-v1.1.0.exe` (~170 MB). The build output folder (`release/DarkThrone Suite/`) is also updated for dev / portable use but **only the Setup.exe goes to users**.

### 3. Smoke-test the installer locally

```bat
release\installers\DarkThroneSuite-Setup-v1.1.0.exe
```

Verify:
- Wizard completes without errors
- App launches from Start Menu shortcut
- Title bar shows `DarkThrone Suite v1.1.0`
- First-run Chromium splash appears (if `%LOCALAPPDATA%\ms-playwright\` is empty)
- Data writes to `%LOCALAPPDATA%\DarkThroneSuite\`, not the install dir

### 4. Publish to GitHub Releases

With `gh` CLI:
```bash
cd /c/Users/Gebruiker/darkthrone
gh release create v1.1.0 \
    release/installers/DarkThroneSuite-Setup-v1.1.0.exe \
    --title "v1.1.0" \
    --notes "- Feature X added
- Bug Y fixed
- ..."
```

Or via the web UI:
- Go to https://github.com/cmdprive/darkthrone-suite/releases/new
- Tag: `v1.1.0` (match the version exactly, include the `v`)
- Title: `v1.1.0`
- Attach the `.exe` from `release/installers/`
- Paste release notes
- Publish

### 5. Verify auto-update triggers

Open an older installed Suite — within ~10 seconds of startup the gold `Update available: v1.1.0` banner should appear at the top. Click it → installer downloads + launches → older version uninstalls and new version installs → new version relaunches automatically.

## How it works

**Version detection** — on every launch, the Suite GETs
`https://api.github.com/repos/cmdprive/darkthrone-suite/releases/latest`
and compares `tag_name` to the bundled `__version__`. If newer, shows a
dismissable banner in the GUI.

**Download + install** — banner click downloads
`DarkThroneSuite-Setup-v{new}.exe` to `%TEMP%`, then launches it with
`/SILENT /SUPPRESSMSGBOXES /NORESTART`. Inno Setup handles the in-place
upgrade natively (uninstalls old, installs new, re-launches).

**Data preservation** — runtime data lives at `%LOCALAPPDATA%\DarkThroneSuite\`
which the installer NEVER touches. Upgrades keep the user's login
session, settings, intel history, growth CSV, etc.

## Rollback

If an update breaks something:
1. User manually uninstalls via Control Panel > Programs
2. Downloads the previous version's `Setup.exe` from the Releases page
3. Reinstalls — their `%LOCALAPPDATA%` data is intact

You (the maintainer) can also **delete a bad release** on GitHub to stop
more users from auto-updating into it. Existing users who already got
the bad version have to manually downgrade.

## Troubleshooting

**Inno Setup not found** — install it from the link above. The build
script checks both `Program Files` and `Program Files (x86)`.

**PyInstaller can't find `_version` or `updater`** — make sure
`build.bat` has the `--add-data "_version.py;."` and
`--add-data "installer\updater.py;."` flags, plus the matching
`--hidden-import` entries.

**Update banner never appears** — the GitHub Releases API is
rate-limited to 60 requests/hour per IP (unauthenticated). If a user
has been relaunching a lot, they may be temporarily rate-limited.
They'll get a check on next launch.

**SmartScreen "Windows protected your PC" warning** — expected for
unsigned binaries. Cost of a code-signing cert is ~$100-300/year.
Current mitigation: tell users to click "More info" → "Run anyway".
