import csv
import datetime
import os
import json
import re
import time
import subprocess
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

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
    """Commits the updated dashboard.html and pushes it to GitHub Pages."""
    print("🚀 Publishing dashboard to GitHub...")
    try:
        subprocess.run(["git", "add", DASHBOARD_FILE], check=True)
        # Check if there is actually anything new to commit
        result = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if result.returncode == 0:
            print("ℹ️  Dashboard unchanged, skipping commit.")
            return
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        subprocess.run(["git", "commit", "-m", f"Update dashboard {timestamp}"], check=True)
        subprocess.run(["git", "push"], check=True)
        print("✅ Dashboard published! Live at: https://cmdprive.github.io/darkthrone-dashboard")
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
                    "level":      int(row.get("Level", "0") or 0),
                    "race":       row.get("Race", ""),
                    "gold":       int(g) if g else 0,
                    "hp":         int(h) if h else 0,
                    "hp_max":     int(hmax) if hmax else 0,
                    "fort_pct":   int(row.get("FortPct", "0") or 0),
                    "turns":      int(row.get("Turns", "0") or 0),
                    "in_range":   row.get("InRange", "0") == "1",
                    "is_bot":     row.get("IsBot", "0") == "1",
                    "is_clan":    row.get("IsClanMember", "0") == "1",
                    "is_friend":  row.get("IsFriend", "0") == "1",
                    "is_hitlist": row.get("IsHitlist", "0") == "1",
                })

    with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    # FIX: Use a more reliable regex that matches up to the first ';' after the
    # opening brace, without relying on non-greedy DOTALL across nested braces.
    json_str = json.dumps(history, ensure_ascii=False)
    new_html = re.sub(
        r"const rawData\s*=\s*\{[^;]*\};",
        lambda _: f"const rawData = {json_str};",
        html,
    )

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


