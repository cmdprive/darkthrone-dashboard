"""
Version constants for the DarkThrone Suite.

Bumping a release:
  1. Update __version__ and __release_tag__ below (keep them in sync).
  2. Update AppVersion + OutputBaseFilename in src/installer/darkthrone_suite.iss.
  3. Run `cd src && build.bat` — produces the installer.
  4. `gh release create vX.Y.Z release/installers/DarkThroneSuite-Setup-vX.Y.Z.exe \
          --notes "..."` to publish.

See src/installer/RELEASE.md for the full checklist.
"""

__version__     = "1.1.0"
__release_tag__ = "v1.1.0"

# GitHub repo hosting public releases. Must be publicly accessible — the
# unauthenticated GitHub API is rate-limited to 60 requests/hour per IP,
# which is plenty for "once on startup per user".
__update_repo__ = "cmdprive/darkthrone-suite"
__update_api__  = f"https://api.github.com/repos/{__update_repo__}/releases/latest"

# Asset filename convention on GitHub Releases — the updater searches for
# the first asset whose name matches this pattern (prefix) in the latest
# release. `v{tag}` suffix is optional since GitHub strips it from tag_name.
__asset_prefix__ = "DarkThroneSuite-Setup-"
