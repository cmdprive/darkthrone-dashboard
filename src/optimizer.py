"""
DarkThrone — Smart Optimizer
======================================
Each tick: READ all data → ANALYSE → DECIDE → ACT

Decision priority (revised — never hold gold back):
  1. Repair fort if damaged
  2. Build / upgrade Mine if affordable
  3. Train ALL idle citizens: spread evenly across workers + combat troops
     (workers + soldier + guard + spy + sentry in equal shares)
  4. Buy max-tier gear for every unit just trained (inline with training)
  5. Upgrade existing troops to max available gear tier with leftover gold
  6. Other buildings (Housing, Barracks, etc.) with remaining gold
  7. Battle upgrades when gear is fully maxed

Gold is NEVER held back — every tick spends as much as possible.
"""

import re, time, datetime, csv, os, json, subprocess, sys, math
from playwright.sync_api import sync_playwright

def _unhide(path):
    """Remove hidden/read-only attribute before writing (Windows cleanup.bat may have set +h)."""
    if sys.platform == "win32" and os.path.isfile(path):
        try:
            import ctypes
            FILE_ATTRIBUTE_NORMAL = 0x80
            ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_NORMAL)
        except Exception:
            pass

AUTH_FILE   = "auth.json"
BASE_URL    = "https://darkthronegame.com/game"
LOG_FILE    = "private_optimizer_log.csv"
STATE_FILE  = "private_optimizer_state.json"
GROWTH_FILE = "private_optimizer_growth.json"
CHART_FILE  = "optimizer_chart.html"

# ── Unit IDs (from dump_training.html) ───────────────────────────────────────
UNIT_ID   = {"worker":1, "soldier":4, "guard":8, "spy":12, "sentry":15}
UNIT_COST = {"worker":2000, "soldier":1500, "guard":1500, "spy":2500, "sentry":2500}

# ── Gear tables: (name, stat, buy_cost) per tier ──────────────────────────────
GEAR = {
    ("guard",   "weapon"): {1:("Sling",25,12500),2:("Hatchet",50,25000),3:("Spear",100,50000),4:("Javelin",150,75000),5:("Crossbow",200,100000),6:("Heavy Crossbow",275,137500),7:("Ballista Bolt",350,175000),8:("Greek Fire",450,225000),9:("Scorpion",550,275000),10:("Ballista",700,350000)},
    ("guard",   "armor"):  {1:("Padded Armor",19,9500),2:("Leather Armor",38,19000),3:("Studded Leather DEF",75,37500),4:("Bronze Chainmail DEF",120,60000),5:("Iron Chainmail DEF",180,90000),6:("Steel Chainmail DEF",250,125000),7:("Bronze Plate DEF",350,175000),8:("Iron Plate DEF",450,225000),9:("Steel Plate DEF",575,287500),10:("Mithril Plate DEF",750,375000)},
    ("soldier", "weapon"): {1:("Dagger",25,12500),2:("Hatchet OFF",50,25000),3:("Quarterstaff",100,50000),4:("Mace OFF",150,75000),5:("Short Sword",200,100000),6:("Long Sword",275,137500),7:("Broad Sword",350,175000),8:("Battle Axe",450,225000),9:("Great Sword",550,275000),10:("War Hammer",700,350000)},
    ("soldier", "armor"):  {1:("Padded Armor OFF",19,9500),2:("Leather Armor OFF",38,19000),3:("Studded Leather OFF",75,37500),4:("Bronze Chainmail OFF",120,60000),5:("Iron Chainmail OFF",180,90000),6:("Steel Chainmail OFF",250,125000),7:("Bronze Plate OFF",350,175000),8:("Iron Plate OFF",450,225000),9:("Steel Plate OFF",575,287500),10:("Mithril Plate OFF",750,375000)},
    ("sentry",  "weapon"): {1:("Club",12,6000),2:("Hatchet SD",25,12500),3:("Mace SD",50,25000),4:("Morning Star",80,40000),5:("Flail",120,60000),6:("War Pick",170,85000),7:("Guard Pike",230,115000),8:("Sentinel Hammer",300,150000),9:("Inquisitor Blade",380,190000),10:("Nullifier",480,240000)},
    ("sentry",  "armor"):  {1:("Padded Guard Vest",12,6000),2:("Leather Guard Armor",25,12500),3:("Studded Guard Armor",50,25000),4:("Bronze Guard Plate",80,40000),5:("Iron Guard Plate",120,60000),6:("Steel Guard Plate",170,85000),7:("Warden Plate",230,115000),8:("Sentinel Bulwark",300,150000),9:("Inquisitor Shield",380,190000),10:("Aegis of Vigilance",480,240000)},
    ("spy",     "weapon"): {1:("Throwing Knife",12,6000),2:("Garrote Wire",25,12500),3:("Blowgun",50,25000),4:("Poison Dagger",80,40000),5:("Stiletto",120,60000),6:("Shadow Blade",170,85000),7:("Assassin Crossbow",230,115000),8:("Nightblade",300,150000),9:("Wrist Blade",380,190000),10:("Void Dagger",480,240000)},
    ("spy",     "armor"):  {1:("Dark Cloak",12,6000),2:("Shadow Vest",25,12500),3:("Infiltrator Garb",50,25000),4:("Nightstalker Suit",80,40000),5:("Phantom Cloak",120,60000),6:("Assassin Leathers",170,85000),7:("Shadow Weave",230,115000),8:("Void Shroud",300,150000),9:("Wraithcloak",380,190000),10:("Shadowmeld Armor",480,240000)},
}

ARMORY_MAX_TIER = {0:3, 1:5, 2:7, 3:8, 4:9, 5:10}
ARMORY_TAB = {"guard":"defense","soldier":"offense","sentry":"spy-defense","spy":"spy-offense"}

# Legacy STRATEGIES dict and DEFAULT_STRATEGY constant were removed
# 2026-04-16 after the decide_v2 engine proved stable. The 3 new profiles
# (grow / combat / defend) live in STRATEGY_WEIGHTS near decide_v2 below.
# Legacy config values (balanced, attack, spy, ...) are auto-migrated to
# the new keys via _normalize_strategy_key().

def load_strategy():
    """Read chosen strategy from user_config.json. Defaults to 'grow'.
    Legacy values (balanced, attack, spy, ...) are re-mapped to the
    3-profile decide_v2 set by _normalize_strategy_key() at call time."""
    cfg_file = "user_config.json"
    if os.path.isfile(cfg_file):
        try:
            with open(cfg_file, encoding="utf-8") as f:
                return json.load(f).get("strategy", "grow")
        except Exception as _e:
            print(f"  ⚠️  load_strategy: {cfg_file} unreadable ({_e}) — using default 'grow'")
    return "grow"

# Buildings in income priority, then citizen-growth, then power
BUILDINGS = [
    ("Mine",           3,  150_000, 5, "income"),
    ("Housing",        3,  100_000, 5, "citizens"),
    ("Spy Academy",    5,  250_000, 5, "spy"),
    ("Mercenary Camp", 7,  200_000, 3, "mercs"),
    ("Barracks",       8,  400_000, 5, "citizens"),
    ("Fortification",  10, 500_000, 5, "power"),
    ("Armory",         10, 750_000, 5, "power"),
]

# Building prerequisites: (building, target_level) → {required_building: required_level}
# Confirmed from game UI screenshots
BUILDING_PREREQ = {
    ("Mine", 2): {"Fortification": 1},
    ("Mine", 3): {"Fortification": 2},
    ("Mine", 4): {"Fortification": 3},
    ("Mine", 5): {"Fortification": 4},
}

# ── Helpers ────────────────────────────────────────────────────────────────────
def num(s):
    """Parse a number string, handling K/M/B suffixes (e.g. '1.19M' → 1190000)."""
    s = str(s or "").strip().replace(",", "")
    m = re.match(r'^([\d.]+)\s*([KkMmBb])?$', s)
    if m:
        val = float(m.group(1))
        suffix = (m.group(2) or "").upper()
        if suffix == "K": val *= 1_000
        elif suffix == "M": val *= 1_000_000
        elif suffix == "B": val *= 1_000_000_000
        return int(val)
    # Fallback: strip all non-digits
    return int(re.sub(r"\D", "", s) or 0)

def strip(html):
    t = re.sub(r'<[^>]*>',' ',html)
    t = re.sub(r'&amp;','&',t); t = re.sub(r'&gt;','>',t); t = re.sub(r'&lt;','<',t)
    return re.sub(r'\s+',' ',t)

def find(text, pat, d=0):
    m = re.search(pat, text)
    return num(m.group(1)) if m else d

def load_state():
    if os.path.isfile(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {"ticks":0}

def save_state(s):
    _unhide(STATE_FILE)
    with open(STATE_FILE,"w") as f: json.dump(s,f,indent=2)

def log(action, detail, g_b, g_a):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    new = not os.path.isfile(LOG_FILE)
    _unhide(LOG_FILE)
    with open(LOG_FILE,"a",newline="",encoding="utf-8") as f:
        w = csv.writer(f)
        if new: w.writerow(["Timestamp","Action","Detail","GoldBefore","GoldAfter"])
        w.writerow([ts,action,detail,g_b,g_a])
    print(f"    📝 {action}: {detail} | {g_b:,}→{g_a:,}g")

# ── Lightweight live header read ───────────────────────────────────────────────
def read_live_header(page) -> dict:
    """Read ONLY the .stat-item header values on whatever page is currently
    loaded.  No navigation, no overview parse — safe to call in a tight loop.

    Returns {'gold', 'turns', 'citizens', 'level', 'xp', 'xp_need'}.
    Missing values default to 0.  Used by both read_state() and battle_loop().
    """
    out = {"gold": 0, "turns": 0, "citizens": 0, "level": 0, "xp": 0, "xp_need": 0}
    for sel, key in [
        (".stat-item[title='Gold'] .stat-value",     "gold"),
        (".stat-item[title='Citizens'] .stat-value", "citizens"),
        (".stat-item[title='Turns'] .stat-value",    "turns"),
        (".stat-item[title='Level'] .stat-value",    "level"),
        (".stat-item[title='XP'] .stat-value",       "xp"),
    ]:
        try:
            el = page.query_selector(sel)
            if el:
                out[key] = num(el.inner_text())
        except Exception:
            pass
    return out


# ── Read full game state ───────────────────────────────────────────────────────
def read_state(page):
    # ── 1. OVERVIEW — combat stats, economy, level, XP, gear Arsenal counts ──
    page.goto(f"{BASE_URL}/overview")
    page.wait_for_load_state("networkidle", timeout=15000)
    t = strip(page.content())

    # Number pattern: matches plain integers with commas OR abbreviated e.g. 1.19M
    _N = r'([\d,]+(?:\.\d+)?[KkMmBb]?)'
    s = {
        "atk":        find(t, r'Offense\s+'     + _N),
        "def":        find(t, r'Defense\s+'     + _N),
        "spy_off":    find(t, r'Spy ATK\s+'     + _N),
        "spy_def":    find(t, r'Spy DEF\s+'     + _N),
        "income":     find(t, _N + r'\s*gold/turn'),
        "gold":       find(t, r'Gold on Hand\s+'+ _N),
        "bank":       find(t, r'Banked Gold\s+' + _N),
        "turns":      find(t, r'Turns\s+([\d,]+)'),
        "mine_lv":    find(t, r'Basic Mine[^L]*Lv\.(\d+)') or
                      (find(t, r'Mine Bonus\s+\+(\d+)%') // 10),
        "housing_lv": find(t, r'Huts[^L]*Lv\.(\d+)'),
        "citizens":   find(t, r'Gold\s+[\d,]+\s+Citizens\s+(\d+)\s+Citizens'),
        "level":      find(t, r'Lvl\s+(\d+)\s+Level'),
        "xp":         find(t, r'(\d[\d,]*)\s+XP\s+[\d,]+\s+XP needed'),
        "xp_need":    find(t, r'[\d,]+\s+XP\s+([\d,]+)\s+XP needed'),
        "xp_pct":     find(t, r'([\d.]+)%\s+to Level'),
        # Army — overview fallback; training page gives exact counts below
        "workers":    find(t, r'Workers\s+([\d,]+)'),
        "soldiers":   find(t, r'Soldier\s+(\d+)'),
        "guards":     find(t, r'Guard\s+(\d+)'),
        "spies":      find(t, r'(?:^| )Spy\s+(\d+)'),
        "sentries":   find(t, r'Sentry\s+(\d+)'),
        "deposits":   0,
    }
    if s["citizens"] == 0:
        m = re.search(r'(\d+)\s+Citizens\s+[\d,]+\s+Turns', t)
        s["citizens"] = num(m.group(1)) if m else 0
    if s["level"] == 0:
        m = re.search(r'(\d+)\s+Level\s+[\d,]+\s+XP', t)
        s["level"] = num(m.group(1)) if m else 0

    # Header bar CSS selectors — most accurate values (delegates to helper
    # so battle_loop can reuse the same read without the whole overview parse).
    hdr = read_live_header(page)
    for key in ("gold", "citizens", "turns", "level"):
        v = hdr.get(key, 0)
        if v or key == "citizens":
            s[key] = v

    # Ranks are NOT on the overview page — read_own_ranks() fetches them
    # from /profile/{id} after save_state() using .rank-item CSS selectors
    s["rank_overall"] = 0
    s["rank_offense"] = 0
    s["rank_defense"] = 0
    s["rank_wealth"]  = 0

    # Gear owned counts from Arsenal section in overview text
    def gear_count(pat):
        m = re.search(pat, t)
        return num(m.group(1)) if m else 0

    # Sum ALL tier quantities for a unit/slot instead of reading only one
    # hardcoded item name.  The old approach (e.g. "Blowgun x(\d+)" for spy
    # weapons) breaks once a player upgrades past T3: the T3 count drops to
    # zero, the code calculates a full gap, and keeps buying unnecessary gear.
    # With sum_gear() the total owned is correct even when multiple tiers coexist:
    #   e.g. 71 Stiletto (T5) + 1 Poison Dagger (T4) + 59 Blowgun (T3) = 131
    #   min(131, 90 units) = 90 → gap = 0 → nothing to buy.
    def sum_gear(unit, slot):
        return sum(gear_count(rf'{re.escape(name)} x(\d+)')
                   for name, _, _ in GEAR.get((unit, slot), {}).values())

    s["_gear_owned"] = {
        ("soldier","weapon"): sum_gear("soldier","weapon"),
        ("soldier","armor"):  sum_gear("soldier","armor"),
        ("guard",  "weapon"): sum_gear("guard",  "weapon"),
        ("guard",  "armor"):  sum_gear("guard",  "armor"),
        ("spy",    "weapon"): sum_gear("spy",    "weapon"),
        ("spy",    "armor"):  sum_gear("spy",    "armor"),
        ("sentry", "weapon"): sum_gear("sentry", "weapon"),
        ("sentry", "armor"):  sum_gear("sentry", "armor"),
    }

    # ── 2. TRAINING — exact owned counts (untrain max) + citizens (train max) ──
    page.goto(f"{BASE_URL}/train")
    page.wait_for_load_state("networkidle", timeout=10000)
    train_js = page.evaluate("""() => {
        const owned = {}, trainable = {};
        document.querySelectorAll('input.multi-untrain-input[data-unit-id]').forEach(inp => {
            owned[inp.dataset.unitId] = parseInt(inp.dataset.max || '0');
        });
        document.querySelectorAll('input.multi-qty-input[data-unit-id]').forEach(inp => {
            trainable[inp.dataset.unitId] = parseInt(inp.dataset.max || '0');
        });
        return {owned, trainable};
    }""")
    uid_map = {"1":"workers","4":"soldiers","8":"guards","12":"spies","15":"sentries"}
    for uid, cnt in train_js["owned"].items():
        key = uid_map.get(uid)
        if key and cnt > 0:
            s[key] = cnt
    # Citizens = max trainable workers (idle population not yet assigned)
    citizens_from_train = train_js["trainable"].get("1", 0)
    if citizens_from_train > 0:
        s["citizens"] = citizens_from_train

    # Build gear dict — cap owned at unit count (regex can over-read)
    g = s["_gear_owned"]
    def _owned(unit_key, slot):
        units = s[unit_key]
        return (min(g[(unit_key.rstrip("s") if unit_key != "sentries" else "sentry", slot)], units), units)
    # Build explicitly to avoid key-name confusion
    s["gear"] = {
        ("soldier","weapon"): (min(g[("soldier","weapon")], s["soldiers"]), s["soldiers"]),
        ("soldier","armor"):  (min(g[("soldier","armor")],  s["soldiers"]), s["soldiers"]),
        ("guard",  "weapon"): (min(g[("guard","weapon")],   s["guards"]),   s["guards"]),
        ("guard",  "armor"):  (min(g[("guard","armor")],    s["guards"]),   s["guards"]),
        ("spy",    "weapon"): (min(g[("spy","weapon")],     s["spies"]),    s["spies"]),
        ("spy",    "armor"):  (min(g[("spy","armor")],      s["spies"]),    s["spies"]),
        ("sentry", "weapon"): (min(g[("sentry","weapon")],  s["sentries"]), s["sentries"]),
        ("sentry", "armor"):  (min(g[("sentry","armor")],   s["sentries"]), s["sentries"]),
    }

    # ── 3. ARMORY — owned tier (sell rows) + max buyable tier (buy rows) ─────
    page.goto(f"{BASE_URL}/armory")
    page.wait_for_selector(".armory-page", timeout=15000)
    armory_js = page.evaluate("""() => {
        const owned = {}, buyable = {};
        document.querySelectorAll('tr.sell-row:not(.disabled)').forEach(row => {
            const tier = parseInt(row.dataset.tier || '0');
            const inp  = row.querySelector('input[name="item_id"]');
            if (inp && tier > 0) {
                const id = parseInt(inp.value);
                if (!owned[id] || tier > owned[id]) owned[id] = tier;
            }
        });
        document.querySelectorAll('tr.buy-row:not(.disabled)').forEach(row => {
            const tier = parseInt(row.dataset.tier || '0');
            const inp  = row.querySelector('input[name="item_id"]');
            if (inp && tier > 0) {
                const id = parseInt(inp.value);
                if (!buyable[id] || tier > buyable[id]) buyable[id] = tier;
            }
        });
        return {owned, buyable};
    }""")
    _id_to_key = {v: k for k, v in ITEM_ID.items()}
    s["gear_tier"] = {}
    s["max_buyable_tier"] = {}
    for id_str, tier in armory_js["owned"].items():
        key = _id_to_key.get(int(id_str))
        if key:
            unit, slot, _ = key
            s["gear_tier"][(unit, slot)] = max(s["gear_tier"].get((unit, slot), 0), tier)
    for id_str, tier in armory_js["buyable"].items():
        key = _id_to_key.get(int(id_str))
        if key:
            unit, slot, _ = key
            s["max_buyable_tier"][(unit, slot)] = max(s["max_buyable_tier"].get((unit, slot), 0), tier)
    for unit in ("guard","soldier","spy","sentry"):
        for slot in ("weapon","armor"):
            s["gear_tier"].setdefault((unit, slot), 1)
            s["max_buyable_tier"].setdefault((unit, slot), 1)

    # ── 4. UPGRADES — battle upgrades owned + buyable ─────────────────────────
    page.goto(f"{BASE_URL}/upgrades")
    page.wait_for_load_state("networkidle", timeout=10000)
    upg_js = page.evaluate("""() => {
        const owned = {}, buyable = {};
        // Owned: sell-mode rows that are NOT disabled
        document.querySelectorAll('.sell-mode tr:not(.disabled) td').length;
        document.querySelectorAll('.sell-mode tr:not(.disabled)').forEach(row => {
            const cells = row.querySelectorAll('td');
            if (cells.length < 4) return;
            const name  = cells[0].querySelector('strong')?.innerText?.trim();
            const count = parseInt(cells[3]?.innerText?.trim() || '0');
            if (name && count > 0) owned[name] = count;
        });
        // Buyable: buy-mode rows that are NOT disabled
        document.querySelectorAll('.buy-mode tr:not(.disabled)').forEach(row => {
            const cells = row.querySelectorAll('td');
            if (cells.length < 4) return;
            const name = cells[0].querySelector('strong')?.innerText?.trim();
            const cost = parseInt((cells[2]?.innerText || '').replace(/[^0-9]/g,'') || '0');
            const owned_max = cells[3]?.innerText?.trim() || '';
            if (name && cost > 0) buyable[name] = {cost, owned_max};
        });
        return {owned, buyable};
    }""")
    s["upgrades_owned"]   = upg_js["owned"]
    s["upgrades_buyable"] = upg_js["buyable"]

    # ── 5. BUILDINGS — all building levels + upgradability via DOM ────────────
    # Scrape the /buildings page and extract per-card:
    #   • level / max (from `.status-value` text "N / M")
    #   • locked / upgradable / needs-gold (from card CSS classes — the game
    #     itself computes whether player-level / prereq / gold requirements
    #     are met and reflects it in the class list)
    #   • cost (from the `data-full` attribute on `.abbreviated-number` —
    #     exact integer, not the "75.00M" display format)
    #   • requirements text (for logging / dashboard display)
    #
    # This avoids hardcoded per-level gate tables: we trust the game's own
    # "upgradable" / "locked" signal instead of maintaining est_mine_lv() /
    # est_armory_lv() / etc.
    page.goto(f"{BASE_URL}/buildings")
    page.wait_for_load_state("networkidle", timeout=10000)
    bldg_js = page.evaluate(r"""() => {
        const result = {};
        document.querySelectorAll('input[name="building_type_id"]').forEach(inp => {
            const id = parseInt(inp.value);
            const form = inp.closest('form');
            let card = inp.parentElement;
            for (let i = 0; i < 10 && card; i++) {
                if (card.classList && card.classList.contains('building-card')) break;
                card = card.parentElement;
            }
            if (!card) return;
            // Level / max
            let level = 0, max = 0;
            const sv = card.querySelector('.status-value');
            if (sv) {
                const m = sv.innerText.match(/(\d+)\s*\/\s*(\d+)/);
                if (m) { level = parseInt(m[1]); max = parseInt(m[2]); }
            }
            // Upgrade eligibility from card class list
            const cls = card.className || '';
            const locked      = cls.includes('locked');
            const upgradable  = cls.includes('upgradable');
            const needs_gold  = cls.includes('needs-gold');
            // Exact cost from data-full attribute
            let cost = 0;
            const full = card.querySelector('.abbreviated-number[data-full]');
            if (full) {
                const raw = (full.getAttribute('data-full') || '').replace(/,/g, '');
                cost = parseInt(raw) || 0;
            }
            // Requirements text (for display)
            const reqs = Array.from(card.querySelectorAll('.req-item'))
                .map(e => (e.innerText || '').trim())
                .filter(Boolean);
            // Submit button disabled state (belt-and-braces)
            const btn = form ? form.querySelector('button[type="submit"]') : null;
            const disabled = btn ? (btn.disabled || btn.classList.contains('disabled')) : true;
            result[id] = {level, max, locked, upgradable, needs_gold,
                          cost, reqs, disabled};
        });
        return result;
    }""")
    # BUILDING_TYPE_ID is {name: int_id} (line ~1310).  Invert to {int_id: name}
    # so we can resolve the scraper's string-keyed id dict back to names.
    id_to_bname = {v: k for k, v in BUILDING_TYPE_ID.items()}
    s["buildings"]      = {}
    s["buildings_meta"] = {}
    for id_str, info in bldg_js.items():
        bname = id_to_bname.get(int(id_str))
        if not bname:
            continue
        s["buildings"][bname]      = info["level"]
        s["buildings_meta"][bname] = {
            "level":      info["level"],
            "max":        info["max"],
            "locked":     bool(info["locked"]),
            "upgradable": bool(info["upgradable"]),
            "needs_gold": bool(info["needs_gold"]),
            "cost":       int(info["cost"]),
            "reqs":       info["reqs"],
            "disabled":   bool(info["disabled"]),
        }
    # Fallbacks from overview text (in case DOM scrape missed something)
    if not s["buildings"].get("Mine") and s.get("mine_lv"):
        s["buildings"]["Mine"] = s["mine_lv"]
    if not s["buildings"].get("Housing") and s.get("housing_lv"):
        s["buildings"]["Housing"] = s["housing_lv"]

    # ── 6. FORT — current HP, max HP, fort level ──────────────────────────────
    page.goto(f"{BASE_URL}/fort")
    page.wait_for_load_state("networkidle", timeout=10000)
    fort_js = page.evaluate("""() => {
        const r = {hp: 100, max_hp: 100, fort_lv: 0, cost_per_hp: 16.75};
        document.querySelectorAll('.fort-stat-box').forEach(box => {
            const label = (box.querySelector('.fort-stat-label')?.innerText || '').trim();
            const val   = (box.querySelector('.fort-stat-value')?.innerText || '').trim();
            const n = parseInt(val.replace(/[^0-9]/g,'')) || 0;
            if (label.includes('Current Health'))      r.hp       = n;
            else if (label.includes('Maximum Health')) r.max_hp   = n;
            else if (label.includes('Fortification'))  r.fort_lv  = n;
        });
        // cost_per_hp is hardcoded in the page JS
        const scripts = Array.from(document.scripts).map(s => s.innerText || s.textContent);
        for (const sc of scripts) {
            const m = sc.match(/costPerHp\\s*=\\s*([\\d.]+)/);
            if (m) { r.cost_per_hp = parseFloat(m[1]); break; }
        }
        return r;
    }""")
    s["fort_hp"]      = fort_js["hp"]
    s["fort_max_hp"]  = fort_js["max_hp"]
    s["fort_pct"]     = round(fort_js["hp"] / max(fort_js["max_hp"], 1) * 100)
    s["fort_lv"]      = fort_js["fort_lv"]
    s["cost_per_hp"]  = fort_js["cost_per_hp"]

    # ── 7. BANK — deposit count + max deposit allowed this transaction ────────
    page.goto(f"{BASE_URL}/bank")
    page.wait_for_selector(".card", timeout=10000)
    bank_js = page.evaluate("""() => {
        const text = document.body.innerText || '';
        const m = text.match(/(\\d+)\\s*\\/\\s*6/);
        const inp = document.querySelector('#deposit_amount, input[name="amount"]');
        const maxDeposit = inp ? parseInt(inp.getAttribute('max') || '0') : 0;
        return {
            deposits: m ? parseInt(m[1]) : 0,
            deposit_max: maxDeposit
        };
    }""")
    s["deposits"]    = bank_js["deposits"]
    s["deposit_max"] = bank_js["deposit_max"]  # 80% of current gold on hand

    # Override Mine level with overview value if buildings page parsing fails
    if s["buildings"].get("Mine", 0) == 0 and s.get("mine_lv", 0) > 0:
        s["buildings"]["Mine"] = s["mine_lv"]
    if s["buildings"].get("Housing", 0) == 0 and s.get("housing_lv", 0) > 0:
        s["buildings"]["Housing"] = s["housing_lv"]

    return s

# ── ANALYSIS ENGINE ────────────────────────────────────────────────────────────
def analyse(s):
    """
    Score each combat category 0.0→1.0.
    score = (fully_equipped_ratio) × (gear_tier_ratio)
    Returns sorted list of categories from weakest to strongest.
    """
    max_buyable = s.get("max_buyable_tier", {})

    cats = []
    for unit in ["guard", "sentry", "soldier", "spy"]:
        w_max_t = max_buyable.get((unit, "weapon"), 1)
        a_max_t = max_buyable.get((unit, "armor"),  1)
        max_t   = max(w_max_t, a_max_t)  # display ceiling

        w_owned, w_units = s["gear"].get((unit,"weapon"), (0, 0))
        a_owned, a_units = s["gear"].get((unit,"armor"),  (0, 0))

        # Fallback: if armory read failed (units=0), use overview counts
        unit_key = {"guard":"guards","sentry":"sentries","soldier":"soldiers","spy":"spies"}[unit]
        units = w_units or a_units or s.get(unit_key, 0)

        if units == 0:
            cats.append({"unit":unit,"score":1.0,"units":0,"w_gap":0,"a_gap":0,
                         "max_t":max_t,"w_max_t":w_max_t,"a_max_t":a_max_t,
                         "w_tier":w_max_t,"a_tier":a_max_t,"fully_maxed":True})
            continue

        w_tier = s["gear_tier"].get((unit,"weapon"), 1)
        a_tier = s["gear_tier"].get((unit,"armor"),  1)

        # When armory read fails (w_owned=0, w_units=0), assume all units need gear
        # This is safe — buying gear for already-geared units is blocked by "max" on the buy form
        if w_units == 0 and units > 0:
            w_owned = 0
        if a_units == 0 and units > 0:
            a_owned = 0

        w_gap = max(0, units - w_owned)
        a_gap = max(0, units - a_owned)

        equip_ratio = min(w_owned, a_owned) / units
        tier_ratio  = min(1.0, min(w_tier / max(w_max_t, 1), a_tier / max(a_max_t, 1)))

        score = equip_ratio * tier_ratio
        fully_maxed = (w_gap == 0 and a_gap == 0 and w_tier >= w_max_t and a_tier >= a_max_t)

        cats.append({
            "unit": unit, "score": score, "units": units,
            "w_gap": w_gap, "a_gap": a_gap,
            "w_owned": w_owned, "a_owned": a_owned,
            "max_t": max_t, "w_max_t": w_max_t, "a_max_t": a_max_t,
            "w_tier": w_tier, "a_tier": a_tier,
            "fully_maxed": fully_maxed,
            "equip_ratio": equip_ratio, "tier_ratio": tier_ratio,
        })

    cats.sort(key=lambda c: c["score"])
    return cats

# ── DECISION ENGINE ────────────────────────────────────────────────────────────
def _gear_cost_for_unit(s, unit):
    """Return (weapon_cost, armor_cost) at max buyable tier for a unit type."""
    max_buyable = s.get("max_buyable_tier", {})
    mt_w = max_buyable.get((unit, "weapon"), 1)
    mt_a = max_buyable.get((unit, "armor"),  1)
    wc = GEAR[(unit, "weapon")].get(mt_w, (None, None, 0))[2]
    ac = GEAR[(unit, "armor") ].get(mt_a, (None, None, 0))[2]
    return wc, ac, mt_w, mt_a


# ──────────────────────────────────────────────────────────────────────
# Legacy decide() function (7-stage priority pipeline) removed
# 2026-04-16 after decide_v2 (below) proved stable. See git history
# if you need to reference the old logic.
# ──────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# MARGINAL-VALUE DECISION ENGINE  (decide_v2)
# ══════════════════════════════════════════════════════════════════════════════
#
# Replaces the hardcoded 7-stage priority pipeline with a generate-score-greedy
# loop that compares every possible spend (build, buy gear, upgrade gear, train,
# battle upgrade) on a single "net-worth per gold" axis.  Strategies bias the
# objective function instead of rearranging stages, so tuning is one dict of
# weights instead of a pile of branches.
#
# Key contract: every option dict emitted by a generator has the same shape as
# the action dicts the existing execute() / _build() / _buy_gear() / _train() /
# _buy_upgrade() / _repair_fort() / _bank() functions already consume.  No
# changes to execute() are required — decide_v2 output is drop-in compatible.

# How many ticks into the future we assume a spend will pay back over.
# BOTH income and stat gains persist — income compounds per tick, and a
# gear/tier upgrade stays installed on every future tick too.  So we treat
# all persistent benefits with the same horizon multiplier.  200 ticks ≈
# 100 hours at 30-min cadence ≈ one game-day of useful lifetime.
HORIZON_TICKS = 200

# Strategy weights for the scoring function.  All weights are in "gold
# equivalent per unit of benefit per tick".  Income is naturally gold/tick,
# so w_income=1.0 is the identity.  Stats are valued via a rough "gold per
# stat point per tick" conversion — 1 ATK point earning roughly 0.2g per
# tick of auto-farming is the Grow calibration point.  Weights then
# rescale that for Combat (ATK-heavy) and Defend (DEF-heavy) profiles.
#
# Keys must match STRATEGY_LABELS in src/installer/darkthrone_app.py.
# Legacy names are remapped via _normalize_strategy_key() below.
STRATEGY_WEIGHTS = {
    # Grow: net-worth focus.  Income dominates, stats are secondary.
    # A Mine/Housing upgrade always beats a gear upgrade in Grow.
    "grow":   {"w_income": 1.0, "w_atk": 0.2, "w_def": 0.1, "w_xp": 1.0},

    # Combat: ATK-heavy.  Income halved, ATK x5 vs Grow.
    "combat": {"w_income": 0.4, "w_atk": 1.0, "w_def": 0.2, "w_xp": 0.5},

    # Defend: DEF-heavy.  Same shape but DEF x10 vs Grow.
    "defend": {"w_income": 0.4, "w_atk": 0.1, "w_def": 1.0, "w_xp": 0.5},
}
DEFAULT_STRATEGY_V2 = "grow"

_LEGACY_STRAT_MAP = {
    "balanced": "grow",
    "economy":  "grow",
    "attack":   "combat",
    "spy":      "combat",
    "defense":  "defend",
    "hybrid":   "defend",
}

def _normalize_strategy_key(key):
    """Return a valid decide_v2 strategy key, mapping legacy names if needed."""
    if not key:
        return DEFAULT_STRATEGY_V2
    key = str(key).strip().lower()
    if key in STRATEGY_WEIGHTS:
        return key
    return _LEGACY_STRAT_MAP.get(key, DEFAULT_STRATEGY_V2)


# ── Scoring ───────────────────────────────────────────────────────────────────
def score_option(opt, w):
    """Return net-worth-equivalent-per-gold for an option dict.

    All three persistent benefits (income/atk/def) are multiplied by
    HORIZON_TICKS because they all produce value every tick for the
    lifetime of the spend — income literally pays out, stat gains earn
    gold indirectly via auto-farming.  The strategy weights convert stat
    points to 'gold-equivalent per tick' so they can sit on the same axis
    as income.  xp_delta is one-shot (level progress) and not multiplied.
    """
    b = opt.get("benefits", {})
    value = (
        float(b.get("income_delta", 0)) * w.get("w_income", 0) * HORIZON_TICKS
        + float(b.get("atk_delta",   0)) * w.get("w_atk",    0) * HORIZON_TICKS
        + float(b.get("def_delta",   0)) * w.get("w_def",    0) * HORIZON_TICKS
        + float(b.get("xp_delta",    0)) * w.get("w_xp",     0)
    )
    cost = max(int(opt.get("cost", 0)), 1)
    return value / cost


# ── Unit stat helpers (used by option generators) ────────────────────────────
def _gear_stat(unit, slot, tier):
    """Return the stat value for a given gear piece."""
    if unit in ("spy", "sentry"):
        table = SPY_WEAPON if slot == "weapon" else SPY_ARMOR
    else:
        table = WEAPON_STATS if slot == "weapon" else ARMOR_STATS
    return table.get(tier, 0)

def _unit_stat_adds_to(unit):
    """Return ('atk'|'def'|'spy_off'|'spy_def') — which stat this unit type
    contributes to when given gear.  Used to route gear stat deltas."""
    return {
        "soldier": "atk",
        "guard":   "def",
        "spy":     "atk",   # spy_off — we lump into atk for scoring simplicity
        "sentry":  "def",   # spy_def — lumped into def
    }.get(unit, "atk")

def _unit_base_stat(unit, player_lv):
    """Base stat per fully-trained unit at current max unit tier."""
    ut = max_unit_tier(player_lv)
    if unit == "soldier": return UNIT_OFF.get(ut, 0)
    if unit == "guard":   return UNIT_DEF.get(ut, 0)
    if unit == "spy":     return UNIT_SPY.get(min(ut, 3), 0)
    if unit == "sentry":  return UNIT_SENT.get(min(ut, 3), 0)
    return 0


# ── Option generators ─────────────────────────────────────────────────────────
def _max_building_lv_at_player_lv(bname, player_lv):
    """Return the highest building level the player is CURRENTLY allowed to
    own given their player level.  Uses the existing est_*_lv() gating
    functions (which encode confirmed in-game player-level requirements:
    Mine Lv2 needs Lv12, Fort Lv2 needs Lv20, Armory Lv2 needs Lv30, etc.).

    For buildings without a dedicated gate function (Housing, Barracks,
    Mercenary Camp), falls back to the max_lv from BUILDINGS — in practice
    these buildings have their initial req_lv in BUILDINGS but no confirmed
    per-level gates, so we allow the full range once req_lv is met."""
    if bname == "Mine":          return est_mine_lv(player_lv)
    if bname == "Armory":        return est_armory_lv(player_lv)
    if bname == "Fortification": return est_fort_lv(player_lv)
    if bname == "Spy Academy":   return est_spy_ac_lv(player_lv)
    # Housing / Barracks / Mercenary Camp: no per-level gate known.
    # Return max_lv from BUILDINGS so the only gate is req_lv + prereqs.
    for _bn, _rl, _bc, _ml, _bt in BUILDINGS:
        if _bn == bname:
            return _ml
    return 0


def _gen_build_options(state):
    """One option per eligible building-level upgrade.

    Upgrade eligibility is determined in this priority order:
      1. state['buildings_meta'][name]['upgradable'] from the live scrape.
         This is the authoritative signal — the game computes the class
         list from every possible gate (player level, prereq building,
         gold availability, etc.) so we trust it over any local estimate.
         We still generate options when `needs_gold` is True because the
         greedy planner will handle affordability across ticks.
      2. Fallback to est_*_lv() gates + BUILDINGS.req_lv when
         buildings_meta is missing (e.g., in unit tests or when the
         scraper failed to populate it).
    """
    out = []
    builds   = state.get("buildings", {})
    meta_all = state.get("buildings_meta", {})
    level    = state.get("level", 1)
    workers  = state.get("workers", 0)
    income   = state.get("income", 0)
    units    = sum(state.get(k, 0) for k in ("soldiers", "guards", "spies", "sentries"))

    for bname, req_lv, base_cost, max_lv, btype in BUILDINGS:
        cur_lv = builds.get(bname, 0)
        meta   = meta_all.get(bname)

        # Absolute ceiling from the BUILDINGS table (e.g., Mine maxes at Lv5).
        if cur_lv >= max_lv:
            continue

        if meta is not None:
            # LIVE-SCRAPE path: trust the game's upgradable flag.
            # 'locked' means the server has decided the level or prereq
            # requirements aren't met — skip entirely, don't waste a slot.
            if meta.get("locked"):
                continue
            if not meta.get("upgradable"):
                continue
            # 'needs_gold' is fine — we still propose the option and let
            # the greedy loop decide when to spend on it.
        else:
            # FALLBACK path (buildings_meta missing): old level-gate logic.
            if level < req_lv:
                continue
            reachable = _max_building_lv_at_player_lv(bname, level)
            if (cur_lv + 1) > reachable:
                continue
            prereq = BUILDING_PREREQ.get((bname, cur_lv + 1), {})
            if any(builds.get(b, 0) < req for b, req in prereq.items()):
                continue

        # Exact cost from the scrape, or formula if scrape didn't provide it.
        cost = (meta.get("cost", 0) if meta else 0) or base_cost * (cur_lv + 1)
        benefits = {"income_delta": 0, "atk_delta": 0, "def_delta": 0, "xp_delta": 0}

        if bname == "Mine":
            # Each mine level adds 10% to (base_inc + workers * WORKER_GOLD).
            # Income delta = current_base_production * 0.10
            base_prod = BASE_INC + workers * WORKER_GOLD
            benefits["income_delta"] = int(base_prod * 0.10)
        elif bname == "Housing":
            # Housing adds citizen slots; we assume ~80% of new slots become
            # workers that produce WORKER_GOLD/tick at current mine multiplier.
            # Slot count per level is ~100 (heuristic).
            new_slots = 100
            benefits["income_delta"] = int(
                new_slots * 0.80 * WORKER_GOLD * mine_mult(est_mine_lv(level))
            )
        elif bname == "Armory":
            # Armory unlocks higher gear tiers.  We approximate the stat gain
            # as "every existing army unit could tier up by ~50 stat points".
            # This is a rough surrogate for the real upgrade opportunity.
            benefits["atk_delta"] = units * 25
            benefits["def_delta"] = units * 25
        elif bname == "Fortification":
            # Fortification adds fort HP + unlocks higher unit tiers.
            benefits["def_delta"] = units * 20
        # Spy Academy, Mercenary Camp, Barracks: no direct scored benefit

        out.append({
            "type":     "BUILD",
            "cost":     cost,
            "benefits": benefits,
            "reason":   f"{bname} Lv{cur_lv}→Lv{cur_lv+1}",
            "name":     bname,
            "lv":       cur_lv + 1,
        })
    return out


def _gen_gear_buy_options(state, cats):
    """One option per (unit, slot) where units are currently under-equipped
    at their current tier.  Buys enough gear to fill the gap (or as much as
    current gold allows)."""
    out = []
    gold = state.get("gold", 0)
    for cat in cats:
        unit  = cat["unit"]
        units = cat["units"]
        if units <= 0:
            continue
        for slot, gap, tier in [
            ("weapon", cat["w_gap"], cat["w_tier"]),
            ("armor",  cat["a_gap"], cat["a_tier"]),
        ]:
            if gap <= 0 or tier not in GEAR.get((unit, slot), {}):
                continue
            name, stat, cost_per = GEAR[(unit, slot)][tier]
            if cost_per <= 0:
                continue
            qty = min(gap, gold // cost_per)
            if qty <= 0:
                continue
            total = cost_per * qty
            benefits = {"income_delta": 0, "atk_delta": 0, "def_delta": 0, "xp_delta": 0}
            key = "atk_delta" if _unit_stat_adds_to(unit) == "atk" else "def_delta"
            benefits[key] = stat * qty
            out.append({
                "type":     "BUY_GEAR",
                "cost":     total,
                "benefits": benefits,
                "reason":   f"Fill {qty}× {unit} {slot} gap @ T{tier}",
                "unit":     unit,
                "slot":     slot,
                "tier":     tier,
                "qty":      qty,
                "name":     name,
                "total":    total,
                "tab":      ARMORY_TAB[unit],
            })
    return out


def _gen_gear_upgrade_options(state, cats):
    """One option per (unit, slot, tier→tier+1) where the unit type has
    equipped gear below its max buyable tier."""
    out = []
    gold = state.get("gold", 0)
    for cat in cats:
        unit  = cat["unit"]
        units = cat["units"]
        if units <= 0:
            continue
        for slot in ("weapon", "armor"):
            cur_t    = cat["w_tier"] if slot == "weapon" else cat["a_tier"]
            slot_max = cat["w_max_t"] if slot == "weapon" else cat["a_max_t"]
            equipped = cat.get("w_owned" if slot == "weapon" else "a_owned", 0)
            if equipped <= 0 or cur_t >= slot_max:
                continue
            # Walk to next tier that exists in the GEAR table.
            next_t = cur_t + 1
            while next_t <= slot_max and next_t not in GEAR.get((unit, slot), {}):
                next_t += 1
            if next_t > slot_max:
                continue
            _, _, old_cost = GEAR[(unit, slot)].get(cur_t,  (None, 0, 0))
            new_name, new_stat, new_cost = GEAR[(unit, slot)][next_t]
            old_stat = _gear_stat(unit, slot, cur_t)
            delta_cost_per_unit = new_cost - old_cost
            if delta_cost_per_unit <= 0:
                continue
            qty = min(equipped, gold // delta_cost_per_unit)
            if qty <= 0:
                continue
            total = delta_cost_per_unit * qty
            stat_delta_total = (new_stat - old_stat) * qty
            benefits = {"income_delta": 0, "atk_delta": 0, "def_delta": 0, "xp_delta": 0}
            key = "atk_delta" if _unit_stat_adds_to(unit) == "atk" else "def_delta"
            benefits[key] = stat_delta_total
            out.append({
                "type":     "UPGRADE_GEAR",
                "cost":     total,
                "benefits": benefits,
                "reason":   f"Upgrade {qty}× {unit} {slot} T{cur_t}→T{next_t}",
                "unit":     unit,
                "slot":     slot,
                "tier":     next_t,
                "qty":      qty,
                "name":     new_name,
                "total":    total,
                "tab":      ARMORY_TAB[unit],
            })
    return out


def _gen_train_options(state, cats):
    """One option per unit type, training up to (idle_citizens) new units
    and immediately gearing them at max buyable tier."""
    out = []
    citizens = state.get("citizens", 0)
    gold     = state.get("gold", 0)
    level    = state.get("level", 1)
    mine_lv  = state.get("mine_lv", est_mine_lv(level))
    if citizens <= 0:
        return out

    for unit in ("worker", "soldier", "guard", "spy", "sentry"):
        if unit == "worker":
            # Train bare workers.  Income_delta per worker = WORKER_GOLD * mine_mult
            cost_per = UNIT_COST[unit]
            qty = min(citizens, gold // cost_per)
            if qty <= 0:
                continue
            total = cost_per * qty
            income_per = int(WORKER_GOLD * mine_mult(mine_lv))
            out.append({
                "type":     "TRAIN",
                "cost":     total,
                "benefits": {"income_delta": income_per * qty, "atk_delta": 0,
                             "def_delta": 0, "xp_delta": 0},
                "reason":   f"Train {qty} workers",
                "unit":     unit,
                "count":    qty,
            })
            continue

        # Combat unit: cost = UNIT_COST + weapon + armor at max buyable tier
        wc, ac, mt_w, mt_a = _gear_cost_for_unit(state, unit)
        full_cost = UNIT_COST[unit] + wc + ac
        if full_cost <= 0:
            continue
        qty = min(citizens, gold // full_cost)
        if qty <= 0:
            continue
        total = full_cost * qty

        # Benefits: base stat + weapon + armor, routed to atk or def.
        base_stat = _unit_base_stat(unit, level)
        weap_stat = _gear_stat(unit, "weapon", mt_w)
        arm_stat  = _gear_stat(unit, "armor",  mt_a)
        per_unit_stat = base_stat + weap_stat + arm_stat
        total_stat    = per_unit_stat * qty
        benefits = {"income_delta": 0, "atk_delta": 0, "def_delta": 0, "xp_delta": 0}
        key = "atk_delta" if _unit_stat_adds_to(unit) == "atk" else "def_delta"
        benefits[key] = total_stat
        out.append({
            "type":     "TRAIN",
            "cost":     total,
            "benefits": benefits,
            "reason":   f"Train {qty}× {unit} +T{mt_w}/T{mt_a} gear",
            "unit":     unit,
            "count":    qty,
            "w_max_t":  mt_w,
            "a_max_t":  mt_a,
        })
    return out


def _gen_battle_upgrade_options(state):
    """Battle upgrades (Strength, Constitution, etc.) multiply existing stats.
    Value scales with your current power, so they're valuable even early.
    Capped to 1 per tick by decide_v2 so they can't dominate the plan."""
    out = []
    upgrades = state.get("upgrades_buyable", {})
    if not upgrades:
        return out

    cur_atk    = state.get("atk",     0)
    cur_def    = state.get("def",     0)
    cur_income = state.get("income",  0)

    # Heuristic mapping: upgrade name → (stat bonus %, target stat).
    # Real percentages come from the game; these are conservative estimates.
    for upg_name, upg_info in upgrades.items():
        cost = upg_info.get("cost", 0) if isinstance(upg_info, dict) else upg_info
        if not cost or cost <= 0:
            continue
        benefits = {"income_delta": 0, "atk_delta": 0, "def_delta": 0, "xp_delta": 0}
        n = upg_name.lower()
        if "strength" in n or "offense" in n or "attack" in n:
            benefits["atk_delta"] = int(cur_atk * 0.05)
        elif "constitution" in n or "defense" in n or "defence" in n:
            benefits["def_delta"] = int(cur_def * 0.05)
        elif "charisma" in n or "income" in n or "wealth" in n:
            benefits["income_delta"] = int(cur_income * 0.05)
        else:
            # Unknown upgrade — assume balanced 3% boost.
            benefits["atk_delta"] = int(cur_atk * 0.03)
            benefits["def_delta"] = int(cur_def * 0.03)

        out.append({
            "type":     "BUY_UPGRADE",
            "cost":     cost,
            "benefits": benefits,
            "reason":   f"Battle upgrade: {upg_name}",
            "name":     upg_name,
            "qty":      1,
            "total":    cost,
        })
    return out


def generate_spend_options(state, cats):
    """Assemble all candidate spend options for this tick."""
    out = []
    out += _gen_build_options(state)
    out += _gen_gear_buy_options(state, cats)
    out += _gen_gear_upgrade_options(state, cats)
    out += _gen_train_options(state, cats)
    out += _gen_battle_upgrade_options(state)
    return out


# ── Main entry point ─────────────────────────────────────────────────────────
def decide_v2(s, cats, strategy=None):
    """Marginal-value decision engine.

    Returns (actions_list, gold_left).  Drop-in replacement for decide().

    Strategy bias is applied via STRATEGY_WEIGHTS[key] — weights multiply the
    per-option benefit values in score_option().  Legacy strategy keys are
    automatically mapped (balanced→grow, attack→combat, etc.).
    """
    strategy_key = _normalize_strategy_key(strategy or load_strategy())
    w = STRATEGY_WEIGHTS.get(strategy_key, STRATEGY_WEIGHTS[DEFAULT_STRATEGY_V2])
    label = {"grow": "📈  Grow", "combat": "⚔️  Combat", "defend": "🛡️  Defend"}[strategy_key]
    print(f"  📋 Strategy: {label} (decide_v2 engine)")

    gold_left = s["gold"]
    actions   = []

    # ── 1. Fort repair (unconditional safety pre-pass) ──────────────────────
    fort_dmg = s.get("fort_max_hp", 100) - s.get("fort_hp", 100)
    if fort_dmg > 0:
        repair_cost = int(fort_dmg * s.get("cost_per_hp", 16.75)) + 1
        if gold_left >= repair_cost:
            actions.append({
                "type":   "REPAIR_FORT",
                "damage": fort_dmg,
                "cost":   repair_cost,
                "reason": f"fort at {s.get('fort_pct', 100)}% HP",
            })
            gold_left -= repair_cost

    # ── 2. Generate, score, sort ────────────────────────────────────────────
    options = generate_spend_options(s, cats)
    for opt in options:
        opt["score"] = score_option(opt, w)
    options.sort(key=lambda o: -o["score"])

    # Optional verbose dump of the top-10 for tuning.
    if os.environ.get("DECIDE_VERBOSE"):
        print(f"  🔬 Top spend options (strategy={strategy_key}):")
        for opt in options[:10]:
            b = opt["benefits"]
            print(f"     score={opt['score']:8.2f}  cost={opt['cost']:>10,}  "
                  f"income+={b['income_delta']:>5}  atk+={b['atk_delta']:>6}  "
                  f"def+={b['def_delta']:>6}  {opt['reason']}")

    # ── 3. Greedy select within budget ──────────────────────────────────────
    MAX_ACTIONS_PER_TICK  = 12
    MAX_BATTLE_UPGRADES   = 1   # cap so one big upgrade doesn't eat all gold
    battle_upgrades_used  = 0

    for opt in options:
        if len(actions) >= MAX_ACTIONS_PER_TICK + 1:   # +1 for fort repair
            break
        # No score <= 0 break — keep scanning ALL options. A gear upgrade
        # that scores 0.001 is still better than hoarding gold. The loop
        # terminates naturally when all options are exhausted or unaffordable.
        if opt["cost"] > gold_left:
            continue   # unaffordable, try next
        if opt["cost"] <= 0:
            continue   # skip zero-cost options (defensive)
        if opt["type"] == "BUY_UPGRADE":
            if battle_upgrades_used >= MAX_BATTLE_UPGRADES:
                continue
            battle_upgrades_used += 1
        actions.append(opt)
        gold_left -= opt["cost"]

    # No banking — all gold should be spent on strategy items. If nothing
    # is available to buy, gold stays in hand for the next tick or auto-battle.

    return actions, gold_left


# ══════════════════════════════════════════════════════════════════════════════
# END MARGINAL-VALUE ENGINE
# ══════════════════════════════════════════════════════════════════════════════


# ── EXECUTE ACTIONS ────────────────────────────────────────────────────────────
def execute(page, actions, gold):
    succeeded = 0
    failed    = 0
    for a in actions:
        t = a["type"]
        if t in ("SAVE_FOR_BUILD","SAVE_FOR_GEAR","SAVE_FOR_UPGRADE","SAVE_FOR_REPAIR"): continue

        gold_before = gold
        if t == "BUILD":
            gold = _build(page, a, gold)
        elif t in ("BUY_GEAR","UPGRADE_GEAR"):
            gold = _buy_gear(page, a, gold)
        elif t == "TRAIN":
            gold = _train(page, a, gold)
        elif t == "REPAIR_FORT":
            gold = _repair_fort(page, a, gold)
        elif t == "BUY_UPGRADE":
            gold = _buy_upgrade(page, a, gold)
        elif t == "BANK":
            gold = _bank(page, a, gold)
        else:
            continue

        # Track: if gold didn't change, the executor silently failed
        if gold < gold_before:
            succeeded += 1
        else:
            failed += 1
            print(f"      ❌ {t} '{a.get('reason','')}' — gold unchanged (likely failed on page)")

    if succeeded or failed:
        total = succeeded + failed
        tag = "✅" if failed == 0 else "⚠️"
        print(f"  {tag} Execute result: {succeeded}/{total} succeeded"
              + (f", {failed} failed" if failed else ""))
    return gold

# Building type IDs (confirmed from dump_buildings.html)
BUILDING_TYPE_ID = {
    "Fortification":1, "Armory":2, "Mine":3, "Spy Academy":4,
    "Barracks":5, "Housing":6, "Mercenary Camp":7,
}

# Armory item IDs (confirmed from dump_armory.html)
# Pattern: off weapons 1-10, off armor 21-30, def weapons 61-70, def armor 71-80
# spy off weapons 81-90, spy off armor 91-100, spy def weapons 101-110, spy def armor 111-120
ITEM_ID = {}
for tier in range(1, 11):
    ITEM_ID[("soldier","weapon",tier)] = tier
    ITEM_ID[("soldier","armor", tier)] = 20 + tier
    ITEM_ID[("guard",  "weapon",tier)] = 60 + tier
    ITEM_ID[("guard",  "armor", tier)] = 70 + tier
    ITEM_ID[("spy",    "weapon",tier)] = 80 + tier
    ITEM_ID[("spy",    "armor", tier)] = 90 + tier
    ITEM_ID[("sentry", "weapon",tier)] = 100 + tier
    ITEM_ID[("sentry", "armor", tier)] = 110 + tier

def _build(page, a, gold):
    btype_id = BUILDING_TYPE_ID.get(a["name"])
    if not btype_id:
        print(f"      ⚠️  Unknown building: {a['name']}")
        return gold
    if gold < a["cost"]:
        print(f"      ⚠️  Cannot afford {a['name']} Lv{a['lv']} — need {a['cost']:,} have {gold:,}")
        return gold
    print(f"    🏗️  {a['name']} Lv{a['lv']} ({a['cost']:,}g) — {a['reason']}")
    try:
        page.goto(f"{BASE_URL}/buildings")
        page.wait_for_selector(".building-card", timeout=10000)
        hidden = page.query_selector(f"input[name='building_type_id'][value='{btype_id}']")
        if hidden:
            btn = hidden.evaluate_handle(
                "el => el.closest('form').querySelector('button[type=\"submit\"]')")
            if btn:
                # Check button is actually enabled before clicking
                is_disabled = btn.evaluate("el => el.disabled || el.classList.contains('disabled')")
                if is_disabled:
                    print(f"      ⚠️  Button disabled for {a['name']} — likely can't afford on server side")
                    return gold
                with page.expect_navigation(wait_until="networkidle", timeout=15000):
                    btn.click()
                log("BUILD", f"{a['name']} Lv{a['lv']}", gold, gold - a["cost"])
                return gold - a["cost"]
        print(f"      ⚠️  building_type_id={btype_id} not found on page")
    except Exception as e:
        print(f"      ⚠️  Build error: {e}")
    return gold

def _buy_gear(page, a, gold):
    item_id = ITEM_ID.get((a["unit"], a["slot"], a["tier"]))
    if not item_id:
        print(f"      ⚠️  No item_id for ({a['unit']},{a['slot']},T{a['tier']})")
        return gold
    print(f"    ⚔️  {a['qty']}× {a['name']} T{a['tier']} ({a['total']:,}g) — {a['reason']}")
    try:
        page.goto(f"{BASE_URL}/armory")
        page.wait_for_selector(".armory-page", timeout=15000)
        js = f"""() => {{
            const buyBtn = document.querySelector('.mode-btn[data-mode="buy"]');
            if (buyBtn && !buyBtn.classList.contains('active')) buyBtn.click();
            const tabBtn = document.querySelector('.tab-btn[data-tab="{a["tab"]}"]');
            if (tabBtn && !tabBtn.classList.contains('active')) tabBtn.click();
            const hidden = document.querySelector(
                'form[action*="armory/buy"] input[name="item_id"][value="{item_id}"]');
            if (!hidden) return false;
            const form = hidden.closest('form');
            const qtyInp = form.querySelector('input[name="quantity"]');
            if (!qtyInp) return false;
            qtyInp.value = '{a["qty"]}';
            qtyInp.dispatchEvent(new Event('input', {{bubbles: true}}));
            qtyInp.dispatchEvent(new Event('change', {{bubbles: true}}));
            form.submit();
            return true;
        }}"""
        with page.expect_navigation(wait_until="networkidle", timeout=15000):
            submitted = page.evaluate(js)
        if submitted:
            log("GEAR", f"{a['qty']}×{a['name']} T{a['tier']}", gold, gold - a["total"])
            return gold - a["total"]
        print(f"      ⚠️  item_id={item_id} not found in armory")
    except Exception as e:
        print(f"      ⚠️  Gear error: {e}")
    return gold

def _buy_new_unit_gear(page, a, gold):
    """Buy weapon + armor for newly trained units at max available tier."""
    unit = a["unit"]
    slot_max = {"weapon": a.get("w_max_t", 1), "armor": a.get("a_max_t", 1)}
    for slot in ("weapon","armor"):
        table = GEAR.get((unit,slot),{})
        if not table: continue
        mt = slot_max[slot]
        available = [t for t in table if t <= mt]
        if not available: continue
        best_t = max(available)
        name, stat, cost = table[best_t]
        total = cost * a["count"]
        if gold >= total:
            fa = {"type":"BUY_GEAR","unit":unit,"slot":slot,"qty":a["count"],
                  "name":name,"tier":best_t,"cost":cost,"total":total,
                  "tab":ARMORY_TAB[unit],"reason":f"gear for {a['count']} new {unit}s"}
            gold = _buy_gear(page, fa, gold)
    return gold

def _train(page, a, gold):
    print(f"    🪖 Train {a['count']}× {a['unit']} ({a['cost']:,}g) — {a['reason']}")
    try:
        page.goto(f"{BASE_URL}/train")
        page.wait_for_load_state("networkidle", timeout=10000)
        uid = UNIT_ID[a["unit"]]
        js = f"""() => {{
            const multiBtn = document.querySelector('.buy-mode-btn[data-mode="multi"]');
            if (multiBtn && !multiBtn.classList.contains('active')) multiBtn.click();
            const inp = document.querySelector('input.multi-qty-input[data-unit-id="{uid}"]');
            if (!inp) return false;
            inp.value = '{a["count"]}';
            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
            const btn = document.getElementById('multi-train-btn');
            if (btn) btn.disabled = false;
            const form = btn ? btn.closest('form') : document.querySelector('form[action*="train"]');
            if (!form) return false;
            form.submit();
            return true;
        }}"""
        with page.expect_navigation(wait_until="networkidle", timeout=15000):
            submitted = page.evaluate(js)
        if submitted:
            log("TRAIN", f"{a['count']}×{a['unit']}", gold, gold - a["cost"])
            return gold - a["cost"]
        print(f"      ⚠️  Input not found for unit_id={uid}")
    except Exception as e:
        print(f"      ⚠️  Train error: {e}")
    return gold

def _bank(page, a, gold):
    try:
        page.goto(f"{BASE_URL}/bank")
        page.wait_for_load_state("networkidle", timeout=10000)
        js = f"""() => {{
            const inp = document.querySelector('#deposit_amount, input[name="amount"]');
            if (!inp) return {{ok: false, amount: 0}};
            const cap = parseInt(inp.getAttribute('max') || '0');
            const amount = cap > 0 ? Math.min({a["amount"]}, cap) : {a["amount"]};
            if (amount < 10000) return {{ok: false, amount: 0}};
            inp.value = amount;
            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
            const form = inp.closest('form');
            if (!form) return {{ok: false, amount: 0}};
            form.submit();
            return {{ok: true, amount}};
        }}"""
        with page.expect_navigation(wait_until="networkidle", timeout=15000):
            result = page.evaluate(js)
        if result["ok"]:
            actual = result["amount"]
            print(f"    🏦 Banked {actual:,}g — {a['reason']}")
            log("BANK", f"{actual:,}g", gold, gold - actual)
            return gold - actual
    except Exception as e:
        print(f"      ⚠️  Bank error: {e}")
    return gold

def _repair_fort(page, a, gold):
    print(f"    🛡️  Repair fort {a['damage']} HP ({a['cost']:,}g) — {a['reason']}")
    try:
        page.goto(f"{BASE_URL}/fort")
        page.wait_for_load_state("networkidle", timeout=10000)
        submitted = page.evaluate(f"""() => {{
            const inp = document.getElementById('repairAmount');
            if (!inp) return false;
            inp.value = '{a["damage"]}';
            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
            const form = inp.closest('form');
            if (!form) return false;
            form.submit();
            return true;
        }}""")
        with page.expect_navigation(wait_until="networkidle", timeout=15000):
            submitted = page.evaluate(f"""() => {{
                const inp = document.getElementById('repairAmount');
                if (!inp) return false;
                inp.value = '{a["damage"]}';
                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                const form = inp.closest('form');
                if (!form) return false;
                form.submit();
                return true;
            }}""")
        if submitted:
            log("FORT_REPAIR", f"{a['damage']} HP", gold, gold - a["cost"])
            return gold - a["cost"]
    except Exception as e:
        print(f"      ⚠️  Fort repair error: {e}")
    return gold

def _buy_upgrade(page, a, gold):
    print(f"    ⬆️  Upgrade: {a['name']} ×{a['qty']} ({a['total']:,}g) — {a['reason']}")
    try:
        page.goto(f"{BASE_URL}/upgrades")
        page.wait_for_load_state("networkidle", timeout=10000)
        js = f"""() => {{
            let target = null;
            document.querySelectorAll('.buy-mode tr:not(.disabled)').forEach(row => {{
                const name = row.querySelector('td strong')?.innerText?.trim();
                if (name === '{a["name"]}') target = row;
            }});
            if (!target) return false;
            const qtyInp = target.querySelector('input[name="quantity"], input[type="number"]');
            if (qtyInp) {{
                qtyInp.value = '{a["qty"]}';
                qtyInp.dispatchEvent(new Event('input', {{bubbles: true}}));
            }}
            const form = target.querySelector('form') || target.closest('form');
            if (form) {{ form.submit(); return true; }}
            const btn = target.querySelector('button[type="submit"], button.btn');
            if (btn) {{ btn.click(); return true; }}
            return false;
        }}"""
        with page.expect_navigation(wait_until="networkidle", timeout=15000):
            submitted = page.evaluate(js)
        if submitted:
            log("UPGRADE", f"{a['qty']}×{a['name']}", gold, gold - a["total"])
            return gold - a["total"]
        print(f"      ⚠️  Upgrade '{a['name']}' not found on page")
    except Exception as e:
        print(f"      ⚠️  Upgrade error: {e}")
    return gold

# ── Growth tracking ───────────────────────────────────────────────────────────
def record_growth(s, tick_num, actions):
    """Append one tick snapshot to GROWTH_FILE and regenerate chart HTML."""
    summary = {}
    gold_spent = 0
    for a in actions:
        if a["type"].startswith("SAVE_"): continue
        summary[a["type"]] = summary.get(a["type"], 0) + 1
        gold_spent += a.get("total", a.get("cost", 0))

    rec = {
        "tick":       tick_num,
        "ts":         datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "gold":       s["gold"],
        "bank":       s["bank"],
        "income":     s["income"],
        "atk":        s["atk"],
        "def":        s["def"],
        "spy_off":    s["spy_off"],
        "spy_def":    s["spy_def"],
        "workers":    s.get("workers",  0),
        "soldiers":   s.get("soldiers", 0),
        "guards":     s.get("guards",   0),
        "spies":      s.get("spies",    0),
        "sentries":   s.get("sentries", 0),
        "army":       s.get("soldiers",0)+s.get("guards",0)+s.get("spies",0)+s.get("sentries",0),
        "gold_spent": gold_spent,
        "actions":    summary,
        "mine_lv":    s.get("buildings", {}).get("Mine", 0),
        "fort_lv":    s.get("fort_lv", 0),
        "level":      s.get("level", 0),
    }

    data = []
    if os.path.isfile(GROWTH_FILE):
        _unhide(GROWTH_FILE)
        with open(GROWTH_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except Exception as _e:
                print(f"  ⚠️  record_growth: {GROWTH_FILE} corrupt — starting fresh ({_e})")
                data = []
    data.append(rec)
    if len(data) > 1000: data = data[-1000:]
    _unhide(GROWTH_FILE)
    with open(GROWTH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    _write_chart_html(data)
    print(f"  📊 Chart updated → {CHART_FILE}  ({len(data)} ticks logged)")


def backfill_growth_from_log():
    """
    Build GROWTH_FILE from existing private_optimizer_log.csv when no growth
    file exists yet.  Each tick is identified by the first TRAIN/BUILD/GEAR
    action after a gold-drop — we use GoldBefore of the first action in a
    burst as the snapshot gold for that tick.
    """
    if not os.path.isfile(LOG_FILE):
        return
    if os.path.isfile(GROWTH_FILE):
        return  # already exists — don't overwrite live data

    rows = []
    with open(LOG_FILE, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    if not rows:
        return

    # Group consecutive rows with the same timestamp minute into one tick
    ticks = []
    cur_ts, cur_rows = None, []
    for r in rows:
        ts_min = r["Timestamp"][:16]          # "YYYY-MM-DD HH:MM"
        if ts_min != cur_ts:
            if cur_rows:
                ticks.append((cur_ts, cur_rows))
            cur_ts, cur_rows = ts_min, [r]
        else:
            cur_rows.append(r)
    if cur_rows:
        ticks.append((cur_ts, cur_rows))

    data = []
    for i, (ts, tick_rows) in enumerate(ticks):
        g_before = num(tick_rows[0]["GoldBefore"])
        gold_spent = sum(
            max(0, num(r["GoldBefore"]) - num(r["GoldAfter"]))
            for r in tick_rows
        )
        summary = {}
        for r in tick_rows:
            summary[r["Action"]] = summary.get(r["Action"], 0) + 1

        data.append({
            "tick":       i + 1,
            "ts":         ts,
            "gold":       g_before,
            "bank":       0,
            "income":     0,
            "atk":        0, "def": 0, "spy_off": 0, "spy_def": 0,
            "workers":    0, "soldiers": 0, "guards": 0, "spies": 0, "sentries": 0,
            "army":       0,
            "gold_spent": gold_spent,
            "actions":    summary,
            "mine_lv":    0, "fort_lv": 0, "level": 0,
        })

    _unhide(GROWTH_FILE)
    with open(GROWTH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    _write_chart_html(data)
    print(f"  📊 Backfilled {len(data)} ticks from {LOG_FILE} → {GROWTH_FILE}")


def _write_chart_html(data):
    import json as _json

    labels    = [d["ts"]         for d in data]
    income    = [d["income"]     for d in data]
    atk       = [d["atk"]        for d in data]
    def_      = [d["def"]        for d in data]
    spy_off   = [d["spy_off"]    for d in data]
    spy_def   = [d["spy_def"]    for d in data]
    gold      = [d["gold"]       for d in data]
    bank      = [d["bank"]       for d in data]
    soldiers  = [d.get("soldiers", 0) for d in data]
    guards    = [d.get("guards",   0) for d in data]
    spies     = [d.get("spies",    0) for d in data]
    sentries  = [d.get("sentries", 0) for d in data]
    spent     = [d.get("gold_spent",0) for d in data]

    # Recent-ticks table (newest first, last 30)
    rows_html = ""
    for r in reversed(data[-30:]):
        acts = ", ".join(f"{v}×{k.replace('_',' ')}" for k, v in r.get("actions", {}).items())
        inc  = f"{r['income']:,}"  if r["income"] else "—"
        atk_ = f"{r['atk']:,}"    if r["atk"]    else "—"
        def__ = f"{r['def']:,}"   if r["def"]    else "—"
        army = r.get("army", 0)
        rows_html += (
            f"<tr><td>{r['ts']}</td><td>#{r['tick']}</td>"
            f"<td>{inc}</td><td>{atk_}</td><td>{def__}</td>"
            f"<td>{army if army else '—'}</td>"
            f"<td>{r.get('gold_spent',0):,}</td><td>{acts}</td></tr>\n"
        )

    updated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DarkThrone — Optimizer Growth</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0d0d0d;color:#ccc;font-family:'Segoe UI',sans-serif;padding:20px}}
  h1{{color:#e8c96d;margin-bottom:18px;font-size:1.4rem;letter-spacing:1px}}
  .grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:20px}}
  .card{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:8px;padding:16px}}
  .card h3{{color:#888;font-size:.78rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}}
  canvas{{max-height:210px}}
  .full{{grid-column:1/-1}}
  table{{width:100%;border-collapse:collapse;font-size:.78rem}}
  th{{background:#222;color:#666;text-align:left;padding:6px 10px;border-bottom:1px solid #333;white-space:nowrap}}
  td{{padding:5px 10px;border-bottom:1px solid #1e1e1e;white-space:nowrap}}
  tr:hover td{{background:#1f1f1f}}
  .meta{{color:#444;font-size:.72rem;margin-top:10px}}
  @media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<h1>⚡ DarkThrone — Optimizer Growth</h1>
<div class="grid">
  <div class="card">
    <h3>💰 Income / Tick</h3>
    <canvas id="cIncome"></canvas>
  </div>
  <div class="card">
    <h3>⚔️ Combat Power</h3>
    <canvas id="cCombat"></canvas>
  </div>
  <div class="card">
    <h3>🪖 Army Composition</h3>
    <canvas id="cArmy"></canvas>
  </div>
  <div class="card">
    <h3>🏦 Gold on Hand &amp; Bank</h3>
    <canvas id="cGold"></canvas>
  </div>
  <div class="card full">
    <h3>💸 Gold Spent per Tick</h3>
    <canvas id="cSpent"></canvas>
  </div>
  <div class="card full">
    <h3>📋 Recent Ticks</h3>
    <div style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>Time</th><th>Tick</th><th>Income</th>
        <th>ATK</th><th>DEF</th><th>Army</th>
        <th>Gold Spent</th><th>Actions</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
    </div>
  </div>
</div>
<p class="meta">Last updated: {updated} &nbsp;|&nbsp; {len(data)} ticks recorded</p>

<script>
const labels   = {_json.dumps(labels)};
const income   = {_json.dumps(income)};
const atk      = {_json.dumps(atk)};
const def_     = {_json.dumps(def_)};
const spy_off  = {_json.dumps(spy_off)};
const spy_def  = {_json.dumps(spy_def)};
const gold     = {_json.dumps(gold)};
const bank     = {_json.dumps(bank)};
const soldiers = {_json.dumps(soldiers)};
const guards   = {_json.dumps(guards)};
const spies    = {_json.dumps(spies)};
const sentries = {_json.dumps(sentries)};
const spent    = {_json.dumps(spent)};

const baseOpts = {{
  responsive:true, maintainAspectRatio:true,
  plugins:{{legend:{{labels:{{color:'#999',boxWidth:11,font:{{size:11}}}}}}}},
  scales:{{
    x:{{ticks:{{color:'#555',maxTicksLimit:10,maxRotation:0}},grid:{{color:'#1e1e1e'}}}},
    y:{{ticks:{{color:'#777'}},grid:{{color:'#222'}}}}
  }}
}};
const stackOpts = {{
  ...baseOpts,
  scales:{{
    x:{{...baseOpts.scales.x, stacked:true}},
    y:{{...baseOpts.scales.y, stacked:true}}
  }}
}};

new Chart(document.getElementById('cIncome'),{{
  type:'line',
  data:{{labels, datasets:[{{
    label:'Income/tick', data:income,
    borderColor:'#e8c96d', backgroundColor:'rgba(232,201,109,.12)',
    tension:.3, pointRadius:1.5, fill:true
  }}]}},
  options:baseOpts
}});

new Chart(document.getElementById('cCombat'),{{
  type:'line',
  data:{{labels, datasets:[
    {{label:'ATK',    data:atk,    borderColor:'#e05252',tension:.3,pointRadius:1.5}},
    {{label:'DEF',    data:def_,   borderColor:'#5299e0',tension:.3,pointRadius:1.5}},
    {{label:'SpyOff', data:spy_off,borderColor:'#e09050',tension:.3,pointRadius:1.5,borderDash:[4,3]}},
    {{label:'SpyDef', data:spy_def,borderColor:'#9050e0',tension:.3,pointRadius:1.5,borderDash:[4,3]}},
  ]}},
  options:baseOpts
}});

new Chart(document.getElementById('cArmy'),{{
  type:'bar',
  data:{{labels, datasets:[
    {{label:'Soldiers',data:soldiers,backgroundColor:'rgba(224,82,82,.75)', stack:'a'}},
    {{label:'Guards',  data:guards,  backgroundColor:'rgba(82,153,224,.75)',stack:'a'}},
    {{label:'Spies',   data:spies,   backgroundColor:'rgba(224,144,80,.75)',stack:'a'}},
    {{label:'Sentries',data:sentries,backgroundColor:'rgba(144,80,224,.75)',stack:'a'}},
  ]}},
  options:stackOpts
}});

new Chart(document.getElementById('cGold'),{{
  type:'line',
  data:{{labels, datasets:[
    {{label:'Gold on Hand',data:gold,borderColor:'#f0c040',backgroundColor:'rgba(240,192,64,.12)',tension:.3,pointRadius:1.5,fill:true}},
    {{label:'Banked',      data:bank,borderColor:'#60c070',backgroundColor:'rgba(96,192,112,.1)', tension:.3,pointRadius:1.5,fill:true}},
  ]}},
  options:baseOpts
}});

new Chart(document.getElementById('cSpent'),{{
  type:'bar',
  data:{{labels, datasets:[{{
    label:'Gold Spent', data:spent,
    backgroundColor:'rgba(232,100,80,.65)', borderColor:'rgba(232,100,80,.9)',
    borderWidth:1
  }}]}},
  options:baseOpts
}});
</script>
</body>
</html>"""

    _unhide(CHART_FILE)
    with open(CHART_FILE, "w", encoding="utf-8") as f:
        f.write(html)


# ── Main tick ─────────────────────────────────────────────────────────────────
def run_tick():
    st = load_state(); st["ticks"] = st.get("ticks",0)+1
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*65}\n⚡ TICK #{st['ticks']} — {ts}\n{'='*65}")

    # Refuse if an interactive battle session is running — concurrent
    # Playwright contexts will corrupt auth.json and fight over CSV writes.
    if os.path.isfile(BATTLE_LOCK_FILE):
        print("  ⏭️  Skipping tick: auto-battle is running (.battle.lock present).")
        return
    if not _acquire_lock(OPT_LOCK_FILE):
        print("  ⏭️  Skipping tick: another optimizer instance holds .optimizer.lock.")
        return

    try:
      with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=AUTH_FILE if os.path.exists(AUTH_FILE) else None)
        page = ctx.new_page()
        page.goto(f"{BASE_URL}/overview")
        if "login" in page.url:
            print("🔑 Session expired — please click 'Login with Browser' in the app to re-authenticate.")
            browser.close()
            return

        s = read_state(page)
        print(f"  💰 Gold: {s['gold']:,} | Bank: {s['bank']:,} | Citizens: {s['citizens']} | Turns: {s.get('turns',0):,}")
        print(f"  ⚔️  ATK: {s['atk']:,} | DEF: {s['def']:,} | SpyOff: {s['spy_off']:,} | SpyDef: {s['spy_def']:,}")
        print(f"  📈 Income: {s['income']:,}/tick | Level: {s['level']} | XP: {s['xp']:,}/{s['xp_need']:,} ({s['xp_pct']}%)")
        print(f"  🪖 Army: workers={s['workers']} soldiers={s['soldiers']} guards={s['guards']} spies={s['spies']} sentries={s['sentries']}")
        fort_bar = "█" * (s.get("fort_pct",100)//10) + "░" * (10 - s.get("fort_pct",100)//10)
        print(f"  🛡️  Fort: {s.get('fort_hp',100)}/{s.get('fort_max_hp',100)} HP [{fort_bar}] {s.get('fort_pct',100)}%  |  Deposits: {s['deposits']}/6")
        print(f"  🏗️  Buildings: {s['buildings']}")
        if s.get("upgrades_owned"):
            print(f"  ⬆️  Upgrades owned: {s['upgrades_owned']}")
        if s.get("upgrades_buyable"):
            print(f"  ⬆️  Upgrades buyable: {list(s['upgrades_buyable'].keys())}")
        print()

        # Analyse
        cats = analyse(s)
        print("  📊 ANALYSIS:")
        for c in cats:
            status = "✅ MAXED" if c["fully_maxed"] else f"{'⚠️' if c['score']<0.5 else '🔶'} score={c['score']:.2f}"
            print(f"     {c['unit']:<10} units={c['units']:<4} w={c.get('w_owned',0)}/{c['units']}(T{c.get('w_tier',1)}) "
                  f"a={c.get('a_owned',0)}/{c['units']}(T{c.get('a_tier',1)}) "
                  f"max_tier=T{c['max_t']} {status}")
        print()

        # Decide — marginal-value engine.
        actions, gold_after = decide_v2(s, cats)
        print("  🧠 DECISIONS:")
        for a in actions:
            icon = {"BUILD":"🏗️","BUY_GEAR":"⚔️","UPGRADE_GEAR":"⬆️","TRAIN":"🪖",
                    "BANK":"🏦","REPAIR_FORT":"🛡️","BUY_UPGRADE":"⬆️",
                    "SAVE_FOR_BUILD":"💾","SAVE_FOR_GEAR":"💾",
                    "SAVE_FOR_UPGRADE":"💾","SAVE_FOR_REPAIR":"💾"}.get(a["type"],"•")
            print(f"     {icon} [{a['type']}] {a.get('reason','')}")
        print()

        # Execute
        gold = execute(page, actions, s["gold"])

        # Growth log + chart
        record_growth(s, st["ticks"], actions)

        # Summary — gold_before vs gold_after shows real spending
        gold_spent = s["gold"] - gold
        if gold_spent > 0:
            print(f"  💰 Gold spent this tick: {gold_spent:,}  (had {s['gold']:,} → {gold:,} left)")
        elif actions:
            print(f"  ⚠️  {len(actions)} action(s) planned but 0 gold spent — executors may have failed")

        if s["xp_need"] > 0:
            left = s["xp_need"]-s["xp"]; ticks = left/60
            print(f"  🎯 Level {s['level']+1}: {left} XP (~{ticks:.0f} ticks)")

        save_state(st)

        # ── Write fresh player snapshot so the estimator uses live data ───────
        _ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        _snapshot = {
            "timestamp":    _ts,
            "level":        s["level"],
            "population":   (s["workers"] + s["soldiers"] + s["guards"]
                             + s["spies"] + s["sentries"] + s.get("citizens", 0)),
            "atk":          s["atk"],
            "def":          s["def"],
            "spy_off":      s["spy_off"],
            "spy_def":      s["spy_def"],
            "income":       s["income"],
            "workers":      s["workers"],
            "soldiers":     s["soldiers"],
            "guards":       s["guards"],
            "spies":        s["spies"],
            "sentries":     s["sentries"],
            "buildings":    s["buildings"],
            "rank_overall": s.get("rank_overall", 0),
            "rank_offense": s.get("rank_offense", 0),
            "rank_defense": s.get("rank_defense", 0),
            "rank_wealth":  s.get("rank_wealth",  0),
        }
        _unhide("private_latest.json")
        with open("private_latest.json", "w", encoding="utf-8") as _f:
            json.dump(_snapshot, _f, indent=2)

        # -- Read own server-wide ranks from player profile page ----------------
        try:
            _own_ranks = read_own_ranks(page)
            if any(_own_ranks.values()):
                _snapshot["rank_overall"] = _own_ranks.get("rank_overall", 0)
                _snapshot["rank_offense"] = _own_ranks.get("rank_offense", 0)
                _snapshot["rank_defense"] = _own_ranks.get("rank_defense", 0)
                _snapshot["rank_wealth"]  = _own_ranks.get("rank_wealth",  0)
                _unhide("private_latest.json")
                with open("private_latest.json", "w", encoding="utf-8") as _f:
                    json.dump(_snapshot, _f, indent=2)
        except Exception as _e:
            print(f"  ⚠️ Own ranks read error: {_e}")

        # -- Scrape rankings - refreshes all players' server-wide ranks ---------
        try:
            scrape_rankings(page, _ts)
        except Exception as _e:
            print(f"  ⚠️ Rankings scrape error: {_e}")

        # -- Scrape public attack list + update dashboard -----------------------
        try:
            scrape_with_page(page, max_pages=50)
        except Exception as _e:
            print(f"  ⚠️ Scraper error: {_e}")

        # -- Harvest spy-log history (manual + automated spies) -----------------
        # Visits /game/spy/logs and pulls any reports we haven't seen yet.
        # Covers manual spies the user ran through the browser — they land in
        # the same intel pipeline as bot-initiated spies. Runs BEFORE the
        # estimator so the fresh reports feed into calibrate_models() this tick.
        try:
            scrape_spy_logs(page, _ts)
        except Exception as _e:
            print(f"  ⚠️ Spy-log harvest error: {_e}")

        # -- Run estimator (writes fresh private_player_estimates.csv) ----------
        try:
            print("  🔍 Running player estimates...")
            estimator_run()
        except Exception as _e:
            print(f"  ⚠️ Estimator error: {_e}")

        # -- Re-run dashboard update so fresh estimates are included -----------
        # (scrape_with_page ran BEFORE the estimator wrote the CSV, so
        #  we call update_dashboard() once more to inject the up-to-date data.)
        try:
            update_dashboard()
        except Exception as _e:
            print(f"  ⚠️ Dashboard re-publish error: {_e}")
        ctx.storage_state(path=AUTH_FILE)
        browser.close()
    finally:
        _release_lock(OPT_LOCK_FILE)


if __name__ == "__main__":
    print("🛡️  Smart Defense Optimizer")
    print("   Logic: analyse all data → weakest category first → gear before citizens")
    print("   Income buildings always priority. Train only when gear is maxed.\n")
    # Backfill chart from existing log on first run (no-op if GROWTH_FILE already exists)
    backfill_growth_from_log()
    while True:
        try: run_tick()
        except KeyboardInterrupt: print("\n👋 Stopped."); break
        except Exception as e: print(f"❌ Error: {e}")
        now = datetime.datetime.now()
        wait = (30*60)-(now.minute%30)*60-now.second
        nxt = now+datetime.timedelta(seconds=wait)
        print(f"\n⏳ Next: {nxt.strftime('%H:%M')} ({wait//60}m). Ctrl+C to stop.")
        time.sleep(wait)


# ========================================================================
# Dashboard & Attack-List Scraper  (merged from scraper.py)
# ========================================================================

BASE_ATTACK_URL = "https://darkthronegame.com/game/attack"
DATA_FILE       = "darkthrone_server_data.csv"
DASHBOARD_FILE  = "index.html"

# Query parameters that match the target URL exactly
ATTACK_PARAMS = "sort=level&dir=desc&range=all&bots=all"


def publish_dashboard():
    """Publish the live attack-list dashboard (data/index.html) to GitHub
    Pages as a subfolder of the existing darkthrone-estimates repo, so
    it coexists with the estimates dashboard without needing a second
    Pages repo.

    Source:  <data>/index.html         (written by update_dashboard)
    Target:  <repo>/attack-list/index.html
    URL:     https://cmdprive.github.io/darkthrone-estimates/attack-list/
    """
    print("🚀 Publishing raw data to GitHub...")
    repo = ESTIMATES_REPO_DIR
    if not os.path.isdir(repo):
        print(f"  ⚠️  Estimates repo not found at {repo}")
        print(f"      Run: git clone https://github.com/cmdprive/darkthrone-estimates \"{repo}\"")
        return

    src  = DASHBOARD_FILE
    if not os.path.isfile(src):
        print(f"  ⚠️  {src} not found — run update_dashboard() first")
        return

    dest_dir  = os.path.join(repo, "attack-list")
    dest_file = os.path.join(dest_dir, "index.html")
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:
        print(f"  ⚠️  Could not create {dest_dir}: {e}")
        return

    import shutil
    try:
        shutil.copy2(src, dest_file)
    except Exception as e:
        print(f"  ⚠️  Copy failed ({src} → {dest_file}): {e}")
        return

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        subprocess.run(["git", "-C", repo, "add", "attack-list/index.html"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "--allow-empty",
                        "-m", f"Attack-list update {timestamp}"], check=True)
        subprocess.run(["git", "-C", repo, "push"], check=True)
        print(f"✅ Attack-list published → {ESTIMATES_SITE_URL}attack-list/")
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


# ========================================================================
# Auto-Battle Loop  (attack + spy farming)
# ========================================================================
#
# A standalone continuous loop driven by the Suite GUI.  Given the user's
# live ATK/SPY_OFF and the estimator's per-player DEF/SPY_DEF, it picks
# targets that are safely defeatable and submits attacks or recon spies
# via Playwright form.submit().  Mutually exclusive with the optimizer
# tick loop — both acquire their own lockfile before touching the browser.
#
# Core design points:
#   • No persistent ledger.  The game itself exposes the daily count per
#     target:
#       attack: <span class="attack-limit" title="Attacks today on this player">(N/5)</span>
#       spy:    <button title="N/5 today">🔍 N/5 ...</button>
#     We parse those numbers on every scrape — they are authoritative and
#     reset at the game's daily boundary automatically.
#   • Safe-target filter: our_atk >= est_def * margin (default 1.2).
#     Skips friends, clan members, out-of-range rows and anything whose
#     est_def is too high.  Bots are fair game unless the user adds a skip.
#   • Rate limited: random.uniform(2.5, 4.5) between actions plus a longer
#     8-15s pause every 10 actions.  Matches human-ish pacing.
#   • Honeypot guard: asserts the hidden input[name="website_url"] is empty
#     before every form submit.  The site uses this as an anti-bot trap.
#   • Lockfile: .battle.lock / .optimizer.lock prevent concurrent Playwright
#     sessions from corrupting auth.json between run_scheduled.bat and
#     interactive GUI runs.

import random as _random

BATTLE_LOG_FILE   = "private_battle_log.csv"
BATTLE_LOCK_FILE  = ".battle.lock"
OPT_LOCK_FILE     = ".optimizer.lock"

# Rate-limit constants — used by battle_loop only.  Fine-tune here if the
# server gets cranky; do NOT expose these in the GUI (too much rope).
BATTLE_MIN_DELAY        = 2.5
BATTLE_MAX_DELAY        = 4.5
BATTLE_LONG_DELAY_MIN   = 8.0
BATTLE_LONG_DELAY_MAX   = 15.0
BATTLE_LONG_PAUSE_EVERY = 10

# When the optimizer thread piggybacks battle during its wait phase, battle
# exits this many seconds BEFORE the next tick time to give run_tick a clean
# window to acquire its lock + open the browser + finish its work.
BATTLE_TICK_BUFFER_SECONDS = 90

# Regex to pull the "(N/5)" counter out of an attack-limit span or spy button.
ATTACK_LIMIT_RE = re.compile(r"\((\d+)\s*/\s*5\)")
SPY_LIMIT_RE    = re.compile(r"(\d+)\s*/\s*5")

# Daily max per target — both attacks and spies cap at 5 per day, per game.
BATTLE_DAILY_MAX = 5

# Fixed spy cost from the game page.
SPY_GOLD_COST  = 3000
SPY_TURNS_COST = 2

# Per-target spy cooldown: don't re-spy the same player within this many
# hours.  Intel from a successful recon is good for at least this long —
# stats don't move fast enough for re-spying to reveal new info, and we
# save gold + turns for new targets instead.  Enforced via the persistent
# private_intel.csv (load_spy_cooldowns) so the cooldown survives across
# Suite restarts.
SPY_COOLDOWN_HOURS = 6


class BattleStop(Exception):
    """Raised to unwind battle_loop cleanly with a reason string.
    Reason is one of: 'user', 'turns', 'gold', 'session', 'no_targets', 'error'."""
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# ── Lockfiles (prevent concurrent Playwright sessions) ───────────────────────
def _acquire_lock(path: str) -> bool:
    """Atomically create a PID-bearing lockfile.  Returns True on success.
    If the file exists but the PID is dead, steals the lock and returns True."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return True
    except FileExistsError:
        pass
    # Already-locked path: check if the owning PID is still alive.
    try:
        with open(path, "r", encoding="ascii") as f:
            pid = int((f.read() or "0").strip() or "0")
    except Exception:
        pid = 0
    if pid > 0 and _pid_alive(pid):
        return False
    # Stale lock — steal it.
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_lock(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  ⚠️  lock release failed for {path}: {e}")


def _pid_alive(pid: int) -> bool:
    """Best-effort cross-platform PID liveness check.  False positives are safe
    (we'd just refuse to start, user retries).  False negatives could double-run
    the browser, so we err on the side of 'assume alive'."""
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


# ── Battle log CSV (audit trail) ─────────────────────────────────────────────
BATTLE_LOG_COLUMNS = [
    "Timestamp", "Mode", "PlayerID", "Player", "Turns",
    "GoldBefore", "GoldAfter", "XpGained", "Result",
]

def _battle_log_row(mode, player_id, name, turns, gold_before, gold_after, xp, result):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _unhide(BATTLE_LOG_FILE)
    new = not os.path.isfile(BATTLE_LOG_FILE)
    with open(BATTLE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(BATTLE_LOG_COLUMNS)
        w.writerow([ts, mode, player_id, name, turns, gold_before, gold_after, xp, result])


# ── Estimates lookup (for target DEF) ────────────────────────────────────────
def load_estimates_lookup() -> dict:
    """Return {Player name (str): {est_atk, est_def, est_spy_off, est_spy_def}}.
    Reads private_player_estimates.csv (written by estimator_run()) and
    overlays it with private_intel.csv (written after each attack/spy).
    Intel values WIN when present because they are ground truth from actual
    reports, and we also apply them as floors — a confirmed DEF can only
    have grown since the observation, so max(intel, estimator) is the best
    current estimate."""
    path = "private_player_estimates.csv"
    lookup = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    name = (row.get("Player") or row.get("Name") or "").strip()
                    if not name:
                        continue
                    clean = name.replace(" (YOU)", "").strip()
                    lookup[clean] = {
                        "name":         clean,
                        "est_atk":      num(row.get("EstATK",    row.get("est_atk",     0))),
                        "est_def":      num(row.get("EstDEF",    row.get("est_def",     0))),
                        "est_spy_off":  num(row.get("EstSpyOff", row.get("est_spy_off", 0))),
                        "est_spy_def":  num(row.get("EstSpyDef", row.get("est_spy_def", 0))),
                        "confidence":   (row.get("Confidence",   "") or "").strip(),
                    }
        except Exception as e:
            print(f"  ⚠️  load_estimates_lookup failed: {e}")

    # Overlay with private_intel.csv — max(intel_floor, estimator_value) so
    # confirmed observations raise the bar without ever lowering it.
    try:
        for name, intel in load_intel_overlay().items():
            cur = lookup.get(name, {
                "name": name, "est_atk": 0, "est_def": 0,
                "est_spy_off": 0, "est_spy_def": 0, "confidence": "INTEL",
            })
            cur["est_atk"]     = max(cur.get("est_atk",     0), int(intel.get("atk",     0)))
            cur["est_def"]     = max(cur.get("est_def",     0), int(intel.get("def",     0)))
            cur["est_spy_off"] = max(cur.get("est_spy_off", 0), int(intel.get("spy_off", 0)))
            cur["est_spy_def"] = max(cur.get("est_spy_def", 0), int(intel.get("spy_def", 0)))
            if intel.get("atk") or intel.get("def") or intel.get("spy_off") or intel.get("spy_def"):
                cur["confidence"] = "INTEL"
            lookup[name] = cur
    except Exception as e:
        print(f"  ⚠️  intel overlay failed: {e}")

    # ── Growth projection: use observed growth rates to predict current
    # stats instead of relying on a stale point-in-time snapshot. The
    # projection can only RAISE an estimate (conservative — we don't
    # want to attack someone the model thinks is weaker than they are).
    try:
        _growth_hist  = load_player_growth()
        _growth_rates = compute_growth_rates(_growth_hist)
        _now = datetime.datetime.now()
        _projected = 0
        for pname, rates in _growth_rates.items():
            if pname not in lookup:
                continue
            ticks_since = ((_now - rates["last_ts"]).total_seconds()) / 1800.0
            if ticks_since <= 0:
                continue
            cur = lookup[pname]
            changed = False
            for stat_key, rate_key, last_key in [
                ("est_def",     "def_per_tick",     "last_def"),
                ("est_atk",     "atk_per_tick",     "last_atk"),
                ("est_spy_off", "spy_off_per_tick", "last_spy_off"),
                ("est_spy_def", "spy_def_per_tick", "last_spy_def"),
            ]:
                rate = rates.get(rate_key, 0)
                last = rates.get(last_key, 0)
                if rate > 0 and last > 0:
                    projected = int(last + rate * ticks_since)
                    if projected > cur.get(stat_key, 0):
                        cur[stat_key] = projected
                        changed = True
            if changed:
                cur["growth_def_per_tick"] = rates.get("def_per_tick", 0)
                _projected += 1
            lookup[pname] = cur
        if _projected:
            print(f"  📈 Growth projection applied to {_projected} player(s)")
    except Exception as e:
        print(f"  ⚠️  growth projection failed: {e}")

    return lookup


# ── Scrape attack list for live battle candidates ────────────────────────────
def scrape_attack_candidates(page, max_pages: int = 20) -> list:
    """Paginates the attack list and returns one dict per target row.
    Same shape as _do_scrape but includes the per-target daily counter
    (attack_count) and attack-form id.  Does NOT write any CSV — pure
    read path for the battle loop."""
    rows_out = []
    last_fp = ""
    for page_num in range(1, max_pages + 1):
        url = f"{BASE_ATTACK_URL}?{ATTACK_PARAMS}&page={page_num}"
        try:
            page.goto(url)
            page.wait_for_selector("#battlelist-table tbody tr", timeout=15000)
        except Exception:
            break

        player_rows = page.query_selector_all("#battlelist-table tbody tr[data-name]")
        page_names = []
        for row in player_rows:
            link_el = row.query_selector("a.player-link")
            href    = link_el.get_attribute("href") if link_el else ""
            m       = re.search(r"/player/(\d+)", href or "")
            pid     = m.group(1) if m else ""
            if not pid:
                continue

            name_span = row.query_selector("a.player-link span:not([class])")
            name = name_span.inner_text().replace("(YOU)", "").strip() if name_span else ""
            if not name:
                name = row.get_attribute("data-name") or ""
            if not name:
                continue
            if "(YOU)" in name:
                continue
            page_names.append(name)

            level   = num(row.get_attribute("data-level") or 0)
            race    = row.get_attribute("data-race")  or ""
            gold    = num(row.get_attribute("data-gold") or 0)
            fort_p  = num(row.get_attribute("data-fort") or 0)

            fort_hp = fort_p
            fort_max = 0
            fb = row.query_selector(".fort-bar")
            if fb:
                title = fb.get_attribute("title") or ""
                hpm = re.match(r"(\d+)/(\d+)", title)
                if hpm:
                    fort_hp = num(hpm.group(1))
                    fort_max = num(hpm.group(2))

            # "In range" = the row contains a real <button.attack-btn>.  Rows
            # that are out of range OR belong to YOU render a disabled span
            # (<span class="btn ... disabled">Attack</span>) instead.  Also
            # check the <tr class="disabled"> marker for belt-and-braces.
            in_range = (
                row.query_selector("button.attack-btn") is not None
                and "disabled" not in (row.get_attribute("class") or "")
            )

            is_bot     = "[bot]" in name.lower()
            is_clan    = row.query_selector(".clan-badge")    is not None
            is_friend  = row.query_selector(".friend-badge")  is not None
            is_hitlist = row.query_selector(".hitlist-badge") is not None

            # Daily attack counter — "(N/5)"
            attack_count = 0
            al = row.query_selector(".attack-limit")
            if al:
                txt = al.inner_text() or ""
                lm = ATTACK_LIMIT_RE.search(txt)
                if lm:
                    attack_count = int(lm.group(1))

            rows_out.append({
                "player_id":    pid,
                "name":         name,
                "level":        level,
                "race":         race,
                "gold":         gold,
                "fort_pct":     fort_p,
                "fort_hp":      fort_hp,
                "fort_max":     fort_max,
                "in_range":     in_range,
                "is_bot":       is_bot,
                "is_clan":      is_clan,
                "is_friend":    is_friend,
                "is_hitlist":   is_hitlist,
                "attack_count": attack_count,
                "list_page":    page_num,   # which attack-list page the form lives on
            })

        fp = ",".join(page_names)
        if not page_names or fp == last_fp:
            break
        last_fp = fp
    return rows_out


def scrape_spy_candidates(page, max_pages: int = 5) -> list:
    """Paginate /game/spy and return one dict per spy-able target row.
    The spy page is structurally different from the attack list — it has
    only name / level / race columns plus recon+sabotage forms — so we
    need a dedicated scraper.  Without this, spy mode would try to spy
    targets that aren't on the /game/spy page and get form_not_found
    forever.

    The spy page's pagination is broken: requesting ?page=N beyond the
    last page silently returns page 1 again, so we'd capture every player
    twice (or more) if max_pages is larger than the real page count.
    We dedupe by player_id via a seen-set AND break on a fingerprint
    match (same as scrape_attack_candidates).
    """
    rows_out = []
    seen_pids = set()
    last_fp = ""
    for page_num in range(1, max_pages + 1):
        url = f"{BASE_URL}/spy" + (f"?page={page_num}" if page_num > 1 else "")
        try:
            page.goto(url)
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            break
        # Use page.evaluate to pull every (pid, name, level, race, count, disabled)
        # in one round-trip — much faster than query_selector per row.
        js = r"""
          () => {
            const out = [];
            document.querySelectorAll('table tbody tr').forEach(tr => {
              const disabled = (tr.className || '').includes('disabled');
              const link = tr.querySelector('a.player-link');
              if (!link) return;
              // Name: direct text nodes only, excluding avatar/badge spans
              const name = Array.from(link.childNodes)
                .filter(n => n.nodeType === 3)
                .map(n => n.textContent.trim())
                .filter(Boolean)
                .join(' ')
                .trim();
              const href = link.getAttribute('href') || '';
              const idm  = href.match(/\/player\/(\d+)/);
              const pid  = idm ? parseInt(idm[1]) : 0;
              const tds  = tr.querySelectorAll('td');
              const level = tds[1] ? parseInt((tds[1].innerText || '').trim()) : 0;
              const race  = tds[2] ? (tds[2].innerText || '').trim()           : '';
              // Recon button carries the daily counter: "1/5 today"
              const reconBtn = tr.querySelector(
                'form[action*="/spy/"] button[title*="today"]');
              let spy_count = 0;
              if (reconBtn) {
                const tm = (reconBtn.getAttribute('title') || '').match(/(\d+)\s*\/\s*5/);
                if (tm) spy_count = parseInt(tm[1]);
              }
              const hasForm = !!tr.querySelector(
                'form[action*="/spy/"][class*="inline"]');
              if (name && pid && hasForm && !disabled) {
                out.push({pid, name, level, race, spy_count});
              }
            });
            return out;
          }
        """
        try:
            batch = page.evaluate(js)
        except Exception:
            break
        if not batch:
            break
        # Fingerprint: if this page returns the same pids as the previous
        # page, pagination has wrapped back to page 1 — stop scraping.
        fp = ",".join(str(e["pid"]) for e in batch)
        if fp == last_fp:
            break
        last_fp = fp
        added_this_page = 0
        for e in batch:
            pid = str(e["pid"])
            if pid in seen_pids:
                continue  # defensive: skip any intra-batch duplicates
            seen_pids.add(pid)
            added_this_page += 1
            rows_out.append({
                "player_id":    pid,
                "name":         e["name"],
                "level":        e["level"],
                "race":         e["race"],
                # Spy page has no gold / fort / in_range data — fill sensible defaults
                "gold":         0,
                "fort_pct":     0,
                "fort_hp":      0,
                "fort_max":     0,
                "in_range":     True,
                "is_bot":       "[bot]" in e["name"].lower(),
                "is_clan":      False,
                "is_friend":    False,
                "is_hitlist":   False,
                # Re-use the attack_count field name so pick_battle_targets
                # can apply the same <5 filter without branching.
                "attack_count": int(e["spy_count"]),
                "list_page":    page_num,
            })
        # If nothing new was added this page, stop — we've saturated the pool.
        if added_this_page == 0:
            break
    return rows_out


def read_reset_countdown(page) -> int:
    """Return seconds until the daily attack counter resets, or 0 if unknown.
    The attack page embeds <span id='attackResetTimer' data-seconds='7986'>2h 13m</span>."""
    try:
        el = page.query_selector("#attackResetTimer")
        if el:
            s = el.get_attribute("data-seconds") or "0"
            return int(s)
    except Exception:
        pass
    return 0


# ── Target picker (pure function — unit-testable) ───────────────────────────
def pick_battle_targets(rows: list, estimates: dict, our_stats: dict,
                         cfg: dict, mode: str = "attack",
                         spy_cooldown=None) -> list:
    """Filter + sort battle candidates.
    rows: list from scrape_attack_candidates() / scrape_spy_candidates()
    estimates: {Player name: {est_def, est_spy_def, ...}} from load_estimates_lookup()
    our_stats: {'atk', 'spy_off', 'gold', 'turns', 'level', ...}
    cfg: battle config dict. Recognised keys:
        margin         — minimum atk/def ratio (float, default 1.2)
        skip_friends   — skip marked-friend rows (bool, default True)
        skip_clan      — skip clanmates (bool, default True)
        skip_bots      — skip [BOT] rows (bool, default False)
        max_per_pass   — max targets returned per call (int, default 20)
        farm_mode      — "gold" (default), "xp", or "match":
                         • "gold"  = richest target first (def desc tiebreaker)
                         • "xp"    = highest-XP-per-turn first, proxied by
                                     (target level - our level)
                         • "match" = strongest-beatable target first. The
                                     margin filter already guarantees
                                     safety, so sorting by est_def DESC
                                     picks the hardest fight we can still
                                     win — uses our atk efficiently on
                                     challenge-matched fights instead of
                                     overkilling weak bots.
        min_gold       — attack-mode gold threshold. Targets carrying less
                         than this much gold on hand are skipped entirely.
                         Ignored in xp mode unless explicitly set.
    mode: 'attack' or 'spy'
    spy_cooldown: optional set of player names we've successfully spied
        within the last SPY_COOLDOWN_HOURS — filtered out in spy mode
        regardless of the daily (N/5) counter.

    Returns filtered+sorted list (same dict shape as rows, with extras)."""
    margin = float(cfg.get("margin", 1.2)) or 1.2
    if margin < 1.0:
        margin = 1.0
    skip_friends = bool(cfg.get("skip_friends", True))
    skip_clan    = bool(cfg.get("skip_clan",    True))
    skip_bots    = bool(cfg.get("skip_bots",    False))
    max_per_pass = int(cfg.get("max_per_pass",  20)) or 20
    farm_mode    = (cfg.get("farm_mode", "gold") or "gold").lower()
    try:
        min_gold = int(cfg.get("min_gold", 0) or 0)
    except (TypeError, ValueError):
        min_gold = 0

    our_atk     = int(our_stats.get("atk",     0))
    our_spy_off = int(our_stats.get("spy_off", 0))
    our_gold    = int(our_stats.get("gold",    0))
    our_turns   = int(our_stats.get("turns",   0))
    our_level   = int(our_stats.get("level",   1) or 1)

    cooldown_set = set(spy_cooldown) if spy_cooldown else set()

    # Filter telemetry — when the result list ends up empty, the caller
    # can use this to explain WHY and suggest which setting to loosen.
    reasons = {
        "out_of_range":   0,
        "friend":         0,
        "clan":           0,
        "bot":            0,
        "daily_cap_5_5":  0,
        "under_min_gold": 0,
        "no_def_est":     0,
        "margin_fail":    0,
        "spy_cooldown":   0,
        "spy_no_def_est": 0,
        "spy_margin_fail":0,
    }

    safe = []
    for r in rows:
        if not r.get("in_range", True):
            reasons["out_of_range"] += 1
            continue
        if skip_friends and r.get("is_friend"):
            reasons["friend"] += 1
            continue
        if skip_clan and r.get("is_clan"):
            reasons["clan"] += 1
            continue
        if skip_bots and r.get("is_bot"):
            reasons["bot"] += 1
            continue
        if r.get("attack_count", 0) >= BATTLE_DAILY_MAX:
            reasons["daily_cap_5_5"] += 1
            continue

        # Gold threshold — skip targets too poor to bother with. Applies
        # to attack mode only (spy mode has its own gold floor via cost).
        # In XP farming we usually care about levels, not wallets — if the
        # user hasn't set min_gold explicitly we let everything through.
        if mode == "attack" and min_gold > 0 and int(r.get("gold", 0) or 0) < min_gold:
            reasons["under_min_gold"] += 1
            continue

        # estimates CSV keys by player NAME (no PlayerID column), so join there.
        name_clean = str(r.get("name", "")).replace(" (YOU)", "").strip()
        est = estimates.get(name_clean, {})
        if mode == "attack":
            est_def = int(est.get("est_def", 0))
            if est_def <= 0:
                reasons["no_def_est"] += 1
                continue   # unknown defense → unsafe by default
            if our_atk < int(est_def * margin):
                reasons["margin_fail"] += 1
                continue
            r["est_def"] = est_def
            r["atk_ratio"] = our_atk / max(est_def, 1)
            safe.append(r)
        elif mode == "spy":
            # Persistent cooldown: if we successfully spied this name within
            # the last SPY_COOLDOWN_HOURS, skip.  Cross-session.
            if name_clean in cooldown_set:
                reasons["spy_cooldown"] += 1
                continue
            if our_gold  < SPY_GOLD_COST:  continue
            if our_turns < SPY_TURNS_COST: continue
            est_sd = int(est.get("est_spy_def", 0))
            if est_sd <= 0:
                reasons["spy_no_def_est"] += 1
                continue
            if our_spy_off < int(est_sd * margin):
                reasons["spy_margin_fail"] += 1
                continue
            r["est_spy_def"] = est_sd
            r["spy_ratio"] = our_spy_off / max(est_sd, 1)
            safe.append(r)

    if mode == "attack":
        if farm_mode == "match":
            # Challenge-matched: pick the STRONGEST target we can still
            # safely beat first. The filter above already enforces
            # est_def <= our_atk / margin, so every surviving row is a
            # safe fight — sorting by est_def DESC then gives us the
            # hardest beatable fight, which maximizes:
            #   • XP per attack (harder target → more XP)
            #   • Gold ceiling (stronger players are usually richer
            #     over time, even if their wallet is empty RIGHT NOW)
            #   • Efficient use of our atk (no overkill on def-83 bots)
            # Ties broken by gold (rich beats poor) then fort_pct.
            safe.sort(key=lambda x: (
                -int(x.get("est_def",  0) or 0),
                -int(x.get("gold",     0) or 0),
                -int(x.get("fort_pct", 0) or 0),
            ))
        elif farm_mode == "xp":
            # Estimate XP gain per attack. DarkThrone awards more XP when
            # the target is higher-level than you, so target_level - our_level
            # is a good proxy. Floor at 1 so equal/lower-level targets still
            # rank above nothing. Secondary sort is est_def DESC (harder
            # fights at the same level give more XP), then gold.
            for t in safe:
                tl = int(t.get("level", 0) or 0)
                t["xp_score"] = max(1, tl - our_level + 5)
            safe.sort(key=lambda x: (
                -x.get("xp_score", 0),
                -int(x.get("est_def", 0) or 0),
                -x.get("gold", 0),
                -x.get("fort_pct", 0),
            ))
        else:  # "gold"
            # Richest target first, but tie-break by est_def DESC so a
            # stronger target beats a weaker one when they're sitting on
            # the same pile of gold. This prevents the bot from farming
            # the same def-83 bot over and over just because they
            # regenerate gold quickly.
            safe.sort(key=lambda x: (
                -x.get("gold", 0),
                -int(x.get("est_def", 0) or 0),
                -x.get("fort_pct", 0),
            ))
    else:
        safe.sort(key=lambda x: (-x.get("gold", 0), -x.get("spy_ratio", 0)))

    # Stash telemetry on the result list so the caller can log WHY
    # there are no safe targets. Using a list subclass would be cleaner
    # but setattr works fine for our one-tick use case.
    result = safe[:max_per_pass]
    try:
        # Lists can't carry attributes, so return a small wrapper instead
        # when the caller needs the reasons. Callers that don't use it
        # still iterate / slice it like a list.
        class _PickResult(list):
            reasons = {}
            pool_size = 0
        wrapped = _PickResult(result)
        wrapped.reasons   = reasons
        wrapped.pool_size = len(rows)
        return wrapped
    except Exception:
        return result


# ── Action executors ─────────────────────────────────────────────────────────
def _wait_post_submit(page, old_url: str, url_hint: str = "") -> None:
    """After form.submit() the navigation kicks off asynchronously and
    Playwright's wait_for_load_state('networkidle') can return while the
    OLD page is still displayed.  This helper:
      1. waits for the URL to change (up to 15s)
      2. waits for domcontentloaded on the new page
      3. waits for networkidle
      4. short sleep so animated banners finish rendering
    """
    try:
        page.wait_for_url(
            lambda u: u != old_url and (url_hint in u if url_hint else True),
            timeout=15000,
        )
    except Exception:
        pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(0.8)


def _submit_attack_form(page, player_id: str, turns: int) -> dict:
    """Submit the hidden <form id='attack-form-{id}'> via page.evaluate.
    Sets the turns input value, dispatches input/change events (so any JS
    listeners run), asserts the honeypot input is empty, then calls
    form.submit() which posts natively with cookies + _token intact.

    Returns the JS result dict: {ok: bool, err?: str}.  Caller decides
    whether a failure is transient (skip target) or fatal (BattleStop).
    The only fatal case here is honeypot_filled — that means the page has
    a bot-detection script filling the field, which we must never submit.
    """
    js = r"""
    (args) => {
        const form = document.getElementById('attack-form-' + args.pid);
        if (!form) return {ok:false, err:'form_not_found'};
        const hp = form.querySelector('input[name="website_url"]');
        if (hp && hp.value) return {ok:false, err:'honeypot_filled'};
        const ti = form.querySelector('input[name="turns"], input.turns-input, select[name="turns"]');
        if (!ti) return {ok:false, err:'turns_input_not_found'};
        ti.value = String(args.turns);
        ti.dispatchEvent(new Event('input',  {bubbles:true}));
        ti.dispatchEvent(new Event('change', {bubbles:true}));
        form.submit();
        return {ok:true};
    }
    """
    result = page.evaluate(js, {"pid": str(player_id), "turns": int(turns)})
    if not result.get("ok") and result.get("err") == "honeypot_filled":
        # Bot-detection script is filling the honeypot — refuse to submit.
        raise BattleStop("error", "honeypot_filled")
    return result


def _submit_spy_form(page, player_id: str) -> dict:
    """Submit the spy recon form by clicking its button via page.evaluate.
    The spy page has one <form action='/game/spy/{id}'> per target — we find
    it by action URL and call form.submit().  Returns the JS result dict;
    caller handles transient failures."""
    js = r"""
    (args) => {
        const forms = document.querySelectorAll('form[action$="/spy/' + args.pid + '"]');
        if (!forms.length) return {ok:false, err:'form_not_found'};
        const form = forms[0];
        const hp = form.querySelector('input[name="website_url"]');
        if (hp && hp.value) return {ok:false, err:'honeypot_filled'};
        const op = form.querySelector('input[name="operation"]');
        if (op) op.value = 'recon';
        form.submit();
        return {ok:true};
    }
    """
    result = page.evaluate(js, {"pid": str(player_id)})
    if not result.get("ok") and result.get("err") == "honeypot_filled":
        raise BattleStop("error", "honeypot_filled")
    return result


def _parse_battle_result(page) -> dict:
    """Inspect the page after a POST and return a result dict.
    Recognized keys: result, xp, gold_gained, detail.
    result ∈ {win, loss, out_of_range, exhausted, spy_ok, spy_fail, unknown}.

    IMPORTANT — the attack-list page contains '<option>Out of range</option>'
    in a filter dropdown, so a naive `"out of range" in body` match produces a
    false positive on EVERY response.  We use context-rich phrases that only
    appear in actual error messages, not UI labels.
    """
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if "/login" in url:
        raise BattleStop("session", "redirected to login")

    body = _page_text(page)   # content()+strip, more reliable than inner_text()

    def has(pattern: str) -> bool:
        return re.search(pattern, body, re.IGNORECASE) is not None

    # ── Hard stops that unwind the loop ────────────────────────────────────
    # "insufficient turns" is specific enough; "not enough turns" + context.
    if has(r"insufficient\s+turns") or has(r"not\s+enough\s+turns\s+(to|for|remaining)"):
        raise BattleStop("turns", "game reports insufficient turns")
    if has(r"not\s+enough\s+gold") or has(r"insufficient\s+gold"):
        raise BattleStop("gold", "game reports insufficient gold")

    # ── Per-target errors (skip this target, keep looping) ─────────────────
    # Require sentence context — "you are / target is / they are out of range"
    # — so the filter dropdown option ("Out of range") doesn't match.
    if has(r"(you|target|player|they)\s+(is|are|'re)\s+out\s+of\s+(range|your\s+range)"):
        return {"result": "out_of_range"}
    if has(r"(already\s+attacked.*\d+.*times|attack.*limit.*reached|5\s*/\s*5\s+today|reached.*daily.*limit)"):
        return {"result": "exhausted"}

    # ── Parse XP / gold numbers out of any victory phrasing ────────────────
    xp_m = (re.search(r"gained\s+([\d,]+)\s*xp", body, re.I)
            or re.search(r"\+\s*([\d,]+)\s*xp",   body, re.I)
            or re.search(r"experience[^0-9]+([\d,]+)", body, re.I))
    gold_m = (re.search(r"stole\s+([\d,]+)\s*gold", body, re.I)
              or re.search(r"\+\s*([\d,]+)\s*gold",  body, re.I)
              or re.search(r"plundered[^0-9]+([\d,]+)", body, re.I))
    xp = num(xp_m.group(1))   if xp_m   else 0
    gg = num(gold_m.group(1)) if gold_m else 0

    # ── Spy-specific outcomes ──────────────────────────────────────────────
    if has(r"(spy|recon|reveal|intelligence).*?(success|gathered|complete)"):
        return {"result": "spy_ok", "xp": 0, "gold_gained": 0}
    if has(r"(spy|recon).*?(fail|caught|alert)"):
        return {"result": "spy_fail", "xp": 0, "gold_gained": 0}

    # ── Attack outcomes ────────────────────────────────────────────────────
    if has(r"(you\s+lost|attack\s+failed|defeat|unsuccessful\s+attack)"):
        return {"result": "loss", "xp": xp, "gold_gained": gg}
    if has(r"(victory|you\s+won|successful\s+attack|battle\s+report|you\s+attacked)"):
        return {"result": "win", "xp": xp, "gold_gained": gg}
    if xp > 0 or gg > 0:
        return {"result": "win", "xp": xp, "gold_gained": gg}

    # No marker matched — unknown.  Don't claim success, don't claim failure.
    return {"result": "unknown"}


# ── Intelligence harvesting (learn from battle + spy reports) ──────────────
#
# Every successful attack or recon spy surfaces the target's actual stats on
# the response page.  We parse those out and append them to private_intel.csv
# so the estimator can use fresh, confirmed values on the next calibration
# pass.  The overlay is read in three places:
#   - load_estimates_lookup() merges intel rows so pick_battle_targets sees
#     the latest defense when picking who to hit next
#   - calibrate_models() merges intel rows into CONFIRMED_STATS so the rank
#     model anchors on fresh data
#   - estimate() treats intel as a floor (same as hardcoded confirmed stats)

INTEL_FILE = "private_intel.csv"
INTEL_COLUMNS = [
    "Timestamp", "Source", "Player", "Level", "Race", "Class",
    "ATK", "DEF", "SpyOff", "SpyDef",
    "Gold", "Bank", "Citizens",
    "Workers", "Soldiers", "Guards", "Spies", "Sentries",
    "FortHP", "FortMax", "Notes",
]

# Intel rows older than this are ignored by load_intel_overlay() — stats
# change fast in PvP so anything older than a day is considered stale.
# Bumped from 6h → 24h after confirming fresh intel (~4h old) was still
# matching live in-game leaderboards exactly.
INTEL_MAX_AGE_HOURS = 24

# record_intel() prunes rows older than this on every append. Keeps the
# file bounded without losing recent history, and avoids the load loops
# having to iterate thousands of dead rows from old sessions.
INTEL_RETENTION_DAYS = 7

# Cutoff used by calibrate_models() when collecting rank-model anchor
# points from the merged CONFIRMED_STATS + intel dict. Players grow
# every tick, so a 1-week-old "confirmed" snapshot is already stale —
# feeding it to the exponential fit drags the whole rank curve down
# and makes the top end underestimate. Anything older than this (or
# lacking a timestamp entirely) is NOT used for calibration, but it's
# still used as a FLOOR in estimate() via max(confirmed, model).
CALIBRATION_FRESH_HOURS = 48

# ── Player growth tracking ─────────────────────────────────────────────
# Every tick, each player earns income and spends it on troops/gear/
# buildings.  By recording per-player stat estimates over time we can
# compute a growth rate and PROJECT current stats forward — so the
# auto-battle targeting uses predicted-NOW instead of stale-yesterday.
#
# NOTE: constant named PLAYER_GROWTH_FILE (not GROWTH_FILE) because
# GROWTH_FILE at line 36 already refers to the OWN-player tick history
# (private_optimizer_growth.json). Re-assigning that name here would
# silently hijack record_growth() and destroy the per-tick chart data.
PLAYER_GROWTH_FILE      = "private_player_growth.csv"
PLAYER_GROWTH_COLUMNS   = [
    "Timestamp", "Player", "Level",
    "EstATK", "EstDEF", "EstSpyOff", "EstSpyDef",
    "EstIncome", "Confidence", "Source",
]
PLAYER_GROWTH_RETENTION_DAYS    = 14    # prune rows older than this on every append
PLAYER_GROWTH_MIN_OBSERVATIONS  = 2     # need ≥2 data points to compute a rate
PLAYER_GROWTH_WINDOW_TICKS      = 336   # 7 days × 48 ticks/day — rate window

# ── Phase 4: projection-vs-intel feedback loop ─────────────────────────────
# Every tick the estimator publishes a projected ATK/DEF/spy for each player
# (via estimate()).  When a fresh spy report later lands on that same player
# we can pair the two — projection at time T, actual at time T' — and compute
# an error %.  Accumulated over many pairs this teaches us:
#   • WHICH players have stable predictions (we can trust their estimate)
#   • WHICH (race, class) buckets have drift (the generic model is wrong for
#     that build archetype, so lower its confidence)
#   • WHICH stat channels the model systematically misses (e.g. spy_def may
#     consistently under-predict because we don't model Spy Academy well)
#
# The output feeds `conf_score` on every estimate dict so the dashboard can
# show "Carrot estimate confidence: 78%" instead of a single CONF/UPPER
# label.  Also feeds battle targeting — only attack when conf_score is high
# enough to trust the def estimate.
PROJECTION_LOG_FILE         = "private_projection_log.csv"
PROJECTION_LOG_COLUMNS      = [
    "Timestamp", "Player", "Level", "Race", "Class",
    "ProjATK", "ProjDEF", "ProjSpyOff", "ProjSpyDef", "ConfLabel",
]
PROJECTION_RETENTION_DAYS   = 7      # 14 days of projections is overkill; we
                                     # only use the last few days anyway
# Live (in-memory, rebuilt per tick inside calibrate_models) — consumed by
# estimate() to stamp conf_score on each player's return dict.
PROJECTION_ERRORS_LIVE: dict = {}    # {player: {atk_err_pct, def_err_pct, ..., samples}}
BUCKET_ERRORS_LIVE:     dict = {}    # {(race, cls): {atk_err_pct_median, ..., samples}}


def _page_text(page) -> str:
    """Return the page body text, flattened for regex matching.

    We use page.content() + manual tag stripping rather than
    page.inner_text("body") because inner_text respects CSS visibility
    and SKIPS elements with display:none / opacity:0 / visibility:hidden.
    The spy report page renders its 'Operation Successful' banner and
    stats block inside an animated container that Playwright's
    inner_text misses at the moment we read the page — content() gets
    them because they're in the DOM even if not yet painted.
    """
    try:
        html = page.content() or ""
    except Exception:
        return ""
    html = re.sub(r"<script[\s\S]*?</script>", " ", html)
    html = re.sub(r"<style[\s\S]*?</style>",   " ", html)
    html = re.sub(r"<[^>]+>", " ", html)
    html = html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    html = html.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", html).strip()


def _parse_kmb(s: str) -> int:
    """Parse '1.62M', '3.4K', '1,234,567', or '12345' into an int.
    Used for spy report numbers that sometimes use the short suffix form."""
    if s is None:
        return 0
    t = str(s).strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([KkMmBb]?)$", t)
    if not m:
        # plain integer fallback
        try:    return int(re.sub(r"\D", "", t) or 0)
        except Exception: return 0
    mag = {"": 1, "K": 1_000, "k": 1_000,
           "M": 1_000_000, "m": 1_000_000,
           "B": 1_000_000_000, "b": 1_000_000_000}
    try:
        return int(float(m.group(1)) * mag.get(m.group(2), 1))
    except Exception:
        return 0


def parse_attack_report(page) -> dict:
    """Parse a /game/attack/log/{id} battle-report page.
    Returns dict with target_name / target_level / target_race / target_class
    / target_def (from 'Final Defense N') / xp_gained / gold_stolen / result.
    Missing fields default to 0 / '' so the caller can feed the dict straight
    into record_intel() without extra guards."""
    text = _page_text(page)
    out = {"source": "attack"}

    if re.search(r"\bVICTORY\b", text):
        out["result"] = "win"
    elif re.search(r"\bDEFEAT\b", text):
        out["result"] = "loss"
    else:
        out["result"] = "unknown"

    # "You attacked <Name> (Level N)" — name may contain spaces/brackets
    m = re.search(r"You\s+attacked\s+(.+?)\s*\(Level\s+(\d+)\)", text)
    if m:
        out["target_name"]  = m.group(1).strip()
        out["target_level"] = int(m.group(2))

    # "VS <Name> <Race> <Class> - Lv.N  <def_value> Defense"
    # Cross-reference to populate race/class if we already have a name.
    if out.get("target_name"):
        name_pat = re.escape(out["target_name"])
        m = re.search(
            rf"{name_pat}\s+(\w+)\s+(\w+)\s*-\s*Lv\.(\d+)\s+([\d,]+)\s*Defense",
            text
        )
        if m:
            out["target_race"]  = m.group(1)
            out["target_class"] = m.group(2)
            # Prefer the parenthetical "(Level N)" above but accept this too
            out.setdefault("target_level", int(m.group(3)))

    # "Final Defense N" — the authoritative post-calc defense we defeated
    m = re.search(r"Final\s+Defense\s+([\d,]+)", text)
    if m:
        out["target_def"] = num(m.group(1))

    # "+N Gold Stolen" / "+N Experience"
    m = re.search(r"\+\s*([\d,]+)\s*Gold\s+Stolen", text, re.I)
    if m: out["gold_stolen"] = num(m.group(1))
    m = re.search(r"\+\s*([\d,]+)\s*Experience", text, re.I)
    if m: out["xp_gained"] = num(m.group(1))

    return out


def parse_spy_report(page) -> dict:
    """Parse a /game/spy/log/{id} intelligence-report page.  Returns dict
    with target_name / target_level / target_race / target_class / target_atk
    / target_def / target_spy_off / target_spy_def / target_gold / target_bank
    / target_citizens / target_fort_hp / target_fort_max / xp_gained /
    army unit counts / result.

    Handles two page formats:
      - FRESH spy result ("Operation Successful / Intel gathered successfully - Name !")
      - HISTORICAL log entry ("Intelligence Report: Name")
    The historical form is what you get when you navigate to an old
    /game/spy/log/{id} URL from the Spy Logs history list — same stat
    block, different headline.
    """
    text = _page_text(page)
    out = {"source": "spy"}

    fresh_success = ("Operation Successful" in text
                     or "Intel gathered successfully" in text)
    historical    = bool(re.search(r"Intelligence\s+Report\s*:", text))
    has_stat_block = bool(re.search(
        r"Total\s+Offense\s+[\d,]+\s+Total\s+Defense\s+[\d,]+", text))

    if fresh_success or (historical and has_stat_block):
        out["result"] = "spy_ok"
    elif re.search(r"(operation|spy|recon)\s+(failed|unsuccessful)|caught|detected|alerted", text, re.I):
        out["result"] = "spy_fail"
    else:
        out["result"] = "unknown"

    if out["result"] != "spy_ok":
        return out   # failed / unknown spies don't reveal the stat block

    # Target name lives in one of two places depending on format:
    #   FRESH:      "Intel gathered successfully - <Name> !"
    #   HISTORICAL: "Intelligence Report: <Name>  Date: <...>"
    # (The historical form's name is terminated by "Date:" or by the
    # end-of-line — match non-greedy and stop before those sentinels.)
    m = re.search(r"Intel\s+gathered\s+successfully\s*-\s*(.+?)\s*[!\?]", text)
    if not m:
        m = re.search(
            r"Intelligence\s+Report\s*:\s*(.+?)\s+Date\s*:",
            text
        )
    if m:
        out["target_name"] = m.group(1).strip()

    # "Experience +N XP"
    m = re.search(r"Experience\s+\+\s*([\d,]+)\s*XP", text)
    if m: out["xp_gained"] = num(m.group(1))

    # "Level N  Race <R>  Class <C>"
    m = re.search(r"Level\s+(\d+)\s+Race\s+(\w+)\s+Class\s+(\w+)", text)
    if m:
        out["target_level"] = int(m.group(1))
        out["target_race"]  = m.group(2)
        out["target_class"] = m.group(3)

    # "Fort HP N / M"
    m = re.search(r"Fort\s+HP\s+([\d,]+)\s*/\s*([\d,]+)", text)
    if m:
        out["target_fort_hp"]  = num(m.group(1))
        out["target_fort_max"] = num(m.group(2))

    # The 4 combat stats appear as a contiguous block:
    #   "Total Offense N  Total Defense N  Spy Offense N  Spy Defense N"
    # Match the whole block in one pattern so we don't collide with the
    # army section where "Spy Offense" is a column label for unit counts.
    m = re.search(
        r"Total\s+Offense\s+([\d,]+)\s+"
        r"Total\s+Defense\s+([\d,]+)\s+"
        r"Spy\s+Offense\s+([\d,]+)\s+"
        r"Spy\s+Defense\s+([\d,]+)",
        text
    )
    if m:
        out["target_atk"]     = num(m.group(1))
        out["target_def"]     = num(m.group(2))
        out["target_spy_off"] = num(m.group(3))
        out["target_spy_def"] = num(m.group(4))

    # Resources — both gold values can use the K/M/B short form.
    m = re.search(r"Gold\s+on\s+Hand\s+([\d\.,]+\s*[KkMmBb]?)", text)
    if m: out["target_gold"] = _parse_kmb(m.group(1))
    m = re.search(r"Gold\s+in\s+Bank\s+([\d\.,]+\s*[KkMmBb]?)", text)
    if m: out["target_bank"] = _parse_kmb(m.group(1))
    # Citizens — match AFTER "Gold in Bank ..." so we don't grab the
    # nav-bar's "Gold 0 Citizens X Turns ..." string that appears earlier
    # in the flattened page text. A bare /Citizens (\d+)/ will pick up my
    # navbar value (~3000) instead of the target's idle count.
    m = re.search(
        r"Gold\s+in\s+Bank\s+[\d\.,]+\s*[KkMmBb]?\s+Citizens\s+([\d,]+)",
        text
    )
    if m: out["target_citizens"] = num(m.group(1))

    # Army composition.  "Spy Offense" / "Spy Defense" as role labels COLLIDE
    # with the same strings used in the stats block, so we slice the text at
    # "Army Composition" and only look in the second half.  Every army row
    # has the pattern "Tn <UnitName> <Role> <Count> ..." where <Count> is
    # the first number immediately after the role label.
    army_text = text
    idx = text.find("Army Composition")
    if idx >= 0:
        # Cut off at the next section ("Armory Inventory" / "Buildings" / etc.)
        tail = text[idx:]
        for stop in ("Armory Inventory", "Buildings", "Share this intel"):
            s = tail.find(stop)
            if s > 0:
                tail = tail[:s]
                break
        army_text = tail
    # Flat count regex — preserved for backward compat with any consumer
    # that reads target_workers/soldiers/etc. scalars.  The rich-row
    # regex below ALSO populates these same keys when it matches; the
    # flat regex is the fallback for reports where the rich pattern
    # (Tn + unit name + role + qty + bonus + total) isn't present.
    for role, key in [
        ("Workers",            "target_workers"),
        ("Offensive Military", "target_soldiers"),
        ("Defensive Military", "target_guards"),
        ("Spy Offense",        "target_spies"),
        ("Spy Defense",        "target_sentries"),
    ]:
        m = re.search(rf"{re.escape(role)}\s+([\d,]+)", army_text)
        if m:
            out[key] = num(m.group(1))

    # Rich-row army parse (Phase 3): full "Tn <UnitName> <Role> <Qty> <Bonus> <Total>"
    # match gives us per-unit stat (the Bonus column), which we need to
    # reconstruct exact ATK/DEF in estimate() when rich intel is fresh.
    # Role strings collide with stat labels so we work within the sliced
    # army_text only (same approach as the flat parser above).
    ARMY_ROLES = (
        "Offensive Military", "Defensive Military",
        "Spy Offense",        "Spy Defense",
        "Workers",
    )
    role_to_key = {
        "Workers":            "workers",
        "Offensive Military": "soldiers",
        "Defensive Military": "guards",
        "Spy Offense":        "spies",
        "Spy Defense":        "sentries",
    }
    army_pat = re.compile(
        r"T(\d+)\s+"
        r"([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,3}?)\s+"
        rf"({'|'.join(re.escape(r) for r in ARMY_ROLES)})\s+"
        r"([\d,]+)\s+([\d,]+)\s+([\d,]+)"
    )
    army_detail: dict = {}
    for mm in army_pat.finditer(army_text):
        tier = int(mm.group(1))
        unit = mm.group(2).strip()
        role = mm.group(3).strip()
        qty  = num(mm.group(4))
        bonus_per_unit = num(mm.group(5))
        total = num(mm.group(6))
        key = role_to_key.get(role, role.lower().replace(" ", "_"))
        army_detail[key] = {
            "tier":  tier,
            "unit":  unit,
            "qty":   qty,
            "bonus": bonus_per_unit,   # per-unit stat contribution
            "total": total,            # qty × bonus (sanity-check field)
        }
    if army_detail:
        out["target_army_detail"] = army_detail

    # ── Armory Inventory ────────────────────────────────────────────────
    # Spy page shows a table: Tn <Item Name> <Category> <Qty> <Bonus> <Total>
    # where <Category> is one of 8 known strings.  Slice the text between
    # "Armory Inventory" and the next section ("Buildings") so item-name
    # regex can't run past the boundary.  The per-row regex anchors on the
    # category string (longest-first in an alternation to avoid prefix
    # collisions — "Spy Defense Armor" must match before "Defense Armor").
    armory_text = ""
    idx = text.find("Armory Inventory")
    if idx >= 0:
        tail = text[idx:]
        for stop in ("Buildings", "Battle Upgrades", "Share this intel"):
            s = tail.find(stop)
            if s > 0:
                tail = tail[:s]
                break
        armory_text = tail

    ARMORY_CATEGORIES = (
        "Spy Offense Weapons", "Spy Offense Armor",
        "Spy Defense Weapons", "Spy Defense Armor",
        "Offense Weapons",     "Offense Armor",
        "Defense Weapons",     "Defense Armor",
    )
    armory: dict = {}
    if armory_text:
        cat_alt = "|".join(re.escape(c) for c in ARMORY_CATEGORIES)
        # T<tier> <Item Words> <Category> <Qty> <Bonus> <Total>
        # Item name is 1-4 words starting with an uppercase letter.  The
        # non-greedy quantifier + category anchor means we always stop at
        # the right category boundary even if the item name contains
        # ambiguous words.
        pat = re.compile(
            r"T(\d+)\s+([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,3}?)\s+"
            rf"({cat_alt})\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)"
        )
        for mm in pat.finditer(armory_text):
            tier  = int(mm.group(1))
            item  = mm.group(2).strip()
            cat   = mm.group(3).strip()
            qty   = num(mm.group(4))
            bonus = num(mm.group(5))
            total = num(mm.group(6))
            armory[item] = {
                "tier":     tier,
                "category": cat,
                "qty":      qty,
                "bonus":    bonus,   # per-item stat contribution
                "total":    total,   # qty × bonus (sanity-check field)
            }
    if armory:
        out["target_armory"] = armory

    # ── Buildings ────────────────────────────────────────────────────────
    # "<Name> Level <N>" for each of 7 known buildings.  Anchor on known
    # names (closed set) to avoid matching "Level N" strings from other
    # contexts (e.g. the player's "Level 29" in demographics).
    BUILDING_NAMES = (
        "Fortification", "Armory", "Mine", "Spy Academy",
        "Barracks", "Housing", "Mercenary Camp",
    )
    buildings_text = ""
    idx = text.find(" Buildings ")   # leading space avoids matching "Buildings" inside other words
    if idx < 0:
        # Fallback: plain find.  We rely on the section being distinct enough.
        idx = text.find("Buildings")
    if idx >= 0:
        tail = text[idx:]
        for stop in ("Battle Upgrades", "Share this intel"):
            s = tail.find(stop)
            if s > 0:
                tail = tail[:s]
                break
        buildings_text = tail

    buildings: dict = {}
    if buildings_text:
        name_alt = "|".join(re.escape(n) for n in BUILDING_NAMES)
        for mm in re.finditer(
            rf"({name_alt})\s+Level\s+(\d+)", buildings_text
        ):
            # snake_case the building name for stable JSON keys
            bname = mm.group(1).lower().replace(" ", "_")
            buildings[bname] = int(mm.group(2))
    if buildings:
        out["target_buildings"] = buildings

    # ── Battle Upgrades ──────────────────────────────────────────────────
    # Table: <Upgrade Name> Battle Upgrades - <Kind> <Qty> <Bonus>
    # Where <Kind> ∈ {Offense, Defense, Spy}.  Kind doubles as the stat
    # channel the upgrade boosts.
    upgrades_text = ""
    idx = text.find("Battle Upgrades")
    if idx >= 0:
        # Skip past the section header itself (the page has "Battle Upgrades"
        # as a label AND as part of each row's category cell) — start the
        # slice AFTER the last column header ("BONUS") so the upgrade-name
        # regex can't capture header words like "BONUS Steed" as a name.
        tail = text[idx:]
        hdr = tail.find("BONUS")
        if 0 < hdr < 200:
            # Advance past "BONUS" itself so the regex starts at the first
            # data row's upgrade name.
            tail = tail[hdr + len("BONUS"):]
        for stop in ("Share this intel", "Report abuse"):
            s = tail.find(stop)
            if s > 0:
                tail = tail[:s]
                break
        upgrades_text = tail

    upgrades: dict = {}
    if upgrades_text:
        pat = re.compile(
            r"([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,3}?)\s+"
            r"Battle\s+Upgrades\s*-\s*(Offense|Defense|Spy)\s+"
            r"([\d,]+)\s+([\d,]+)"
        )
        for mm in pat.finditer(upgrades_text):
            uname = mm.group(1).strip()
            upgrades[uname] = {
                "kind":  mm.group(2),   # Offense | Defense | Spy
                "qty":   num(mm.group(3)),
                "bonus": num(mm.group(4)),
            }
    if upgrades:
        out["target_upgrades"] = upgrades

    return out


# ── Rich per-target intel snapshot ────────────────────────────────────────
# private_target_intel.json is a companion to private_intel.csv.  The CSV is
# append-only (historical audit log), bounded by INTEL_RETENTION_DAYS.  The
# JSON is latest-per-player (keyed by name), storing everything the spy-report
# parser found — including nested dicts (army / armory / buildings / upgrades)
# that don't fit the flat CSV schema.  Consumers that need full context
# (estimate(), growth-model trainer) read the JSON; consumers that only need
# recent combat stats keep reading the CSV.
TARGET_INTEL_FILE            = "private_target_intel.json"
TARGET_INTEL_RETENTION_HOURS = 24 * 14   # 14 days — matches CSV retention


def load_target_intel_snapshot() -> dict:
    """Load {player_name: rich_intel_dict} from private_target_intel.json.
    Returns {} if the file is missing or unparseable — never raises."""
    path = TARGET_INTEL_FILE
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_target_intel_snapshot(snap: dict) -> None:
    """Atomic write of the full snapshot back to disk, pruning entries older
    than TARGET_INTEL_RETENTION_HOURS so stale data doesn't pile up."""
    if not isinstance(snap, dict):
        return
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=TARGET_INTEL_RETENTION_HOURS)
    pruned = {}
    for name, rec in snap.items():
        ts = (rec or {}).get("captured_at", "")
        row_dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                row_dt = datetime.datetime.strptime(ts, fmt)
                break
            except (ValueError, TypeError):
                pass
        if row_dt is None or row_dt >= cutoff:
            pruned[name] = rec
    _unhide(TARGET_INTEL_FILE)
    tmp = TARGET_INTEL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pruned, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, TARGET_INTEL_FILE)


def record_target_intel(entry: dict) -> None:
    """Merge one parse_*_report() dict into the rich snapshot.  Latest per
    player wins — the key is the target name, and every call overwrites any
    prior entry for that player.  Kept deliberately small: canonicalize the
    entry into a fixed shape, then persist.  Callers should have already
    CSV-recorded the same entry via record_intel()."""
    name = (entry.get("target_name") or "").strip()
    if not name:
        return
    ts   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    snap = load_target_intel_snapshot()

    # Canonical shape — strip the "target_" prefix to match in-code
    # field names used by calibrate_models / estimate.  Missing scalar
    # fields are omitted (not zeroed) so a sparse report doesn't overwrite
    # an earlier richer capture with blanks.
    rec: dict = {
        "captured_at": ts,
        "source":      entry.get("source", "spy"),
    }
    for src_key, dst_key in (
        ("target_level",    "level"),
        ("target_race",     "race"),
        ("target_class",    "cls"),
        ("target_fort_hp",  "fort_hp"),
        ("target_fort_max", "fort_max"),
        ("target_atk",      "atk"),
        ("target_def",      "def"),
        ("target_spy_off",  "spy_off"),
        ("target_spy_def",  "spy_def"),
        ("target_gold",     "gold"),
        ("target_bank",     "bank"),
        ("target_citizens", "citizens"),
    ):
        v = entry.get(src_key)
        if v not in (None, "", 0):   # skip blanks/zeros (sparse-report safety)
            rec[dst_key] = v

    # Army composition — only include fields that were actually present
    # in the entry, so a minimal attack report doesn't clobber a rich spy
    # snapshot from hours ago.
    army = {}
    for src_key, dst_key in (
        ("target_workers",  "workers"),
        ("target_soldiers", "soldiers"),
        ("target_guards",   "guards"),
        ("target_spies",    "spies"),
        ("target_sentries", "sentries"),
    ):
        if src_key in entry and entry[src_key] not in (None, ""):
            army[dst_key] = int(entry[src_key])
    if army:
        rec["army"] = army

    # Nested rich fields — copy only when present
    if entry.get("target_armory"):      rec["armory"]      = dict(entry["target_armory"])
    if entry.get("target_buildings"):   rec["buildings"]   = dict(entry["target_buildings"])
    if entry.get("target_upgrades"):    rec["upgrades"]    = dict(entry["target_upgrades"])
    if entry.get("target_army_detail"): rec["army_detail"] = dict(entry["target_army_detail"])

    # Merge-in rather than replace: preserve prior rich fields when this
    # capture is sparser (e.g. an attack report after a fresh spy).  The
    # sparse-report safety above means scalars don't get overwritten with
    # zeros — but we still want to KEEP the prior armory/buildings if the
    # new entry doesn't include them.
    prior = snap.get(name) or {}
    merged = dict(prior)
    merged.update(rec)    # new scalar fields overwrite old ones
    # Nested dicts: keep prior nested data if new entry didn't supply it
    for nest in ("army", "armory", "buildings", "upgrades", "army_detail"):
        if nest in rec:
            merged[nest] = rec[nest]
        elif nest in prior:
            merged[nest] = prior[nest]

    snap[name] = merged
    try:
        save_target_intel_snapshot(snap)
    except Exception:
        # Never let snapshot-write failure block the rest of the intel pipe.
        pass


def record_intel(entry: dict, log_fn=None) -> None:
    """Append one row to private_intel.csv from a parse_*_report() dict.
    Silently no-ops if there's no target_name (parser failed)."""
    if not entry.get("target_name"):
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        ts,
        entry.get("source", ""),
        entry.get("target_name", ""),
        entry.get("target_level",    ""),
        entry.get("target_race",     ""),
        entry.get("target_class",    ""),
        entry.get("target_atk",      ""),
        entry.get("target_def",      ""),
        entry.get("target_spy_off",  ""),
        entry.get("target_spy_def",  ""),
        entry.get("target_gold",     ""),
        entry.get("target_bank",     ""),
        entry.get("target_citizens", ""),
        entry.get("target_workers",  ""),
        entry.get("target_soldiers", ""),
        entry.get("target_guards",   ""),
        entry.get("target_spies",    ""),
        entry.get("target_sentries", ""),
        entry.get("target_fort_hp",  ""),
        entry.get("target_fort_max", ""),
        entry.get("notes", ""),
    ]
    _unhide(INTEL_FILE)
    new = not os.path.isfile(INTEL_FILE)

    # Prune rows older than INTEL_RETENTION_DAYS days BEFORE appending, so
    # the file stays bounded. We keep the header + every row newer than
    # the cutoff, atomic-replace, then append the new row below.
    if not new:
        try:
            cutoff = datetime.datetime.now() - datetime.timedelta(days=INTEL_RETENTION_DAYS)
            kept, dropped = [], 0
            with open(INTEL_FILE, "r", encoding="utf-8") as rf:
                rd = csv.DictReader(rf)
                header = rd.fieldnames or INTEL_COLUMNS
                for r in rd:
                    ts_str = (r.get("Timestamp") or "").strip()
                    row_dt = None
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                        try:
                            row_dt = datetime.datetime.strptime(ts_str, fmt)
                            break
                        except ValueError:
                            pass
                    if row_dt is not None and row_dt < cutoff:
                        dropped += 1
                        continue
                    kept.append(r)
            if dropped > 0:
                tmp = INTEL_FILE + ".tmp"
                with open(tmp, "w", newline="", encoding="utf-8") as wf:
                    w = csv.DictWriter(wf, fieldnames=header)
                    w.writeheader()
                    w.writerows(kept)
                os.replace(tmp, INTEL_FILE)
                if log_fn:
                    log_fn(f"  🧹 pruned {dropped} intel row(s) >{INTEL_RETENTION_DAYS}d old", "dim")
        except Exception as e:
            # Never let pruning failure block new-row append.
            if log_fn:
                log_fn(f"  ⚠️  intel prune skipped: {e}", "dim")

    with open(INTEL_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(INTEL_COLUMNS)
        w.writerow(row)

    # Also merge into the rich per-target snapshot (Phase 1).  Carries any
    # nested fields (army / armory / buildings / upgrades) that the CSV
    # can't store.  Best-effort: snapshot-write failure never blocks the
    # CSV-based pipeline that every existing consumer depends on.
    try:
        record_target_intel(entry)
    except Exception as e:
        if log_fn:
            log_fn(f"  ⚠️  rich-intel snapshot update failed: {e}", "dim")

    if log_fn:
        src  = entry.get("source", "?")
        name = entry.get("target_name", "?")
        bits = []
        # Use 'in entry' (not truthy) so 0 def is counted as data, not "nothing".
        if "target_def"     in entry: bits.append(f"def={int(entry['target_def']):,}")
        if "target_atk"     in entry: bits.append(f"atk={int(entry['target_atk']):,}")
        if "target_spy_off" in entry: bits.append(f"spy_off={int(entry['target_spy_off']):,}")
        if "target_spy_def" in entry: bits.append(f"spy_def={int(entry['target_spy_def']):,}")
        # Indicate when we captured rich data on this pass
        rich_bits = []
        if entry.get("target_armory"):    rich_bits.append(f"armory={len(entry['target_armory'])}")
        if entry.get("target_buildings"): rich_bits.append(f"buildings={len(entry['target_buildings'])}")
        if entry.get("target_upgrades"):  rich_bits.append(f"upgrades={len(entry['target_upgrades'])}")
        if rich_bits:
            bits.append("rich=[" + " ".join(rich_bits) + "]")
        detail = "  ".join(bits) if bits else "no numbers"
        log_fn(f"  🛰  intel({src}) {name}: {detail}", "dim")

    # Also record this intel observation in the growth CSV so we get
    # exact data points for growth-rate computation. Intel readings are
    # the MOST reliable growth signal — two spies on the same player at
    # different times give an exact delta.
    if entry.get("target_name") and (entry.get("target_def") or entry.get("target_atk")):
        try:
            record_player_growth([{
                "Player":     entry["target_name"],
                "Level":      entry.get("target_level", ""),
                "EstATK":     entry.get("target_atk",    0),
                "EstDEF":     entry.get("target_def",    0),
                "EstSpyOff":  entry.get("target_spy_off", 0),
                "EstSpyDef":  entry.get("target_spy_def", 0),
                "EstIncome":  0,
                "Confidence": "INTEL",
            }], source="intel")
        except Exception:
            pass  # growth recording must never block intel recording


# ── Player growth tracking ─────────────────────────────────────────────────
# Same pattern as own-player growth (private_optimizer_growth.json) but for
# EVERY tracked player.  Append-only CSV, bounded by PLAYER_GROWTH_RETENTION_DAYS.
# Two data sources feed it:
#   1. estimator_run() → one row per player per tick (model estimates)
#   2. record_intel()  → one row per spy/attack report (exact observations)
# load_estimates_lookup() reads it back, computes per-player growth rates,
# and projects stats forward so the auto-battle uses predicted-NOW numbers.

def record_player_growth(results: list, source: str = "estimate") -> None:
    """Append player stat observations to the rolling growth CSV.

    results: list of dicts, each must have at least 'Player'. Keys consumed:
      Player, Level, EstATK, EstDEF, EstSpyOff, EstSpyDef, EstIncome, Confidence
    source: 'estimate' (from estimator_run) or 'intel' (from spy/attack report)
    """
    if not results:
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Prune rows older than PLAYER_GROWTH_RETENTION_DAYS ────────
    _unhide(PLAYER_GROWTH_FILE)
    if os.path.isfile(PLAYER_GROWTH_FILE):
        try:
            cutoff = datetime.datetime.now() - datetime.timedelta(days=PLAYER_GROWTH_RETENTION_DAYS)
            kept, dropped = [], 0
            with open(PLAYER_GROWTH_FILE, "r", encoding="utf-8") as rf:
                rd = csv.DictReader(rf)
                for r in rd:
                    ts_str = (r.get("Timestamp") or "").strip()
                    row_dt = None
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                        try:
                            row_dt = datetime.datetime.strptime(ts_str, fmt)
                            break
                        except ValueError:
                            pass
                    if row_dt is not None and row_dt < cutoff:
                        dropped += 1
                        continue
                    kept.append(r)
            if dropped > 0:
                tmp = PLAYER_GROWTH_FILE + ".tmp"
                with open(tmp, "w", newline="", encoding="utf-8") as wf:
                    w = csv.DictWriter(wf, fieldnames=PLAYER_GROWTH_COLUMNS)
                    w.writeheader()
                    w.writerows(kept)
                os.replace(tmp, PLAYER_GROWTH_FILE)
        except Exception:
            pass  # pruning failure must never block the append

    # ── Append new rows ────────────────────────────────────────────
    new_file = not os.path.isfile(PLAYER_GROWTH_FILE)
    with open(PLAYER_GROWTH_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(PLAYER_GROWTH_COLUMNS)
        for r in results:
            name = (r.get("Player") or "").strip()
            if not name or "(YOU)" in name:
                continue
            w.writerow([
                ts,
                name,
                r.get("Level", ""),
                r.get("EstATK",    0),
                r.get("EstDEF",    0),
                r.get("EstSpyOff", 0),
                r.get("EstSpyDef", 0),
                r.get("EstIncome") or r.get("EstIncomeTick") or 0,
                r.get("Confidence", ""),
                source,
            ])


def load_player_growth() -> dict:
    """Read the growth CSV and return per-player time series.

    Returns {Player: [{"ts": datetime, "atk": int, "def": int,
                        "spy_off": int, "spy_def": int, "source": str}, ...]}
    sorted by timestamp ascending per player.
    Only rows within the last PLAYER_GROWTH_WINDOW_TICKS ticks (~7 days) are loaded.
    """
    if not os.path.isfile(PLAYER_GROWTH_FILE):
        return {}
    cutoff = datetime.datetime.now() - datetime.timedelta(
        minutes=PLAYER_GROWTH_WINDOW_TICKS * 30)
    growth = {}
    try:
        with open(PLAYER_GROWTH_FILE, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = (row.get("Player") or "").strip()
                if not name:
                    continue
                ts_str = (row.get("Timestamp") or "").strip()
                row_dt = None
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        row_dt = datetime.datetime.strptime(ts_str, fmt)
                        break
                    except ValueError:
                        pass
                if row_dt is None or row_dt < cutoff:
                    continue
                entry = {
                    "ts":      row_dt,
                    "atk":     int(row.get("EstATK", 0) or 0),
                    "def":     int(row.get("EstDEF", 0) or 0),
                    "spy_off": int(row.get("EstSpyOff", 0) or 0),
                    "spy_def": int(row.get("EstSpyDef", 0) or 0),
                    "source":  (row.get("Source") or "").strip(),
                }
                growth.setdefault(name, []).append(entry)
    except Exception as e:
        print(f"  ⚠️  load_player_growth failed: {e}")
    # Sort each player's series by timestamp
    for series in growth.values():
        series.sort(key=lambda x: x["ts"])
    return growth


def compute_growth_rates(growth: dict) -> dict:
    """Compute per-player stat growth rates from historical observations.

    For each player with >= PLAYER_GROWTH_MIN_OBSERVATIONS data points, computes
    a simple linear slope: (latest - earliest) / ticks_between.
    Negative slopes are clamped to 0 — players don't lose stats in
    DarkThrone (gear and units are persistent).

    Returns {Player: {
        "def_per_tick": float, "atk_per_tick": float,
        "spy_off_per_tick": float, "spy_def_per_tick": float,
        "last_ts": datetime, "last_def": int, "last_atk": int,
        "last_spy_off": int, "last_spy_def": int,
        "observations": int,
    }}
    """
    rates = {}
    for name, series in growth.items():
        if len(series) < PLAYER_GROWTH_MIN_OBSERVATIONS:
            continue
        first = series[0]
        last  = series[-1]
        dt_seconds = (last["ts"] - first["ts"]).total_seconds()
        if dt_seconds < 1800:
            continue   # need at least 1 tick of separation
        ticks = dt_seconds / 1800.0

        # Sanity cap on observed rate — anything above this is almost
        # certainly bad data (model feedback loop from old bug, or a
        # player value jump caused by an intel correction). Real DT
        # growth is at most a few thousand stat per tick even for
        # top-level players.
        MAX_PLAUSIBLE_RATE_PER_TICK = 10_000

        def _slope(stat_key):
            v0 = first.get(stat_key, 0)
            v1 = last.get(stat_key, 0)
            if v1 <= 0 or v0 <= 0:
                return 0.0
            rate = max(0.0, (v1 - v0) / ticks)
            if rate > MAX_PLAUSIBLE_RATE_PER_TICK:
                return 0.0   # discard as noise
            return rate

        rates[name] = {
            "def_per_tick":     _slope("def"),
            "atk_per_tick":     _slope("atk"),
            "spy_off_per_tick": _slope("spy_off"),
            "spy_def_per_tick": _slope("spy_def"),
            "last_ts":          last["ts"],
            "last_def":         last.get("def", 0),
            "last_atk":         last.get("atk", 0),
            "last_spy_off":     last.get("spy_off", 0),
            "last_spy_def":     last.get("spy_def", 0),
            "observations":     len(series),
        }
    return rates


# ── Phase 4: projection log + error feedback ────────────────────────────────
def record_projections(results: list, log_fn=None) -> None:
    """Append today's tick projections to private_projection_log.csv.

    One row per player in `results`.  Called at the end of estimator_run()
    so we have a dated record of what the model predicted.  Later intel
    captures pair with these rows to compute accuracy deltas.
    """
    if not results:
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for r in results:
        name = (r.get("Player") or "").strip()
        if not name:
            continue
        rows.append([
            ts, name,
            int(r.get("Level",     0) or 0),
            (r.get("Race")   or "").strip(),
            (r.get("Class")  or "").strip(),
            int(r.get("EstATK",    0) or 0),
            int(r.get("EstDEF",    0) or 0),
            int(r.get("EstSpyOff", 0) or 0),
            int(r.get("EstSpyDef", 0) or 0),
            (r.get("Confidence") or "").strip(),
        ])
    if not rows:
        return

    _unhide(PROJECTION_LOG_FILE)
    new_file = not os.path.isfile(PROJECTION_LOG_FILE)

    # Prune rows older than PROJECTION_RETENTION_DAYS so the file stays bounded.
    if not new_file:
        try:
            cutoff = datetime.datetime.now() - datetime.timedelta(days=PROJECTION_RETENTION_DAYS)
            kept, dropped = [], 0
            with open(PROJECTION_LOG_FILE, "r", encoding="utf-8") as rf:
                rd = csv.DictReader(rf)
                header = rd.fieldnames or PROJECTION_LOG_COLUMNS
                for row in rd:
                    ts_str = (row.get("Timestamp") or "").strip()
                    dt = None
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                        try:    dt = datetime.datetime.strptime(ts_str, fmt); break
                        except ValueError: pass
                    if dt is not None and dt < cutoff:
                        dropped += 1
                        continue
                    kept.append(row)
            if dropped > 0:
                tmp = PROJECTION_LOG_FILE + ".tmp"
                with open(tmp, "w", newline="", encoding="utf-8") as wf:
                    w = csv.DictWriter(wf, fieldnames=header)
                    w.writeheader()
                    w.writerows(kept)
                os.replace(tmp, PROJECTION_LOG_FILE)
                if log_fn:
                    log_fn(f"  🧹 pruned {dropped} projection row(s) >{PROJECTION_RETENTION_DAYS}d old", "dim")
        except Exception as e:
            if log_fn:
                log_fn(f"  ⚠️  projection prune skipped: {e}", "dim")

    with open(PROJECTION_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(PROJECTION_LOG_COLUMNS)
        for row in rows:
            w.writerow(row)


def load_projection_log() -> dict:
    """Read the projection log back as a per-player time-series.

    Returns {player: [{ts, atk, def, spy_off, spy_def, level, race, cls}, ...]}
    sorted by ts ascending per player.
    """
    out: dict = {}
    if not os.path.isfile(PROJECTION_LOG_FILE):
        return out
    try:
        with open(PROJECTION_LOG_FILE, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = (row.get("Player") or "").strip()
                if not name:
                    continue
                ts_str = (row.get("Timestamp") or "").strip()
                dt = None
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:    dt = datetime.datetime.strptime(ts_str, fmt); break
                    except ValueError: pass
                if dt is None:
                    continue
                out.setdefault(name, []).append({
                    "ts":       dt,
                    "level":    int(row.get("Level",     0) or 0),
                    "race":     (row.get("Race")  or "").strip(),
                    "cls":      (row.get("Class") or "").strip(),
                    "atk":      int(row.get("ProjATK",   0) or 0),
                    "def":      int(row.get("ProjDEF",   0) or 0),
                    "spy_off":  int(row.get("ProjSpyOff",0) or 0),
                    "spy_def":  int(row.get("ProjSpyDef",0) or 0),
                    "conf":     (row.get("ConfLabel") or "").strip(),
                })
    except Exception as e:
        print(f"  ⚠️  load_projection_log failed: {e}")
    for series in out.values():
        series.sort(key=lambda x: x["ts"])
    return out


def compute_projection_errors(proj_log: dict, intel_overlay: dict) -> tuple:
    """Pair projections-at-time-T with intel captured near time T to compute
    per-stat error fractions.

    Returns (player_errs, bucket_errs) where:
      player_errs = {name: {atk_err_pct, def_err_pct, spy_off_err_pct,
                            spy_def_err_pct, samples, last_checked}}
      bucket_errs = {(race, cls): {atk_err_pct_median, ..., samples}}

    Pairing logic: for each player with BOTH a projection history and a
    fresh intel row, find the projection row NEWEST that's still older
    than the intel's timestamp by more than 1 tick (30 min).  That's the
    projection the model made which the intel then refuted/confirmed.
    """
    player_errs: dict = {}
    bucket_accumulator: dict = {}   # {(race, cls): {stat: [err_pcts]}}

    now = datetime.datetime.now()

    for name, intel in (intel_overlay or {}).items():
        if not intel or not isinstance(intel, dict):
            continue
        # Parse intel timestamp
        ts_str = (intel.get("timestamp") or intel.get("captured_at") or "").strip()
        intel_dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:    intel_dt = datetime.datetime.strptime(ts_str, fmt); break
            except ValueError: pass
        if intel_dt is None:
            continue

        # Need actual observed values to compare against
        actual_atk = int(intel.get("atk", 0)     or 0)
        actual_def = int(intel.get("def", 0)     or 0)
        actual_spo = int(intel.get("spy_off", 0) or 0)
        actual_spd = int(intel.get("spy_def", 0) or 0)

        # Most-recent projection BEFORE the intel was captured
        series = proj_log.get(name, [])
        prior = [p for p in series if p["ts"] < intel_dt - datetime.timedelta(minutes=30)]
        if not prior:
            continue
        proj = prior[-1]

        def _err(proj_v, actual_v):
            if actual_v <= 0 or proj_v <= 0:
                return None
            return abs(proj_v - actual_v) / actual_v

        errs = {
            "atk_err_pct":     _err(proj["atk"],     actual_atk),
            "def_err_pct":     _err(proj["def"],     actual_def),
            "spy_off_err_pct": _err(proj["spy_off"], actual_spo),
            "spy_def_err_pct": _err(proj["spy_def"], actual_spd),
        }
        # Drop None entries (stat wasn't observed or wasn't projected)
        errs = {k: v for k, v in errs.items() if v is not None}
        if not errs:
            continue
        errs["samples"]      = 1
        errs["last_checked"] = now
        player_errs[name] = errs

        # Accumulate bucket-level errors
        race = intel.get("race") or proj.get("race", "")
        cls  = intel.get("cls")  or proj.get("cls",  "")
        if race and cls:
            bkey = (race, cls)
            buc = bucket_accumulator.setdefault(bkey, {
                "atk_err_pct": [], "def_err_pct": [],
                "spy_off_err_pct": [], "spy_def_err_pct": [],
            })
            for k in ("atk_err_pct", "def_err_pct", "spy_off_err_pct", "spy_def_err_pct"):
                if k in errs:
                    buc[k].append(errs[k])

    # Collapse bucket lists to medians + sample counts
    bucket_errs: dict = {}
    for bkey, stats in bucket_accumulator.items():
        summary = {"samples": 0}
        for stat_key, vals in stats.items():
            if not vals:
                continue
            s = sorted(vals)
            n = len(s)
            med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
            summary[stat_key + "_median"] = med
            summary["samples"] = max(summary["samples"], n)
        if summary["samples"] > 0:
            bucket_errs[bkey] = summary

    return player_errs, bucket_errs


def _confidence_score(name: str, race: str, cls: str,
                      has_fresh_rich: bool,
                      has_stale_confirmed: bool,
                      has_observed_growth: bool) -> float:
    """Return a confidence score in [0, 1] for an estimate.

    Scoring model:
        0.30   baseline (rank curve only)
      + 0.35   fresh rich intel (≤48h) — we just saw current state
      + 0.15   stale confirmed floor (>48h) — better than rank curve alone
      + 0.15   known demographics (race, class)
      + 0.10   observed growth rate (player has ≥2 growth-log entries)
      - penalty from player-specific projection error history (if any)
      - smaller penalty from bucket-level error (if player-specific absent)
    Final value clamped to [0, 1].
    """
    score = 0.30
    if has_fresh_rich:
        score += 0.35
    elif has_stale_confirmed:
        score += 0.15
    if race and cls:
        score += 0.15
    if has_observed_growth:
        score += 0.10

    # Accuracy penalty: prefer player-specific error data when available,
    # otherwise fall back to the player's (race, class) bucket.
    pe = PROJECTION_ERRORS_LIVE.get(name)
    if pe:
        # Average across whichever stats we have for this player
        stat_keys = ("atk_err_pct", "def_err_pct", "spy_off_err_pct", "spy_def_err_pct")
        errs = [pe[k] for k in stat_keys if k in pe]
        if errs:
            avg_err = sum(errs) / len(errs)
            score -= 0.50 * min(avg_err, 1.0)
    elif race and cls:
        be = BUCKET_ERRORS_LIVE.get((race, cls))
        if be:
            stat_keys = ("atk_err_pct_median", "def_err_pct_median",
                         "spy_off_err_pct_median", "spy_def_err_pct_median")
            errs = [be[k] for k in stat_keys if k in be]
            if errs:
                avg_err = sum(errs) / len(errs)
                score -= 0.30 * min(avg_err, 1.0)

    return max(0.0, min(1.0, score))


def load_spy_cooldowns() -> dict:
    """Return {player_name: datetime} of the most recent SUCCESSFUL recon
    spy report for each target in private_intel.csv.

    A row counts as 'successful' when the Level column is populated — that
    means parse_spy_report actually extracted the target's profile block.
    Empty / failed rows (e.g. old entries from before the parser fix, or
    `skipped_form_not_found`) have no Level and are ignored.

    Used by battle_loop to enforce the SPY_COOLDOWN_HOURS no-re-spy window
    ACROSS Suite sessions — the cooldown is stored on disk, not in RAM,
    so closing + relaunching the Suite doesn't reset it.
    """
    path = INTEL_FILE
    out = {}
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("Source") or "").strip() != "spy":
                    continue
                name  = (row.get("Player")    or "").strip()
                level = (row.get("Level")     or "").strip()
                ts    = (row.get("Timestamp") or "").strip()
                if not name or not level or not ts:
                    continue   # skipped / failed / legacy row
                dt = None
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        dt = datetime.datetime.strptime(ts, fmt)
                        break
                    except ValueError:
                        pass
                if dt is None:
                    continue
                # Keep the LATEST timestamp per player
                if name not in out or dt > out[name]:
                    out[name] = dt
    except Exception as e:
        print(f"  ⚠️  load_spy_cooldowns failed: {e}")
    return out


def load_intel_overlay() -> dict:
    """Read private_intel.csv and return {Player name: latest_stats}.
    Latest row per player wins (by timestamp order in the file — append-only).

    Rows older than INTEL_MAX_AGE_HOURS are skipped so stale data from
    previous sessions doesn't override the rank model with numbers that
    no longer reflect the player's current state.
    """
    path = INTEL_FILE
    if not os.path.isfile(path):
        return {}
    overlay = {}
    now    = datetime.datetime.now()
    cutoff = now - datetime.timedelta(hours=INTEL_MAX_AGE_HOURS)
    dropped_stale = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = (row.get("Player") or "").strip()
                if not name:
                    continue
                ts_str = (row.get("Timestamp") or "").strip()
                row_dt = None
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        row_dt = datetime.datetime.strptime(ts_str, fmt)
                        break
                    except ValueError:
                        pass
                if row_dt is not None and row_dt < cutoff:
                    dropped_stale += 1
                    continue
                overlay[name] = {
                    "level":    num(row.get("Level",    0)),
                    "race":     row.get("Race",  "").strip(),
                    "cls":      row.get("Class", "").strip(),
                    "atk":      num(row.get("ATK",    0)),
                    "def":      num(row.get("DEF",    0)),
                    "spy_off":  num(row.get("SpyOff", 0)),
                    "spy_def":  num(row.get("SpyDef", 0)),
                    "gold":     num(row.get("Gold",   0)),
                    "bank":     num(row.get("Bank",   0)),
                    "citizens": num(row.get("Citizens", 0)),
                    # Fort state — previously dropped by this loader even
                    # though the CSV columns exist.  Phase 1 restores them
                    # so estimate() can reason about repair state.
                    "fort_hp":  num(row.get("FortHP",  0)),
                    "fort_max": num(row.get("FortMax", 0)),
                    # Army composition — flat unit counts from the CSV.
                    # load_target_intel_snapshot() returns the same data as
                    # a nested 'army' dict below; we keep both shapes so
                    # old consumers that read flat fields still work AND
                    # new consumers can use the nested form.
                    "workers":  num(row.get("Workers",  0)),
                    "soldiers": num(row.get("Soldiers", 0)),
                    "guards":   num(row.get("Guards",   0)),
                    "spies":    num(row.get("Spies",    0)),
                    "sentries": num(row.get("Sentries", 0)),
                    "source":   row.get("Source", "").strip(),
                    "timestamp":ts_str,
                }
    except Exception as e:
        print(f"  ⚠️  load_intel_overlay failed: {e}")
    if dropped_stale:
        print(f"  🕒 load_intel_overlay: dropped {dropped_stale} stale row(s) (>{INTEL_MAX_AGE_HOURS}h)")

    # ── Merge in rich snapshot data (Phase 1) ─────────────────────────────
    # private_target_intel.json carries nested fields (army/armory/
    # buildings/upgrades) that don't fit the flat CSV schema.  Scalars in
    # the rich snapshot act as a fallback when the CSV row is missing or
    # older — rich snapshot retention is 14 days vs CSV's 7.
    try:
        rich = load_target_intel_snapshot()
        dropped_rich = 0
        for name, rec in (rich or {}).items():
            if not isinstance(rec, dict):
                continue
            ts_str = rec.get("captured_at", "")
            row_dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    row_dt = datetime.datetime.strptime(ts_str, fmt)
                    break
                except ValueError:
                    pass
            if row_dt is not None and row_dt < cutoff:
                dropped_rich += 1
                continue   # honor the same freshness cutoff as CSV rows

            row = overlay.get(name, {"source": rec.get("source", "spy"),
                                      "timestamp": ts_str})
            # Fill scalars from rich snapshot if CSV didn't supply them.
            for key in ("level", "race", "cls", "atk", "def",
                        "spy_off", "spy_def", "gold", "bank", "citizens",
                        "fort_hp", "fort_max"):
                if not row.get(key) and rec.get(key) not in (None, ""):
                    row[key] = rec[key]
            # Nested fields always copy over (rich snapshot is their only home).
            for nest in ("army", "armory", "buildings", "upgrades"):
                if nest in rec:
                    row[nest] = rec[nest]
            overlay[name] = row
        if dropped_rich:
            print(f"  🕒 load_intel_overlay: dropped {dropped_rich} stale rich-intel entr(ies)")
    except Exception as e:
        print(f"  ⚠️  load_intel_overlay rich-merge failed: {e}")

    return overlay


def do_attack(page, target: dict, turns: int, log_fn, dry_run: bool = False) -> dict:
    """Attack target with N turns.  Returns the parse_battle_result dict."""
    pid       = str(target["player_id"])
    name      = target["name"]
    list_page = int(target.get("list_page", 1)) or 1

    # Navigate to the exact attack-list page that contains this target's
    # form element.  Each row's <form id="attack-form-{pid}"> only exists
    # on the page where the target is listed, so visiting page=1 blindly
    # would miss anyone ranked lower than the first 50.
    page.goto(f"{BASE_ATTACK_URL}?{ATTACK_PARAMS}&page={list_page}")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    gold_before = read_live_header(page).get("gold", 0)

    if dry_run:
        log_fn(f"  [DRY] ⚔  would attack {name} (pid={pid}) with {turns} turns  (ratio {target.get('atk_ratio',0):.2f}×)", "dim")
        return {"result": "dry_run", "xp": 0, "gold_gained": 0}

    old_url = page.url
    sub = _submit_attack_form(page, pid, turns)
    if not sub.get("ok"):
        err = sub.get("err", "unknown")
        log_fn(f"  ⚔  {name}: skipped ({err}) — target moved or exhausted", "dim")
        _battle_log_row("attack", pid, name, turns, gold_before, gold_before,
                        0, f"skipped_{err}")
        return {"result": "skipped", "xp": 0, "gold_gained": 0, "err": err}
    # Wait for the real battle-report page to land before parsing.
    _wait_post_submit(page, old_url, url_hint="/attack/log/")
    res = _parse_battle_result(page)
    # Always dump the LAST attack response for diagnosis — overwrites each
    # time, so the file only holds the most recent response.
    try:
        _unhide("debug_last_attack_response.html")
        with open("debug_last_attack_response.html", "w", encoding="utf-8") as f:
            f.write(f"<!-- URL: {page.url} -->\n")
            f.write(f"<!-- target: {name} pid={pid} -->\n")
            f.write(f"<!-- battle_result: {res} -->\n")
            f.write(page.content() or "")
    except Exception:
        pass

    # Harvest intel from the battle report page — even on loss the "Final
    # Defense N" value tells us a floor we can feed back into the model.
    try:
        intel = parse_attack_report(page)
        # Prefer the server-side name from the report if parsed, otherwise
        # fall back to the scraped row's name so we always log something.
        intel.setdefault("target_name", name)
        # Overlay the server-reported gold/xp onto res so the audit row agrees.
        if intel.get("gold_stolen"): res.setdefault("gold_gained", intel["gold_stolen"])
        if intel.get("xp_gained"):   res["xp"] = intel["xp_gained"]
        record_intel(intel, log_fn=log_fn)
    except Exception as e:
        log_fn(f"  ⚠️  intel parse failed for {name}: {e}", "dim")

    # On error pages the header selectors may not match → 0.  Fall back to
    # gold_before so the audit log is meaningful instead of "went to zero".
    post_hdr = read_live_header(page)
    gold_after = post_hdr.get("gold", 0) or gold_before

    _battle_log_row("attack", pid, name, turns, gold_before, gold_after,
                    res.get("xp", 0), res.get("result", "unknown"))

    result = res.get("result", "unknown")
    xp     = res.get("xp", 0)
    gg     = res.get("gold_gained", 0)
    if result == "win":
        log_fn(f"  ⚔  {name}: WIN  +{xp:,} xp  +{gg:,}g  (now {gold_after:,}g)", "battle")
    elif result == "loss":
        log_fn(f"  ⚔  {name}: LOSS — estimate was too low", "red")
    elif result == "out_of_range":
        log_fn(f"  ⚔  {name}: out of range (skipping)", "dim")
    elif result == "exhausted":
        log_fn(f"  ⚔  {name}: already at 5/5 today", "dim")
    else:
        log_fn(f"  ⚔  {name}: {result}  +{xp:,} xp", "battle")
    return res


def do_spy(page, target: dict, log_fn, dry_run: bool = False) -> dict:
    """Recon spy the target.  Returns the parse_battle_result dict."""
    pid       = str(target["player_id"])
    name      = target["name"]
    list_page = int(target.get("list_page", 1)) or 1
    gold_before = read_live_header(page).get("gold", 0)

    if dry_run:
        log_fn(f"  [DRY] 🔍 would spy {name} (pid={pid})  (ratio {target.get('spy_ratio',0):.2f}×)", "dim")
        return {"result": "dry_run", "xp": 0, "gold_gained": 0}

    # Navigate to the EXACT /game/spy page the target was scraped from.
    # The spy page paginates (~19 players per page); each row's inline
    # <form action="/game/spy/{pid}"> only exists on the page where the
    # target is listed, so navigating blindly to page 1 would find zero
    # forms for any target from pages 2+ and every submit would fail
    # with form_not_found.  scrape_spy_candidates records list_page on
    # every row for exactly this purpose (same pattern as do_attack).
    spy_url = f"{BASE_URL}/spy" + (f"?page={list_page}" if list_page > 1 else "")
    try:
        page.goto(spy_url)
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    old_url = page.url
    sub = _submit_spy_form(page, pid)
    if not sub.get("ok"):
        err = sub.get("err", "unknown")
        log_fn(f"  🔍 {name}: skipped ({err}) — target moved or exhausted", "dim")
        _battle_log_row("spy", pid, name, SPY_TURNS_COST, gold_before, gold_before,
                        0, f"skipped_{err}")
        return {"result": "skipped", "xp": 0, "gold_gained": 0, "err": err}
    # Wait for the real spy-log page to land before parsing.
    _wait_post_submit(page, old_url, url_hint="/spy/log/")
    res = _parse_battle_result(page)

    # Always dump the LAST spy response to disk for diagnosis — overwrites
    # each time, so the file only holds the most recent.
    try:
        _unhide("debug_last_spy_response.html")
        with open("debug_last_spy_response.html", "w", encoding="utf-8") as f:
            f.write(f"<!-- URL: {page.url} -->\n")
            f.write(f"<!-- target: {name} pid={pid} -->\n")
            f.write(f"<!-- battle_result: {res} -->\n")
            f.write(page.content() or "")
    except Exception:
        pass

    # Harvest intel from the spy report — this is the GOLD MINE.  A
    # successful recon reveals every combat stat and unit count, which we
    # feed straight back into private_intel.csv for the next calibration.
    try:
        intel = parse_spy_report(page)
        intel.setdefault("target_name", name)
        record_intel(intel, log_fn=log_fn)
    except Exception as e:
        log_fn(f"  ⚠️  intel parse failed for {name}: {e}", "dim")

    post_hdr = read_live_header(page)
    gold_after = post_hdr.get("gold", 0) or gold_before

    _battle_log_row("spy", pid, name, SPY_TURNS_COST, gold_before, gold_after,
                    0, res.get("result", "unknown"))

    result = res.get("result", "unknown")
    if result == "spy_ok" or result == "unknown":
        log_fn(f"  🔍 {name}: recon ok  (now {gold_after:,}g)", "battle")
    elif result == "spy_fail":
        log_fn(f"  🔍 {name}: recon failed — spy defense too high", "red")
    else:
        log_fn(f"  🔍 {name}: {result}", "battle")
    return res


# ── Main battle loop (entry point called from GUI thread) ───────────────────
def battle_loop(stop_event, cfg: dict, log_fn, deadline_ts=None) -> dict:
    """Run auto-attack or auto-spy until the user stops, turns/gold run out,
    the session expires, or no safe targets remain.

    cfg keys (all optional, defaults in brackets):
      mode          : 'attack' | 'spy'              ['attack']
      margin        : float                         [1.2]
      turns_per_hit : int 1-10                      [5]
      skip_friends  : bool                          [True]
      skip_clan     : bool                          [True]
      skip_bots     : bool                          [False]
      max_per_pass  : int (soft cap per scrape)     [20]
      max_total     : int (hard cap for whole run)  [200]
      dry_run       : bool                          [False]

    deadline_ts : optional unix timestamp.  When set, the loop exits
        cleanly as soon as time.time() >= deadline_ts.  Used by the
        optimizer thread to piggyback battle actions during its 30-min
        wait phase — the optimizer calls battle_loop with
        deadline_ts = (next tick time - BATTLE_TICK_BUFFER_SECONDS) so
        battle always hands the browser back before the next tick.

    log_fn(msg, tag): callback for the GUI log.  No stdout redirect is used,
    so the caller does not need to worry about capture threads."""
    from playwright.sync_api import sync_playwright

    mode = (cfg.get("mode") or "attack").lower()
    if mode not in ("attack", "spy"):
        log_fn(f"  ❌ unknown battle mode: {mode}", "red")
        return {"ok": False, "reason": "bad_mode", "actions": 0}

    turns_per_hit = int(cfg.get("turns_per_hit", 5))
    turns_per_hit = max(1, min(10, turns_per_hit))
    max_total     = int(cfg.get("max_total", 200))
    dry_run       = bool(cfg.get("dry_run", False))

    # Refuse if another optimizer tick is actively running (holds OPT_LOCK).
    # The "optimizer thread is alive" case is NOT blocked here anymore —
    # during the optimizer's wait phase it releases OPT_LOCK, so battle
    # from within that same thread (via deadline_ts) works fine.
    if os.path.isfile(OPT_LOCK_FILE):
        log_fn("  ❌ Optimizer tick in progress (.optimizer.lock present) — "
               "wait for it to finish first.", "red")
        return {"ok": False, "reason": "opt_running", "actions": 0}

    if not _acquire_lock(BATTLE_LOCK_FILE):
        log_fn("  ❌ Another battle session is already running — aborting.", "red")
        return {"ok": False, "reason": "locked", "actions": 0}

    actions = 0
    wins    = 0
    losses  = 0
    t0      = time.time()
    reason  = "done"
    # Session-level exhausted set: once a target refuses to submit
    # (form_not_found etc.), never try it again this run — otherwise the
    # re-scrape loop bounces forever on stale candidates.
    session_exhausted = set()

    # Spy cooldown (names only; intel CSV has no player_id column).
    # Loaded ONCE at session start from private_intel.csv (persistent) and
    # then grown during the session as we successfully spy new targets, so
    # we don't re-spy anyone within SPY_COOLDOWN_HOURS.
    spy_cooldown = set()
    if mode == "spy":
        try:
            _now    = datetime.datetime.now()
            _cutoff = _now - datetime.timedelta(hours=SPY_COOLDOWN_HOURS)
            _cds    = load_spy_cooldowns()
            spy_cooldown = {n for n, dt in _cds.items() if dt > _cutoff}
            # Show the 3 most-recent + total count so the user can see
            # which targets are on cooldown without scrolling the CSV.
            recent = sorted(_cds.items(), key=lambda x: x[1], reverse=True)[:3]
            hint = ", ".join(f"{n} ({int((_now-dt).total_seconds()//60)}m ago)"
                             for n, dt in recent if n in spy_cooldown)
            log_fn(f"  ⏱  spy cooldown: {len(spy_cooldown)} on cooldown "
                   f"(≤{SPY_COOLDOWN_HOURS}h rule)" +
                   (f"  — recent: {hint}" if hint else ""), "dim")
        except Exception as e:
            log_fn(f"  ⚠️  spy cooldown load failed: {e}", "dim")

    # Seed our_stats from private_latest.json so we know our ATK / SpyOff.
    # The GUI doesn't pass our_stats in cfg; without this seed, atk/spy_off
    # default to 0 and every target fails the margin filter — the loop
    # silently exits with "No safe targets left this pass" on every call.
    _seed_stats = {}
    try:
        _seed_stats = load_your_stats() or {}
    except Exception as _e:
        log_fn(f"  ⚠️  load_your_stats failed: {_e}", "dim")
    _base_stats = dict(cfg.get("our_stats") or {})
    for _k in ("atk", "def", "spy_off", "spy_def"):
        _base_stats.setdefault(_k, int(_seed_stats.get(_k, 0)))
    # Level is needed for XP-farm-mode scoring in pick_battle_targets.
    _base_stats.setdefault("level", int(_seed_stats.get("level", 1) or 1))

    log_fn(f"⚔  Battle loop starting — mode={mode} margin={cfg.get('margin',1.2)} "
           f"turns/hit={turns_per_hit} dry={dry_run}", "battle")
    log_fn(f"  📊 your stats: ATK={_base_stats.get('atk',0):,} "
           f"SpyOff={_base_stats.get('spy_off',0):,}", "dim")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(storage_state=AUTH_FILE)
            page = ctx.new_page()
            try:
                # Warm up: hit the attack list once to get header + reset timer.
                page.goto(f"{BASE_ATTACK_URL}?{ATTACK_PARAMS}&page=1")
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                if "/login" in (page.url or ""):
                    raise BattleStop("session", "redirected to login on warm-up")

                rs = read_reset_countdown(page)
                if rs:
                    hrs = rs // 3600
                    mins = (rs % 3600) // 60
                    log_fn(f"  ⏲  Daily reset in {hrs}h {mins}m", "dim")

                while not stop_event.is_set() and actions < max_total:
                    # Deadline check — used when the optimizer thread piggybacks
                    # battle during its wait phase.  Exit cleanly before the
                    # next tick time so we don't block run_tick().
                    if deadline_ts and time.time() >= deadline_ts:
                        raise BattleStop("deadline", "tick time approaching")

                    hdr = read_live_header(page)
                    our_stats = dict(_base_stats)
                    our_stats["gold"]  = hdr.get("gold",  our_stats.get("gold",  0))
                    our_stats["turns"] = hdr.get("turns", our_stats.get("turns", 0))

                    if mode == "attack" and our_stats["turns"] < 1:
                        raise BattleStop("turns", "out of turns")
                    if mode == "spy" and our_stats["turns"] < SPY_TURNS_COST:
                        raise BattleStop("turns", "not enough turns for spy")
                    if mode == "spy" and our_stats["gold"] < SPY_GOLD_COST:
                        raise BattleStop("gold", "not enough gold for spy")

                    # Fresh scrape for current (N/5) counts.  Spy mode reads
                    # /game/spy (a different target pool); attack mode reads
                    # the attack list.  Mixing them causes form_not_found
                    # loops because spy forms only exist on /game/spy.
                    if mode == "spy":
                        rows = scrape_spy_candidates(page, max_pages=int(cfg.get("scrape_pages", 5)))
                    else:
                        rows = scrape_attack_candidates(page, max_pages=int(cfg.get("scrape_pages", 10)))
                    # Remove anything already permanently exhausted this session.
                    rows = [r for r in rows if str(r.get("player_id")) not in session_exhausted]
                    estimates = load_estimates_lookup()
                    targets = pick_battle_targets(
                        rows, estimates, our_stats, cfg, mode=mode,
                        spy_cooldown=(spy_cooldown if mode == "spy" else None),
                    )
                    if not targets:
                        log_fn("  ℹ️  No safe targets left this pass.", "dim")
                        # Show the filter breakdown so the user knows WHICH
                        # setting killed the pool, instead of guessing.
                        reasons = getattr(targets, "reasons", None) or {}
                        pool_size = getattr(targets, "pool_size", len(rows))
                        nonzero = [(k, v) for k, v in reasons.items() if v > 0]
                        if nonzero:
                            nonzero.sort(key=lambda kv: -kv[1])
                            pretty = "  ".join(f"{k}={v}" for k, v in nonzero)
                            log_fn(f"  📋 filter breakdown (pool={pool_size}): {pretty}", "dim")
                            # Suggest the most likely culprit to loosen.
                            top_reason = nonzero[0][0]
                            suggestions = {
                                "under_min_gold": "lower Min gold (e.g. 50,000 or 0)",
                                "margin_fail":    "lower Margin (e.g. 1.2)",
                                "daily_cap_5_5":  "wait for daily reset or try different mode",
                                "out_of_range":   "your level range has few targets right now",
                                "no_def_est":     "run the estimator to get DEF estimates",
                                "spy_cooldown":   f"wait ≤{SPY_COOLDOWN_HOURS}h for spy cooldown",
                                "spy_margin_fail":"lower Margin or wait for more spies",
                            }
                            tip = suggestions.get(top_reason)
                            if tip:
                                log_fn(f"  💡 tip: {tip}", "dim")
                        raise BattleStop("no_targets", "filter + daily caps exhausted pool")

                    log_fn(f"  🎯 Pass: {len(targets)} candidate(s)", "dim")
                    consecutive_skips = 0

                    for tgt in targets:
                        if stop_event.is_set():
                            raise BattleStop("user", "stop requested")
                        if actions >= max_total:
                            raise BattleStop("user", "max_total reached")

                        # Defensive: if a target was marked exhausted earlier
                        # this session (form_not_found, moved, etc.), don't
                        # retry it — the scraper may still return duplicates
                        # or a lingering cached row.
                        if str(tgt.get("player_id")) in session_exhausted:
                            continue

                        # Re-check live header so we don't run out mid-loop.
                        hdr = read_live_header(page)
                        if mode == "attack" and hdr.get("turns", 0) < turns_per_hit:
                            # Try with smaller hit if possible.
                            if hdr.get("turns", 0) >= 1:
                                effective_turns = max(1, hdr["turns"])
                            else:
                                raise BattleStop("turns", "out of turns mid-pass")
                        else:
                            effective_turns = turns_per_hit

                        if mode == "spy":
                            if hdr.get("turns", 0) < SPY_TURNS_COST:
                                raise BattleStop("turns", "not enough turns for spy")
                            if hdr.get("gold", 0)  < SPY_GOLD_COST:
                                raise BattleStop("gold", "not enough gold for spy")

                        if mode == "attack":
                            # do_attack navigates to the target's list_page itself.
                            res = do_attack(page, tgt, effective_turns, log_fn, dry_run=dry_run)
                        else:
                            res = do_spy(page, tgt, log_fn, dry_run=dry_run)

                        r = res.get("result", "unknown")
                        # Skipped targets (form_not_found etc.) don't count as
                        # "actions" — mark them permanently exhausted for this
                        # session so we stop trying them on every re-scrape.
                        if r == "skipped":
                            session_exhausted.add(str(tgt.get("player_id")))
                            consecutive_skips += 1
                            if consecutive_skips >= 3:
                                log_fn("  🔄 3 consecutive skips — re-scraping", "dim")
                                break   # leaves inner for, outer while re-scrapes
                            continue
                        consecutive_skips = 0
                        actions += 1
                        if r == "win" or r == "spy_ok":
                            wins += 1
                            # In spy mode, mark the target as cooling down
                            # for the remainder of this session so the next
                            # pass doesn't re-pick them.  The persistent
                            # copy is already in private_intel.csv via
                            # record_intel called inside do_spy.
                            if mode == "spy":
                                spy_cooldown.add(str(tgt.get("name", "")).replace(" (YOU)", "").strip())
                        elif r == "loss" or r == "spy_fail":
                            losses += 1

                        # Jitter between actions.
                        delay = _random.uniform(BATTLE_MIN_DELAY, BATTLE_MAX_DELAY)
                        if actions % BATTLE_LONG_PAUSE_EVERY == 0:
                            delay = _random.uniform(BATTLE_LONG_DELAY_MIN, BATTLE_LONG_DELAY_MAX)
                            log_fn(f"  💤 long pause {delay:.1f}s (every {BATTLE_LONG_PAUSE_EVERY})", "dim")
                        # Sleep in 0.5s slices so stop_event / deadline react quickly.
                        slept = 0.0
                        while slept < delay and not stop_event.is_set():
                            if deadline_ts and time.time() >= deadline_ts:
                                break
                            time.sleep(min(0.5, delay - slept))
                            slept += 0.5
                    # end inner for — loop back to re-scrape on next pass
            finally:
                try:
                    ctx.storage_state(path=AUTH_FILE)
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
    except BattleStop as bs:
        reason = bs.reason
        log_fn(f"⚔  Battle loop stopped: {bs}", "dim")
    except Exception as e:
        reason = "error"
        log_fn(f"❌ Battle loop error: {e}", "red")
    finally:
        _release_lock(BATTLE_LOCK_FILE)

    elapsed = time.time() - t0
    total = wins + losses
    winrate = (wins / total * 100.0) if total else 0.0
    log_fn(f"⚔  Done. {actions} action(s) in {elapsed/60:.1f}m — "
           f"wins={wins} losses={losses} winrate={winrate:.0f}%", "battle")
    return {"ok": True, "reason": reason, "actions": actions,
            "wins": wins, "losses": losses, "elapsed": elapsed}


# ========================================================================
# Private Scraper  (merged from scraper_private.py)
# ========================================================================

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


# Reverse lookup used by scrape_buildings().  NOTE: this is intentionally a
# separate variable from BUILDING_TYPE_ID (line ~1310) which is {name→id};
# a previous version re-used the same name here and silently clobbered the
# optimizer's forward-lookup dict, causing _build() to fail with "Unknown
# building: Housing" and read_state() to stop populating s["buildings"]
# for every building except Mine (which had a separate overview fallback).
BUILDING_NAME_BY_ID = {
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
            name = BUILDING_NAME_BY_ID.get(int(id_str), f"building_{id_str}")
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


# ── Spy-log history harvester (Phase 1) ──────────────────────────────────
# Visits /game/spy/logs and parses every spy report we haven't seen yet.
# Captures MANUAL spies (user ran them through the browser) identically to
# bot-initiated spies — the logs list contains both.  Dedup state is kept
# in a JSON file so we don't re-parse the same report every tick; that would
# waste bandwidth and double-count intel observations for growth tracking.
SEEN_SPY_LOGS_FILE = "private_seen_spy_logs.json"
# Cap per tick to avoid flooding: if the user just spied 50 people all at
# once, we catch up over a few ticks rather than hammering the server in
# one burst.  Each fetch is ~1 page load (1-2s), so 20 adds ~40s to a tick.
SPY_LOGS_MAX_PER_TICK = 20


def _load_seen_spy_logs() -> set:
    """Load the set of already-parsed spy log IDs.  Returns empty set if
    the file is missing or malformed — never raises."""
    if not os.path.isfile(SEEN_SPY_LOGS_FILE):
        return set()
    try:
        with open(SEEN_SPY_LOGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ids = data.get("seen_ids", []) if isinstance(data, dict) else []
        return set(str(x) for x in ids)
    except Exception:
        return set()


def _save_seen_spy_logs(seen: set, max_keep: int = 5000) -> None:
    """Persist the seen-ID set.  Caps at max_keep entries (keep most recent
    numeric IDs) to prevent unbounded file growth over weeks of play."""
    try:
        ids_sorted = sorted(
            (s for s in seen if str(s).isdigit()),
            key=lambda s: int(s),
            reverse=True,
        )[:max_keep]
        # Preserve any non-numeric IDs as-is (shouldn't happen, but be safe)
        extras = [s for s in seen if not str(s).isdigit()]
        payload = {"seen_ids": sorted(ids_sorted + extras),
                   "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        _unhide(SEEN_SPY_LOGS_FILE)
        tmp = SEEN_SPY_LOGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, SEEN_SPY_LOGS_FILE)
    except Exception:
        pass


def scrape_spy_logs(page, ts, log_fn=None) -> dict:
    """Visit /game/spy/logs and harvest any unseen spy reports.  Parses each
    new log via parse_spy_report() and funnels through record_intel() so
    MANUAL spies (run in the user's browser) land in the same pipeline as
    bot-initiated ones — same CSV, same rich JSON snapshot, same growth
    tracking.

    Returns a dict with counters: {'seen_before': N, 'new': N, 'failed': N}.
    """
    def _log(msg, tone="info"):
        if log_fn: log_fn(msg, tone)
        else:      print(msg)

    _log("  🕵️  Harvesting spy-logs history...")
    stats = {"seen_before": 0, "new": 0, "failed": 0, "skipped_nonspyok": 0}

    seen_ids = _load_seen_spy_logs()

    # Load the history page and extract all spy-log IDs.  The list renders
    # each report as an anchor <a href="/game/spy/log/{id}">...</a>.  The
    # JS below is tolerant of markup drift — we match on href pattern, not
    # on specific wrapper classes, so a minor template change doesn't break
    # harvesting silently.
    try:
        page.goto(f"{BASE_URL}/spy/logs")
        page.wait_for_load_state("networkidle", timeout=15000)
        id_list = page.evaluate(r"""() => {
            const ids = new Set();
            document.querySelectorAll('a[href*="/spy/log/"]').forEach(a => {
                const m = (a.getAttribute('href') || '').match(/\/spy\/log\/(\d+)/);
                if (m) ids.add(m[1]);
            });
            return Array.from(ids);
        }""")
    except Exception as e:
        _log(f"     ⚠️  spy-logs list fetch failed: {e}", "warn")
        return stats

    # Newest first — most game-history lists render reverse-chronologically.
    # When the cap kicks in we want the most recent intel, not the oldest.
    id_list = sorted((str(x) for x in id_list or []),
                     key=lambda s: int(s) if s.isdigit() else 0,
                     reverse=True)

    new_ids = [x for x in id_list if x not in seen_ids]
    stats["seen_before"] = len(id_list) - len(new_ids)

    if not new_ids:
        _log(f"     ✅ {stats['seen_before']} log(s) already parsed — nothing new.")
        return stats

    to_fetch = new_ids[:SPY_LOGS_MAX_PER_TICK]
    if len(new_ids) > SPY_LOGS_MAX_PER_TICK:
        _log(f"     ℹ️  {len(new_ids)} new logs found — fetching top {SPY_LOGS_MAX_PER_TICK} this tick.")

    for log_id in to_fetch:
        try:
            page.goto(f"{BASE_URL}/spy/log/{log_id}")
            page.wait_for_load_state("networkidle", timeout=10000)
            entry = parse_spy_report(page)
            if entry.get("result") != "spy_ok":
                stats["skipped_nonspyok"] += 1
                # Still mark as seen so we don't keep re-fetching failed
                # spies every tick (they never transition to success).
                seen_ids.add(log_id)
                continue
            entry["source"] = "spy_log_history"   # distinguish in CSV audit
            record_intel(entry, log_fn=log_fn)
            seen_ids.add(log_id)
            stats["new"] += 1
        except Exception as e:
            stats["failed"] += 1
            _log(f"     ⚠️  log {log_id} parse failed: {e}", "warn")

    _save_seen_spy_logs(seen_ids)

    msg = (f"     ✅ harvested {stats['new']} new spy report(s) "
           f"({stats['skipped_nonspyok']} non-successful skipped, "
           f"{stats['seen_before']} already known, "
           f"{stats['failed']} failed)")
    _log(msg)
    return stats


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
    Scrape global leaderboards directly from the detail pages:
      /leaderboards/global/overall   → overall rank for every player
      /leaderboards/global/offense   → offense (ATK) rank
      /leaderboards/global/defense   → defense (DEF) rank

    Paginates through all pages (~50 rows each) until empty.
    Saves rank_map to private_rankings_snapshot.json for the estimator.
    """
    print("  🏆 Scraping global rankings...")

    # JS that extracts podium (ranks 1-3) AND .ranking-row entries (ranks 4+)
    # from the current page.  The podium uses a DIFFERENT HTML structure —
    # .podium-spot / .podium-name / .podium-base-N — and was previously ignored,
    # which meant the scraper was silently dropping the top-3 players every tick.
    EXTRACT_JS = r"""() => {
        const rows = [];

        // ── Top-3 podium (rank 1, 2, 3 — different HTML structure) ──────────
        document.querySelectorAll('.podium-spot').forEach(spot => {
            const nameEl   = spot.querySelector('.podium-name');
            const playerEl = spot.querySelector('.podium-player');
            const baseEl   = spot.querySelector('[class*="podium-base-"]');
            if (!nameEl || !baseEl) return;
            const rank = parseInt((baseEl.innerText || '').trim());
            if (!rank) return;
            // .podium-name has only the player name as text (no badges)
            const name = (nameEl.innerText || '').trim();
            const href = playerEl ? (playerEl.getAttribute('href') || '') : '';
            const idM  = href.match(/\/player\/(\d+)/);
            const pid  = idM ? parseInt(idM[1]) : 0;
            const clanEl = spot.querySelector('.podium-clan');
            const clan = (clanEl ? clanEl.innerText || '' : '').replace(/[\[\]]/g,'').trim();
            if (name && rank) rows.push({rank, name, pid, clan});
        });

        // ── Regular ranking rows (rank 4+) ──────────────────────────────────
        document.querySelectorAll('.ranking-row').forEach(row => {
            const posEl  = row.querySelector('.ranking-position');
            const nameEl = row.querySelector('.player-name');
            if (!posEl || !nameEl) return;
            const rankM = posEl.innerText.match(/#?(\d+)/);
            if (!rankM) return;
            const rank = parseInt(rankM[1]);
            // Extract name from direct text nodes only — ignores Friend/Clan badges
            const name = Array.from(nameEl.childNodes)
                .filter(n => n.nodeType === 3)
                .map(n => n.textContent.trim())
                .filter(Boolean)
                .join(' ')
                .trim();
            const href   = nameEl.getAttribute('href') || '';
            const idM    = href.match(/\/player\/(\d+)/);
            const pid    = idM ? parseInt(idM[1]) : 0;
            const clanEl = row.querySelector('.player-clan');
            const clan   = (clanEl?.innerText || '').replace(/[\[\]]/g,'').trim();
            if (name && rank) rows.push({rank, name, pid, clan});
        });
        return rows;
    }"""

    rank_map   = {}   # {player_name: {overall, off_rank, def_rank, clan, player_id}}
    csv_rows   = []
    total_p    = 0

    CATS = [
        ("overall", "overall"),
        ("offense", "off_rank"),
        ("defense", "def_rank"),
    ]

    for cat_slug, field in CATS:
        base = f"{BASE_URL}/leaderboards/global/{cat_slug}?period=alltime&view=detail"
        page_num = 1
        while True:
            url = base if page_num == 1 else f"{base}&page={page_num}"
            try:
                page.goto(url)
                page.wait_for_load_state("networkidle", timeout=15000)
                entries = page.evaluate(EXTRACT_JS)
            except Exception as _e:
                print(f"     ⚠️  {cat_slug} page {page_num} failed: {_e}")
                break
            if not entries:
                break
            for e in entries:
                n = e["name"]
                if not n:
                    continue
                if n not in rank_map:
                    rank_map[n] = {"clan": e["clan"] or "—",
                                   "player_id": e["pid"]}
                rank_map[n][field] = e["rank"]
                if cat_slug == "overall":
                    total_p = max(total_p, e["rank"])
                csv_rows.append([ts, cat_slug, e["rank"], n, e["clan"], e["pid"]])
            page_num += 1

    if csv_rows:
        append_rows(FILE_RANKINGS,
                    ["Timestamp", "Category", "Rank", "Player", "Clan", "PlayerID"],
                    csv_rows)

    _live["rank_map"]      = rank_map
    _live["total_players"] = total_p

    _unhide("private_rankings_snapshot.json")
    with open("private_rankings_snapshot.json", "w", encoding="utf-8") as f:
        json.dump({"timestamp": ts, "total_players": total_p,
                   "rank_map": rank_map}, f, indent=2)

    n_players = len(rank_map)
    print(f"     ✅ {n_players} players ranked | Total: {total_p} | "
          f"{len(csv_rows)} rows written")


# ---------------------------------------------------------------------------
# Read own server-wide ranks from /stats (player's own profile page)
# ---------------------------------------------------------------------------

def _get_own_player_id() -> int:
    """Read own player ID from private_player_profiles.csv (most recent row for own name)."""
    if not os.path.isfile(FILE_PROFILES):
        return 0
    try:
        rows = read_csv(FILE_PROFILES)
        # Own name should contain 'beginner' (case-insensitive) — adjust if needed
        own_rows = [r for r in rows if 'beginner' in r.get('Player', '').lower()]
        if own_rows:
            return int(own_rows[-1].get('PlayerID', 0))
    except Exception:
        pass
    return 0


def read_own_ranks(page) -> dict:
    """
    Navigate to own public profile page (/profile/{id}) and extract server-wide ranks
    using CSS selectors .rank-label / .rank-value (same structure as other player profiles).
    Falls back to all-zeros on error.
    """
    result = {"rank_overall": 0, "rank_offense": 0, "rank_defense": 0,
              "rank_wealth": 0, "rank_spy_off": 0, "rank_spy_def": 0}
    try:
        pid = _get_own_player_id()
        if not pid:
            print("  ⚠️  read_own_ranks: player ID not found in profiles CSV")
            return result

        page.goto(f"{BASE_URL}/player/{pid}")
        page.wait_for_load_state("networkidle", timeout=15000)

        # Profile page uses .rank-item > .rank-label + .rank-value structure
        RANK_JS = r"""() => {
            const out = {rank_overall:0, rank_offense:0, rank_defense:0,
                         rank_wealth:0, rank_spy_off:0, rank_spy_def:0};
            document.querySelectorAll('.rank-item').forEach(item => {
                const labelEl = item.querySelector('.rank-label');
                const valueEl = item.querySelector('.rank-value');
                if (!labelEl || !valueEl) return;
                const label = labelEl.innerText.trim().toLowerCase();
                const m = valueEl.innerText.match(/#(\d+)/);
                if (!m) return;
                const v = parseInt(m[1]);
                if (label.includes('overall'))                     out.rank_overall = v;
                else if (label.includes('offense'))                out.rank_offense = v;
                else if (label.includes('defense'))                out.rank_defense = v;
                else if (label.includes('net') || label.includes('wealth')) out.rank_wealth = v;
                else if (label.includes('spy') && label.includes('off'))    out.rank_spy_off = v;
                else if (label.includes('spy') && label.includes('def'))    out.rank_spy_def = v;
            });
            return out;
        }"""
        ranks = page.evaluate(RANK_JS)
        result.update({k: v for k, v in ranks.items() if v > 0})
        if any(result.values()):
            print(f"  📊 Own ranks: Overall #{result['rank_overall']} | "
                  f"Offense #{result['rank_offense']} | Defense #{result['rank_defense']} | "
                  f"Wealth #{result['rank_wealth']}")
        else:
            print(f"  ⚠️  read_own_ranks: no .rank-item elements found on /player/{pid}")
    except Exception as e:
        print(f"  ⚠️  read_own_ranks failed: {e}")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


# ========================================================================
# Estimator  (merged from estimator.py)
# ========================================================================

OUTPUT      = "private_player_estimates.csv"

# ── GitHub Pages publishing ────────────────────────────────────────────────────
# Clone your second GitHub repo here:
#   git clone https://github.com/cmdprive/darkthrone-estimates C:\Users\Gebruiker\darkthrone-estimates
# Then enable GitHub Pages on that repo (Settings → Pages → branch: main, folder: / (root))
# The live URL will be: https://cmdprive.github.io/darkthrone-estimates/
ESTIMATES_REPO_DIR = r"C:\Users\Gebruiker\darkthrone-estimates"
ESTIMATES_SITE_URL = "https://cmdprive.github.io/darkthrone-estimates/"

# ── EXACT gear stats (from dump_armory.html) ──────────────────────────────────
WEAPON_STATS = {1:25, 2:50, 3:100, 4:150, 5:200, 6:275, 7:350, 8:450, 9:550, 10:700}
ARMOR_STATS  = {1:19, 2:38, 3:75,  4:120, 5:180, 6:250, 7:350, 8:450, 9:575, 10:750}
SPY_WEAPON   = {1:12, 2:25, 3:50,  4:80,  5:120, 6:170, 7:230, 8:300, 9:380, 10:480}
SPY_ARMOR    = {1:12, 2:25, 3:50,  4:80,  5:120, 6:170, 7:230, 8:300, 9:380, 10:480}

# ── Unit base stats (from dump_training.html) ─────────────────────────────────
UNIT_OFF  = {1:3, 2:20, 3:50, 4:100}
UNIT_DEF  = {1:3, 2:20, 3:50, 4:100}
UNIT_SPY  = {1:5, 2:25, 3:60}
UNIT_SENT = {1:5, 2:25, 3:60}

# ── CONFIRMED unlock gates ────────────────────────────────────────────────────
# Armory Lv → max GEAR tier
# "Each armory level unlocks 2 new equipment tiers" (confirmed from game UI)
# Lv2 confirmed: T6–T7, requires Player Level 30 + Fort Lv1 + 7.00M gold
ARMORY_LV_TO_GEAR_TIER = {0:3, 1:5, 2:7, 3:9, 4:10, 5:10}
#  Lv0→T3, Lv1→T5(+2), Lv2→T7(+2), Lv3→T9(+2), Lv4→T10(+1 max), Lv5→T10

# Fort Lv → max GEAR tier (fort is the secondary gate alongside armory)
# Fort Lv1 confirmed as required for Armory Lv2 (T6–T7) — same unlock pattern
FORT_LV_TO_GEAR_TIER   = {0:3, 1:5, 2:7, 3:9, 4:10, 5:10}

# Fort Lv → max UNIT tier
# Fort Lv1 only adds fort HP — T2 units require Fort Lv2 (Player Level 20, CONFIRMED)
# Pattern: each fort level unlocks the next unit tier.
FORT_LV_TO_UNIT_TIER   = {0:1, 1:1, 2:2, 3:3, 4:4, 5:4}

# Spy Academy Lv → max spy gear tier (mirrors armory progression)
SPY_AC_TO_SPY_TIER     = {0:3, 1:5, 2:7, 3:9, 4:10, 5:10}

# ── Building level estimates from player level ────────────────────────────────
# Rules: Armory/Fort require Level 10. Mine requires Level 3.
# Fort is cheaper (500k×lv) than Armory (750k×lv) so players build it faster.
# GEAR TIER = min(armory_gate, fort_gate) — BOTH must be satisfied.
# T10 gear therefore requires both Armory Lv5 AND Fort Lv5.

def est_mine_lv(player_lv):
    """Mine level estimates from confirmed in-game building requirements:
      Lv1: Player Level  3            (initial build)
      Lv2: Player Level 12 + Fort Lv1 (cost 3.50M gold) ← CONFIRMED screenshot
      Lv3: Player Level ~25 + Fort?   (estimated — no confirmed data)
      Lv4: Player Level ~40?          (estimated)
      Lv5: Player Level ~55?          (estimated)
    NOTE: Fort Lv1 itself requires Player Level 10, so Mine Lv2 effectively
    requires both Level 12 AND Fort already built → hard gate at Level 12."""
    if player_lv < 3:  return 0
    if player_lv < 12: return 1   # Lv2 locked until Player Level 12 + Fort Lv1
    if player_lv < 25: return 2   # estimated
    if player_lv < 40: return 3   # estimated
    if player_lv < 55: return 4   # estimated
    return 5

def est_armory_lv(player_lv):
    """Armory upgrade player-level requirements (confirmed from game UI):
      Lv1: Player Level 10              (T4–T5 gear)
      Lv2: Player Level 30 + 7.00M gold (T6–T7 gear) ← CONFIRMED
      Lv3: ~Player Level 50 (estimated) (T8–T9 gear)
      Lv4: ~Player Level 70 (estimated) (T10 gear)
      Lv5: ~Player Level 90 (estimated) (T10 maxed)
    Mungus (Lv30, confirmed 2026-04-15) is the first known player with Armory Lv2."""
    if player_lv < 10: return 0
    if player_lv < 30: return 1   # T4–T5 cap for ALL current players
    if player_lv < 50: return 2   # T6–T7  (Player Lv30 confirmed)
    if player_lv < 70: return 3   # T8–T9  (estimated)
    if player_lv < 90: return 4   # T10    (estimated)
    return 5

def est_fort_lv(player_lv):
    """Fortification player-level requirements (confirmed + estimated):
      Lv1: Player Level 10              ← CONFIRMED
      Lv2: Player Level 20, 3.00M gold  ← CONFIRMED (screenshot 2026-04-15)
      Lv3: ~Player Level 30 (estimated — +10 level pattern, fort costs 500k×lv)
      Lv4: ~Player Level 40 (estimated)
      Lv5: ~Player Level 50 (estimated)
    Fort upgrades every ~10 player levels (vs Armory every ~20) because fort
    costs ~500k×lv vs Armory ~750k×lv.
    T2 units open at Fort Lv2 (player lv 20) — CONFIRMED by user."""
    if player_lv < 10: return 0
    if player_lv < 20: return 1   # Fort Lv1 — only HP bonus, no new unit tier
    if player_lv < 30: return 2   # Fort Lv2 — T2 units unlock (player lv 20 confirmed)
    if player_lv < 40: return 3   # Fort Lv3 — T3 units (estimated)
    if player_lv < 50: return 4   # Fort Lv4 — T4 units (estimated)
    return 5                       # Fort Lv5 — T4 units max (estimated)

def est_spy_ac_lv(player_lv):
    """Spy Academy requires Player Level 5 for Lv1.
    Spy Academy costs 250k×lv (3× cheaper than Armory at 750k×lv).
    No confirmed player-level gates for Lv2+ yet, but follows same
    economic pattern as Armory. Lv2 assumed to require ~Player Level 20
    (lower than Armory's Level 30 due to cheaper cost).
    All current players (max Lv21) are treated as Spy Academy Lv1 → T5 gear."""
    if player_lv < 5:  return 0
    if player_lv < 20: return 1   # T4–T5 spy gear (current server cap)
    if player_lv < 40: return 2   # T6–T7 estimated
    if player_lv < 60: return 3   # T8–T9 estimated
    if player_lv < 80: return 4   # T10    estimated
    return 5

def max_gear_tier(player_lv):
    """Gear tier is capped by BOTH armory level AND fort level (user confirmed)."""
    armory_cap = ARMORY_LV_TO_GEAR_TIER[est_armory_lv(player_lv)]
    fort_cap   = FORT_LV_TO_GEAR_TIER  [est_fort_lv(player_lv)]
    return min(armory_cap, fort_cap)

def max_unit_tier(player_lv):
    return FORT_LV_TO_UNIT_TIER[est_fort_lv(player_lv)]

def max_spy_tier(player_lv):
    return SPY_AC_TO_SPY_TIER[est_spy_ac_lv(player_lv)]

def mine_mult(mine_lv):
    """Each mine level adds 10% to total income. Confirmed from game text."""
    return 1.0 + mine_lv * 0.10

def stat_per_unit(player_lv):
    """Stat per fully-geared unit at max available tier for this level."""
    gt = max_gear_tier(player_lv)
    ut = max_unit_tier(player_lv)
    return UNIT_OFF[ut] + WEAPON_STATS[gt] + ARMOR_STATS[gt]


# ── Derived growth rate ─────────────────────────────────────────────────────
# "Upper-bound growth if the player spends every coin on maxing out their
# highest-tier gear + units at their current unlock ceiling."
#
# Used as a FALLBACK projection when we don't yet have enough observed
# data points to compute a real slope (compute_growth_rates needs ≥2
# observations spaced ≥30 min apart). Observed rate always wins when
# > 0; derived is the educated guess we fall back on.
#
# Inputs all come from existing helpers — no new data sources needed:
#   - est_mine_lv(), mine_mult(), BASE_INC, WORKER_GOLD → income
#   - max_gear_tier(), max_unit_tier(), max_spy_tier() → tier unlocks
#   - UNIT_OFF/DEF/SPY/SENT + WEAPON_STATS/ARMOR_STATS/SPY_* → stat-per-unit
#   - GEAR[...][tier] → gear cost
#   - UNIT_COST → train cost
#   - RACE / CLASS → bonuses
# ── Phase 2: Empirically-fitted allocation fractions ────────────────────────
# derive_growth_rate() computes a per-tick stat growth assuming the player
# allocates some fraction of their gold income to a specific stat type.
# Historically that fraction was hardcoded at 0.5 ("half goes to this stat").
# With rich intel history we can do better: observe how players with a given
# (race, class) actually split their budget, and fit the fraction per bucket.
#
#   Carrot / Mettalica (Elf/Goblin Clerics): high def fraction, low atk
#   Jasbob / Fighters:                       high atk fraction, low def
#   Thieves:                                 more income → buildings, less on gear
#
# Learning per-(race, class, stat) fractions bakes those real strategies into
# the model — so a newly-spotted Elf Cleric inherits Carrot-like growth
# assumptions even before we have ANY intel on her specifically.
#
# Populated once per tick by calibrate_models().  Empty at startup → every
# derive_growth_rate() call falls back to DEFAULT_ALLOCATION_FRACTION.
DEFAULT_ALLOCATION_FRACTION = 0.5
ALLOCATION_MIN_SAMPLES      = 3     # buckets with fewer players stay at default
ALLOCATION_MAX_FRACTION     = 1.0   # no player can spend >100% of income on one stat
FITTED_ALLOCATIONS: dict    = {}    # {(race, cls, stat): median_fraction}


def _theoretical_stat_rate(level: int, race: str, cls: str,
                           pop: int, stat_type: str) -> float:
    """derive_growth_rate() with allocation = 1.0 — the theoretical per-tick
    growth if a player dumped 100% of income into this one stat.  Used as
    the denominator when fitting observed allocation fractions.

    Returns 0.0 when inputs are invalid so callers can skip those samples."""
    if level <= 0 or pop <= 0:
        return 0.0

    mine_lv  = est_mine_lv(level)
    workers  = max(int(pop * 0.80), 1)
    rb       = RACE.get(race, RACE['Human'])
    cb       = CLASS.get(cls,  CLASS['Fighter'])
    income   = ((BASE_INC + workers * WORKER_GOLD)
                * mine_mult(mine_lv)
                * (1 + rb.get('income', 0) + cb.get('income', 0)))
    if income <= 0:
        return 0.0

    gt = max_gear_tier(level)
    ut = max_unit_tier(level)
    st = max_spy_tier(level)

    if stat_type == 'atk':
        stat_pu   = UNIT_OFF[ut] + WEAPON_STATS[gt] + ARMOR_STATS[gt]
        unit_cost = UNIT_COST.get('soldier', 1000)
        gear_cost = (GEAR[('soldier', 'weapon')][gt][2]
                     + GEAR[('soldier', 'armor')][gt][2])
        bonus     = rb.get('atk', 0) + cb.get('atk', 0)
    elif stat_type == 'def':
        stat_pu   = UNIT_DEF[ut] + WEAPON_STATS[gt] + ARMOR_STATS[gt]
        unit_cost = UNIT_COST.get('guard', 1000)
        gear_cost = (GEAR[('guard', 'weapon')][gt][2]
                     + GEAR[('guard', 'armor')][gt][2])
        bonus     = rb.get('def', 0) + cb.get('def', 0)
    elif stat_type == 'spy_off':
        spy_ut    = min(ut, max(UNIT_SPY))
        stat_pu   = UNIT_SPY[spy_ut] + SPY_WEAPON[st] + SPY_ARMOR[st]
        unit_cost = UNIT_COST.get('spy', 1000)
        gear_cost = (GEAR[('spy', 'weapon')][st][2]
                     + GEAR[('spy', 'armor')][st][2])
        bonus     = rb.get('spy', 0) + cb.get('spy', 0)
    elif stat_type == 'spy_def':
        sent_ut   = min(ut, max(UNIT_SENT))
        stat_pu   = UNIT_SENT[sent_ut] + SPY_WEAPON[st] + SPY_ARMOR[st]
        unit_cost = UNIT_COST.get('sentry', 1000)
        gear_cost = (GEAR[('sentry', 'weapon')][st][2]
                     + GEAR[('sentry', 'armor')][st][2])
        bonus     = rb.get('spy', 0) + cb.get('spy', 0)
    else:
        return 0.0

    total_cost = unit_cost + gear_cost
    if total_cost <= 0:
        return 0.0

    # allocation = 1.0 (100% of income → this stat)
    units_per_tick = income / total_cost
    return max(0.0, units_per_tick * stat_pu * (1 + bonus))


def compute_allocation_observations(growth_rates: dict,
                                    demographics: dict) -> dict:
    """For each player in growth_rates with known (level, race, class, pop),
    compute the ratio observed_per_tick / theoretical_per_tick for each of
    the 4 combat stats, and bucket by (race, class, stat).

    Args:
        growth_rates: output of compute_growth_rates() — {name: {atk_per_tick, ...}}
        demographics: {name: (level, race, cls, pop)} — pass None for
                      unknown players; those are skipped.

    Returns {(race, cls, stat): [ratio, ratio, ...]}.  Empty when nothing
    observable (too few players, or no demographic data).
    """
    buckets: dict = {}
    for name, rates in (growth_rates or {}).items():
        demo = demographics.get(name) if demographics else None
        if not demo:
            continue
        level, race, cls, pop = demo
        if level <= 0 or pop <= 0 or not race or not cls:
            continue
        for stat_key, rate_key in (
            ('atk',     'atk_per_tick'),
            ('def',     'def_per_tick'),
            ('spy_off', 'spy_off_per_tick'),
            ('spy_def', 'spy_def_per_tick'),
        ):
            observed = float(rates.get(rate_key, 0) or 0)
            if observed <= 0:
                continue   # no positive growth observed this window
            theoretical = _theoretical_stat_rate(level, race, cls, pop, stat_key)
            if theoretical <= 0:
                continue
            ratio = observed / theoretical
            # Clamp unreasonable ratios — noise (bad intel, level jumps that
            # the theoretical rate can't model) would otherwise skew the fit.
            # A ratio > 1 means the player grew faster than the theoretical
            # 100%-allocation bound, which can only happen transiently (e.g.
            # big gear upgrade from banked gold); we don't want that one
            # observation to drag the bucket median to nonsense.
            if ratio > ALLOCATION_MAX_FRACTION * 1.5:
                continue
            ratio = max(0.0, min(ratio, ALLOCATION_MAX_FRACTION))
            buckets.setdefault((race, cls, stat_key), []).append(ratio)
    return buckets


def fit_allocation_fractions(buckets: dict,
                             min_samples: int = ALLOCATION_MIN_SAMPLES) -> dict:
    """Aggregate per-bucket ratios into a single fraction.  Uses median
    (not mean) so a single outlier player doesn't skew the fit.  Buckets
    with fewer than min_samples observations are dropped — the caller will
    fall back to DEFAULT_ALLOCATION_FRACTION."""
    out: dict = {}
    for key, ratios in (buckets or {}).items():
        if not ratios or len(ratios) < min_samples:
            continue
        s = sorted(ratios)
        n = len(s)
        # Manual median (stdlib statistics module not needed for 4-8 items)
        mid = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
        out[key] = float(mid)
    return out


def derive_growth_rate(level: int, race: str, cls: str,
                       pop: int, stat_type: str) -> float:
    """Per-tick stat growth assuming income → units+gear at the player's
    max unlocked tier, with an EMPIRICALLY-FITTED allocation fraction when
    we have enough observations for this (race, class, stat) bucket.
    Falls back to DEFAULT_ALLOCATION_FRACTION otherwise.

    stat_type: one of 'atk', 'def', 'spy_off', 'spy_def'.
    Returns float stat-points per 30-minute tick; 0.0 if inputs invalid.
    """
    if level <= 0 or pop <= 0:
        return 0.0
    base = _theoretical_stat_rate(level, race, cls, pop, stat_type)
    if base <= 0:
        return 0.0
    frac = FITTED_ALLOCATIONS.get((race, cls, stat_type),
                                  DEFAULT_ALLOCATION_FRACTION)
    return max(0.0, base * frac)


# ── Phase 3: Precision from rich intel ──────────────────────────────────────
# When we have a fresh spy report for a target we don't need to GUESS their
# mine level or reconstruct ATK from a rank curve — we know what buildings
# they own, what gear is in their armory, and what battle upgrades they've
# bought.  These helpers turn that intel into exact values estimate() can
# publish instead of approximations.
#
# Freshness window matches CALIBRATION_FRESH_HOURS (48h).  Beyond that the
# intel is a floor, not a current-truth — the player has almost certainly
# grown past what we last saw.
RICH_INTEL_FRESH_HOURS = CALIBRATION_FRESH_HOURS


def _is_rich_intel_fresh(rich_intel: dict) -> bool:
    """True if the snapshot was captured within RICH_INTEL_FRESH_HOURS.
    Stale snapshots still provide useful FLOORS (via CONFIRMED_STATS path)
    but their exact-state numbers (mine level, army comp, etc.) have
    already drifted — we don't trust them as current-truth."""
    if not rich_intel or not isinstance(rich_intel, dict):
        return False
    ts = rich_intel.get("captured_at", "")
    if not ts:
        return False
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.datetime.strptime(ts, fmt)
            break
        except ValueError:
            continue
    else:
        return False
    age = datetime.datetime.now() - dt
    return age.total_seconds() < RICH_INTEL_FRESH_HOURS * 3600


def _exact_income_from_intel(rich_intel: dict, race: str, cls: str):
    """Compute exact gold/tick from known mine level + worker count.

    Returns int income or None when the needed fields aren't in the snapshot.
    """
    if not rich_intel or not isinstance(rich_intel, dict):
        return None
    army      = rich_intel.get("army", {}) or {}
    buildings = rich_intel.get("buildings", {}) or {}
    workers   = int(army.get("workers", 0) or 0)
    mine_lv   = buildings.get("mine")
    if mine_lv is None or workers <= 0:
        return None
    rb = RACE.get(race, RACE['Human'])
    cb = CLASS.get(cls,  CLASS['Fighter'])
    return int((BASE_INC + workers * WORKER_GOLD)
               * mine_mult(int(mine_lv))
               * (1 + rb.get('income', 0) + cb.get('income', 0)))


def _reconstruct_stat_from_intel(rich_intel: dict, race: str, cls: str,
                                  kind: str):
    """Rebuild exact ATK or DEF from the target's army + armory + battle
    upgrades as captured in the spy report.

    Formula (matches in-game displayed total):
        base = soldiers × per_unit_bonus
             + min(weapon_qty, soldiers) × weapon_bonus
             + min(armor_qty,  soldiers) × armor_bonus
             + battle_upgrade_qty × upgrade_bonus
        total = base × (1 + race_bonus + class_bonus)

    kind: 'atk' (offensive military + Offense gear + Offense upgrades)
       or 'def' (defensive military + Defense gear + Defense upgrades).

    Returns int or None if required fields missing.
    """
    if not rich_intel or not isinstance(rich_intel, dict):
        return None
    if kind not in ('atk', 'def'):
        return None

    army_detail = rich_intel.get("army_detail", {}) or {}
    armory      = rich_intel.get("armory", {})      or {}
    upgrades    = rich_intel.get("upgrades", {})    or {}

    role_key      = 'soldiers'         if kind == 'atk' else 'guards'
    gear_weapon   = 'Offense Weapons'  if kind == 'atk' else 'Defense Weapons'
    gear_armor    = 'Offense Armor'    if kind == 'atk' else 'Defense Armor'
    upgrade_kind  = 'Offense'          if kind == 'atk' else 'Defense'
    bonus_stat    = 'atk'              if kind == 'atk' else 'def'

    role = army_detail.get(role_key) or {}
    qty  = int(role.get('qty', 0)   or 0)
    unit_bonus = int(role.get('bonus', 0) or 0)
    if qty <= 0:
        return None

    # Unit contribution
    base = qty * unit_bonus

    # Weapons (1:1 with units — game enforces this cap)
    weapon_stat = 0
    for _item, detail in armory.items():
        if (detail or {}).get('category') == gear_weapon:
            wqty   = int(detail.get('qty', 0)   or 0)
            wbonus = int(detail.get('bonus', 0) or 0)
            equipped = min(wqty, qty)
            weapon_stat += equipped * wbonus
    base += weapon_stat

    # Armor (1:1 with units)
    armor_stat = 0
    for _item, detail in armory.items():
        if (detail or {}).get('category') == gear_armor:
            aqty   = int(detail.get('qty', 0)   or 0)
            abonus = int(detail.get('bonus', 0) or 0)
            equipped = min(aqty, qty)
            armor_stat += equipped * abonus
    base += armor_stat

    # Battle upgrades (Steed for Offense, Guard Tower for Defense)
    upgrade_stat = 0
    for _upg, detail in upgrades.items():
        if (detail or {}).get('kind') == upgrade_kind:
            uqty   = int(detail.get('qty', 0)   or 0)
            ubonus = int(detail.get('bonus', 0) or 0)
            upgrade_stat += uqty * ubonus
    base += upgrade_stat

    # Race/class multiplier
    rb = RACE.get(race, RACE['Human'])
    cb = CLASS.get(cls,  CLASS['Fighter'])
    mult = 1 + rb.get(bonus_stat, 0) + cb.get(bonus_stat, 0)
    return int(base * mult)


def _fort_damage_pct(rich_intel: dict):
    """Fort damage as a fraction [0, 1].  0 = full HP, 1 = destroyed.
    Returns None when fort fields aren't available."""
    if not rich_intel or not isinstance(rich_intel, dict):
        return None
    hp  = rich_intel.get("fort_hp")
    mx  = rich_intel.get("fort_max")
    if hp is None or not mx:
        return None
    try:
        hp_f = float(hp); mx_f = float(mx)
        if mx_f <= 0:
            return None
        return max(0.0, 1.0 - hp_f / mx_f)
    except (TypeError, ValueError):
        return None

# ── Race/class bonuses (CONFIRMED from in-game UI screenshot 2026-04-08) ──────
# Source: game Race/Class selection screen, showing exact bonuses per choice.
#
#  RACE      BONUS         NOTES
#  ────────────────────────────────────────────────────────────────
#  Human     +5% Offense   ATK multiplier
#  Goblin    +5% Defense   DEF multiplier
#  Elf       +5% Defense   DEF multiplier  ← was wrongly coded as +5% spy
#  Undead    +5% Offense   ATK multiplier  ← was wrongly coded as +5% def + +5% spy
#
#  CLASS     BONUS         NOTES
#  ────────────────────────────────────────────────────────────────
#  Fighter   +5% Offense   ATK multiplier
#  Cleric    +5% Defense   DEF multiplier
#  Thief     +5% Income    Gold income mult ← was wrongly coded as +5% spy
#  Assassin  +5% Intel     Spy off+def mult ← was wrongly coded as +10% spy
#
# Combined examples:
#   Ashcipher  (Human/Fighter):   +10% ATK total
#   Radagon    (Goblin/Cleric):   +10% DEF total  ← explains why he's #1 DEF
#   Mungus     (Undead/Thief):    +5%  ATK + 5% Income
#   JT         (Goblin/Thief):    +5%  DEF + 5% Income
#   sirclement (Undead/Assassin): +5%  ATK + 5% Spy off+def
#
# 'income' key → applied to gold income formula only (not combat stats).
# 'spy' key    → applied to BOTH spy_off and spy_def (Intel covers both).
RACE = {
    'Human':  {'atk':0.05, 'def':0.00, 'spy':0.00, 'income':0.00},
    'Goblin': {'atk':0.00, 'def':0.05, 'spy':0.00, 'income':0.00},
    'Elf':    {'atk':0.00, 'def':0.05, 'spy':0.00, 'income':0.00},
    'Undead': {'atk':0.05, 'def':0.00, 'spy':0.00, 'income':0.00},
}
CLASS = {
    'Fighter':  {'atk':0.05, 'def':0.00, 'spy':0.00, 'income':0.00},
    'Cleric':   {'atk':0.00, 'def':0.05, 'spy':0.00, 'income':0.00},
    'Thief':    {'atk':0.00, 'def':0.00, 'spy':0.00, 'income':0.05},
    'Assassin': {'atk':0.00, 'def':0.00, 'spy':0.05, 'income':0.00},
}

WORKER_GOLD  = 65
BASE_INC     = 1000
TICKS        = 48


# ── Rank-calibrated exponential decay models ──────────────────────────────────
# stat(rank) = A * exp(-k * rank)
# Models are pre-seeded from CONFIRMED data and re-calibrated at runtime
# whenever profiles have been scraped and ranks are known.
#
# ATK — seed from Ashcipher(off_rank=1,ATK=76408); dynamically recalibrated each
#   tick using confirmed player anchors (Mungus ATK=323975 @2026-04-15, etc.)
ATK_RANK_A = 83_484.0
ATK_RANK_K = 0.0885

# DEF — seeded from Radagon (April-8 snapshot, likely stale).
# k is set to 0.015 — a much flatter curve appropriate for a young/compressed server.
# calibrate_models() will re-anchor A each tick using YOUR real rank+DEF as a data point,
# so rank_def(your_rank) == your_real_def and everyone above you scores HIGHER.
DEF_RANK_A = 84_647.0
DEF_RANK_K = 0.015

# SPY OFFENSE — no confirmed spy rank data yet; A=0 means "not yet calibrated"
# Will be seeded from first confirmed (spy_off_rank, spy_off) pair after profile scrape.
SPY_OFF_RANK_A = 0.0
SPY_OFF_RANK_K = 0.0

# SPY DEFENSE — same: seeded once we have (spy_def_rank, spy_def) pairs
SPY_DEF_RANK_A = 0.0
SPY_DEF_RANK_K = 0.0


def _fit_exponential(rank1, val1, rank2, val2):
    """Fit stat = A * exp(-k * rank) from two (rank, value) data points.
    Returns (A, k). Returns (0, 0) if inputs are invalid."""
    if rank1 <= 0 or rank2 <= 0 or val1 <= 0 or val2 <= 0 or rank1 == rank2:
        return 0.0, 0.0
    k = math.log(val1 / val2) / (rank2 - rank1)
    A = val1 / math.exp(-k * rank1)
    return (A, k) if A > 0 and k > 0 else (0.0, 0.0)


def _fit_exp_regression(points):
    """Fit stat = A * exp(-k * rank) via log-linear least-squares over
    ALL supplied (rank, value) points. Gives extra weight to the lowest
    rank (top of the curve) because rank 1-10 are what we most need
    accurate — the tail of the distribution doesn't matter for battle
    decisions. Returns (A, k) or (0, 0) if the fit fails.

    Weighting scheme: each point gets weight = 1 / log(rank + 1).
    rank 1 → weight ~1.44, rank 30 → weight ~0.29, rank 80 → weight ~0.23.
    This keeps the low ranks authoritative without letting a single
    noisy top-end point dominate.
    """
    pts = [(r, v) for (r, v) in points if r > 0 and r < 900 and v > 0]
    if len(pts) < 2:
        return 0.0, 0.0
    # Transform to log space: log(v) = log(A) - k * r
    import math as _m
    xs, ys, ws = [], [], []
    for r, v in pts:
        xs.append(float(r))
        ys.append(_m.log(float(v)))
        ws.append(1.0 / _m.log(float(r) + 1.0))
    W  = sum(ws)
    if W <= 0:
        return 0.0, 0.0
    mx = sum(w * x for w, x in zip(ws, xs)) / W
    my = sum(w * y for w, y in zip(ws, ys)) / W
    num = sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys))
    den = sum(w * (x - mx) ** 2 for w, x in zip(ws, xs))
    if den <= 0:
        return 0.0, 0.0
    slope     = num / den          # slope of log(v) vs r   =   -k
    intercept = my - slope * mx    # log(A)
    k = -slope
    A = _m.exp(intercept)
    if A <= 0 or k <= 0:
        return 0.0, 0.0
    return A, k


def _seed_model(points: list, base_k: float, label: str, your_point=None):
    """Fit or seed a model from a list of (rank, value) confirmed points.
    Returns (A, k).

    Priority of fit strategies:
      1. YOU + highest-value point (2-point exact fit). YOU is the
         freshest ground truth for your rank; the highest-value point
         (e.g. Radagon 530k at rank 1) is the best-known top-of-curve
         floor. The resulting curve passes exactly through BOTH, which
         is what battle decisions need.
      2. Log-linear weighted regression over all points, re-anchored
         vertically so the curve passes through the highest-value
         point. Used when YOU isn't available (spy ranks not scraped).
      3. 2-point fallback (widest-span pair picker).
      4. 1-point fallback (seed from the single known point).
    """
    points = sorted(p for p in points if p[0] > 0 and 0 < p[0] < 900 and p[1] > 0)
    if not points:
        return 0.0, 0.0

    def _is_your(p):
        return your_point is not None and p == your_point

    # Strategy 1: regression re-anchored at TOP confirmed value (≥4 pts)
    # With 4+ calibration points the regression uses ALL the data —
    # rank-1 to rank-50 — to find the best-fit slope instead of the
    # naive 2-point line that misses the middle of the curve.
    #
    # We re-anchor at the HIGHEST-VALUE confirmed point (usually rank 1)
    # so the top of the curve is exact. This prevents the model from
    # producing rank-2 estimates higher than rank-1 (which is logically
    # impossible and causes display rank-order violations on the
    # dashboard). YOUR rank always shows your real stat via the is_you
    # branch in estimate() regardless of the model's prediction there.
    if len(points) >= 4:
        A, k = _fit_exp_regression(points)
        if A > 0 and k > 0:
            k_c = min(k, 0.12)   # slightly higher cap than 2-pt to let the slope breathe
            top = max(points, key=lambda p: p[1])
            A = top[1] / math.exp(-k_c * top[0])
            your_est = int(A * math.exp(-k_c * your_point[0])) if your_point else 0
            preview = ", ".join(f"r{p[0]}={p[1]:,}" for p in points[:4])
            anchor_lbl = f"top r{top[0]}={top[1]:,}"
            you_lbl    = f"  YOUR r{your_point[0]}→{your_est:,}" if your_point else ""
            print(f"     📐 {label} regression ({len(points)} pts, "
                  f"anchored {anchor_lbl}): "
                  f"A={A:,.0f} k={k_c:.4f}{you_lbl}  [{preview} ...]")
            return A, k_c

    # Strategy 2: YOU + highest-value anchor (fallback with <4 pts)
    if your_point and your_point[0] > 0 and your_point[0] < 900 and your_point[1] > 0:
        others = [p for p in points if not _is_your(p)]
        if others:
            top = max(others, key=lambda p: p[1])
            A, k = _fit_exponential(top[0], top[1], your_point[0], your_point[1])
            if A > 0 and k > 0:
                k_c = min(k, 0.10)
                if k_c < k:
                    A = top[1] / math.exp(-k_c * top[0])
                print(f"     📐 {label} calibrated (YOU+top): "
                      f"A={A:,.0f} k={k_c:.4f}  "
                      f"(YOU {your_point[0]}={your_point[1]:,}, "
                      f"top rank {top[0]}={top[1]:,}, {len(points)} pts)")
                return A, k_c

    # Strategy 3: regression with re-anchor at top of curve (no YOUR point)
    if len(points) >= 3:
        A, k = _fit_exp_regression(points)
        if A > 0 and k > 0:
            top = max(points, key=lambda p: p[1])
            A = top[1] / math.exp(-k * top[0])
            print(f"     📐 {label} regression ({len(points)} pts) re-anchored: "
                  f"A={A:,.0f} k={k:.4f}  top: rank {top[0]}={top[1]:,}")
            return A, k

    # Strategy 3: widest-span 2-point fallback
    if len(points) >= 2:
        candidates = []   # (has_your, span, A, k, p1, p2)
        for i in range(len(points)):
            for j in range(i + 1, len(points)):
                p1, p2 = points[i], points[j]
                A, k = _fit_exponential(p1[0], p1[1], p2[0], p2[1])
                if A > 0 and k > 0:
                    span = p2[0] - p1[0]
                    has_your = _is_your(p1) or _is_your(p2)
                    candidates.append((has_your, span, A, k, p1, p2))
        if candidates:
            candidates.sort(key=lambda c: (not c[0], -c[1], c[3]))
            has_your, span, A, k, p1, p2 = candidates[0]
            tag = " [YOUR]" if has_your else ""
            print(f"     📐 {label} calibrated (2pt):  A={A:,.0f} k={k:.4f} "
                  f"(pts: {p1}, {p2}, span={span}){tag}")
            return A, k
        # No valid pair — seed from best single point
        if your_point and your_point[0] > 0 and your_point[1] > 0 and your_point[0] < 900:
            r, v = your_point
            note = "YOUR live anchor"
        else:
            best = max(points, key=lambda p: p[1])
            r, v = best
            note = "highest-value floor"
        k = base_k
        A = v / math.exp(-k * r)
        print(f"     ⚠️  {label}: no valid 2-pt fit — seeding from {note}")
        print(f"     📐 {label} seeded (fallback 1pt): A={A:,.0f} k={k:.4f} "
              f"(rank={r}, val={v:,})")
        return A, k

    # Strategy 4: single point
    if len(points) == 1:
        r, v = points[0]
        k = base_k
        A = v / math.exp(-k * r)
        print(f"     📐 {label} seeded (1pt): A={A:,.0f} k={k:.4f} "
              f"(rank={r}, val={v:,})")
        return A, k
    return 0.0, 0.0


def rank_atk(off_rank: int) -> int:
    if off_rank <= 0 or off_rank >= 900 or ATK_RANK_A <= 0: return 0
    return int(ATK_RANK_A * math.exp(-ATK_RANK_K * off_rank))

def rank_def(def_rank: int) -> int:
    if def_rank <= 0 or def_rank >= 900 or DEF_RANK_A <= 0: return 0
    return int(DEF_RANK_A * math.exp(-DEF_RANK_K * def_rank))

def rank_spy_off(spy_off_rank: int) -> int:
    if spy_off_rank <= 0 or spy_off_rank >= 900 or SPY_OFF_RANK_A <= 0: return 0
    return int(SPY_OFF_RANK_A * math.exp(-SPY_OFF_RANK_K * spy_off_rank))

def rank_spy_def(spy_def_rank: int) -> int:
    if spy_def_rank <= 0 or spy_def_rank >= 900 or SPY_DEF_RANK_A <= 0: return 0
    return int(SPY_DEF_RANK_A * math.exp(-SPY_DEF_RANK_K * spy_def_rank))


def calibrate_models(profiles: dict, you: dict = None):
    """Re-calibrate all four stat models.

    Data sources (best to least reliable):
      1. YOUR live stats from private_latest.json  — always fresh, exact rank known
      2. CONFIRMED_STATS cross-referenced with scraped profile ranks — may be stale
         (if a confirmed point produces k < 0 the two-point fit is rejected)

    YOUR data is added last so it is always included in the point list and acts as
    the primary anchor when confirmed data is stale / contradicts server ranks.

    k is capped at 0.05 (max) to prevent absurdly steep rank curves on young servers.
    """
    global ATK_RANK_A, ATK_RANK_K
    global DEF_RANK_A, DEF_RANK_K
    global SPY_OFF_RANK_A, SPY_OFF_RANK_K
    global SPY_DEF_RANK_A, SPY_DEF_RANK_K

    global CONFIRMED_STATS_LIVE   # module-level merged dict, read by estimate()
    atk_pts, def_pts, spo_pts, spd_pts = [], [], [], []

    # Rank snapshot — freshest + most complete rank source (every ranked
    # player, scraped this tick).  Used below as a fallback when a confirmed
    # anchor's rank isn't in the profile-scrape output + CS doesn't carry a
    # rank hint.  Without this, anchors for top players like Jasbob/Mungus/
    # Tycoon silently drop out of the fit and the curve defaults to its
    # seed values — which is why rank_atk(5) used to return ~53k instead
    # of ~400k on a server where rank 1 has 545k ATK.
    _rank_snap_for_cal = {}
    try:
        if os.path.isfile('private_rankings_snapshot.json'):
            with open('private_rankings_snapshot.json', encoding='utf-8') as f:
                _rank_snap_for_cal = json.load(f).get('rank_map', {}) or {}
    except Exception as _e:
        pass

    # Merge hardcoded CONFIRMED_STATS with any intel harvested from real
    # attack / spy reports (private_intel.csv).  Intel wins on a per-stat
    # basis — if we spied someone's def=4,152 yesterday that beats whatever
    # floor was hardcoded, AND the per-stat max() floor treatment in
    # estimate() keeps the model conservative against growth.
    #
    # Each merged stat also carries a per-stat "verified_at" timestamp so
    # calibrate_models can age-filter: stale snapshots still act as a
    # floor in estimate() but no longer drag the rank curve down as
    # anchors. Hardcoded CONFIRMED_STATS entries may specify an optional
    # 'verified_at' field in two forms:
    #   'verified_at': '2026-04-15'              # applies to every stat
    #   'verified_at': {'def': '2026-04-15'}     # per-stat (others stay stale)
    # If missing, the entry is treated as unknown-age and excluded from
    # calibration entirely.
    def _parse_verified_at(v):
        if not v: return None
        if isinstance(v, datetime.datetime): return v
        s = str(v).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    def _resolve_verified_at(entry: dict, stat: str):
        """Look up the verified_at timestamp for a single stat on a
        hardcoded CONFIRMED_STATS row. Returns a datetime or None."""
        v = entry.get('verified_at')
        if not v:
            return None
        if isinstance(v, dict):
            return _parse_verified_at(v.get(stat))
        return _parse_verified_at(v)

    confirmed_all = {}
    for k, v in CONFIRMED_STATS.items():
        base = dict(v)
        # Per-stat verified_at: a hardcoded entry can be partly fresh
        # (e.g. def was re-confirmed today) and partly stale (e.g. atk
        # from an old screenshot) — stamp each stat individually so
        # calibrate only pulls the fresh ones into the fit.
        for stat in ('atk', 'def', 'spy_off', 'spy_def'):
            if base.get(stat):
                dt = _resolve_verified_at(v, stat)
                if dt is not None:
                    base[f'{stat}_verified_at'] = dt
        confirmed_all[k] = base

    try:
        overlay = load_intel_overlay()
        for iname, ient in overlay.items():
            base = dict(confirmed_all.get(iname, {}))
            intel_dt = _parse_verified_at(ient.get('timestamp', ''))
            for k_intel, k_cs in [
                ("atk",     "atk"),
                ("def",     "def"),
                ("spy_off", "spy_off"),
                ("spy_def", "spy_def"),
            ]:
                v = int(ient.get(k_intel, 0))
                if v > 0 and v > int(base.get(k_cs, 0) or 0):
                    base[k_cs] = v
                    # Stamp this stat with the intel timestamp so the
                    # calibration step can tell fresh intel apart from
                    # the hardcoded floors.
                    if intel_dt is not None:
                        base[f'{k_cs}_verified_at'] = intel_dt
            # Demographic fields — prefer intel (fresher) when present.
            for k in ("level", "race", "cls"):
                if ient.get(k):
                    base[k] = ient[k]
            confirmed_all[iname] = base
    except Exception as e:
        print(f"  ⚠️  calibrate intel overlay failed: {e}")

    # Collect calibration anchor points from confirmed spy data + scraped profiles.
    # Philosophy: confirmed stats are SNAPSHOTS in time — they tell us the minimum
    # (floor) a player had when we observed them, not their current value.
    # Ranks change every tick; a player's profile rank is CURRENT, not the rank at
    # which their stats were spied. We therefore always prefer the FRESHEST rank
    # (scraped profile rank > CONFIRMED_STATS rank hint).
    # Confirmed off_rank/def_rank in CONFIRMED_STATS is only a calibration hint for
    # players whose profile hasn't been scraped yet; once profiles are scraped the
    # live profile rank supersedes any hardcoded rank.
    # Publish the merged dict for estimate() to read this tick.
    CONFIRMED_STATS_LIVE = confirmed_all

    cal_cutoff = (datetime.datetime.now()
                  - datetime.timedelta(hours=CALIBRATION_FRESH_HOURS))

    def _is_fresh(cs: dict, stat: str) -> bool:
        """Per-stat freshness check for calibration anchors."""
        dt = cs.get(f'{stat}_verified_at')
        return isinstance(dt, datetime.datetime) and dt >= cal_cutoff

    stale_dropped = {'atk': 0, 'def': 0, 'spy_off': 0, 'spy_def': 0}
    bots_dropped = 0

    # ── Growth projection for calibration anchors ──────────────────
    # Rank-1 (and every rank) grows every tick. A confirmed stat
    # captured yesterday is already stale — if we feed it to the fit
    # as-is, the whole curve is anchored too low. Project each stat
    # FORWARD to "now" using the player's observed growth rate before
    # feeding it to the curve fit. This keeps the rank model aligned
    # with current reality as everyone levels up.
    global GROWTH_RATES_LIVE
    _cal_now    = datetime.datetime.now()
    _growth_map = {}
    try:
        _growth_map = compute_growth_rates(load_player_growth())
    except Exception as e:
        print(f"     ⚠️  calibrate: growth projection unavailable ({e})")
    # Publish for estimate() to project confirmed stats forward too —
    # otherwise the dashboard displays stale snapshots (e.g. TGO 394k
    # ATK frozen at the April-15 confirmation) even as the player grows.
    GROWTH_RATES_LIVE = _growth_map

    # ── Phase 2: fit per-(race, class, stat) allocation fractions ─────
    # Walk observed per-tick rates in _growth_map, pair each with the
    # player's demographics (level/race/class/pop) so we can compute a
    # theoretical 100%-allocation rate as the denominator.  The ratio
    # observed:theoretical is the empirical fraction that player allocated
    # to this stat.  Buckets with ≥ALLOCATION_MIN_SAMPLES players get a
    # median-of-ratios fit; everything else falls back to the default.
    # derive_growth_rate() (called immediately below when we build
    # DERIVED_GROWTH_RATES_LIVE) reads FITTED_ALLOCATIONS so the fitted
    # fractions take effect THIS tick, not next.
    global FITTED_ALLOCATIONS
    _alloc_demos: dict = {}
    try:
        _baseline_for_alloc = {p[0].replace(' (YOU)', ''): p for p in PLAYERS}
        # Every name appearing in the growth map gets its best-known
        # (level, race, cls, pop) triple.  Source priority: profile scrape
        # (freshest) > CONFIRMED_STATS_LIVE (merged with intel overlay) >
        # PLAYERS tuple (stale demographic fallback).
        for _cname in (_growth_map or {}).keys():
            _lvl = 0; _race = ''; _cls = ''; _pop = 0
            base = _baseline_for_alloc.get(_cname)
            if base:
                _lvl, _race, _cls, _pop = base[1], base[2], base[3], base[4]
            cs  = (confirmed_all or {}).get(_cname) or {}
            if cs.get('level'):     _lvl  = cs['level']  or _lvl
            if cs.get('race'):      _race = cs['race']   or _race
            if cs.get('cls'):       _cls  = cs['cls']    or _cls
            prof = (profiles or {}).get(_cname, {})
            if prof.get('level'):       _lvl  = prof['level']       or _lvl
            if prof.get('race'):        _race = prof['race']        or _race
            if prof.get('cls'):         _cls  = prof['cls']         or _cls
            if prof.get('population'):  _pop  = prof['population']  or _pop
            # Skip bots — they don't follow the standard build progression
            if '[bot]' in _cname.lower():
                continue
            _alloc_demos[_cname] = (_lvl, _race, _cls, _pop)
        _buckets = compute_allocation_observations(_growth_map, _alloc_demos)
        FITTED_ALLOCATIONS = fit_allocation_fractions(_buckets)
        if FITTED_ALLOCATIONS:
            # Brief summary in the tick log — full detail in the fitted dict.
            _samples = sum(len(v) for v in _buckets.values())
            print(f"     📈 allocation fit: {len(FITTED_ALLOCATIONS)} bucket(s) "
                  f"over {_samples} observation(s) "
                  f"(of {len(_buckets)} total bucket(s) before min-samples gate)")
    except Exception as _e:
        print(f"     ⚠️  allocation fit failed: {_e}")
        FITTED_ALLOCATIONS = {}

    # ── Phase 4: projection vs intel error feedback ──────────────────────
    # Pair projections from private_projection_log.csv with the latest intel
    # overlay to measure how accurate our estimates have been.  Populates
    # PROJECTION_ERRORS_LIVE (per-player) + BUCKET_ERRORS_LIVE (per-bucket)
    # which estimate() reads via _confidence_score() to stamp conf_score.
    # First run with no history → no errors yet, conf_score falls back to
    # the heuristic-only path.  As projections + intel accumulate, confidence
    # scores become data-driven.
    global PROJECTION_ERRORS_LIVE, BUCKET_ERRORS_LIVE
    try:
        _proj_log = load_projection_log()
        # Reuse the overlay loaded above if available; otherwise re-read.
        # `overlay` may be undefined if the earlier try/except failed before
        # the assignment, hence the defensive locals() check.
        _overlay_for_err = (overlay if 'overlay' in locals() and isinstance(overlay, dict)
                            else load_intel_overlay())
        _p_err, _b_err = compute_projection_errors(_proj_log, _overlay_for_err)
        PROJECTION_ERRORS_LIVE = _p_err
        BUCKET_ERRORS_LIVE     = _b_err
        if _p_err:
            # Brief summary — full detail available to estimate() via module state.
            n_players = len(_p_err)
            avg_err = sum(
                (sum(p.get(k, 0) for k in
                     ('atk_err_pct','def_err_pct','spy_off_err_pct','spy_def_err_pct')
                     if k in p) / max(1, sum(1 for k in
                     ('atk_err_pct','def_err_pct','spy_off_err_pct','spy_def_err_pct') if k in p)))
                for p in _p_err.values()
            ) / max(1, n_players)
            print(f"     🎯 projection accuracy: {n_players} player(s) checked, "
                  f"avg stat error {avg_err:.1%} — "
                  f"{len(_b_err)} (race,class) bucket(s) with error history")
    except Exception as _e:
        print(f"     ⚠️  projection-error compute failed: {_e}")
        PROJECTION_ERRORS_LIVE = {}
        BUCKET_ERRORS_LIVE     = {}

    # Populate DERIVED growth rates from level/race/class/population.
    # This is a FALLBACK for players whose observed rate is 0 (not enough
    # growth-CSV observations yet). Every known player gets a derived
    # rate so no confirmed snapshot stays frozen.
    global DERIVED_GROWTH_RATES_LIVE
    _derived_map = {}
    try:
        # Walk the PLAYERS baseline first, then overlay any additional
        # players found in the scraped profiles (which may include
        # players not in the static PLAYERS list).
        _seen_d = set()
        _baseline = {p[0].replace(' (YOU)', ''): p for p in PLAYERS}
        # Union of PLAYERS + profiles keys
        _all_names = set(_baseline.keys()) | set((profiles or {}).keys())
        for _clean_p in _all_names:
            base = _baseline.get(_clean_p)
            _plvl  = base[1] if base else 0
            _prace = base[2] if base else ''
            _pcls  = base[3] if base else ''
            _ppop  = base[4] if base else 0
            # Prefer scraped profile data when it's richer than the baseline
            _prof = profiles.get(_clean_p, {}) if profiles else {}
            _plvl  = _prof.get('level',      _plvl)  or _plvl
            _prace = _prof.get('race',       _prace) or _prace
            _pcls  = _prof.get('cls',        _pcls)  or _pcls
            _ppop  = _prof.get('population', _ppop)  or _ppop
            if _plvl <= 0 or _ppop <= 0:
                continue
            # Skip bots — they don't follow the derived model
            if '[bot]' in _clean_p.lower():
                continue
            _derived_map[_clean_p] = {
                'atk_per_tick':     derive_growth_rate(_plvl, _prace, _pcls, _ppop, 'atk'),
                'def_per_tick':     derive_growth_rate(_plvl, _prace, _pcls, _ppop, 'def'),
                'spy_off_per_tick': derive_growth_rate(_plvl, _prace, _pcls, _ppop, 'spy_off'),
                'spy_def_per_tick': derive_growth_rate(_plvl, _prace, _pcls, _ppop, 'spy_def'),
            }
    except Exception as e:
        print(f"     ⚠️  calibrate: derived-growth build failed ({e})")
    DERIVED_GROWTH_RATES_LIVE = _derived_map
    if _derived_map:
        print(f"     📊 derived growth rates: {len(_derived_map)} players")

    def _project(cname: str, stat: str, raw_val: int, verified_at) -> int:
        """Advance a confirmed stat forward by the player's growth rate
        × ticks-since-observation. Returns the projected current value
        or the raw value if no growth data is available.

        Rate selection: OBSERVED wins when > 0; DERIVED is the fallback
        so players with only 1 data point still get a sensible projection
        instead of staying frozen at the snapshot value."""
        if raw_val <= 0:
            return raw_val
        if not isinstance(verified_at, datetime.datetime):
            return raw_val
        key = f"{stat}_per_tick"
        obs_rates = _growth_map.get(cname) or {}
        drv_rates = _derived_map.get(cname) or {}
        rate = obs_rates.get(key, 0) or 0
        if rate <= 0:
            rate = drv_rates.get(key, 0) or 0
        if rate <= 0:
            return raw_val
        ticks_since = max(0.0, (_cal_now - verified_at).total_seconds() / 1800.0)
        if ticks_since <= 0:
            return raw_val
        return int(raw_val + rate * ticks_since)

    projected_count = 0
    for cname, cs in confirmed_all.items():
        # Bots follow a completely different stat curve than real players:
        # they're mass-spawned with near-zero def (≈100) regardless of
        # their leaderboard rank, which makes them poisonous anchors for
        # the exponential rank-curve fit. A single Radagon at rank 1 with
        # 530k def plus 50 bots at rank 75-82 with ~100 def produces a
        # nonsense slope. Skip bots entirely for calibration; they still
        # serve fine as battle targets via the estimates lookup path.
        if '[bot]' in cname.lower():
            bots_dropped += 1
            continue

        p = profiles.get(cname, {})
        rs = _rank_snap_for_cal.get(cname, {})
        # Rank priority (freshest first):
        #   1. Profile scrape — rank at the moment the profile was fetched
        #   2. Rank snapshot  — global leaderboard scrape this tick (covers
        #                       all 250+ ranked players, not just the
        #                       profile-scanned subset)
        #   3. CS rank hint   — static fallback from hardcoded entries
        ar  = p.get('off_rank',     0) or rs.get('off_rank',     0) or cs.get('off_rank',     0)
        dr  = p.get('def_rank',     0) or rs.get('def_rank',     0) or cs.get('def_rank',     0)
        sor = p.get('spy_off_rank', 0) or rs.get('spy_off_rank', 0) or cs.get('spy_off_rank', 0)
        sdr = p.get('spy_def_rank', 0) or rs.get('spy_def_rank', 0) or cs.get('spy_def_rank', 0)
        # Filter sentinel rank 999 ("unknown rank") and 0 at point-collection time,
        # AND skip any stat whose verified_at is older than the freshness cutoff.
        if 0 < ar  < 900 and cs.get('atk',     0):
            if _is_fresh(cs, 'atk'):
                raw = cs['atk']
                proj = _project(cname, 'atk', raw, cs.get('atk_verified_at'))
                if proj > raw: projected_count += 1
                atk_pts.append((ar, proj))
            else: stale_dropped['atk'] += 1
        if 0 < dr  < 900 and cs.get('def',     0):
            if _is_fresh(cs, 'def'):
                raw = cs['def']
                proj = _project(cname, 'def', raw, cs.get('def_verified_at'))
                if proj > raw: projected_count += 1
                def_pts.append((dr, proj))
            else: stale_dropped['def'] += 1
        if 0 < sor < 900 and cs.get('spy_off', 0):
            if _is_fresh(cs, 'spy_off'):
                raw = cs['spy_off']
                proj = _project(cname, 'spy_off', raw, cs.get('spy_off_verified_at'))
                spo_pts.append((sor, proj))
            else: stale_dropped['spy_off'] += 1
        if 0 < sdr < 900 and cs.get('spy_def', 0):
            if _is_fresh(cs, 'spy_def'):
                raw = cs['spy_def']
                proj = _project(cname, 'spy_def', raw, cs.get('spy_def_verified_at'))
                spd_pts.append((sdr, proj))
            else: stale_dropped['spy_def'] += 1
    if bots_dropped:
        print(f"     🤖 calibrate: excluded {bots_dropped} [bot] row(s) from rank fit")
    if projected_count:
        print(f"     📈 calibrate: {projected_count} anchor(s) projected forward via growth rate")

    total_stale = sum(stale_dropped.values())
    if total_stale:
        print(f"     🕒 calibrate: dropped stale anchors "
              f"(>{CALIBRATION_FRESH_HOURS}h) "
              f"atk={stale_dropped['atk']} def={stale_dropped['def']} "
              f"spo={stale_dropped['spy_off']} spd={stale_dropped['spy_def']}")

    # Add YOUR live stats as the most-reliable anchor point
    # (rank scraped this tick from the server, stat read directly from the game)
    your_atk_pt = your_def_pt = your_spo_pt = your_spd_pt = None
    if you:
        r_atk = you.get('rank_offense', 0)
        r_def = you.get('rank_defense', 0)
        if r_atk > 0 and you.get('atk', 0) > 0:
            your_atk_pt = (r_atk, you['atk'])
            atk_pts.append(your_atk_pt)
            print(f"     📌 ATK anchor: rank #{r_atk} = {you['atk']:,}  (your live stats)")
        if r_def > 0 and you.get('def', 0) > 0:
            your_def_pt = (r_def, you['def'])
            def_pts.append(your_def_pt)
            print(f"     📌 DEF anchor: rank #{r_def} = {you['def']:,}  (your live stats)")
        # Spy ranks from /stats page — only included when we actually scraped them
        r_spo = you.get('rank_spy_off', 0)
        r_spd = you.get('rank_spy_def', 0)
        if r_spo > 0 and you.get('spy_off', 0) > 0:
            your_spo_pt = (r_spo, you['spy_off'])
            spo_pts.append(your_spo_pt)
        if r_spd > 0 and you.get('spy_def', 0) > 0:
            your_spd_pt = (r_spd, you['spy_def'])
            spd_pts.append(your_spd_pt)

    MAX_K = 0.10   # cap: prevents absurdly steep curves while still allowing
                   # steep-but-real slopes from wide-span fits (e.g. ATK 0.09)

    def _set_model(pts, A, k, label, your_pt=None):
        """Cap k at MAX_K and re-anchor A.
        Preference order for the re-anchor point:
          1. YOUR own live (rank, stat) — always fresh and exact this tick
          2. Highest-VALUE point — the biggest floor we know, most current
        We never re-anchor to the lowest-RANK point because that point is
        often stale confirmed data from days ago."""
        k_c = min(k, MAX_K)
        if k > MAX_K and pts:
            if your_pt and your_pt[0] > 0 and your_pt[1] > 0 and your_pt[0] < 900:
                r0, v0 = your_pt
                src = "YOUR live"
            else:
                valid = [p for p in pts if 0 < p[0] < 900 and p[1] > 0]
                if not valid:
                    return A, k_c
                best = max(valid, key=lambda p: p[1])
                r0, v0 = best
                src = "highest-value"
            A = v0 / math.exp(-k_c * r0)
            print(f"     🔧 {label}: k capped {k:.4f}→{k_c:.4f}, "
                  f"A re-anchored to {src} rank {r0}={v0:,} → A={A:,.0f}")
        return A, k_c

    A, k = _seed_model(atk_pts, ATK_RANK_K, 'ATK model', your_point=your_atk_pt)
    if A: ATK_RANK_A, ATK_RANK_K = _set_model(atk_pts, A, k, 'ATK', your_pt=your_atk_pt)

    A, k = _seed_model(def_pts, DEF_RANK_K, 'DEF model', your_point=your_def_pt)
    if A: DEF_RANK_A, DEF_RANK_K = _set_model(def_pts, A, k, 'DEF', your_pt=your_def_pt)

    A, k = _seed_model(spo_pts, ATK_RANK_K, 'SpyOff model', your_point=your_spo_pt)
    if A: SPY_OFF_RANK_A, SPY_OFF_RANK_K = _set_model(spo_pts, A, k, 'SpyOff', your_pt=your_spo_pt)

    A, k = _seed_model(spd_pts, DEF_RANK_K, 'SpyDef model', your_point=your_spd_pt)
    if A: SPY_DEF_RANK_A, SPY_DEF_RANK_K = _set_model(spd_pts, A, k, 'SpyDef', your_pt=your_spd_pt)

# ── Helpers ────────────────────────────────────────────────────────────────────
def read_csv(path):
    if not os.path.isfile(path): return []
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f))

# NOTE: num() is defined at line ~141 (K/M/B suffix-aware). A duplicate
# definition used to live here and silently shadowed the full version,
# breaking gold parsing for any "1.5M"-style input — removed 2026-04-16.

# ── Load YOUR real confirmed stats ─────────────────────────────────────────────
def load_your_stats():
    """Read live stats from private_latest.json (written by scraper_private.py).
    Falls back to CSV reading if the JSON doesn't exist yet."""
    you = {
        'workers': 0, 'off_units': 0, 'def_units': 0, 'spy_units': 0, 'sent_units': 0,
        'atk': 0, 'def': 0, 'spy_off': 0, 'spy_def': 0,
        'income': 0, 'mine_lv': 0, 'population': 0, 'level': 1,
    }

    # ── Primary: live JSON snapshot (always correct, no column-shift risk) ────
    if os.path.isfile('private_latest.json'):
        with open('private_latest.json', encoding='utf-8') as f:
            live = json.load(f)
        you['level']      = live.get('level',      you['level'])
        # population = TOTAL (workers + military + idle citizens combined).
        # Never fall back to idle 'citizens' — that is far smaller and would
        # produce wildly wrong income / military estimates.
        you['population'] = live.get('population', 0) or live.get('citizens', 0)
        you['atk']        = live.get('atk',        you['atk'])
        you['def']        = live.get('def',        you['def'])
        you['spy_off']    = live.get('spy_off',    you['spy_off'])
        you['spy_def']    = live.get('spy_def',    you['spy_def'])
        you['income']     = live.get('income',     you['income'])
        you['workers']    = live.get('workers',    you['workers'])
        you['off_units']  = live.get('soldiers',   you['off_units'])
        you['def_units']  = live.get('guards',     you['def_units'])
        you['spy_units']  = live.get('spies',      you['spy_units'])
        you['sent_units'] = live.get('sentries',   you['sent_units'])
        you['mine_lv']      = live.get('mine_lv',      you['mine_lv'])
        # Server-wide rank badges (read from .rank-badge DOM elements by optimizer)
        you['rank_overall'] = live.get('rank_overall', 0)
        you['rank_offense'] = live.get('rank_offense', 0)
        you['rank_defense'] = live.get('rank_defense', 0)
        you['rank_wealth']  = live.get('rank_wealth',  0)
        # Buildings dict (if present)
        buildings = live.get('buildings', {})
        if buildings.get('Mine', 0) > 0:
            you['mine_lv'] = buildings['Mine']
        print(f"     📸 Loaded live snapshot: Lv={you['level']} "
              f"ATK={you['atk']:,} DEF={you['def']:,} "
              f"Workers={you['workers']:,} Income={you['income']:,}/tick")
        return you

    # ── Fallback: read from CSVs (may have stale/misaligned headers) ─────────
    print("     ⚠️  private_latest.json not found — reading from CSVs (run scraper first)")
    rows = read_csv('private_self_stats.csv')
    if rows:
        r = rows[-1]
        for key, col in [('atk','ATK'),('def','DEF'),('spy_off','SpyOffense'),
                         ('spy_def','SpyDefense'),('income','Income'),
                         ('level','Level'),('population','Population')]:
            v = num(r.get(col, 0))
            if v: you[key] = v
    unit_rows = read_csv('private_units.csv')
    if unit_rows:
        last_ts = unit_rows[-1]['Timestamp']
        for r in unit_rows:
            if r['Timestamp'] != last_ts: continue
            n = r['Unit'].strip().lower()
            if n == 'worker':  you['workers']   = num(r.get('Owned', 0))
            if n == 'soldier': you['off_units']  = num(r.get('Owned', 0))
            if n == 'guard':   you['def_units']  = num(r.get('Owned', 0))
            if n == 'spy':     you['spy_units']  = num(r.get('Owned', 0))
            if n == 'sentry':  you['sent_units'] = num(r.get('Owned', 0))
    bld_rows = read_csv('private_buildings.csv')
    if bld_rows:
        last_ts = bld_rows[-1]['Timestamp']
        for r in bld_rows:
            if r['Timestamp'] == last_ts and r.get('Building','').strip() == 'Mine':
                you['mine_lv'] = num(r.get('Level', 0))
    return you

# ── Load army leaderboard snapshot ────────────────────────────────────────────
def load_army_snapshot() -> dict:
    """Load private_army_snapshot.json written by scrape_army_leaderboards().
    Returns {player_name: {army_size, army_rank, units_trained, building_upgrades, ...}}"""
    if not os.path.isfile('private_army_snapshot.json'):
        return {}
    try:
        with open('private_army_snapshot.json', encoding='utf-8') as f:
            snap = json.load(f)
        data = snap.get('players', {})
        print(f"  🪖 Army snapshot loaded: {len(data)} players "
              f"(scraped {snap.get('timestamp','?')})")
        return data
    except Exception as ex:
        print(f"  ⚠️  Could not load army snapshot: {ex}")
        return {}

# ── Load scraped public profile data ──────────────────────────────────────────
def load_scraped_profiles():
    """Load the latest scraped row per player from private_player_profiles.csv.
    Returns Level, Race, Class, Population, Gold, FortHP, Rankings.
    ATK/DEF/Spy are NOT available here — those come from CONFIRMED_STATS only."""
    rows = read_csv('private_player_profiles.csv')
    latest = {}
    for r in rows:
        name = r.get('Player', '').strip()
        if name:
            latest[name] = r   # last row per player wins

    result = {}
    for name, r in latest.items():
        result[name] = {
            'level':         num(r.get('Level',        0)),
            'race':              r.get('Race',         '').strip(),
            'cls':               r.get('Class',        '').strip(),
            'clan':              r.get('Clan',         '').strip(),
            'gold':          num(r.get('GoldOnHand',   0)),
            'population':    num(r.get('Population',   0)),
            'fort_hp':       num(r.get('FortHP',       0)),
            'fort_max':      num(r.get('FortMax',      0)),
            'has_fort':      num(r.get('HasFort',      0)),
            'off_rank':      num(r.get('OffenseRank',  0)) or 999,
            'def_rank':      num(r.get('DefenseRank',  0)) or 999,
            'spy_off_rank':  num(r.get('SpyOffRank',   0)) or 999,
            'spy_def_rank':  num(r.get('SpyDefRank',   0)) or 999,
            'overall':       num(r.get('OverallRank',  0)) or 999,
            'total_players': num(r.get('TotalPlayers', 0)),
        }
    return result

# ── Confirmed real stats from profile screenshots ─────────────────────────────
# Add any player whose profile page you have seen directly.
# atk/def/spy_off/spy_def are EXACT values shown on their profile.
# citizens_idle = idle citizens shown on profile (NOT total population).
# gold / bank = snapshot at time of screenshot (not used for estimation, just stored).
#
# CONFIRMED_STATS_LIVE = CONFIRMED_STATS merged with intel harvested from
# battle/spy reports (private_intel.csv).  Populated by calibrate_models()
# once per tick and consulted by estimate() in place of the raw hardcoded
# dict so every fresh report immediately improves the per-player estimates.
CONFIRMED_STATS_LIVE: dict = {}

# Per-player growth rates — populated by calibrate_models() at the start
# of each estimator run by calling compute_growth_rates(load_player_growth()).
# estimate() reads this cache to project confirmed snapshots forward by
# (rate × ticks_since_verified_at). Without this, fresh-confirmed values
# (e.g. TGO 394k ATK recorded yesterday) stay frozen on the dashboard
# even as the player levels up every tick.
GROWTH_RATES_LIVE: dict = {}

# Derived (formula-based) growth rates — populated by calibrate_models()
# for every known player from their level / race / class / population via
# derive_growth_rate(). Used as a FALLBACK projection when we don't yet
# have 2+ observed data points to compute a real slope (and the GROWTH_RATES_LIVE
# entry is 0). Observed always wins when > 0; derived fills the gap.
DERIVED_GROWTH_RATES_LIVE: dict = {}

CONFIRMED_STATS = {
    # Verified directly from in-game profile screenshots.
    # atk/def/spy_off/spy_def are exact values AT THE TIME they were
    # captured — treat them as FLOORS for estimate() (players grow),
    # not as current-truth. The optional 'verified_at' field (ISO date
    # string) is used by calibrate_models() to age-filter anchors:
    # only recent-enough entries are fed to the rank fit, while the
    # numbers themselves remain available as per-player floors.
    #
    # NOTE: level/race/cls fields are CAPTURE-TIME CONTEXT ONLY — they
    # are NOT read by the optimizer at runtime.  Current demographics
    # come from profile scraping + rank snapshots every tick.  Do not
    # rely on these fields being accurate for long-term play.
    'Ashcipher': {
        'level': 19, 'race': 'Human', 'cls': 'Fighter',
        'atk': 76_408, 'def': 41_965, 'spy_off': 162,   'spy_def': 445,
        'gold': 980_667, 'bank': 1_720_000, 'citizens_idle': 332,
        # no verified_at → not used as a calibration anchor
    },
    'Mungus': {
        'level': 30, 'race': 'Undead', 'cls': 'Thief',
        'atk': 323_975, 'def': 96_973, 'spy_off': 210, 'spy_def': 910,
        'verified_at': '2026-04-15',
    },
    'Mettalica': {
        'level': 30, 'race': 'Goblin', 'cls': 'Cleric',
        'atk': 318_936, 'def': 56_352, 'spy_off': 49_219, 'spy_def': 24_525,
        'gold': 28_797, 'bank': 14_050_000, 'citizens_idle': 16,
        'verified_at': {
            'atk':     '2026-04-16 21:53',
            'def':     '2026-04-16 21:53',
            'spy_off': '2026-04-16 21:53',
            'spy_def': '2026-04-16 21:53',
        },
    },
    'JT': {
        'level': 15, 'race': 'Goblin', 'cls': 'Thief',
        'atk': 19_145, 'def': 26_000, 'spy_off': 210,   'spy_def': 28_000,
        'gold':    782, 'bank': 1_010_000, 'citizens_idle': 1_385,
        'verified_at': {
            'def':     '2026-04-16',   # fresh — user intel
            'spy_def': '2026-04-16',   # fresh — user intel
            # atk / spy_off unchanged since original capture; no verified_at
            # → not fed to calibration, but still act as per-player floors.
        },
    },
    'TGO Jasbob1989': {
        'level': 29, 'race': 'Undead', 'cls': 'Fighter',
        'atk': 545_000, 'def': 120_000,
        'verified_at': {
            'atk': '2026-04-16',   # fresh user confirmation
            'def': '2026-04-15',   # older — def value is a floor only
        },
    },
    # User-confirmed DEF values from Discord / in-game intel (2026-04-16).
    # Full spy report 2026-04-16 — all four combat stats + level bump.
    'Tycoon': {
        'level': 28, 'race': 'Goblin', 'cls': 'Cleric',
        'atk': 281_610, 'def': 388_584,
        'spy_off': 22_018, 'spy_def': 31_370,
        'verified_at': {
            'atk':     '2026-04-16',
            'def':     '2026-04-16',
            'spy_off': '2026-04-16',
            'spy_def': '2026-04-16',
        },
    },
    'aminmetz': {
        'level': 21, 'race': 'Elf', 'cls': 'Cleric',
        'def': 166_000,
        'verified_at': {'def': '2026-04-16'},
    },
    # Full spy report 2026-04-16 20:23 — def-focused Cleric, rank-7 DEF anchor.
    'Carrot': {
        'level': 29, 'race': 'Elf', 'cls': 'Cleric',
        'atk': 188_264, 'def': 359_970,
        'spy_off': 414, 'spy_def': 2_130,
        'gold': 100_856, 'bank': 0, 'citizens_idle': 44,
        'verified_at': {
            'atk':     '2026-04-16 20:23',
            'def':     '2026-04-16 20:23',
            'spy_off': '2026-04-16 20:23',
            'spy_def': '2026-04-16 20:23',
        },
    },
    # Spy report 2026-04-17 — mid-rank anchor (off 16 / def 42).  Fort
    # HP 1,000/1,000 = Fortification level 0/1 (minimal fort investment).
    'Barney': {
        'level': 29, 'race': 'Elf', 'cls': 'Cleric',
        'atk': 146_617, 'def': 80_723,
        'spy_off': 315, 'spy_def': 8_600,
        'verified_at': {
            'atk':     '2026-04-17',
            'def':     '2026-04-17',
            'spy_off': '2026-04-17',
            'spy_def': '2026-04-17',
        },
    },
    # Spy report 2026-04-17 — Undead Thief, balanced build (ATK 104k /
    # DEF 129k), strong spy for his rank.  Fort HP 3k/3k = Fort level 2.
    'Chill': {
        'level': 28, 'race': 'Undead', 'cls': 'Thief',
        'atk': 104_782, 'def': 129_736,
        'spy_off': 11_523, 'spy_def': 13_610,
        'verified_at': {
            'atk':     '2026-04-17',
            'def':     '2026-04-17',
            'spy_off': '2026-04-17',
            'spy_def': '2026-04-17',
        },
    },
    # DEF was re-confirmed 2026-04-15 via Discord screenshot from the
    # player (handle 'Ragnawar'): "Top def is mine: 530k". The other
    # combat stats are still from the April 8 profile screenshot — too
    # old to anchor the rank curves for atk/spy but still useful as
    # floors. Per-stat verified_at so only def is fed to calibration.
    'Radagon Of The Golden Order': {
        'level': 18, 'race': 'Goblin', 'cls': 'Cleric',
        'atk':  9_611, 'def': 530_000, 'spy_off': 4_226, 'spy_def': 3_760,
        'verified_at': {
            'def': '2026-04-15',   # fresh — Discord confirmation
            'atk':     '2026-04-08',
            'spy_off': '2026-04-08',
            'spy_def': '2026-04-08',
        },
    },
}

# ── Known players ─────────────────────────────────────────────────────────────
# Fallback demographic data only — level/pop are rough estimates.
# RANKS here (overall/off_rank/def_rank) are STALE defaults and are overridden
# every tick by scrape_rankings() which writes private_rankings_snapshot.json.
# Always re-scrape rankings after each game tick for accurate estimates.
# Format: (name, level, race, class, population, clan, overall_rank, off_rank, def_rank)
PLAYERS = [
    ('Ashcipher',                  19,'Human',  'Fighter', 2760,'RQUM', 99, 99, 99),
    ('TGO Jasbob1989',             29,'Undead', 'Fighter', 2757,'TGO',  99, 99, 99),
    ('Nerv',                       20,'Human',  'Fighter', 2754,'TGO',  99, 99, 99),
    ('Radagon Of The Golden Order',18,'Goblin', 'Cleric',  2600,'TGO',  99, 99, 99),
    ('sirclement_xxviii',          18,'Undead', 'Assassin',2647,'—',    99, 99, 99),
    ('Tycoon',                     28,'Goblin', 'Cleric',  2700,'RQUM', 99, 99, 99),
    ('TGO_Gaara',                  18,'—',      '—',       2500,'TGO',  99, 99, 99),
    ('Carrot',                     29,'Elf',    'Cleric',  2688,'RQUM', 99, 99, 99),
    ('NapoleonBorntoparty',        13,'Goblin', 'Thief',   2500,'TGO',  99, 99, 99),
    ('Hesiana',                    18,'—',      '—',       2500,'—',    99, 99, 99),
    ('Mungus',                     30,'Undead', 'Thief',   2556,'RQUM', 99, 99, 99),
    ('Mettalica',                  27,'Goblin', 'Cleric',  2500,'—',    99, 99, 99),
    ('flavio_2121',                13,'Undead', 'Fighter', 2500,'TGO',  99, 99, 99),
    ('CtrlAltDefeat',              13,'Elf',    'Fighter', 2400,'—',    99, 99, 99),
    ('Harley',                     19,'—',      '—',       2500,'TGO',  99, 99, 99),
    ('aminmetz',                   13,'Elf',    'Cleric',  2500,'—',    99, 99, 99),
    ('Liquidathor',                13,'Goblin', 'Thief',   2500,'HNTC', 99, 99, 99),
    ('Cobalt',                     13,'Elf',    'Cleric',  2500,'RQUM', 99, 99, 99),
    ('Dino',                       13,'—',      '—',       2500,'—',    99, 99, 99),
    ('Hellfire',                   13,'Goblin', 'Thief',   2500,'HNTC', 99, 99, 99),
    ('Don Gato',                   13,'—',      '—',       2500,'HNTC', 99, 99, 99),
    ('It was a fun time',          19,'—',      '—',       2500,'—',    99, 99, 99),
    ('Punching bag waiting for reset',19,'—',   '—',       2500,'TGO',  99, 99, 99),
    ('Chill',                      28,'Undead', 'Thief',   2500,'RQUM', 99, 99, 99),
    ('Barney',                     29,'Elf',    'Cleric',  2500,'RQUM', 99, 99, 99),
    ('Hes',                        13,'Elf',    'Cleric',  2500,'—',    99, 99, 99),
    ('The Defender',               15,'Goblin', 'Cleric',  2813,'HNTC', 99, 99, 99),
    ('Division Bell',              13,'Elf',    'Thief',   2500,'HNTC', 99, 99, 99),
    ('LordMike13',                 13,'Elf',    'Cleric',  2500,'HNTC', 99, 99, 99),
    ('Sorrowglow',                 17,'Undead', 'Fighter', 2245,'RQUM', 99, 99, 99),
    ('Hucksley_Nash',              14,'Elf',    'Thief',   2041,'—',    99, 99, 99),
    ('JT',                         15,'Goblin', 'Thief',   2500,'—',    99, 99, 99),
    ('TGO_Beginner (YOU)',         11,'Goblin', 'Cleric',  5091,'TGO',  99, 99, 99),
]

# ── Estimate one player ────────────────────────────────────────────────────────
def estimate(name, level, race, cls, pop, clan, overall, off_rank, def_rank, you, **kwargs):
    is_you = 'YOU' in name
    clean  = name.replace(' (YOU)', '')
    rb = RACE.get(race, RACE['Human'])
    cb = CLASS.get(cls,  CLASS['Fighter'])

    # ── Phase 3: rich-intel overrides ───────────────────────────────────────
    # When the caller supplies a fresh rich_intel snapshot (from Phase 1's
    # private_target_intel.json), compute EXACT values for income/mine/fort
    # and expose ATK/DEF reconstructions + building levels + fort damage.
    # Freshness gate matches calibrate_models (48h) — beyond that the state
    # has drifted so we only trust the rich_intel as a floor, not current-truth.
    rich_intel       = kwargs.get('rich_intel') or None
    rich_fresh       = bool(rich_intel) and _is_rich_intel_fresh(rich_intel)
    rich_income      = _exact_income_from_intel(rich_intel, race, cls) if rich_fresh else None
    rich_atk_rx      = _reconstruct_stat_from_intel(rich_intel, race, cls, 'atk') if rich_fresh else None
    rich_def_rx      = _reconstruct_stat_from_intel(rich_intel, race, cls, 'def') if rich_fresh else None
    rich_fort_damage = _fort_damage_pct(rich_intel)   # None if no fort fields

    def _apply_rich_overrides(result: dict) -> dict:
        """Final step before returning: replace approximations with exact
        values when rich intel is fresh, and publish additional fields
        (fort state, building levels, reconstructions) for consumers.
        Also stamps the Phase 4 conf_score on every return path so
        consumers (dashboard, battle targeting) can filter by confidence."""
        # Phase 4: confidence score — computed for EVERY return path,
        # not just the rich-intel one.  Inputs are determined from what
        # we know about this player when estimate() was called.
        _cs_entry = (CONFIRMED_STATS_LIVE or CONFIRMED_STATS).get(clean, {})
        has_any_cs = bool(_cs_entry)
        _obs_rates = (GROWTH_RATES_LIVE or {}).get(clean, {})
        has_growth = bool(_obs_rates) and any(
            (_obs_rates.get(k, 0) or 0) > 0
            for k in ("atk_per_tick", "def_per_tick", "spy_off_per_tick", "spy_def_per_tick")
        )
        result['conf_score'] = round(_confidence_score(
            name                 = clean,
            race                 = race,
            cls                  = cls,
            has_fresh_rich       = rich_fresh,
            has_stale_confirmed  = has_any_cs and not rich_fresh,
            has_observed_growth  = has_growth,
        ), 3)

        if not rich_fresh:
            return result
        bld = (rich_intel or {}).get('buildings', {}) or {}
        army_detail_here = (rich_intel or {}).get('army_detail', {}) or {}
        # Exact income always wins when we can compute it — more accurate
        # than est_mine_lv(level) + pop * 0.80 guess.
        if rich_income is not None:
            result['income'] = rich_income
        if 'mine' in bld:
            result['mine_lv'] = int(bld['mine'])
        # Building levels exposed for dashboard / growth model consumers
        for src, dst in (
            ('fortification',  'fortification_lv'),
            ('armory',         'armory_lv'),
            ('spy_academy',    'spy_academy_lv'),
            ('housing',        'housing_lv'),
            ('barracks',       'barracks_lv'),
            ('mercenary_camp', 'mercenary_camp_lv'),
        ):
            if src in bld:
                result[dst] = int(bld[src])
        # Fort state for attackers planning damage
        if 'fort_hp' in (rich_intel or {}):
            result['fort_hp']      = int(rich_intel['fort_hp']      or 0)
            result['fort_max']     = int(rich_intel.get('fort_max',   0) or 0)
            if rich_fort_damage is not None:
                result['fort_damage_pct'] = round(rich_fort_damage, 4)
        # ATK/DEF reconstruction for debugging + Phase-4 prediction.  We
        # DON'T replace result['atk'] / result['def'] with the reconstruction
        # here — the reported Total Offense / Total Defense (stored in CS
        # as 'atk'/'def' when captured) is the authoritative in-game value
        # and already reflects game-side bonuses we may not model perfectly.
        # The reconstruction is useful as a cross-check and as a basis for
        # "if they train N more knights next tick, ATK = ...".
        if rich_atk_rx is not None:
            result['atk_reconstructed'] = int(rich_atk_rx)
        if rich_def_rx is not None:
            result['def_reconstructed'] = int(rich_def_rx)
        # Exact army composition (per-unit stats) — useful for Phase 4
        # predictions ("next tick they buy 10 Knights → ATK +X").
        if army_detail_here:
            result['army_detail'] = army_detail_here
        return result

    # ── CONFIRMED: combat-stat anchors from profile screenshots + intel ─────
    # Level/race/cls come from the caller (profile scrape + rank snapshot —
    # always fresher than hardcoded CS entries, since players level every
    # few hours).  CS is used ONLY for atk/def/spy anchors; values may be
    # weeks old, so we compare against the rank-calibrated model and use
    # whichever is HIGHER (players only grow, never shrink).  Population
    # ceiling (see below) guards against impossibly large values.
    # CONFIRMED_STATS_LIVE is the merged dict (hardcoded + intel from reports);
    # fall back to raw CONFIRMED_STATS when calibrate_models hasn't run yet.
    _cs_source = CONFIRMED_STATS_LIVE or CONFIRMED_STATS
    if clean in _cs_source:
        c = _cs_source[clean]
        mine_lv = est_mine_lv(level)
        workers = int(pop * 0.80)
        income  = int((BASE_INC + workers * WORKER_GOLD) * mine_mult(mine_lv)
                      * (1 + rb.get('income', 0) + cb.get('income', 0)))
        # Rank-calibrated estimates (recalibrated this tick with YOUR live anchor)
        s_off  = kwargs.get('spy_off_rank', 999)
        s_def  = kwargs.get('spy_def_rank', 999)
        cal_atk = rank_atk(off_rank) if off_rank < 900 else 0
        cal_def = rank_def(def_rank) if def_rank < 900 else 0
        cal_so  = rank_spy_off(s_off) if s_off < 900 else 0
        cal_sd  = rank_spy_def(s_def) if s_def < 900 else 0
        # Stat selection: FRESH vs STALE confirmed values, with growth
        # projection applied to fresh values so the dashboard doesn't
        # show yesterday's snapshot frozen while the player keeps growing.
        #
        # FRESH (verified_at within CALIBRATION_FRESH_HOURS):
        #   Start from the confirmed value (ground truth at capture time)
        #   and project forward by the player's observed growth rate:
        #       projected = confirmed + rate_per_tick × ticks_since
        #   then compare against the rank-model estimate and pick the
        #   higher of the two (model may be even more accurate if rank
        #   just shifted). This keeps confirmed stats from going stale.
        #
        # STALE (old or no verified_at):
        #   Fall back to max(confirmed_floor, model) — the confirmed
        #   value is too old to project reliably, so treat it as a
        #   lower bound and let the model drive.
        _now_est       = datetime.datetime.now()
        _cal_cutoff    = _now_est - datetime.timedelta(hours=CALIBRATION_FRESH_HOURS)
        _rates         = GROWTH_RATES_LIVE.get(clean, {}) if GROWTH_RATES_LIVE else {}
        _derived_rates = DERIVED_GROWTH_RATES_LIVE.get(clean, {}) if DERIVED_GROWTH_RATES_LIVE else {}
        def _pick_stat(stat_key, confirmed_val, model_val):
            vat = c.get(f'{stat_key}_verified_at')
            is_fresh = (isinstance(vat, datetime.datetime)
                        and vat >= _cal_cutoff
                        and confirmed_val > 0)
            if is_fresh:
                # Project the confirmed value forward by growth rate.
                # OBSERVED wins when > 0 (real measured growth);
                # DERIVED is the fallback so players without enough
                # history still get a sensible projection.
                observed = _rates.get(f'{stat_key}_per_tick', 0) or 0
                derived  = _derived_rates.get(f'{stat_key}_per_tick', 0) or 0
                rate     = observed if observed > 0 else derived
                if rate > 0:
                    ticks = max(0.0, (_now_est - vat).total_seconds() / 1800.0)
                    projected = int(confirmed_val + rate * ticks)
                else:
                    projected = confirmed_val
                # Use the HIGHER of (projected confirmed) and (rank model)
                # — the model may reflect rank movement that growth rate
                # alone doesn't capture.
                return max(projected, model_val)
            # Stale — use floor + model
            return max(confirmed_val, model_val)
        atk_v = _pick_stat('atk',     c.get('atk',     0), cal_atk)
        def_v = _pick_stat('def',     c.get('def',     0), cal_def)
        spo_v = _pick_stat('spy_off', c.get('spy_off', 0), cal_so)
        spd_v = _pick_stat('spy_def', c.get('spy_def', 0), cal_sd)
        # Population ceiling: a player CANNOT have more stat than their ENTIRE
        # population fully equipped.  Caps unrealistic model outliers.
        gt = max_gear_tier(level);  ut = max_unit_tier(level)
        max_atk_pu = UNIT_OFF[ut] + WEAPON_STATS[gt] + ARMOR_STATS[gt]
        max_def_pu = UNIT_DEF[ut] + WEAPON_STATS[gt] + ARMOR_STATS[gt]
        atk_v = min(atk_v, int(pop * max_atk_pu * 1.10))
        def_v = min(def_v, int(pop * max_def_pu * 1.10))
        conf  = 'CONFIRMED'
        return _apply_rich_overrides({
            'pop':       pop,     'workers':   workers,
            'off_u':     '?',     'def_u':     '?',
            'spy_u':     '?',     'sent_u':    '?',
            'atk':       atk_v,   'def':       def_v,
            'spy_off':   spo_v,   'spy_def':   spd_v,
            'income':    income,  'mine_lv':   mine_lv,
            'gear_t':    gt,
            'unit_t':    FORT_LV_TO_UNIT_TIER[est_fort_lv(level)],
            'army_size': kwargs.get('army_size', 0),
            'upgrades':  max(0, kwargs.get('building_upgrades', -1)),
            'conf':      conf,
        })

    if is_you:
        return _apply_rich_overrides({
            'pop':       you['population'],
            'workers':   you['workers'],
            'off_u':     you['off_units'],
            'def_u':     you['def_units'],
            'spy_u':     you['spy_units'],
            'sent_u':    you.get('sent_units', 0),
            'atk':       you['atk'],
            'def':       you['def'],
            'spy_off':   you['spy_off'],
            'spy_def':   you['spy_def'],
            'income':    you['income'],
            'mine_lv':   you['mine_lv'],
            'gear_t':    max_gear_tier(level),
            'unit_t':    FORT_LV_TO_UNIT_TIER[est_fort_lv(level)],
            'army_size': kwargs.get('army_size', 0),
            'upgrades':  max(0, kwargs.get('building_upgrades', -1)),
            'conf':      'CONFIRMED',
        })

    # ── What we can derive precisely ─────────────────────────────────────────
    # 1. Max gear tier: gated by Armory level, which requires Level 10
    #    → Below Level 10: HARD LIMIT T3 regardless of anything else
    # 2. Max unit tier: gated by Fortification, also requires Level 10
    # 3. Mine level: Lv1 req Level 3, Lv2 req Level 12 + Fort Lv1 (confirmed)
    # 4. Workers: reverse from income — or estimate 80% of pop conservatively
    # 5. Military: ~15% of pop (conservative real-world bound)

    # ── Data from army leaderboards (kwargs injected by run()) ───────────────────
    army_size         = kwargs.get('army_size',         0)   # exact military count
    building_upgrades = kwargs.get('building_upgrades', -1)  # total upgrades bought; -1=unknown
    units_trained     = kwargs.get('units_trained',      0)  # cumulative units ever trained

    arm_lv  = est_armory_lv(level)
    fort_lv = est_fort_lv(level)
    spy_lv  = est_spy_ac_lv(level)

    gear_t  = max_gear_tier(level)              # min(armory_gate, fort_gate)
    unit_t  = FORT_LV_TO_UNIT_TIER[fort_lv]
    spy_gt  = SPY_AC_TO_SPY_TIER[spy_lv]
    spy_ut  = min(3, max(1, spy_lv))            # T1–T3 spy unit tier

    # ── Mine level: level-based estimate, refined by building_upgrades ────────
    # building_upgrades==0 → player has NO buildings at all → mine_lv=0
    # building_upgrades>0  → use level-based estimate (we don't know distribution)
    # building_upgrades==-1 → unknown, use level-based estimate unchanged
    if building_upgrades == 0:
        mine_lv = 0       # confirmed: zero upgrades bought, no mine
    else:
        mine_lv = est_mine_lv(level)

    # Workers = 80% of pop, military = army_size if known, else 15% of pop
    workers  = int(pop * 0.80)
    # Income: mine bonus + race/class income bonus (Thief +5% income)
    income   = int((BASE_INC + workers * WORKER_GOLD) * mine_mult(mine_lv)
                   * (1 + rb.get('income', 0) + cb.get('income', 0)))

    # ── Military count ────────────────────────────────────────────────────────
    # army_size from largest_army leaderboard is EXACT — use it directly.
    # Fallback: estimate 15% of population (our previous assumption).
    if army_size > 0:
        military = army_size
    else:
        military = int(pop * 0.15)

    # ── Combat split (soldiers vs guards) ─────────────────────────────────────
    # Spies + sentries = 8% of military; remainder is combat
    spy_pool = int(military * 0.08)
    combat   = military - spy_pool
    ow = 1.0 / off_rank
    dw = 1.0 / def_rank
    off_u = int(combat * ow / (ow + dw))
    def_u = combat - off_u

    # ── Spy pool split: spies (offense) vs sentries (defense) ─────────────────
    # Weight the spy/sentry split by the same off/def rank ratio.
    # A def_rank=1 player (e.g. Radagon) likely has more sentries than spies.
    spy_off_rank = kwargs.get('spy_off_rank', 999)
    spy_def_rank = kwargs.get('spy_def_rank', 999)
    sow = 1.0 / spy_off_rank
    sdw = 1.0 / spy_def_rank
    spy_u  = int(spy_pool * sow / (sow + sdw))  # spies (attack)
    sent_u = spy_pool - spy_u                    # sentries (defense)

    # ── Per-unit spy stats ─────────────────────────────────────────────────────
    spu_off  = UNIT_OFF[unit_t]          + WEAPON_STATS[gear_t] + ARMOR_STATS[gear_t]
    spu_def  = UNIT_DEF[unit_t]          + WEAPON_STATS[gear_t] + ARMOR_STATS[gear_t]
    spu_spy  = UNIT_SPY.get(spy_ut, 5)   + SPY_WEAPON.get(spy_gt, 12) + SPY_ARMOR.get(spy_gt, 12)
    spu_sent = UNIT_SENT.get(spy_ut, 5)  + SPY_WEAPON.get(spy_gt, 12) + SPY_ARMOR.get(spy_gt, 12)

    # ── Formula-based estimates ────────────────────────────────────────────────
    # 'spy' bonus (Assassin +5% Intel) applies to BOTH spy offense AND spy defense.
    # 'def' bonus (Elf/Goblin/Cleric) applies to main combat guards, NOT sentries.
    formula_atk     = int(off_u  * spu_off  * (1 + rb.get('atk',0) + cb.get('atk',0)))
    formula_def     = int(def_u  * spu_def  * (1 + rb.get('def',0) + cb.get('def',0)))
    formula_spy_off = int(spy_u  * spu_spy  * (1 + rb.get('spy',0) + cb.get('spy',0)))
    formula_spy_def = int(sent_u * spu_sent * (1 + rb.get('spy',0) + cb.get('spy',0)))

    # ── Rank-calibrated overrides (more accurate when rank is known) ──────────
    cal_atk     = rank_atk(off_rank)         if off_rank     < 900 else 0
    cal_def     = rank_def(def_rank)         if def_rank     < 900 else 0
    cal_spy_off = rank_spy_off(spy_off_rank) if spy_off_rank < 900 else 0
    cal_spy_def = rank_spy_def(spy_def_rank) if spy_def_rank < 900 else 0

    atk     = cal_atk     or formula_atk
    def_    = cal_def     or formula_def
    spy_off = cal_spy_off or formula_spy_off
    spy_def = cal_spy_def or formula_spy_def

    # ── Server rank-1 ceiling ──────────────────────────────────────────────────
    # No unconfirmed player can have more ATK/DEF than the server rank-1 player.
    # rank_atk(1) / rank_def(1) are anchored to confirmed rank-1 stats, so they
    # represent the true server maximum.  This prevents formula/model over-shoots
    # for players whose formula produces an implausibly high value.
    if ATK_RANK_A > 0:
        atk = min(atk, rank_atk(1))
    if DEF_RANK_A > 0:
        def_ = min(def_, rank_def(1))

    # ── Population ceiling ─────────────────────────────────────────────────────
    # A player CANNOT have more ATK/DEF than their ENTIRE population, fully
    # equipped with max-tier gear.  This catches model outliers where an estimated
    # stat would be impossible given the player's population and level.
    max_atk_pu  = UNIT_OFF[unit_t] + WEAPON_STATS[gear_t] + ARMOR_STATS[gear_t]
    max_def_pu  = UNIT_DEF[unit_t] + WEAPON_STATS[gear_t] + ARMOR_STATS[gear_t]
    pop_ceil_atk = int(pop * max_atk_pu * 1.10)   # 1.10 = best race+class bonus
    pop_ceil_def = int(pop * max_def_pu * 1.10)
    atk  = min(atk,  pop_ceil_atk)
    def_ = min(def_, pop_ceil_def)

    # ── Confidence label ───────────────────────────────────────────────────────
    # Priority: army size known > rank-calibrated > formula upper-bound
    has_army  = army_size > 0
    cal_combat = (off_rank < 900 and ATK_RANK_A > 0) or (def_rank < 900 and DEF_RANK_A > 0)
    cal_spy    = ((spy_off_rank < 900 and SPY_OFF_RANK_A > 0) or
                  (spy_def_rank < 900 and SPY_DEF_RANK_A > 0))
    if has_army and cal_combat and cal_spy: conf = 'ARMY+RNK'
    elif has_army and cal_combat:           conf = 'ARMY+CMB'
    elif has_army:                          conf = 'ARMY-SIZE'
    elif cal_combat and cal_spy:            conf = 'RANK-CAL'
    elif cal_combat:                        conf = 'RANK-CMB'
    elif cal_spy:                           conf = 'RANK-SPY'
    elif level >= 10:                       conf = 'UPPER BOUND'
    else:                                   conf = 'UB (T3 lv<10)'

    return _apply_rich_overrides({
        'pop':        pop,       'workers':   workers,
        'off_u':      off_u,     'def_u':     def_u,
        'spy_u':      spy_u,     'sent_u':    sent_u,
        'atk':        atk,       'def':       def_,
        'spy_off':    spy_off,   'spy_def':   spy_def,
        'income':     income,    'mine_lv':   mine_lv,
        'gear_t':     gear_t,    'unit_t':    unit_t,
        'army_size':  army_size, 'upgrades':  max(0, building_upgrades),
        'conf':       conf,
    })

# ── GitHub publish ─────────────────────────────────────────────────────────────
def publish_estimates(ts: str):
    """Copy the generated HTML to the estimates GitHub Pages repo and push it."""
    repo = ESTIMATES_REPO_DIR

    if not os.path.isdir(repo):
        print(f"  ⚠️  Estimates repo not found at {repo}")
        print(f"      Run: git clone https://github.com/cmdprive/darkthrone-estimates \"{repo}\"")
        return

    src  = HTML_OUTPUT          # private_player_estimates.html  (local)
    dest = os.path.join(repo, "index.html")

    if not os.path.isfile(src):
        print(f"  ⚠️  {src} not found — run write_html_report() first")
        return

    # Copy HTML → index.html in the repo
    import shutil
    shutil.copy2(src, dest)

    try:
        subprocess.run(["git", "-C", repo, "add", "index.html"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "--allow-empty",
                        "-m", f"Estimates update {ts}"], check=True)
        subprocess.run(["git", "-C", repo, "push"], check=True)
        print(f"  🚀 Estimates published → {ESTIMATES_SITE_URL}")
    except subprocess.CalledProcessError as e:
        print(f"  ⚠️  Git publish failed: {e}")


# ── HTML report ───────────────────────────────────────────────────────────────
HTML_OUTPUT = "private_player_estimates.html"

def write_html_report(results: list, ts: str):
    """Write a fully self-contained HTML intelligence dashboard from the estimate results."""
    import json as _json

    # Serialise results — replace non-serialisable '?' with null
    safe = []
    for r in results:
        row = {}
        for k, v in r.items():
            row[k] = None if v == '?' else v
        safe.append(row)

    data_js = _json.dumps(safe, ensure_ascii=False)

    # Read actual server ranks from private_latest.json (written by optimizer each tick)
    _server_atk_rank = 0
    _server_def_rank = 0
    _server_overall_rank = 0
    _server_wealth_rank = 0
    if os.path.isfile('private_latest.json'):
        try:
            with open('private_latest.json', encoding='utf-8') as _f:
                _live = _json.load(_f)
            _server_atk_rank     = _live.get('rank_offense', 0)
            _server_def_rank     = _live.get('rank_defense', 0)
            _server_overall_rank = _live.get('rank_overall', 0)
            _server_wealth_rank  = _live.get('rank_wealth',  0)
        except Exception:
            pass

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DarkThrone — Player Intelligence</title>
<style>
  :root {{
    --bg:      #0d0f14;
    --panel:   #161b24;
    --border:  #2a3142;
    --accent:  #c9a84c;
    --accent2: #8b6914;
    --text:    #cdd6f4;
    --dim:     #6c7086;
    --green:   #a6e3a1;
    --blue:    #89b4fa;
    --lblue:   #74c7ec;
    --orange:  #fab387;
    --red:     #f38ba8;
    --yellow:  #f9e2af;
    --you:     #1e2a1a;
    --you-border: #a6e3a1;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif;
          font-size: 13px; }}

  header {{ background: var(--panel); border-bottom: 2px solid var(--accent2);
            padding: 14px 20px; display: flex; align-items: center; gap: 16px;
            flex-wrap: wrap; }}
  header h1 {{ font-size: 18px; color: var(--accent); letter-spacing: 1px; flex: 1; }}
  header .ts  {{ color: var(--dim); font-size: 11px; }}

  .controls {{ background: var(--panel); border-bottom: 1px solid var(--border);
               padding: 10px 20px; display: flex; gap: 10px; flex-wrap: wrap;
               align-items: center; }}
  .tabs {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .tab {{ padding: 5px 14px; border: 1px solid var(--border); border-radius: 4px;
          cursor: pointer; background: transparent; color: var(--dim);
          font-size: 12px; transition: all .15s; }}
  .tab:hover {{ border-color: var(--accent); color: var(--accent); }}
  .tab.active {{ background: var(--accent2); border-color: var(--accent); color: #fff;
                 font-weight: 600; }}

  .search-wrap {{ margin-left: auto; }}
  .search-wrap input {{ background: var(--bg); border: 1px solid var(--border);
                        color: var(--text); border-radius: 4px; padding: 5px 10px;
                        font-size: 12px; width: 200px; }}
  .search-wrap input:focus {{ outline: none; border-color: var(--accent); }}

  .show-bots-wrap {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--dim); }}
  .show-bots-wrap input {{ accent-color: var(--accent); }}

  .table-wrap {{ overflow-x: auto; padding: 0 10px 20px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
  thead th {{ background: #1a1f2e; color: var(--accent); font-size: 11px; text-transform: uppercase;
              letter-spacing: .5px; padding: 8px 8px; border-bottom: 2px solid var(--accent2);
              white-space: nowrap; cursor: pointer; user-select: none; position: sticky; top: 0;
              z-index: 2; }}
  thead th:hover {{ color: #fff; }}
  thead th .sort-icon {{ opacity: .35; margin-left: 4px; }}
  thead th.sorted-asc  .sort-icon {{ opacity: 1; }}
  thead th.sorted-desc .sort-icon {{ opacity: 1; }}

  tbody tr {{ border-bottom: 1px solid var(--border); transition: background .1s; }}
  tbody tr:hover {{ background: #1e2333; }}
  tbody tr.you-row {{ background: var(--you) !important; outline: 1px solid var(--you-border);
                      outline-offset: -1px; }}
  tbody tr.you-row td {{ color: var(--green); font-weight: 600; }}
  td {{ padding: 6px 8px; white-space: nowrap; }}

  /* rank badge */
  .rank {{ display: inline-block; min-width: 26px; text-align: center;
           background: #1e2333; border-radius: 3px; padding: 1px 5px;
           font-size: 11px; color: var(--dim); }}
  .rank.r1 {{ background: #3d2e00; color: var(--accent); font-weight: 700; }}
  .rank.r2 {{ background: #2a2a2a; color: #ccc; }}
  .rank.r3 {{ background: #2a1a00; color: #c87533; }}
  .rank.top10 {{ color: var(--text); }}

  /* confidence badges */
  .conf {{ display: inline-block; border-radius: 3px; padding: 1px 7px;
           font-size: 10px; font-weight: 600; letter-spacing: .4px; }}
  .conf-CONFIRMED   {{ background: #1a3a1a; color: var(--green); border: 1px solid #2a5a2a; }}
  .conf-RANK-CAL    {{ background: #1a2a4a; color: var(--blue);  border: 1px solid #2a4a7a; }}
  .conf-RANK-ATK    {{ background: #162535; color: var(--lblue); border: 1px solid #2a4a6a; }}
  .conf-UPPER-BOUND {{ background: #3a2000; color: var(--orange);border: 1px solid #6a3a00; }}
  .conf-UB          {{ background: #3a2000; color: var(--orange);border: 1px solid #6a3a00; }}

  /* stat bars */
  .bar-cell {{ min-width: 120px; }}
  .bar-wrap {{ display: flex; align-items: center; gap: 6px; }}
  .bar {{ height: 8px; border-radius: 4px; flex-shrink: 0; min-width: 2px; }}
  .bar-atk  {{ background: linear-gradient(90deg,#f38ba8,#e05080); }}
  .bar-def  {{ background: linear-gradient(90deg,#89b4fa,#4a80d0); }}
  .bar-spyo {{ background: linear-gradient(90deg,#cba6f7,#9060d0); }}
  .bar-spyd {{ background: linear-gradient(90deg,#74c7ec,#4090b0); }}
  .bar-inc  {{ background: linear-gradient(90deg,#a6e3a1,#50a050); }}
  .bar-val  {{ font-size: 12px; font-weight: 600; }}

  /* clan badge */
  .clan {{ font-size: 10px; background: #2a2a3a; border-radius: 3px;
           padding: 1px 5px; color: var(--dim); }}

  /* race/class tags */
  .race-Human  {{ color: #f9e2af; }}
  .race-Goblin {{ color: var(--green); }}
  .race-Undead {{ color: #cba6f7; }}
  .race-Elf    {{ color: var(--lblue); }}

  /* summary cards */
  .cards {{ display: flex; gap: 12px; padding: 14px 20px; flex-wrap: wrap; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
           padding: 12px 16px; min-width: 160px; flex: 1; }}
  .card-title {{ font-size: 10px; text-transform: uppercase; letter-spacing: .8px;
                 color: var(--dim); margin-bottom: 6px; }}
  .card-val {{ font-size: 20px; font-weight: 700; }}
  .card-sub {{ font-size: 11px; color: var(--dim); margin-top: 2px; }}

  /* legend */
  .legend {{ display: flex; gap: 14px; padding: 6px 20px 10px;
             flex-wrap: wrap; font-size: 11px; color: var(--dim); }}
  .legend-item {{ display: flex; align-items: center; gap: 5px; }}

  /* no-data */
  .no-data {{ text-align: center; padding: 40px; color: var(--dim); }}
</style>
</head>
<body>

<header>
  <h1>⚔️ DarkThrone — Player Intelligence</h1>
  <span class="ts">Last updated: {ts}</span>
</header>

<div id="cards" class="cards"></div>

<div class="controls">
  <div class="tabs" id="tabs">
    <button class="tab active" data-sort="EstATK"     data-dir="-1">⚔️ ATK</button>
    <button class="tab"        data-sort="EstDEF"     data-dir="-1">🛡️ DEF</button>
    <button class="tab"        data-sort="EstSpyOff"  data-dir="-1">🗡️ Spy ATK</button>
    <button class="tab"        data-sort="EstSpyDef"  data-dir="-1">👁️ Spy DEF</button>
    <button class="tab"        data-sort="EstIncomeDay" data-dir="-1">💰 Income</button>
    <button class="tab"        data-sort="Population" data-dir="-1">👥 Population</button>
    <button class="tab"        data-sort="Level"      data-dir="-1">🏆 Level</button>
    <button class="tab"        data-sort="Player"     data-dir="1">🔤 Name</button>
  </div>
  <div class="show-bots-wrap">
    <input type="checkbox" id="showBots" checked>
    <label for="showBots">Show bots</label>
  </div>
  <div class="search-wrap">
    <input type="text" id="search" placeholder="🔍 Filter player / clan…">
  </div>
</div>

<div class="legend">
  <span style="color:var(--dim)">Confidence:</span>
  <span class="legend-item"><span class="conf conf-CONFIRMED">CONFIRMED</span> real stats from profile screenshot</span>
  <span class="legend-item"><span class="conf conf-RANK-CAL">RANK-CAL</span> calibrated from leaderboard ranks</span>
  <span class="legend-item"><span class="conf conf-RANK-ATK">RANK-ATK</span> ATK calibrated, DEF estimated</span>
  <span class="legend-item"><span class="conf conf-UPPER-BOUND">UPPER BOUND</span> formula estimate only</span>
</div>

<div class="table-wrap">
  <table id="tbl">
    <thead>
      <tr>
        <th data-col="__rank">#</th>
        <th data-col="Player">Player</th>
        <th data-col="Clan">Clan</th>
        <th data-col="Level">Lv</th>
        <th data-col="Race">Race</th>
        <th data-col="Class">Class</th>
        <th data-col="Population">Pop</th>
        <th data-col="GearTier">Gear</th>
        <th data-col="UnitTier">Unit</th>
        <th data-col="EstATK"    class="bar-cell sorted-desc">ATK <span class="sort-icon">▼</span></th>
        <th data-col="EstDEF"    class="bar-cell">DEF <span class="sort-icon">↕</span></th>
        <th data-col="EstSpyOff" class="bar-cell">Spy ATK <span class="sort-icon">↕</span></th>
        <th data-col="EstSpyDef" class="bar-cell">Spy DEF <span class="sort-icon">↕</span></th>
        <th data-col="EstIncomeDay">Income/day</th>
        <th data-col="MineLv">Mine</th>
        <th data-col="Confidence">Conf</th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
  <div class="no-data" id="noData" style="display:none">No players match the current filter.</div>
</div>

<script>
const RAW = {data_js};
// Actual server-wide ranks read from private_latest.json (set by optimizer each tick)
const SERVER_ATK_RANK     = {_server_atk_rank};
const SERVER_DEF_RANK     = {_server_def_rank};
const SERVER_OVERALL_RANK = {_server_overall_rank};
const SERVER_WEALTH_RANK  = {_server_wealth_rank};

// ── active sort state ────────────────────────────────────────────────────────
let sortCol = 'EstATK', sortDir = -1;   // -1 = desc, 1 = asc
let searchQ  = '';
let showBots = true;

// ── max values for bar scaling ───────────────────────────────────────────────
const maxATK  = Math.max(...RAW.map(r => r.EstATK  || 0));
const maxDEF  = Math.max(...RAW.map(r => r.EstDEF  || 0));
const maxSPYO = Math.max(...RAW.map(r => r.EstSpyOff || 0));
const maxSPYD = Math.max(...RAW.map(r => r.EstSpyDef || 0));
const maxINC  = Math.max(...RAW.map(r => r.EstIncomeDay || 0));

function fmt(n) {{
  if (n === null || n === undefined) return '?';
  if (n >= 1_000_000) return (n/1_000_000).toFixed(1) + 'M';
  if (n >= 1_000)     return (n/1_000).toFixed(1) + 'k';
  return n.toString();
}}
function fmtFull(n) {{
  if (n === null || n === undefined) return '?';
  return n.toLocaleString();
}}

function bar(val, max, cls) {{
  const pct = max > 0 ? Math.round((val||0) / max * 100) : 0;
  const w   = Math.max(pct * 1.2, val > 0 ? 4 : 0);   // max ~120px
  return `<div class="bar-wrap">
    <div class="bar ${{cls}}" style="width:${{w}}px"></div>
    <span class="bar-val">${{fmt(val)}}</span>
  </div>`;
}}

function confCls(c) {{
  if (!c) return '';
  if (c === 'CONFIRMED')   return 'conf-CONFIRMED';
  if (c.startsWith('RANK-CAL'))  return 'conf-RANK-CAL';
  if (c.startsWith('RANK-ATK'))  return 'conf-RANK-ATK';
  return 'conf-UPPER-BOUND';
}}

function confLabel(c) {{
  if (!c) return '?';
  if (c === 'CONFIRMED')  return 'CONFIRMED';
  if (c.startsWith('RANK-CAL'))  return 'RANK-CAL';
  if (c.startsWith('RANK-ATK'))  return 'RANK-ATK';
  if (c.startsWith('UB'))        return 'UB';
  return 'UPPER BOUND';
}}

function rankBadge(n) {{
  let cls = n <= 1 ? 'r1' : n <= 2 ? 'r2' : n <= 3 ? 'r3' : n <= 10 ? 'top10' : '';
  return `<span class="rank ${{cls}}">#${{n}}</span>`;
}}

function isBot(r) {{
  // Prefer the explicit IsBot column (written by estimator_run) — it's
  // authoritative. Fall back to name-pattern detection for older rows
  // that predate the column.
  if (r && (r.IsBot === 1 || r.IsBot === '1' || r.IsBot === true)) return true;
  return (r.Player || '').toLowerCase().includes('[bot]') ||
         (r.Player || '').toLowerCase().startsWith('bot');
}}

function getFiltered() {{
  return RAW.filter(r => {{
    if (!showBots && isBot(r)) return false;
    if (searchQ) {{
      const q = searchQ.toLowerCase();
      if (!(r.Player||'').toLowerCase().includes(q) &&
          !(r.Clan||'').toLowerCase().includes(q)   &&
          !(r.Race||'').toLowerCase().includes(q)   &&
          !(r.Class||'').toLowerCase().includes(q))
        return false;
    }}
    return true;
  }});
}}

function getSorted(rows) {{
  return [...rows].sort((a, b) => {{
    let va = a[sortCol], vb = b[sortCol];
    if (va === null || va === undefined) va = sortDir === -1 ? -Infinity : Infinity;
    if (vb === null || vb === undefined) vb = sortDir === -1 ? -Infinity : Infinity;
    if (typeof va === 'string') return sortDir * va.localeCompare(vb);
    return sortDir * (vb - va);
  }});
}}

function render() {{
  const rows   = getSorted(getFiltered());
  const tbody  = document.getElementById('tbody');
  const noData = document.getElementById('noData');
  tbody.innerHTML = '';

  if (rows.length === 0) {{
    noData.style.display = '';
    return;
  }}
  noData.style.display = 'none';

  rows.forEach((r, i) => {{
    const isYou = (r.Player||'').toLowerCase().includes('beginner');
    const tr = document.createElement('tr');
    if (isYou) tr.classList.add('you-row');

    const raceCls = 'race-' + (r.Race||'');
    const clanTag = r.Clan && r.Clan !== '—'
                    ? `<span class="clan">${{r.Clan}}</span>` : '';

    tr.innerHTML = `
      <td>${{rankBadge(i+1)}}</td>
      <td><strong>${{r.Player||''}}</strong> ${{isYou ? '← YOU' : ''}}</td>
      <td>${{clanTag}}</td>
      <td><strong>${{r.Level||'?'}}</strong></td>
      <td><span class="${{raceCls}}">${{r.Race||'—'}}</span></td>
      <td>${{r.Class||'—'}}</td>
      <td title="Workers: ${{fmtFull(r.EstWorkers)}}">${{fmtFull(r.Population)}}</td>
      <td>T${{r.GearTier||'?'}}</td>
      <td>T${{r.UnitTier||'?'}}</td>
      <td class="bar-cell">${{bar(r.EstATK,  maxATK,  'bar-atk')}}</td>
      <td class="bar-cell">${{bar(r.EstDEF,  maxDEF,  'bar-def')}}</td>
      <td class="bar-cell">${{bar(r.EstSpyOff,maxSPYO,'bar-spyo')}}</td>
      <td class="bar-cell">${{bar(r.EstSpyDef,maxSPYD,'bar-spyd')}}</td>
      <td class="bar-cell">${{bar(r.EstIncomeDay,maxINC,'bar-inc')}}</td>
      <td>${{r.MineLv||0}}</td>
      <td><span class="conf ${{confCls(r.Confidence)}}">${{confLabel(r.Confidence)}}</span></td>
    `;
    tbody.appendChild(tr);
  }});

  updateSortHeaders();
  renderCards(rows);
}}

function updateSortHeaders() {{
  document.querySelectorAll('thead th').forEach(th => {{
    th.classList.remove('sorted-asc','sorted-desc');
    const ico = th.querySelector('.sort-icon');
    if (ico) ico.textContent = '↕';
    if (th.dataset.col === sortCol) {{
      th.classList.add(sortDir === -1 ? 'sorted-desc' : 'sorted-asc');
      if (ico) ico.textContent = sortDir === -1 ? '▼' : '▲';
    }}
  }});
}}

function renderCards(rows) {{
  const you = RAW.find(r => (r.Player||'').toLowerCase().includes('beginner'));
  // Live in-game leaderboards EXCLUDE bots, so our rank cards must too —
  // otherwise rank #1 in the card disagrees with what the game shows.
  // Bots stay in the CSV + table (they're valid auto-battle targets) but
  // are filtered out of ranking math here.
  const nonBot    = RAW.filter(r => !isBot(r));
  const allSorted = (col) => [...nonBot].sort((a,b) => (b[col]||0)-(a[col]||0));

  // Use actual server-wide ranks if available, else fall back to rank within tracked non-bot players
  const atkRank = SERVER_ATK_RANK > 0 ? SERVER_ATK_RANK
                : (you ? allSorted('EstATK').findIndex(r=>r.Player===you.Player)+1 : '?');
  const defRank = SERVER_DEF_RANK > 0 ? SERVER_DEF_RANK
                : (you ? allSorted('EstDEF').findIndex(r=>r.Player===you.Player)+1 : '?');
  const top1atk = nonBot.reduce((a,b)=>(a.EstATK||0)>(b.EstATK||0)?a:b, nonBot[0] || RAW[0]);
  const top1def = nonBot.reduce((a,b)=>(a.EstDEF||0)>(b.EstDEF||0)?a:b, nonBot[0] || RAW[0]);

  document.getElementById('cards').innerHTML = `
    <div class="card">
      <div class="card-title">Total Players</div>
      <div class="card-val">${{RAW.length}}</div>
      <div class="card-sub">${{rows.length}} shown</div>
    </div>
    <div class="card">
      <div class="card-title">Your ATK Rank</div>
      <div class="card-val" style="color:var(--red)">#${{atkRank}}</div>
      <div class="card-sub">${{SERVER_ATK_RANK > 0 ? 'server-wide' : 'tracked players only'}} · ${{you ? fmtFull(you.EstATK) : '?'}}</div>
    </div>
    <div class="card">
      <div class="card-title">Your DEF Rank</div>
      <div class="card-val" style="color:var(--blue)">#${{defRank}}</div>
      <div class="card-sub">${{SERVER_DEF_RANK > 0 ? 'server-wide' : 'tracked players only'}} · ${{you ? fmtFull(you.EstDEF) : '?'}}</div>
    </div>
    <div class="card">
      <div class="card-title">Top ATK</div>
      <div class="card-val" style="color:var(--red)">${{fmtFull(top1atk.EstATK)}}</div>
      <div class="card-sub">${{top1atk.Player}}</div>
    </div>
    <div class="card">
      <div class="card-title">Top DEF</div>
      <div class="card-val" style="color:var(--blue)">${{fmtFull(top1def.EstDEF)}}</div>
      <div class="card-sub">${{top1def.Player}}</div>
    </div>
    <div class="card">
      <div class="card-title">Confirmed Players</div>
      <div class="card-val" style="color:var(--green)">${{RAW.filter(r=>r.Confidence==='CONFIRMED').length}}</div>
      <div class="card-sub">exact stats known</div>
    </div>
  `;
}}

// ── event: tab click ─────────────────────────────────────────────────────────
document.getElementById('tabs').addEventListener('click', e => {{
  const btn = e.target.closest('.tab');
  if (!btn) return;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  sortCol = btn.dataset.sort;
  sortDir = parseInt(btn.dataset.dir);
  render();
}});

// ── event: column header click ───────────────────────────────────────────────
document.querySelector('thead').addEventListener('click', e => {{
  const th = e.target.closest('th[data-col]');
  if (!th || th.dataset.col === '__rank') return;
  if (sortCol === th.dataset.col) {{
    sortDir *= -1;
  }} else {{
    sortCol = th.dataset.col;
    sortDir = typeof RAW[0]?.[sortCol] === 'string' ? 1 : -1;
  }}
  // sync active tab
  document.querySelectorAll('.tab').forEach(t => {{
    t.classList.toggle('active', t.dataset.sort === sortCol);
  }});
  render();
}});

// ── event: search ────────────────────────────────────────────────────────────
document.getElementById('search').addEventListener('input', e => {{
  searchQ = e.target.value.trim();
  render();
}});

// ── event: bots checkbox ─────────────────────────────────────────────────────
document.getElementById('showBots').addEventListener('change', e => {{
  showBots = e.target.checked;
  render();
}});

// ── initial render ───────────────────────────────────────────────────────────
render();
</script>
</body>
</html>"""

    _unhide(HTML_OUTPUT)
    with open(HTML_OUTPUT, 'w', encoding='utf-8') as f:
        f.write(html)


# ── Main ───────────────────────────────────────────────────────────────────────
def estimator_run():
    you      = load_your_stats()
    profiles = load_scraped_profiles()
    ts       = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    results  = []

    # Calibrate rank→stat models.  YOUR live rank+stats (from private_latest.json)
    # act as the primary anchor so the model is always consistent with the current
    # server state.  CONFIRMED_STATS values are used as secondary points but their
    # combat values may be stale; the model uses whichever is higher.
    calibrate_models(profiles, you)

    # Load rankings snapshot (written by scrape_rankings)
    rank_snap = {}
    if os.path.isfile('private_rankings_snapshot.json'):
        with open('private_rankings_snapshot.json', encoding='utf-8') as f:
            snap = json.load(f)
        rank_snap = snap.get('rank_map', {})
        total_players = snap.get('total_players', 248)
    else:
        total_players = 248

    # Load army leaderboard snapshot (written by scrape_army_leaderboards)
    army_snap = load_army_snapshot()
    # Phase 3: rich per-target intel from private_target_intel.json.  Each
    # entry carries exact building levels, army composition, armory inventory,
    # battle upgrades, and fort state — used by estimate() via the rich_intel
    # kwarg to publish precise values (income, mine_lv, fort_damage_pct) for
    # players we've spied within the freshness window.
    try:
        target_intel_snap = load_target_intel_snapshot()
    except Exception as _e:
        print(f"  ⚠️  target-intel snapshot load failed: {_e}")
        target_intel_snap = {}

    # ── Merge scraped profiles into PLAYERS list ──────────────────────────────
    # Any player found by the sequential profile scrape but NOT in PLAYERS gets
    # added with rank/level info from their profile page.
    known_names = {name.replace(' (YOU)', '') for name, *_ in PLAYERS}
    extra_players = []
    for pname, pd in profiles.items():
        if pname in known_names or 'YOU' in pname:
            continue
        if pd.get('level', 0) == 0:
            continue  # skip empty/failed profiles
        extra_players.append((
            pname,
            pd.get('level',    1),
            pd.get('race',     '—'),
            pd.get('cls',      '—'),
            pd.get('population', 0),
            pd.get('clan',     '—'),
            pd.get('overall',  999),
            pd.get('off_rank', 999),
            pd.get('def_rank', 999),
        ))
    if extra_players:
        print(f"  ℹ️  {len(extra_players)} extra players discovered from profile scan — added to estimates")

    # Also include players from the rankings snapshot who are NOT already in PLAYERS
    # or profile-scraped.  We know their server rank, so the rank-calibrated model
    # can produce meaningful ATK/DEF estimates for them — critical for the TOP
    # ATK/DEF ranking + YOUR-rank calculation to reflect the full server, not just
    # the ~30-entry PLAYERS list.
    #
    # The rankings snapshot doesn't include level (it's scraped from the
    # rankings pages, not individual profile pages).  When level is missing we
    # infer a placeholder from overall rank: rank 1 ≈ lv 30, rank 250 ≈ lv 5.
    # The rank model uses the player's ACTUAL rank for atk/def estimation, so
    # this placeholder level only affects income/population defaults (which
    # matter less at display time).
    snap_known = known_names | {p[0] for p in extra_players}
    rank_snap_extra = []
    for pname, rs in rank_snap.items():
        if pname in snap_known or 'YOU' in pname:
            continue
        if not (rs.get('off_rank') or rs.get('def_rank') or rs.get('overall')):
            continue   # skip players with no rank info at all
        lv = rs.get('level', 0) or 0
        if lv == 0:
            # Linear placeholder: rank 1 → lv 30; falls off linearly by total.
            overall = rs.get('overall', 999) or 999
            if 0 < overall < 900:
                lv = max(1, int(30 - overall * 25.0 / max(1, total_players)))
            else:
                lv = 10   # fallback — middle of the pack
        rank_snap_extra.append((
            pname,
            lv,
            rs.get('race',       '—'),
            rs.get('cls',        '—'),
            rs.get('population',   0) or 2500,   # placeholder for pop ceiling math
            rs.get('clan',       '—'),
            rs.get('overall',    999) or 999,
            rs.get('off_rank',   999) or 999,
            rs.get('def_rank',   999) or 999,
        ))
    if rank_snap_extra:
        print(f"  ℹ️  {len(rank_snap_extra)} additional players injected from rankings snapshot")
    extra_players.extend(rank_snap_extra)

    # Estimator runs silently — only top-10 summary is printed at the end
    # (the full per-player table was removed to keep the activity log clean).

    for name, level, race, cls, pop, clan, overall, off_rank, def_rank in PLAYERS + extra_players:
        clean = name.replace(' (YOU)', '')

        # Spy ranks — start unknown
        spy_off_rank = 999
        spy_def_rank = 999

        # Override with scraped public profile data (Level, Race, Class, Pop, all Ranks)
        if clean in profiles:
            sp = profiles[clean]
            if sp.get('level',         0) > 0:     level        = sp['level']
            if sp.get('race',          ''):         race         = sp['race']
            if sp.get('cls',           ''):         cls          = sp['cls']
            if sp.get('clan',          ''):         clan         = sp['clan']
            if sp.get('population',    0) > 0:     pop          = sp['population']
            if sp.get('off_rank',    999) < 999:   off_rank     = sp['off_rank']
            if sp.get('def_rank',    999) < 999:   def_rank     = sp['def_rank']
            if sp.get('spy_off_rank',999) < 999:   spy_off_rank = sp['spy_off_rank']
            if sp.get('spy_def_rank',999) < 999:   spy_def_rank = sp['spy_def_rank']
            if sp.get('overall',     999) < 999:   overall      = sp['overall']

        # Override with live rankings snapshot
        if clean in rank_snap:
            rs = rank_snap[clean]
            if rs.get('level',       0) > 0:   level        = rs['level']
            if rs.get('clan',        ''):       clan         = rs['clan']
            if rs.get('overall',     0) > 0:   overall      = rs['overall']
            if rs.get('off_rank',    0) > 0:   off_rank     = rs['off_rank']
            if rs.get('def_rank',    0) > 0:   def_rank     = rs['def_rank']
            if rs.get('spy_off_rank',0) > 0:   spy_off_rank = rs['spy_off_rank']
            if rs.get('spy_def_rank',0) > 0:   spy_def_rank = rs['spy_def_rank']

        # CONFIRMED_STATS is for combat-stat anchors only (atk/def/spy_*),
        # consumed by calibrate_models().  Demographics (level/race/cls) go
        # stale quickly — players level up every few hours — so we rely on
        # profile scrape + rank snapshot above, never on hardcoded values.

        # YOUR live optimizer data always wins last — most accurate source
        if 'YOU' in name:
            if you.get('level',      0) > 0: level    = you['level']
            if you.get('population', 0) > 0: pop      = you['population']

        # Army leaderboard data (exact military count, upgrade count, etc.)
        ad = army_snap.get(clean, {})

        e = estimate(name, level, race, cls, pop, clan, overall, off_rank, def_rank, you,
                     spy_off_rank     = spy_off_rank,
                     spy_def_rank     = spy_def_rank,
                     army_size        = ad.get('army_size',         0),
                     building_upgrades= ad.get('building_upgrades', -1),
                     units_trained    = ad.get('units_trained',      0),
                     rich_intel       = target_intel_snap.get(clean))
        # Per-player line stored for post-loop top-10 summary (not printed
        # individually — 300+ lines floods the activity log).
        tag = '← YOU' if 'YOU' in name else ''
        _print_line = (
            f'{clean:<26} Lv{level:>2} {e["atk"]:>9,} ATK  '
            f'{e["def"]:>9,} DEF  {e["conf"]:<10} {tag}'
        )
        results.append({
            'Timestamp': ts, 'Player': clean, 'Clan': clan,
            'Level': level, 'Race': race, 'Class': cls,
            'Population': e['pop'], 'EstWorkers': e['workers'],
            'ArmySize': e['army_size'],         # exact if from leaderboard, else 0
            'BuildingUpgrades': e['upgrades'],  # total upgrades; 0 if none, -1 if unknown→clamped to 0
            'EstOffUnits': e['off_u'], 'EstDefUnits': e['def_u'],
            'EstSpyUnits': e['spy_u'], 'EstSentryUnits': e['sent_u'],
            'GearTier': e['gear_t'], 'UnitTier': e['unit_t'],
            'EstATK': e['atk'], 'EstDEF': e['def'],
            'EstSpyOff': e['spy_off'], 'EstSpyDef': e['spy_def'],
            'EstIncomeTick': e['income'], 'EstIncomeDay': e['income'] * TICKS,
            'MineLv': e['mine_lv'], 'Confidence': e['conf'],
            # Phase 4: data-driven confidence [0,1] — higher = more trust.
            # Rolls in freshness of rich intel, known demographics, observed
            # growth rate, and historical projection-vs-actual error.
            'ConfScore': e.get('conf_score', 0.0),
            # Bots are still valid auto-battle targets, but the live
            # in-game leaderboards exclude them — so the dashboard's rank
            # cards need a way to filter them out of EstATK/EstDEF
            # ordering to match what the user sees in-game.
            'IsBot': 1 if '[bot]' in clean.lower() else 0,
        })

    # Save CSV — always overwrite so header never goes stale
    _unhide(OUTPUT)
    with open(OUTPUT, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    print(f'\n✅ Saved to {OUTPUT}')

    # Phase 4: append today's projections to private_projection_log.csv so
    # future intel captures can measure how accurate we were.  NOT part of
    # the growth CSV (different purpose + different retention window).
    try:
        record_projections(results)
    except Exception as _pe:
        print(f'  ⚠️  projection log append failed: {_pe}')

    # Save HTML dashboard
    write_html_report(results, ts)
    print(f'✅ Saved to private_player_estimates.html')

    # Publish to GitHub Pages
    publish_estimates(ts)

    # NOTE: we deliberately DON'T record estimator output to the growth CSV.
    # Writing projected model values back as "observations" creates a
    # feedback loop: projection → growth CSV → higher observed rate →
    # bigger projection next tick → explosion. Only spy-report intel
    # (via record_intel → record_player_growth with source="intel")
    # should contribute to observed growth rates. The derived-rate
    # fallback in calibrate_models covers players without intel history.

    # Summary — show top 10 ATK + DEF instead of flooding 300+ lines
    by_atk  = sorted(results, key=lambda x: -x['EstATK'])
    by_def  = sorted(results, key=lambda x: -x['EstDEF'])
    nonbot  = [r for r in results if '[bot]' not in r['Player'].lower()]
    you_r   = next(r for r in results if 'Beginner' in r['Player'])
    print(f'\n  📊 Estimated {len(results)} players ({len(nonbot)} non-bot)')
    print(f'  ── TOP 10 ATK ──')
    for i, r in enumerate(by_atk[:10], 1):
        tag = ' ← YOU' if 'Beginner' in r['Player'] else ''
        print(f'    {i:>2}. {r["Player"]:<26} ATK={r["EstATK"]:>9,}  DEF={r["EstDEF"]:>9,}  {r["Confidence"]:<10}{tag}')
    print(f'  ── TOP 10 DEF ──')
    for i, r in enumerate(by_def[:10], 1):
        tag = ' ← YOU' if 'Beginner' in r['Player'] else ''
        print(f'    {i:>2}. {r["Player"]:<26} DEF={r["EstDEF"]:>9,}  ATK={r["EstATK"]:>9,}  {r["Confidence"]:<10}{tag}')
    you_ar = sorted(results, key=lambda x:-x['EstATK']).index(you_r)+1
    you_dr = sorted(results, key=lambda x:-x['EstDEF']).index(you_r)+1
    # Use actual server ranks if available in private_latest.json
    _srv_atk = you.get('rank_offense', 0)
    _srv_def = you.get('rank_defense', 0)
    if _srv_atk > 0 and _srv_def > 0:
        print(f'\n  YOUR rank: ATK #{_srv_atk} (server)  DEF #{_srv_def} (server)  '
              f'[tracked: ATK #{you_ar}  DEF #{you_dr} of {len(results)}]')
    else:
        print(f'\n  YOUR rank: ATK #{you_ar}  DEF #{you_dr} of {len(results)} tracked')
    print(f'  ATK gap to #1: {by_atk[0]["EstATK"]:,} vs your {you_r["EstATK"]:,} '
          f'({by_atk[0]["EstATK"]//max(1,you_r["EstATK"])}×)')
    print(f'  DEF gap to #1: {by_def[0]["EstDEF"]:,} vs your {you_r["EstDEF"]:,} '
          f'({by_def[0]["EstDEF"]//max(1,you_r["EstDEF"])}×)')

    # Gear tier explanation
    your_lv = you.get('level', 2)
    # Level thresholds match est_armory_lv / est_fort_lv gates:
    #   Lv  2- 9 → Fort Lv0, Armory Lv0 → T1 unit + T3 gear
    #   Lv 10-19 → Fort Lv1, Armory Lv1 → T1 unit + T5 gear  (fort adds HP only, not new units)
    #   Lv 20-29 → Fort Lv2, Armory Lv1 → T2 unit + T5 gear  (T2 units CONFIRMED at Fort Lv2/Lv20)
    #   Lv 30-49 → Fort Lv3, Armory Lv2 → T3 unit + T7 gear  (Armory Lv2 CONFIRMED at Lv30)
    #   Lv 50-69 → Fort Lv5, Armory Lv3 → T4 unit + T9 gear  (estimated)
    #   Lv 70+   → Fort Lv5, Armory Lv4 → T4 unit + T10 gear (estimated)
    tier_rows = [(2,  'T1 unit + T3 gear'),
                 (10, 'T1 unit + T5 gear'),
                 (20, 'T2 unit + T5 gear'),
                 (30, 'T3 unit + T7 gear'),
                 (50, 'T4 unit + T9 gear'),
                 (70, 'T4 unit + T10 gear')]
    print(f'\n  GEAR TIER SUMMARY (stat per fully-geared unit):')
    for lv, label in tier_rows:
        you_tag = '  ← YOU' if lv <= your_lv < (tier_rows[tier_rows.index((lv,label))+1][0]
                                                  if tier_rows.index((lv,label)) < len(tier_rows)-1
                                                  else 999) else ''
        print(f'  Lv{lv:<3} ({label}): {stat_per_unit(lv):,}/unit{you_tag}')

    return results
