import csv
import datetime
import os
import json
import re
import sys
import time
import subprocess
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


def _unhide(path):
    """Remove hidden/read-only attribute so the file can be written (Windows)."""
    if sys.platform == "win32" and os.path.isfile(path):
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)
        except Exception:
            pass

# --- CONFIGURATION ---
AUTH_FILE = "auth.json"
DATA_FILE = "darkthrone_server_data.csv"
DASHBOARD_FILE = "index.html"
BASE_ATTACK_URL = "https://darkthronegame.com/game/attack"

# Query parameters that match the target URL exactly
ATTACK_PARAMS = "sort=level&dir=desc&range=all&bots=all"

# Set to True on first run to print raw HTML of each row for debugging
DEBUG_SELECTORS = False


def publish_dashboard():
    """Commits the updated dashboard and pushes it to GitHub, forcing an update every time."""
    print("🚀 Publishing raw data to GitHub...")
    try:
        # Stage the file
        subprocess.run(["git", "add", DASHBOARD_FILE], check=True)
        
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        
        # Use --allow-empty to ensure a commit is created even with no file changes
        subprocess.run([
            "git", "commit", "--allow-empty", "-m", f"Raw data upload {timestamp}"
        ], check=True)
        
        # Push to GitHub
        subprocess.run(["git", "push"], check=True)
        print(f"✅ Data published! View at: https://cmdprive.github.io/darkthrone-dashboard")
        
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Git publish failed: {e}")