def scrape():
    # FIX: Write header once before the loop, not conditionally inside it.
    ensure_csv_header()
    existing_keys = load_existing_keys()
    today = datetime.date.today().isoformat()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            storage_state=AUTH_FILE if os.path.exists(AUTH_FILE) else None
        )
        page = context.new_page()

        print("🌍 Opening DarkThrone...")
        page.goto(f"{BASE_ATTACK_URL}?{ATTACK_PARAMS}&page=1")

        # Handle manual login if session expired
        if "login" in page.url:
            print("🔑 ACTION REQUIRED: Please log in via Google in the browser window...")
            # Wait indefinitely for the attack table to appear after login
            page.wait_for_selector("table tr", timeout=0)
            context.storage_state(path=AUTH_FILE)
            print("✅ Session saved.")

        last_page_fingerprint = ""

        for page_num in range(1, 201):
            print(f"📄 Scraping Page {page_num}...")

            # Wait for the battlelist table body rows to appear.
            # The correct selector confirmed from page HTML: #battlelist-table tbody tr
            try:
                page.wait_for_selector("#battlelist-table tbody tr", timeout=15000)
            except PlaywrightTimeoutError:
                print(f"⚠️  Page {page_num}: battlelist table didn't load. Stopping.")
                print(f"   🔗 Current URL: {page.url}")
                break

            # Each player row is a <tr> with data-name, data-gold, data-fort attributes.
            # e.g. <tr data-name="mungus" data-level="18" data-gold="13292" data-fort="100">
            player_rows = page.query_selector_all("#battlelist-table tbody tr[data-name]")
            found_on_page = []
            page_names = []

            if DEBUG_SELECTORS:
                print(f"  [DEBUG] Player rows found: {len(player_rows)}")

            for row in player_rows:
                # --- Player ID from profile link href ---
                link_el = row.query_selector("a.player-link")
                href = link_el.get_attribute("href") if link_el else ""
                id_match = re.search(r"/player/(\d+)", href)
                player_id = id_match.group(1) if id_match else ""

                # --- Core data attributes on the <tr> ---
                level = row.get_attribute("data-level") or ""
                race  = row.get_attribute("data-race") or ""
                gold  = row.get_attribute("data-gold") or "0"
                fort_pct = row.get_attribute("data-fort") or "0"

                # --- Display name (properly cased) ---
                name_span = row.query_selector("a.player-link span:last-child")
                name = name_span.inner_text().replace("(YOU)", "").strip() if name_span else ""
                if not name:
                    name = row.get_attribute("data-name") or ""
                if not name:
                    continue

                page_names.append(name)

                # --- Fort HP current and max from fort-bar title ("800/1000 HP") ---
                fort_hp = fort_pct
                fort_max_hp = "0"
                fort_bar = row.query_selector(".fort-bar")
                if fort_bar:
                    title = fort_bar.get_attribute("title") or ""
                    hp_match = re.match(r"(\d+)/(\d+)", title)
                    if hp_match:
                        fort_hp     = hp_match.group(1)
                        fort_max_hp = hp_match.group(2)

                # --- Turns (6th td — shows number if visible, else "-") ---
                tds = row.query_selector_all("td")
                turns = tds[5].inner_text().strip() if len(tds) > 5 else "-"
                turns = turns if turns != "-" else "0"

                # --- Attack range status (7th td) ---
                in_range = "0"
                if len(tds) > 6:
                    action_text = tds[6].inner_text().strip()
                    in_range = "0" if "out of range" in action_text.lower() else "1"

                # --- Badges ---
                is_bot        = "1" if "[bot]" in name.lower() else "0"
                is_clan       = "1" if row.query_selector(".clan-badge") else "0"
                is_friend     = "1" if row.query_selector(".friend-badge") else "0"
                is_hitlist    = "1" if row.query_selector(".hitlist-badge") else "0"

                if DEBUG_SELECTORS:
                    print(f"  [DEBUG] {name!r} id={player_id} lv={level} race={race} "
                          f"gold={gold} hp={fort_hp}/{fort_max_hp} turns={turns} "
                          f"range={in_range} bot={is_bot} clan={is_clan} "
                          f"friend={is_friend} hitlist={is_hitlist}")

                if (today, name) in existing_keys:
                    continue

                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                found_on_page.append([
                    timestamp, player_id, name, level, race,
                    gold, fort_hp, fort_max_hp, fort_pct,
                    turns, in_range, is_bot, is_clan, is_friend, is_hitlist
                ])
                existing_keys.add((today, name))

            # --- END DETECTION ---
            current_fingerprint = ",".join(page_names)
            if not page_names:
                print(f"🏁 No players on page {page_num}. Stopping.")
                break
            if current_fingerprint == last_page_fingerprint:
                print(f"🏁 Duplicate page detected at {page_num}. Stopping.")
                break

            last_page_fingerprint = current_fingerprint
            print(f"   ✅ Found {len(player_rows)} players ({len(found_on_page)} new)")

            if found_on_page:
                with open(DATA_FILE, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerows(found_on_page)

            page.goto(f"{BASE_ATTACK_URL}?{ATTACK_PARAMS}&page={page_num + 1}")

        # FIX: Refresh auth state at the end of a successful run.
        context.storage_state(path=AUTH_FILE)
        browser.close()

    update_dashboard()
    publish_dashboard()
    print("✨ Full Scan and Sync Complete!")


if __name__ == "__main__":
    while True:
        now = datetime.datetime.now()
        # Calculate seconds until the next :00 or :30
        minutes_past = now.minute % 30
        seconds_past = minutes_past * 60 + now.second
        wait_seconds = (30 * 60) - seconds_past

        next_run = now + datetime.timedelta(seconds=wait_seconds)
        print(f"⏳ Waiting until {next_run.strftime('%H:%M')} for next scrape. Press Ctrl+C to stop.")
        time.sleep(wait_seconds)

        print(f"\n🕐 Starting scrape at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        try:
            scrape()
        except Exception as e:
            print(f"❌ Scrape failed: {e}")
