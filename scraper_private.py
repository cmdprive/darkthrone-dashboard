import csv
import datetime
import json
import os
import re
import sys
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


def _unhide(path):
    if sys.platform == "win32" and os.path.isfile(path):
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)
        except Exception:
            pass

# --- CONFIGURATION ---
AUTH_FILE = "auth.json"
BASE_URL = "https://darkthronegame.com/game"

# Live snapshot written after every scrape — estimator reads this directly
FILE_LATEST = "private_latest.json"

# Private data files - keep these local, never push to GitHub
FILE_SELF_STATS     = "private_self_stats.csv"
FILE_UNITS          = "private_units.csv"
FILE_ARMORY         = "private_armory.csv"
FILE_BUILDINGS      = "private_buildings.csv"
FILE_BANK           = "private_bank.csv"
FILE_BATTLE_LOGS    = "private_battle_logs.csv"
FILE_FORT_ATTACKS   = "private_fort_attacks.csv"
FILE_FORT_STATS     = "private_fort_stats.csv"
FILE_UPGRADES       = "private_upgrades.csv"
FILE_PROFILES       = "private_player_profiles.csv"


# ---------------------------------------------------------------------------
# Live snapshot dict — populated by each scraper, written to JSON at the end
# ---------------------------------------------------------------------------
_live = {}

# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def append_rows(filepath, header, rows):
    """Append rows to a CSV. If the file exists with a different/old header,
    recreate it with the correct header so column mapping never breaks."""
    _unhide(filepath)
    if os.path.isfile(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            existing = f.readline().strip()
        if existing != ",".join(header):
            print(f"     ℹ️  Recreating {os.path.basename(filepath)} (stale header detected)")
            os.remove(filepath)
    new = not os.path.isfile(filepath)
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def row_exists(filepath, key_col, key_val):
    """Check if a row with key_val in key_col already exists."""
    if not os.path.isfile(filepath):
        return False
    with open(filepath, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get(key_col) == key_val:
                return True
    return False


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def _strip(html):
    """Strip all HTML tags and normalize whitespace so regex works reliably."""
    t = re.sub(r'<[^>]+>', ' ', html)
    t = re.sub(r'&amp;', '&', t)
    t = re.sub(r'&gt;',  '>', t)
    t = re.sub(r'&lt;',  '<', t)
    return re.sub(r'\s+', ' ', t)


def scrape_self_stats(page, ts):
    """Scrape all self stats from the overview page — strips HTML before parsing."""
    print("  📊 Scraping self stats from overview...")
    try:
        page.goto(f"{BASE_URL}/overview")
        page.wait_for_load_state("networkidle", timeout=15000)
        # CRITICAL: strip tags first — confirmed from debug_overview_text.txt
        t = _strip(page.content())

        def find(pattern, default="0"):
            m = re.search(pattern, t)
            return re.sub(r"\D", "", m.group(1)) if m else default

        # Combat — confirmed from live page
        atk     = find(r'Offense\s+([\d,]+)')
        def_    = find(r'Defense\s+([\d,]+)')
        spy_off = find(r'Spy ATK\s+([\d,]+)')
        spy_def = find(r'Spy DEF\s+([\d,]+)')

        # Economy
        income  = find(r'([\d,]+)\s*gold/turn')
        workers = find(r'Workers\s+([\d,]+)')
        mine_lv = find(r'Basic Mine[^L]*Lv\.(\d+)') or \
                  str(int(find(r'Mine Bonus\s+\+(\d+)%', '0')) // 10)

        # Army — confirmed patterns
        soldiers   = find(r'Soldier\s+(\d+)')
        guards     = find(r'Guard\s+(\d+)')
        spies      = find(r'(?:^| )Spy\s+(\d+)')
        sentries   = find(r'Sentry\s+(\d+)')
        total_army = find(r'Total Army Size\s*([\d,]+)')

        # Resources
        gold  = find(r'Gold on Hand\s+([\d,]+)')
        bank  = find(r'Banked Gold\s+([\d,]+)')

        # IDLE CITIZENS — the unassigned recruits shown in the header bar
        # e.g. "24,098 Gold  0 Citizens  2,140 Turns"
        citizens = find(r'Gold\s+[\d,]+\s+Citizens\s+(\d+)\s+Citizens')
        if citizens == "0":
            m2 = re.search(r'(\d+)\s+Citizens\s+[\d,]+\s+Turns', t)
            citizens = re.sub(r"\D", "", m2.group(1)) if m2 else "0"

        # TOTAL POPULATION — grows by up to 300 per day (recruited at 00:00 server time).
        # Shown separately on the overview page as "X Population" and on every
        # public player profile at /game/player/{id}.
        # Pattern 1: "270 Citizens  2,330 Population" (overview stat list)
        pop = find(r'(\d[\d,]*)\s+Population')
        if pop == "0":
            # Pattern 2: label before the number "Population 2,330"
            pop = find(r'Population\s+([\d,]+)')

        turns = find(r'Turns\s+([\d,]+)')

        # Level: header "Lvl 3 Level" — confirmed from live page
        level = find(r'Lvl\s+(\d+)\s+Level')
        if level == "0":
            level = find(r'(\d+)\s+Level\s+[\d,]+\s+XP')

        # XP — confirmed
        xp_curr = find(r'(\d[\d,]*)\s+XP\s+[\d,]+\s+XP needed')
        xp_need = find(r'[\d,]+\s+XP\s+([\d,]+)\s+XP needed')
        xp_pct  = find(r'([\d.]+)%\s+to Level')

        # Proficiencies
        strength     = find(r'Strength\s+(\d+)')
        constitution = find(r'Constitution\s+(\d+)')
        dexterity    = find(r'Dexterity\s+(\d+)')
        vigilance    = find(r'Vigilance\s+(\d+)')
        wealth       = find(r'Wealth\s+(\d+)')
        charisma     = find(r'Charisma\s+(\d+)')

        # Combat record
        att_w = find(r'Attacks\s+(\d+)\s*-\s*\d+')
        att_l = find(r'Attacks\s+\d+\s*-\s*(\d+)')
        def_w = find(r'Defenses\s+(\d+)\s*-\s*\d+')
        def_l = find(r'Defenses\s+\d+\s*-\s*(\d+)')

        # Header bar overrides — most current values
        for selector, var_name in [
            (".stat-item[title='Level'] .stat-value",    "level"),
            (".stat-item[title='Citizens'] .stat-value", "citizens"),
            (".stat-item[title='Gold'] .stat-value",     "gold"),
        ]:
            el = page.query_selector(selector)
            if el:
                val = re.sub(r"\D", "", el.inner_text())
                if val:
                    if var_name == "level":      level    = val
                    elif var_name == "citizens": citizens = val
                    elif var_name == "gold":     gold     = val

        append_rows(FILE_SELF_STATS,
            ["Timestamp","Gold","BankBalance","Citizens","Population","Turns","Level",
             "XP","XPNeeded","XPPct",
             "ATK","DEF","SpyOffense","SpyDefense",
             "Workers","Soldiers","Guards","Spies","Sentries","TotalArmy",
             "Income",
             "Strength","Constitution","Dexterity","Vigilance","Wealth","Charisma",
             "AttackWins","AttackLosses","DefenseWins","DefenseLosses"],
            [[ts, gold, bank, citizens, pop, turns, level,
              xp_curr, xp_need, xp_pct,
              atk, def_, spy_off, spy_def,
              workers, soldiers, guards, spies, sentries, total_army,
              income,
              strength, constitution, dexterity, vigilance, wealth, charisma,
              att_w, att_l, def_w, def_l]]
        )
        # ── Populate live snapshot ────────────────────────────────────────
        def n(v): return int(re.sub(r"\D", "", str(v or "")) or 0)
        _live.update({
            "timestamp":  ts,
            "level":      n(level),
            "gold":       n(gold),
            "bank":       n(bank),
            "citizens":   n(citizens),    # idle / unassigned only
            "population": n(pop),          # TOTAL population (grows +300/day max)
            "turns":      n(turns),
            "atk":        n(atk),
            "def":        n(def_),
            "spy_off":    n(spy_off),
            "spy_def":    n(spy_def),
            "income":     n(income),
            "workers":    n(workers),
            "soldiers":   n(soldiers),
            "guards":     n(guards),
            "spies":      n(spies),
            "sentries":   n(sentries),
            "total_army": n(total_army),
            "xp":         n(xp_curr),
            "xp_need":    n(xp_need),
            "xp_pct":     n(xp_pct),
        })

        print(f"     ✅ Lv={level} XP={xp_curr}/{xp_need} ({xp_pct}%) | "
              f"ATK={atk} DEF={def_} SpyOff={spy_off} | "
              f"Income={income}/tick | Pop={pop} Citizens={citizens}")
    except Exception as e:
        print(f"     ⚠️ Self stats failed: {e}")


def scrape_units(page, ts):
    """Scrape owned unit counts from training page using data-unit-id inputs (same as optimizer)."""
    print("  🪖 Scraping units...")
    try:
        page.goto(f"{BASE_URL}/train")
        page.wait_for_load_state("networkidle", timeout=10000)

        unit_js = page.evaluate("""() => {
            const owned = {}, trainable = {};
            document.querySelectorAll('input.multi-untrain-input[data-unit-id]').forEach(inp => {
                owned[inp.dataset.unitId] = parseInt(inp.dataset.max || '0');
            });
            document.querySelectorAll('input.multi-qty-input[data-unit-id]').forEach(inp => {
                trainable[inp.dataset.unitId] = parseInt(inp.dataset.max || '0');
            });
            return {owned, trainable};
        }""")

        uid_to_name  = {"1": "worker", "4": "soldier", "8": "guard", "12": "spy", "15": "sentry"}
        live_key_map = {"worker":"workers","soldier":"soldiers","guard":"guards",
                        "spy":"spies","sentry":"sentries"}
        rows = []
        for uid, name in uid_to_name.items():
            owned     = unit_js["owned"].get(uid, 0)
            trainable = unit_js["trainable"].get(uid, 0)
            # Fallback: if JS returned 0, use the overview-scraped count from _live
            if owned == 0 and name != "worker":
                owned = _live.get(live_key_map[name], 0)
            rows.append([ts, name, uid, owned, trainable])
            # Keep _live in sync with best available count
            if owned > 0:
                _live[live_key_map[name]] = owned
        # Citizens = max trainable workers (idle population)
        citizens = unit_js["trainable"].get("1", 0)
        if citizens > 0:
            _live["citizens"] = citizens
        rows.append([ts, "citizens_idle", "0", citizens, citizens])

        append_rows(FILE_UNITS,
            ["Timestamp", "Unit", "UnitID", "Owned", "Trainable"],
            rows
        )
        print(f"     ✅ {len(rows)-1} unit types + idle citizens={citizens}")
    except Exception as e:
        print(f"     ⚠️ Units failed: {e}")


def scrape_armory(page, ts):
    """Scrape owned armory items (sell-rows only) and max buyable tiers (buy-rows)."""
    print("  ⚔️ Scraping armory...")
    try:
        page.goto(f"{BASE_URL}/armory")
        page.wait_for_selector(".armory-page", timeout=15000)

        armory_js = page.evaluate("""() => {
            const owned = [], buyable = [];
            // sell-rows = items the player already owns
            document.querySelectorAll('tr.sell-row:not(.disabled)').forEach(row => {
                const tier = parseInt(row.dataset.tier || '0');
                const inp  = row.querySelector('input[name="item_id"]');
                const cells = row.querySelectorAll('td');
                const name  = cells[0]?.innerText?.trim() || '';
                const stats = cells[1]?.innerText?.trim() || '';
                const qty   = parseInt((cells[3]?.innerText || '').replace(/[^0-9]/g,'') || '0');
                if (inp && tier > 0 && qty > 0)
                    owned.push({item_id: parseInt(inp.value), tier, name, stats, qty});
            });
            // buy-rows = items available to purchase (not yet owned at that tier)
            document.querySelectorAll('tr.buy-row:not(.disabled)').forEach(row => {
                const tier = parseInt(row.dataset.tier || '0');
                const inp  = row.querySelector('input[name="item_id"]');
                const cells = row.querySelectorAll('td');
                const name  = cells[0]?.innerText?.trim() || '';
                const cost  = parseInt((cells[2]?.innerText || '').replace(/[^0-9]/g,'') || '0');
                if (inp && tier > 0)
                    buyable.push({item_id: parseInt(inp.value), tier, name, cost});
            });
            return {owned, buyable};
        }""")

        owned_rows   = [[ts, "owned",   r["item_id"], r["tier"], r["name"], r["stats"], r["qty"], ""]
                        for r in armory_js["owned"]]
        buyable_rows = [[ts, "buyable", r["item_id"], r["tier"], r["name"], "",          0,         r["cost"]]
                        for r in armory_js["buyable"]]

        append_rows(FILE_ARMORY,
            ["Timestamp", "RowType", "ItemID", "Tier", "Item", "Stats", "Owned", "BuyCost"],
            owned_rows + buyable_rows
        )
        print(f"     ✅ {len(owned_rows)} owned items, {len(buyable_rows)} buyable tiers")
    except Exception as e:
        print(f"     ⚠️ Armory failed: {e}")


BUILDING_TYPE_ID = {
    1: "Fortification", 2: "Armory", 3: "Mine", 4: "Spy Academy",
    5: "Barracks", 6: "Housing", 7: "Mercenary Camp",
}

def scrape_buildings(page, ts):
    """Scrape building levels via building_type_id inputs (same method as optimizer)."""
    print("  🏗️ Scraping buildings...")
    try:
        page.goto(f"{BASE_URL}/buildings")
        page.wait_for_load_state("networkidle", timeout=10000)

        bldg_js = page.evaluate("""() => {
            const result = {};
            document.querySelectorAll('input[name="building_type_id"]').forEach(inp => {
                const id = parseInt(inp.value);
                let el = inp.parentElement;
                for (let i = 0; i < 8 && el; i++) {
                    const sv = el.querySelector('.status-value');
                    if (sv) {
                        const m = sv.innerText.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
                        if (m) { result[id] = {level: parseInt(m[1]), max: parseInt(m[2])}; break; }
                    }
                    el = el.parentElement;
                }
            });
            return result;
        }""")

        rows = []
        buildings_live = {}
        for id_str, info in bldg_js.items():
            name = BUILDING_TYPE_ID.get(int(id_str), f"building_{id_str}")
            rows.append([ts, name, int(id_str), info["level"], info["max"]])
            buildings_live[name] = info["level"]

        # Populate live snapshot
        _live["buildings"] = buildings_live
        _live["mine_lv"]   = buildings_live.get("Mine", 0)

        append_rows(FILE_BUILDINGS,
            ["Timestamp", "Building", "BuildingTypeID", "Level", "MaxLevel"],
            rows
        )
        print(f"     ✅ {len(rows)} buildings recorded: {buildings_live}")
    except Exception as e:
        print(f"     ⚠️ Buildings failed: {e}")


def scrape_bank(page, ts):
    """Scrape bank balance and deposit count."""
    print("  🏦 Scraping bank...")
    try:
        page.goto(f"{BASE_URL}/bank")
        page.wait_for_selector(".card", timeout=10000)
        # Strip HTML before parsing
        t = _strip(page.content())

        def find(pattern, default="0"):
            m = re.search(pattern, t, re.IGNORECASE)
            return re.sub(r"\D", "", m.group(1)) if m else default

        gold_hand = find(r'Gold on Hand\s+([\d,]+)')
        bank_bal  = find(r'Bank(?:ed)? (?:Gold|Balance)\s+([\d,]+)')
        deposits  = find(r'(\d+)\s*/\s*6\s+(?:used|deposits)', "0")

        append_rows(FILE_BANK,
            ["Timestamp", "GoldOnHand", "BankBalance", "DepositsUsed"],
            [[ts, gold_hand, bank_bal, deposits]]
        )
        print(f"     ✅ GoldOnHand={gold_hand} Bank={bank_bal} Deposits={deposits}/6")
    except Exception as e:
        print(f"     ⚠️ Bank failed: {e}")


def scrape_battle_logs(page, ts):
    """Scrape battle log entries not yet recorded."""
    print("  📜 Scraping battle logs...")
    try:
        page.goto(f"{BASE_URL}/battle-logs")
        page.wait_for_selector("table, .card", timeout=10000)

        new_rows = []
        for row in page.query_selector_all("tbody tr"):
            cells = row.query_selector_all("td")
            if len(cells) >= 7:
                date        = cells[0].inner_text().strip()
                attacker    = cells[1].inner_text().strip()
                result      = cells[2].inner_text().strip()
                gold        = re.sub(r"\D", "", cells[3].inner_text())
                xp          = re.sub(r"\D", "", cells[4].inner_text())
                your_losses = cells[5].inner_text().strip()
                enemy_losses= cells[6].inner_text().strip()

                # Use date+attacker as dedup key
                key = f"{date}|{attacker}"
                if not row_exists(FILE_BATTLE_LOGS, "_key", key):
                    new_rows.append([ts, date, attacker, result, gold, xp,
                                     your_losses, enemy_losses, key])

        append_rows(FILE_BATTLE_LOGS,
            ["ScrapedAt", "Date", "Attacker", "Result", "Gold", "XP",
             "YourLosses", "EnemyLosses", "_key"],
            new_rows
        )
        print(f"     ✅ {len(new_rows)} new battle log entries")
    except Exception as e:
        print(f"     ⚠️ Battle logs failed: {e}")


def scrape_fort_attacks(page, ts):
    """Scrape recent attacks on your fort."""
    print("  🏰 Scraping fort attack history...")
    try:
        page.goto(f"{BASE_URL}/fort")
        page.wait_for_selector(".card", timeout=10000)

        new_rows = []
        for row in page.query_selector_all("tbody tr"):
            cells = row.query_selector_all("td")
            if len(cells) >= 4:
                attacker = cells[0].inner_text().strip()
                atk_type = cells[1].inner_text().strip()
                damage   = cells[2].inner_text().strip()
                when     = cells[3].inner_text().strip()

                key = f"{attacker}|{atk_type}|{damage}|{when}"
                if not row_exists(FILE_FORT_ATTACKS, "_key", key):
                    new_rows.append([ts, attacker, atk_type, damage, when, key])

        append_rows(FILE_FORT_ATTACKS,
            ["ScrapedAt", "Attacker", "Type", "Damage", "When", "_key"],
            new_rows
        )
        print(f"     ✅ {len(new_rows)} new fort attack entries")
    except Exception as e:
        print(f"     ⚠️ Fort attacks failed: {e}")


PROFILE_HEADER = [
    "Timestamp", "Player", "PlayerID",
    "Level", "Race", "Class", "Clan",
    "GoldOnHand", "Population", "FortHP", "FortMax", "HasFort",
    "OverallRank", "OffenseRank", "DefenseRank",
    "SpyOffRank", "SpyDefRank",
    "NetWorthRank", "TotalPlayers",
]

PROFILE_JS = """() => {
    const t = (document.body.innerText || '').replace(/[\\r\\n]+/g, ' ').replace(/\\s+/g, ' ');
    const n = s => parseInt((s||'').replace(/[^0-9]/g,'')) || 0;
    const find  = (pat, def=0)  => { const m=t.match(pat); return m ? n(m[1]) : def; };
    const findS = (pat, def='') => { const m=t.match(pat); return m ? m[1].trim() : def; };

    // Detect invalid / deleted player page
    if (/player not found|deleted|this account/i.test(t) || t.length < 200) {
        return null;
    }

    // Rankings sidebar — handles both "Overall #91 / 248" and "Overall #91"
    const ov_m   = t.match(/Overall\\s+#(\\d+)(?:\\s*\\/\\s*(\\d+))?/i);
    const off_m  = t.match(/Offense\\s+#(\\d+)/i);
    const def_m  = t.match(/Defense\\s+#(\\d+)/i);
    const nw_m   = t.match(/Net\\s*Worth\\s+#(\\d+)/i);
    // Spy rankings — labelled "Spy Offense #X" or "Spy ATK #X" depending on version
    const spo_m  = t.match(/Spy\\s+(?:Offense|ATK)\\s+#(\\d+)/i);
    const spd_m  = t.match(/Spy\\s+(?:Defense|DEF)\\s+#(\\d+)/i);

    // Fortress: "800 / 1,000 HP" or "No Fort"
    const fort_m  = t.match(/([\\d,]+)\\s*\\/\\s*([\\d,]+)\\s*(?:HP|Fort)/i);
    const no_fort = /No Fort/i.test(t);

    // Player name from page title / h1 (for unknown-ID discovery)
    const nameEl = document.querySelector('h1, .player-name, [class*="player-name"]');
    const pageTitle = (nameEl?.innerText || document.title || '').trim()
                        .replace(/\\s*[-|].*$/, '').trim();

    return {
        name:     pageTitle,
        level:    find(/LEVEL\\s+(\\d+)/i),
        race:     findS(/RACE\\s+(\\w+)/i),
        cls:      findS(/CLASS\\s+(\\w+(?:\\s+\\w+)?)/i),
        clan:     findS(/CLAN\\s+([^\\n]{1,30})(?:\\s{2,}|$)/i),
        gold:     find(/GOLD\\s+ON\\s+HAND\\s+([\\d,]+)/i),
        pop:      find(/POPULATION\\s+([\\d,]+)/i),
        fort_hp:  fort_m ? n(fort_m[1]) : 0,
        fort_max: fort_m ? n(fort_m[2]) : 0,
        has_fort: fort_m ? 1 : 0,
        rank_ov:  ov_m  ? parseInt(ov_m[1])  : 0,
        total_p:  ov_m && ov_m[2] ? parseInt(ov_m[2]) : 0,
        rank_off: off_m ? parseInt(off_m[1]) : 0,
        rank_def: def_m ? parseInt(def_m[1]) : 0,
        rank_spo: spo_m ? parseInt(spo_m[1]) : 0,
        rank_spd: spd_m ? parseInt(spd_m[1]) : 0,
        rank_nw:  nw_m  ? parseInt(nw_m[1])  : 0,
    };
}"""


def _scrape_one_profile(page, pid):
    """Visit /game/player/{pid} and return stats dict, or None if invalid."""
    try:
        page.goto(f"https://darkthronegame.com/game/player/{pid}",
                  wait_until="domcontentloaded", timeout=15000)
        # Brief wait for dynamic content
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        stats = page.evaluate(PROFILE_JS)
        return stats  # None if page returned null (invalid player)
    except Exception as e:
        return None


def scrape_player_profiles(page, ts, force_refresh=False, scan_up_to=500):
    """
    Scrapes the public profile page of EVERY player on the server by:
      1. Collecting known {name → ID} pairs from darkthrone_server_data.csv
      2. Scanning IDs 1..scan_up_to to discover ANY player not in the attack list
         (skips IDs already matched to a known name, skips invalid/empty pages)

    Public profiles show: Level, Race, Class, Clan, Gold on Hand, Population,
    Fortress HP, and Rankings (Overall, Offense, Defense, Net Worth).
    ATK/DEF/Spy are NOT shown — those come from CONFIRMED_STATS only.

    Set force_refresh=True to re-scrape IDs already processed today.
    """
    print("  👤 Scraping ALL player profiles (attack list + sequential ID scan)...")

    # ── Step 1: build known name→ID and ID→name maps from attack CSV ──────────
    attack_csv = "darkthrone_server_data.csv"
    name_to_id: dict = {}   # name → pid  (str)
    id_to_name: dict = {}   # pid  → name (str)

    if os.path.isfile(attack_csv):
        with open(attack_csv, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = row.get("Player", "").strip()
                pid  = row.get("PlayerID", "").strip()
                if name and pid:
                    if name not in name_to_id:
                        name_to_id[name] = pid
                    if pid not in id_to_name:
                        id_to_name[pid] = name

    # ── Step 2: build set of IDs already scraped today ─────────────────────────
    today = ts[:10]
    done_ids: set = set()   # set of pid strings processed today

    if not force_refresh and os.path.isfile(FILE_PROFILES):
        with open(FILE_PROFILES, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("Timestamp", "")[:10] == today:
                    done_ids.add(row.get("PlayerID", ""))

    # ── Step 3: build final ID list — known IDs first, then unknown range ─────
    known_ids   = [(pid, id_to_name.get(pid, "")) for pid in id_to_name]
    unknown_ids = [(str(i), "") for i in range(1, scan_up_to + 1)
                   if str(i) not in id_to_name]
    all_ids = known_ids + unknown_ids

    new_rows: list = []
    scraped  = 0
    skipped  = 0
    empty    = 0

    for pid, known_name in all_ids:
        if pid in done_ids:
            skipped += 1
            continue

        stats = _scrape_one_profile(page, pid)

        if stats is None:
            # Invalid / deleted / not-a-player page
            empty += 1
            done_ids.add(pid)
            continue

        # Resolve display name: prefer known attack-list name, fall back to page title
        name = known_name or stats.get("name", "") or f"Player_{pid}"

        # Skip if it looks like a login page redirect or empty page
        if stats.get("level", 0) == 0 and stats.get("pop", 0) == 0 and not stats.get("race"):
            empty += 1
            done_ids.add(pid)
            continue

        new_rows.append([
            ts, name, pid,
            stats["level"], stats["race"], stats["cls"], stats["clan"],
            stats["gold"], stats["pop"], stats["fort_hp"], stats["fort_max"], stats["has_fort"],
            stats["rank_ov"], stats["rank_off"], stats["rank_def"],
            stats["rank_spo"], stats["rank_spd"],
            stats["rank_nw"], stats["total_p"],
        ])
        done_ids.add(pid)

        # Update _live rank_map with freshly scraped ranks (more accurate than rankings page)
        rank_map = _live.setdefault("rank_map", {})
        if name not in rank_map:
            rank_map[name] = {}
        rank_map[name].update({
            "level":        stats["level"],
            "clan":         stats["clan"]    or rank_map[name].get("clan",        "—"),
            "overall":      stats["rank_ov"] or rank_map[name].get("overall",      0),
            "off_rank":     stats["rank_off"]or rank_map[name].get("off_rank",     0),
            "def_rank":     stats["rank_def"]or rank_map[name].get("def_rank",     0),
            "spy_off_rank": stats["rank_spo"]or rank_map[name].get("spy_off_rank", 0),
            "spy_def_rank": stats["rank_spd"]or rank_map[name].get("spy_def_rank", 0),
            "nw_rank":      stats["rank_nw"] or rank_map[name].get("nw_rank",      0),
        })

        fort_str = f"{stats['fort_hp']}/{stats['fort_max']}" if stats["has_fort"] else "No Fort"
        print(f"     [{pid:>4}] {name:<26} Lv{stats['level']:>2} {stats['race']:<7} {stats['cls']:<9} | "
              f"Pop={stats['pop']:>5,}  Gold={stats['gold']:>8,} | "
              f"Fort={fort_str:<12} | "
              f"ov#{stats['rank_ov']} off#{stats['rank_off']} def#{stats['rank_def']}")
        scraped += 1

        # Polite delay between unknown-ID probes to avoid hammering the server
        if not known_name:
            time.sleep(0.3)

    if new_rows:
        append_rows(FILE_PROFILES, PROFILE_HEADER, new_rows)

    # Refresh rankings snapshot with freshly scraped per-profile ranks
    if _live.get("rank_map"):
        snap_path = "private_rankings_snapshot.json"
        snap = {}
        if os.path.isfile(snap_path):
            with open(snap_path, encoding="utf-8") as f:
                try: snap = json.load(f)
                except Exception: snap = {}
        snap["rank_map"] = _live["rank_map"]
        snap["profile_scraped_at"] = ts
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2)
        print(f"     📸 Rankings snapshot updated with per-profile ranks")

    print(f"     ✅ Scraped {scraped} profiles | skipped {skipped} (already today) | "
          f"{empty} empty/invalid IDs")


def scrape_fort_stats(page, ts):
    """Scrape fort HP, max HP, fort level and repair cost (same as optimizer)."""
    print("  🛡️ Scraping fort stats...")
    try:
        page.goto(f"{BASE_URL}/fort")
        page.wait_for_load_state("networkidle", timeout=10000)

        fort_js = page.evaluate("""() => {
            const r = {hp: 0, max_hp: 0, fort_lv: 0, cost_per_hp: 16.75};
            document.querySelectorAll('.fort-stat-box').forEach(box => {
                const label = (box.querySelector('.fort-stat-label')?.innerText || '').trim();
                const val   = (box.querySelector('.fort-stat-value')?.innerText || '').trim();
                const n = parseInt(val.replace(/[^0-9]/g,'')) || 0;
                if (label.includes('Current Health'))      r.hp       = n;
                else if (label.includes('Maximum Health')) r.max_hp   = n;
                else if (label.includes('Fortification'))  r.fort_lv  = n;
            });
            const scripts = Array.from(document.scripts).map(s => s.innerText || s.textContent);
            for (const sc of scripts) {
                const m = sc.match(/costPerHp\\s*=\\s*([\\d.]+)/);
                if (m) { r.cost_per_hp = parseFloat(m[1]); break; }
            }
            return r;
        }""")

        hp       = fort_js["hp"]
        max_hp   = fort_js["max_hp"]
        fort_lv  = fort_js["fort_lv"]
        cpp      = fort_js["cost_per_hp"]
        pct      = round(hp / max(max_hp, 1) * 100)
        dmg      = max_hp - hp
        repair_c = int(dmg * cpp) + 1 if dmg > 0 else 0

        append_rows(FILE_FORT_STATS,
            ["Timestamp", "FortHP", "FortMaxHP", "FortPct", "FortLevel", "CostPerHP", "DamageHP", "RepairCost"],
            [[ts, hp, max_hp, pct, fort_lv, cpp, dmg, repair_c]]
        )
        print(f"     ✅ Fort {hp}/{max_hp} HP ({pct}%) Lv={fort_lv} damage={dmg} repair_cost={repair_c:,}g")
    except Exception as e:
        print(f"     ⚠️ Fort stats failed: {e}")


def scrape_upgrades(page, ts):
    """Scrape owned and buyable battle upgrades (same as optimizer)."""
    print("  ⬆️ Scraping upgrades...")
    try:
        page.goto(f"{BASE_URL}/upgrades")
        page.wait_for_load_state("networkidle", timeout=10000)

        upg_js = page.evaluate("""() => {
            const owned = [], buyable = [];
            document.querySelectorAll('.sell-mode tr:not(.disabled)').forEach(row => {
                const cells = row.querySelectorAll('td');
                if (cells.length < 4) return;
                const name  = cells[0].querySelector('strong')?.innerText?.trim();
                const count = parseInt(cells[3]?.innerText?.trim() || '0');
                if (name && count > 0) owned.push({name, count});
            });
            document.querySelectorAll('.buy-mode tr:not(.disabled)').forEach(row => {
                const cells = row.querySelectorAll('td');
                if (cells.length < 4) return;
                const name = cells[0].querySelector('strong')?.innerText?.trim();
                const cost = parseInt((cells[2]?.innerText || '').replace(/[^0-9]/g,'') || '0');
                const max  = cells[3]?.innerText?.trim() || '';
                if (name && cost > 0) buyable.push({name, cost, max});
            });
            return {owned, buyable};
        }""")

        rows = [[ts, "owned",   r["name"], r["count"], 0,        ""] for r in upg_js["owned"]]
        rows += [[ts, "buyable", r["name"], 0,          r["cost"], r["max"]] for r in upg_js["buyable"]]

        append_rows(FILE_UPGRADES,
            ["Timestamp", "RowType", "Name", "OwnedCount", "BuyCost", "MaxOwnable"],
            rows
        )
        print(f"     ✅ {len(upg_js['owned'])} owned upgrades, {len(upg_js['buyable'])} buyable")
    except Exception as e:
        print(f"     ⚠️ Upgrades failed: {e}")


FILE_RANKINGS           = "private_rankings.csv"
FILE_ARMY_LEADERBOARDS  = "private_army_leaderboards.csv"

def scrape_army_leaderboards(page, ts):
    """Scrape the four army/social detail leaderboards that give us hard data
    on every ranked player's army size, training volume, population and building
    investment.  Results are merged into a single player-keyed JSON snapshot
    (private_army_snapshot.json) consumed by the estimator.

    Leaderboards scraped:
      army/largest_army       → total current army size  (military count)
      army/units_trained      → cumulative units ever trained
      army/highest_population → total population ranking
      social/building_upgrades→ total building upgrades purchased
    """
    LEADERBOARDS = [
        ("largest_army",      f"{BASE_URL}/leaderboards/army/largest_army?period=alltime&view=detail"),
        ("units_trained",     f"{BASE_URL}/leaderboards/army/units_trained?period=alltime&view=detail"),
        ("highest_population",f"{BASE_URL}/leaderboards/army/highest_population?period=alltime&view=detail"),
        ("building_upgrades", f"{BASE_URL}/leaderboards/social/building_upgrades?period=alltime&view=detail"),
    ]

    # JS extractor — works for the detail-view leaderboard table format
    EXTRACT_JS = """() => {
        const rows = [];
        // Detail pages use a <table> with tbody rows; each row = one ranked player.
        const tableRows = document.querySelectorAll('table tbody tr, [class*="leaderboard"] tr');
        tableRows.forEach(tr => {
            const cells = Array.from(tr.querySelectorAll('td'));
            if (cells.length < 2) return;

            // Rank — first cell, usually "#1" or just "1"
            const rankTxt = (cells[0]?.innerText || '').trim();
            const rankM   = rankTxt.match(/^#?(\\d+)/);
            if (!rankM) return;
            const rank = parseInt(rankM[1]);

            // Player name — prefer <a href*=player>, fall back to 2nd cell text
            const nameEl = tr.querySelector('a[href*="player"]');
            let name = (nameEl?.innerText || cells[1]?.innerText || '').trim();
            name = name.replace(/\\s*\\[.*?\\]/g, '').replace(/\\s+/g,' ').trim();
            if (!name || name.length < 2) return;

            // Clan badge (optional)
            const clanEl = tr.querySelector('[class*="clan"],[class*="tag"],.badge');
            const clan   = (clanEl?.innerText || '').replace(/[\\[\\]]/g,'').trim();

            // Value — last numeric cell (army size, units trained, etc.)
            let value = 0;
            for (let i = cells.length - 1; i >= 0; i--) {
                const n = parseInt((cells[i]?.innerText || '').replace(/[^0-9]/g,''));
                if (n > 0) { value = n; break; }
            }

            rows.push({rank, name, clan, value});
        });
        return rows;
    }"""

    print("  🪖 Scraping army / social leaderboards...")
    all_rows = []   # CSV rows
    player_map = {} # {name: {army_size, army_rank, units_trained, ...}}

    for key, url in LEADERBOARDS:
        try:
            page.goto(url)
            page.wait_for_load_state("networkidle", timeout=15000)
            entries = page.evaluate(EXTRACT_JS)

            for e in entries:
                name = e["name"]
                rank = e["rank"]
                val  = e["value"]
                clan = e["clan"]
                all_rows.append([ts, key, rank, name, clan, val])

                p = player_map.setdefault(name, {"clan": clan or "—"})
                if key == "largest_army":
                    p["army_size"] = val
                    p["army_rank"] = rank
                elif key == "units_trained":
                    p["units_trained"]      = val
                    p["units_trained_rank"] = rank
                elif key == "highest_population":
                    p["population"]      = val
                    p["population_rank"] = rank
                elif key == "building_upgrades":
                    p["building_upgrades"]      = val
                    p["upgrades_rank"] = rank

            print(f"     ✅ {key}: {len(entries)} players")
        except Exception as ex:
            print(f"     ⚠️  {key} failed: {ex}")

    # Derive military fraction for players where both army_size and population known
    for name, p in player_map.items():
        army = p.get("army_size", 0)
        pop  = p.get("population", 0)
        if army > 0 and pop > 0:
            p["military_fraction"] = round(army / pop, 4)

    # Write CSV
    if all_rows:
        append_rows(FILE_ARMY_LEADERBOARDS,
            ["Timestamp", "Category", "Rank", "Player", "Clan", "Value"],
            all_rows)

    # Write JSON snapshot consumed by estimator
    snap = {"timestamp": ts, "players": player_map}
    _unhide("private_army_snapshot.json")
    with open("private_army_snapshot.json", "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2)
    print(f"     📸 Army snapshot → private_army_snapshot.json  "
          f"({len(player_map)} unique players)")

    # Merge into _live so the estimator can access it in the same run
    _live["army_data"] = player_map


def scrape_rankings(page, ts):
    """
    Scrapes the Global Rankings page — all tabs (Global, Combat, Spy, Economy, Army).
    Captures rank, player name, clan, value for each leaderboard.
    This gives us offense rank, defense rank, level rank, etc. for every tracked player.
    """
    print("  🏆 Scraping global rankings...")
    try:
        page.goto(f"{BASE_URL}/rankings")
        page.wait_for_load_state("networkidle", timeout=15000)

        rankings = page.evaluate("""() => {
            const results = [];

            // Each leaderboard widget on the page
            document.querySelectorAll('.leaderboard-widget, .rankings-widget, [class*="leaderboard"], [class*="ranking"]').forEach(widget => {
                // Widget title (e.g. "OVERALL POWER", "OFFENSE", "DEFENSE", "LEVEL")
                const title = (widget.querySelector('h2,h3,[class*="title"],[class*="header"]')
                               ?.innerText || '').trim().toUpperCase();
                if (!title) return;

                widget.querySelectorAll('tr, [class*="row"], li').forEach(row => {
                    const text = row.innerText || '';
                    // Look for rank number like "#1" or "1."
                    const rankM = text.match(/^#?(\\d+)/);
                    if (!rankM) return;
                    const rank = parseInt(rankM[1]);

                    // Player name — strip clan badges
                    const nameEl = row.querySelector('a, [class*="player"], [class*="name"]');
                    let name = (nameEl?.innerText || '').trim();
                    // Remove clan tag like "[RQUM]"
                    name = name.replace(/\\s*\\[.*?\\]/g, '').trim();
                    if (!name) return;

                    // Clan tag
                    const clanEl = row.querySelector('[class*="clan"], .badge');
                    const clan = (clanEl?.innerText || '').replace(/[\\[\\]]/g,'').trim();

                    // Value (shown for level ranking)
                    const cells = row.querySelectorAll('td');
                    const value = cells.length > 0
                        ? parseInt((cells[cells.length-1]?.innerText||'').replace(/[^0-9]/g,'')) || 0
                        : 0;

                    results.push({category: title, rank, name, clan, value});
                });
            });

            // Fallback: scrape "Your rank: #X / Y" lines to get total player count
            const bodyText = document.body.innerText || '';
            const totalM = bodyText.match(/Your rank:\\s*#\\d+\\s*\\/\\s*(\\d+)/);
            const total = totalM ? parseInt(totalM[1]) : 0;

            return {entries: results, total_players: total};
        }""")

        rows = []
        for e in rankings.get("entries", []):
            rows.append([ts, e["category"], e["rank"], e["name"], e["clan"], e["value"]])

        total = rankings.get("total_players", 0)

        append_rows(FILE_RANKINGS,
            ["Timestamp", "Category", "Rank", "Player", "Clan", "Value"],
            rows
        )

        # Also update _live with rankings info
        _live["total_players"] = total
        cats = {}
        for e in rankings.get("entries", []):
            cats.setdefault(e["category"], []).append(e)
        print(f"     ✅ {len(rows)} ranking entries across {len(cats)} categories | "
              f"Total players: {total}")

        # Build a flat dict of {player: {overall, offense, defense, level, clan}} for estimator
        rank_map = {}
        for e in rankings.get("entries", []):
            n = e["name"]
            if n not in rank_map:
                rank_map[n] = {"clan": e["clan"] or "—"}
            cat = e["category"]
            if "OVERALL" in cat:
                rank_map[n]["overall"]      = e["rank"]
            elif "SPY" in cat and ("OFF" in cat or "ATK" in cat):
                rank_map[n]["spy_off_rank"] = e["rank"]
            elif "SPY" in cat and ("DEF" in cat or "DEFENSE" in cat):
                rank_map[n]["spy_def_rank"] = e["rank"]
            elif "OFFENSE" in cat or "ATTACK" in cat:
                rank_map[n]["off_rank"]     = e["rank"]
            elif "DEFENSE" in cat:
                rank_map[n]["def_rank"]     = e["rank"]
            elif "LEVEL" in cat:
                rank_map[n]["lv_rank"]      = e["rank"]
                rank_map[n]["level"]        = e["value"]

        _live["rank_map"] = rank_map

        # Write a separate JSON snapshot for the estimator to consume
        _unhide("private_rankings_snapshot.json")
        with open("private_rankings_snapshot.json", "w", encoding="utf-8") as f:
            json.dump({"timestamp": ts, "total_players": total,
                       "rank_map": rank_map, "entries": rankings.get("entries", [])}, f, indent=2)
        print(f"     📸 Rankings snapshot → private_rankings_snapshot.json")

    except Exception as e:
        print(f"     ⚠️ Rankings failed: {e}")


# ---------------------------------------------------------------------------
# Read own server-wide ranks from /stats (player's own profile page)
# ---------------------------------------------------------------------------

def read_own_ranks(page) -> dict:
    """
    Navigate to /stats (your own profile) and extract your server-wide rank numbers.
    Returns dict with rank_overall, rank_offense, rank_defense, rank_wealth, rank_spy_off, rank_spy_def.
    Falls back to all-zeros on error.
    """
    result = {"rank_overall": 0, "rank_offense": 0, "rank_defense": 0,
              "rank_wealth": 0, "rank_spy_off": 0, "rank_spy_def": 0}
    try:
        page.goto(f"{BASE_URL}/stats")
        page.wait_for_load_state("networkidle", timeout=15000)

        RANK_JS = r"""() => {
            const t = document.body.innerText || '';
            const n = s => { const m = s?.match(/[\d,]+/); return m ? parseInt(m[0].replace(/,/g,'')) : 0; };
            const ov_m  = t.match(/Overall\s+#(\d+)/i);
            const off_m = t.match(/Offense\s+#(\d+)/i);
            const def_m = t.match(/Defense\s+#(\d+)/i);
            const nw_m  = t.match(/Net\s*Worth\s+#(\d+)/i);
            const spo_m = t.match(/Spy\s+(?:Offense|ATK)\s+#(\d+)/i);
            const spd_m = t.match(/Spy\s+(?:Defense|DEF)\s+#(\d+)/i);
            return {
                rank_overall:  ov_m  ? parseInt(ov_m[1])  : 0,
                rank_offense:  off_m ? parseInt(off_m[1]) : 0,
                rank_defense:  def_m ? parseInt(def_m[1]) : 0,
                rank_wealth:   nw_m  ? parseInt(nw_m[1])  : 0,
                rank_spy_off:  spo_m ? parseInt(spo_m[1]) : 0,
                rank_spy_def:  spd_m ? parseInt(spd_m[1]) : 0,
            };
        }"""
        ranks = page.evaluate(RANK_JS)
        result.update({k: v for k, v in ranks.items() if v > 0})
        if any(result.values()):
            print(f"  📊 Own ranks: Overall #{result['rank_overall']} | "
                  f"Offense #{result['rank_offense']} | Defense #{result['rank_defense']} | "
                  f"Wealth #{result['rank_wealth']}")
        else:
            print("  ⚠️  read_own_ranks: no rank text found on /stats page")
    except Exception as e:
        print(f"  ⚠️  read_own_ranks failed: {e}")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape_private():
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n🔒 Private scrape at {ts}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=AUTH_FILE if os.path.exists(AUTH_FILE) else None
        )
        page = context.new_page()

        print("🌍 Opening DarkThrone...")
        page.goto(f"{BASE_URL}/stats")

        if "login" in page.url:
            print("🔑 Please log in...")
            page.wait_for_url("**/game/**", timeout=0)
            context.storage_state(path=AUTH_FILE)

        # Navigate to a page that has the header bar loaded
        page.goto(f"{BASE_URL}/bank")
        page.wait_for_load_state("networkidle", timeout=15000)

        scrape_self_stats(page, ts)
        scrape_bank(page, ts)
        scrape_units(page, ts)
        scrape_armory(page, ts)
        scrape_buildings(page, ts)
        scrape_fort_stats(page, ts)
        scrape_upgrades(page, ts)
        scrape_army_leaderboards(page, ts)  # army size, units trained, population, upgrades
        scrape_rankings(page, ts)          # global leaderboard (offense/defense/spy ranks)
        scrape_battle_logs(page, ts)
        scrape_fort_attacks(page, ts)
        scrape_player_profiles(page, ts, force_refresh=True)

        context.storage_state(path=AUTH_FILE)
        browser.close()

    # Write live snapshot JSON immediately after browser closes
    with open(FILE_LATEST, "w", encoding="utf-8") as f:
        json.dump(_live, f, indent=2)
    print(f"  📸 Live snapshot → {FILE_LATEST}")

    print("✨ Private scrape complete!")
    print(f"   Files saved: {FILE_SELF_STATS}, {FILE_BANK}, {FILE_UNITS},")
    print(f"                {FILE_ARMORY}, {FILE_BUILDINGS}, {FILE_FORT_STATS}, {FILE_UPGRADES},")
    print(f"                {FILE_BATTLE_LOGS}, {FILE_FORT_ATTACKS},")
    print(f"                {FILE_ARMY_LEADERBOARDS}, private_army_snapshot.json")

    # Run the economic advisor and estimator after every scrape
    try:
        from advisor import run_advisor
        run_advisor()
    except Exception as e:
        print(f"⚠️ Advisor failed: {e}")

    try:
        from estimator import run
        run()
    except Exception as e:
        print(f"⚠️ Estimator failed: {e}")

    # Re-inject fresh rank data into the public dashboard after every private scrape.
    # scraper.update_dashboard() reads private_rankings_snapshot.json and pushes to GitHub.
    try:
        from scraper import update_dashboard, publish_dashboard
        print("  🔄 Refreshing public dashboard with updated rankings...")
        update_dashboard()
        publish_dashboard()
    except Exception as e:
        print(f"⚠️ Dashboard rank refresh failed: {e}")


if __name__ == "__main__":
    import sys

    # --once flag: run exactly once and exit (used by Windows Task Scheduler)
    if "--once" in sys.argv:
        print(f"\n🕐 Scheduled run at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        try:
            scrape_private()
        except Exception as e:
            print(f"❌ Scrape failed: {e}")
        sys.exit(0)

    # Default: continuous loop every 30 minutes (manual / dev use)
    while True:
        try:
            scrape_private()
        except Exception as e:
            print(f"❌ Private scrape failed: {e}")

        now = datetime.datetime.now()
        seconds_past = (now.minute % 30) * 60 + now.second
        wait_seconds = (30 * 60) - seconds_past
        next_run = now + datetime.timedelta(seconds=wait_seconds)
        print(f"⏳ Next private scrape at {next_run.strftime('%H:%M')}. Press Ctrl+C to stop.")
        time.sleep(wait_seconds)