def update_dashboard():
    """Reads the CSV and injects the data into your dashboard.html file."""
    if not os.path.exists(DATA_FILE) or not os.path.exists(DASHBOARD_FILE):
        print("⚠️ Missing CSV or HTML file. Cannot sync dashboard.")
        return

    history = {}
    print("🔄 Syncing data to Dashboard...")

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("Player")
            if name:
                if name not in history:
                    history[name] = []
                g = re.sub(r"\D", "", row.get("Gold", "0"))
                h = re.sub(r"\D", "", row.get("FortHP", "0"))
                hmax = re.sub(r"\D", "", row.get("FortMaxHP", "0"))
                history[name].append({
                    "timestamp":  row["Timestamp"],
                    "player_id":  row.get("PlayerID", ""),
                    "level":      int(re.sub(r"\D", "", row.get("Level", "0")) or 0),
                    "race":       row.get("Race", ""),
                    "gold":       int(g) if g else 0,
                    "hp":         int(h) if h else 0,
                    "hp_max":     int(hmax) if hmax else 0,
                    "fort_pct":   int(re.sub(r"\D", "", row.get("FortPct", "0")) or 0),
                    "turns":      int(re.sub(r"\D", "", row.get("Turns", "0")) or 0),
                    "in_range":   row.get("InRange", "0") == "1",
                    "is_bot":     row.get("IsBot", "0") == "1",
                    "is_clan":    row.get("IsClanMember", "0") == "1",
                    "is_friend":  row.get("IsFriend", "0") == "1",
                    "is_hitlist": row.get("IsHitlist", "0") == "1",
                })

    with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    # ── 1. Inject player data ────────────────────────────────────────────────────
    # FIX: Use a more reliable regex that matches up to the first ';' after the
    # opening brace, without relying on non-greedy DOTALL across nested braces.
    json_str = json.dumps(history, ensure_ascii=False)
    new_html = re.sub(
        r"const rawData\s*=\s*\{[^;]*\};",
        lambda _: f"const rawData = {json_str};",
        html,
    )

    # ── 1b. Inject ranking data from private_rankings_snapshot.json ──────────────
    # Written by scraper_private.py after each leaderboard scrape.
    # rank_map: { "PlayerName": { overall, off_rank, def_rank, spy_off_rank,
    #                              spy_def_rank, lv_rank, level, clan } }
    rank_map = {}
    snap_path = "private_rankings_snapshot.json"
    if os.path.isfile(snap_path):
        try:
            with open(snap_path, "r", encoding="utf-8") as f:
                snap = json.load(f)
            rank_map = snap.get("rank_map", {})
            print(f"   📊 Loaded {len(rank_map)} player rankings from snapshot.")
        except Exception as e:
            print(f"   ⚠️  Could not load rankings snapshot: {e}")

    rank_json = json.dumps(rank_map, ensure_ascii=False)
    new_html = re.sub(
        r"const rankData\s*=\s*\{[^;]*\};",
        lambda _: f"const rankData = {rank_json};",
        new_html,
    )

    # ── 1c. Inject estimate data from private_player_estimates.csv ───────────────
    est_map = {}
    est_path = "private_player_estimates.csv"
    if os.path.isfile(est_path):
        try:
            with open(est_path, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    name = row.get("Player", "").strip()
                    if name:
                        # Keep latest row per player (CSV is appended, last wins)
                        def _ei(v):
                            try: return int(v)
                            except (ValueError, TypeError): return 0
                        est_map[name] = {
                            "atk":     _ei(row.get("EstATK")),
                            "def":     _ei(row.get("EstDEF")),
                            "spy_off": _ei(row.get("EstSpyOff")),
                            "spy_def": _ei(row.get("EstSpyDef")),
                            "conf":    row.get("Confidence", ""),
                        }
            print(f"   📊 Loaded estimates for {len(est_map)} players.")
        except Exception as e:
            print(f"   ⚠️  Could not load estimates: {e}")

    est_json = json.dumps(est_map, ensure_ascii=False)
    new_html = re.sub(
        r"const estimateData\s*=\s*\{[^;]*\};",
        lambda _: f"const estimateData = {est_json};",
        new_html,
    )

    # ── 2. Inject / update last-scraped timestamp ────────────────────────────────
    ts_now   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ts_tag   = f'<meta name="scrape-timestamp" content="{ts_now}">'
    if '<meta name="scrape-timestamp"' in new_html:
        new_html = re.sub(
            r'<meta name="scrape-timestamp"[^>]*>',
            ts_tag,
            new_html,
        )
    else:
        new_html = new_html.replace("</head>", f"  {ts_tag}\n</head>", 1)

    # ── 3. Inject auto-refresh: reload page 90 s after each game tick ────────────
    # Game ticks at :00 and :30 every hour.  We refresh 90 s after each tick
    # (tick + ~60 s scrape + 30 s GitHub Pages propagation) so the user always
    # sees fresh data automatically without touching the browser.
    # The script calculates exact milliseconds to the next :01:30 or :31:30 mark.
    REFRESH_SCRIPT_MARKER = "/* auto-refresh-injected */"
    refresh_script = f"""\
<script id="auto-refresh">{REFRESH_SCRIPT_MARKER}
  (function(){{
    // Reload 90 seconds after the next game tick (:00 or :30 of each hour).
    // This matches when the scraper publishes fresh data to GitHub Pages.
    function msToNextRefresh() {{
      var now   = new Date();
      var sec   = now.getMinutes() % 30 * 60 + now.getSeconds();
      var delay = (30 * 60 - sec + 90) * 1000; // ms until next tick + 90 s
      return delay;
    }}
    function scheduleRefresh() {{
      var ms = msToNextRefresh();
      var at = new Date(Date.now() + ms);
      console.log('[auto-refresh] Next reload at ' + at.toLocaleTimeString() +
                  ' (in ' + Math.round(ms/1000) + 's)');
      var bar = document.getElementById('refresh-bar');
      if (bar) bar.textContent = '🔄 Auto-refresh in ' +
                                  Math.round(ms/60000) + ' min  |  Last scraped: {ts_now}';
      setTimeout(function(){{ location.reload(); }}, ms);
    }}
    if (document.readyState === 'loading') {{
      document.addEventListener('DOMContentLoaded', scheduleRefresh);
    }} else {{
      scheduleRefresh();
    }}
  }})();
</script>"""

    # ── 4. Ensure the refresh status bar element exists in the HTML ─────────────
    REFRESH_BAR_HTML = (
        '<div id="refresh-bar" style="'
        'background:#1a1f2e;color:#6c7086;font-size:11px;'
        'text-align:center;padding:4px 0;border-bottom:1px solid #2a3142;'
        'letter-spacing:.3px;">'
        f'Last scraped: {ts_now}'
        '</div>'
    )
    if 'id="refresh-bar"' not in new_html:
        # Insert right after the first </header> tag
        new_html = new_html.replace("</header>", f"</header>\n{REFRESH_BAR_HTML}", 1)
    else:
        new_html = re.sub(
            r'<div id="refresh-bar"[^>]*>.*?</div>',
            REFRESH_BAR_HTML,
            new_html,
            flags=re.DOTALL,
        )

    if REFRESH_SCRIPT_MARKER in new_html:
        # Replace the entire existing auto-refresh script block
        new_html = re.sub(
            r'<script id="auto-refresh">.*?</script>',
            refresh_script,
            new_html,
            flags=re.DOTALL,
        )
    else:
        # First time — insert just before </body>
        new_html = new_html.replace("</body>", f"{refresh_script}\n</body>", 1)

    _unhide(DASHBOARD_FILE)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(new_html)
    print(f"✅ Dashboard Synced! {len(history)} unique players tracked.")


def ensure_csv_header():
    """Write the CSV header if the file does not exist yet."""
    if not os.path.isfile(DATA_FILE):
        with open(DATA_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                "Timestamp", "PlayerID", "Player", "Level", "Race",
                "Gold", "FortHP", "FortMaxHP", "FortPct",
                "Turns", "InRange", "IsBot", "IsClanMember", "IsFriend", "IsHitlist"
            ])


def load_existing_keys():
    """Return a set of (timestamp_date, player) pairs already in the CSV to
    avoid duplicate rows when the scraper is re-run on the same day."""
    keys = set()
    if not os.path.isfile(DATA_FILE):
        return keys
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_date = row.get("Timestamp", "")[:10]  # YYYY-MM-DD
            player = row.get("Player", "")
            if ts_date and player:
                keys.add((ts_date, player))
    return keys


def _do_scrape(page, max_pages: int = 200):
    """Core scrape loop — accepts an already-authenticated page.
    Navigates to the attack list and scrapes up to max_pages pages.
    """
    ensure_csv_header()
    existing_keys = load_existing_keys()
    today = datetime.date.today().isoformat()

    print(f"  🌐 Scraping attack list (up to {max_pages} pages)...")
    page.goto(f"{BASE_ATTACK_URL}?{ATTACK_PARAMS}&page=1")

    last_page_fingerprint = ""

    for page_num in range(1, max_pages + 1):
        try:
            page.wait_for_selector("#battlelist-table tbody tr", timeout=15000)
        except PlaywrightTimeoutError:
            print(f"  ⚠️  Page {page_num}: table didn't load. Stopping.")
            break

        player_rows = page.query_selector_all("#battlelist-table tbody tr[data-name]")
        found_on_page = []
        page_names = []

        for row in player_rows:
            link_el  = row.query_selector("a.player-link")
            href     = link_el.get_attribute("href") if link_el else ""
            id_match = re.search(r"/player/(\d+)", href)
            player_id = id_match.group(1) if id_match else ""

            level    = row.get_attribute("data-level") or ""
            race     = row.get_attribute("data-race")  or ""
            gold     = row.get_attribute("data-gold")  or "0"
            fort_pct = row.get_attribute("data-fort")  or "0"

            name_span = row.query_selector("a.player-link span:not([class])")
            name = name_span.inner_text().replace("(YOU)", "").strip() if name_span else ""
            if not name:
                name = row.get_attribute("data-name") or ""
            if not name:
                continue

            page_names.append(name)

            fort_hp = fort_pct
            fort_max_hp = "0"
            fort_bar = row.query_selector(".fort-bar")
            if fort_bar:
                title    = fort_bar.get_attribute("title") or ""
                hp_match = re.match(r"(\d+)/(\d+)", title)
                if hp_match:
                    fort_hp     = hp_match.group(1)
                    fort_max_hp = hp_match.group(2)

            tds        = row.query_selector_all("td")
            turns_raw  = tds[5].inner_text().strip() if len(tds) > 5 else "0"
            turns_m    = re.search(r"\d+", turns_raw)
            turns      = turns_m.group(0) if turns_m else "0"

            in_range = "0"
            if len(tds) > 6:
                in_range = "0" if "out of range" in tds[6].inner_text().lower() else "1"

            is_bot     = "1" if "[bot]" in name.lower() else "0"
            is_clan    = "1" if row.query_selector(".clan-badge")    else "0"
            is_friend  = "1" if row.query_selector(".friend-badge")  else "0"
            is_hitlist = "1" if row.query_selector(".hitlist-badge") else "0"

            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            found_on_page.append([
                timestamp, player_id, name, level, race,
                gold, fort_hp, fort_max_hp, fort_pct,
                turns, in_range, is_bot, is_clan, is_friend, is_hitlist
            ])
            existing_keys.add((today, name))

        current_fingerprint = ",".join(page_names)
        if not page_names:
            print(f"  🏁 No players on page {page_num}. Done.")
            break
        if current_fingerprint == last_page_fingerprint:
            print(f"  🏁 Duplicate page {page_num}. Done.")
            break

        last_page_fingerprint = current_fingerprint
        if found_on_page:
            _unhide(DATA_FILE)
            with open(DATA_FILE, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(found_on_page)

        page.goto(f"{BASE_ATTACK_URL}?{ATTACK_PARAMS}&page={page_num + 1}")

    print(f"  ✅ Attack list scraped.")


def scrape_with_page(page, max_pages: int = 50):
    """Called from optimizer — reuses its authenticated browser page.
    Scrapes the attack list, rebuilds the dashboard, and publishes to GitHub.
    """
    _do_scrape(page, max_pages)
    update_dashboard()
    publish_dashboard()
    print("  📡 Dashboard published.")


def scrape(max_pages: int = 200):
    """Standalone scrape — opens its own headless browser."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=AUTH_FILE if os.path.exists(AUTH_FILE) else None
        )
        page = context.new_page()
        page.goto(f"{BASE_ATTACK_URL}?{ATTACK_PARAMS}&page=1")
        if "login" in page.url:
            print("🔑 Session expired — run the optimizer app to re-login.")
            browser.close()
            return
        _do_scrape(page, max_pages)
        context.storage_state(path=AUTH_FILE)
        browser.close()
    update_dashboard()
    publish_dashboard()
    print("✨ Full Scan and Sync Complete!")


def _next_tick_wait(lead_seconds: int = 60) -> float:
    """Return seconds to sleep so the next scrape starts `lead_seconds` after
    the next game tick (:00 or :30 of every hour).

    Example: if now is 12:22:05 the next tick is at 12:30:00.
    With lead_seconds=60 we wait until 12:31:00 → returns 528.95 s.
    """
    now = datetime.datetime.now()
    # How many seconds into the current 30-min slot?
    slot_seconds = (now.minute % 30) * 60 + now.second + now.microsecond / 1e6
    # Seconds until the next tick boundary (:00 or :30)
    until_tick = (30 * 60) - slot_seconds
    # Add the lead offset (so we scrape fresh data, not stale data from 1s ago)
    return until_tick + lead_seconds


if __name__ == "__main__":
    import sys

    # ── --once: single run for Task Scheduler ────────────────────────────────────
    if "--once" in sys.argv:
        print(f"\n🕐 Scheduled run at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        try:
            scrape()
        except Exception as e:
            print(f"❌ Scrape failed: {e}")
        sys.exit(0)

    # ── --fast: top-players-only quick pass (pages 1-10, ~200 players) ───────────
    # Runs every 5 minutes continuously. Use this for near-live top-player updates.
    # The game only ticks every 30 min — data deeper in the list rarely changes
    # between ticks, so fast-mode keeps the most-watched players current.
    if "--fast" in sys.argv:
        FAST_PAGES    = 10      # pages 1-10 ≈ top ~200 ranked players
        FAST_INTERVAL = 5 * 60  # re-scrape every 5 minutes
        print(f"⚡ Fast-mode: scraping top {FAST_PAGES} pages every {FAST_INTERVAL//60} minutes.")
        print(f"   Run with no flags for full 200-page scrape aligned to game ticks.")
        while True:
            print(f"\n🕐 Fast scrape at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            try:
                scrape(max_pages=FAST_PAGES)
            except Exception as e:
                print(f"❌ Scrape failed: {e}")
            next_run = datetime.datetime.now() + datetime.timedelta(seconds=FAST_INTERVAL)
            print(f"⏳ Next fast scrape at {next_run.strftime('%H:%M:%S')} "
                  f"(in {FAST_INTERVAL//60} min). Press Ctrl+C to stop.")
            time.sleep(FAST_INTERVAL)

    # ── Default: full 200-page scrape, aligned to game tick boundaries ────────────
    # DarkThrone ticks every 30 minutes at :00 and :30 of each hour.
    # We wait until 60 seconds AFTER each tick so the server has settled, then
    # scrape all pages. This means every published snapshot contains fresh data.
    print("🔄 Full-scrape mode — tick-aligned (starts 60 s after each :00/:30).")
    print("   Run with --fast for a quick top-players-only live mode.")
    while True:
        print(f"\n🕐 Starting full scrape at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        try:
            scrape()
        except Exception as e:
            print(f"❌ Scrape failed: {e}")

        wait = _next_tick_wait(lead_seconds=60)
        next_run = datetime.datetime.now() + datetime.timedelta(seconds=wait)
        print(f"⏳ Next tick-aligned scrape at {next_run.strftime('%H:%M:%S')} "
              f"(in {wait/60:.1f} min). Press Ctrl+C to stop.")
        time.sleep(wait)