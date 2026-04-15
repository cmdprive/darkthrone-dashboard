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

# ── Strategy profiles ─────────────────────────────────────────────────────────
# weights  : relative share of idle citizens assigned to each unit type
#            (0 = never train this type)
# gear_pri : unit types sorted by gear-fill priority (first = buy gear first)
# bld_skip : building types to skip for this strategy
STRATEGIES = {
    "balanced": {
        "label":    "⚖️  Balanced",
        "desc":     "Even spread — workers, soldiers, guards, spies, sentries",
        "weights":  {"worker":1, "soldier":1, "guard":1, "spy":1, "sentry":1},
        "gear_pri": ["guard","soldier","spy","sentry"],
        "bld_skip": [],
    },
    "attack": {
        "label":    "⚔️  Attack",
        "desc":     "Heavy soldiers — max offense, light defense",
        "weights":  {"worker":2, "soldier":5, "guard":1, "spy":1, "sentry":0},
        "gear_pri": ["soldier","guard","spy","sentry"],
        "bld_skip": [],
    },
    "defense": {
        "label":    "🛡️  Defense",
        "desc":     "Heavy guards — max defense, light offense",
        "weights":  {"worker":2, "soldier":1, "guard":5, "spy":0, "sentry":1},
        "gear_pri": ["guard","soldier","sentry","spy"],
        "bld_skip": [],
    },
    "economy": {
        "label":    "💰  Economy",
        "desc":     "Max workers and income buildings, minimal army",
        "weights":  {"worker":7, "soldier":1, "guard":1, "spy":0, "sentry":0},
        "gear_pri": ["guard","soldier"],
        "bld_skip": ["spy","mercs"],
    },
    "spy": {
        "label":    "🗡️  Spy",
        "desc":     "Heavy spies and sentries, intelligence focused",
        "weights":  {"worker":2, "soldier":0, "guard":0, "spy":4, "sentry":4},
        "gear_pri": ["spy","sentry","soldier","guard"],
        "bld_skip": [],
    },
    "hybrid": {
        "label":    "⚔️🛡️  Hybrid",
        "desc":     "Soldiers + guards only, skip spy units",
        "weights":  {"worker":2, "soldier":3, "guard":3, "spy":0, "sentry":0},
        "gear_pri": ["soldier","guard","spy","sentry"],
        "bld_skip": ["spy"],
    },
}
DEFAULT_STRATEGY = "balanced"

def load_strategy():
    """Read chosen strategy from user_config.json, fall back to balanced."""
    cfg_file = "user_config.json"
    if os.path.isfile(cfg_file):
        try:
            with open(cfg_file, encoding="utf-8") as f:
                return json.load(f).get("strategy", DEFAULT_STRATEGY)
        except Exception:
            pass
    return DEFAULT_STRATEGY

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

    # Header bar CSS selectors — most accurate values
    for sel, key in [
        (".stat-item[title='Gold'] .stat-value",     "gold"),
        (".stat-item[title='Citizens'] .stat-value", "citizens"),
        (".stat-item[title='Turns'] .stat-value",    "turns"),
        (".stat-item[title='Level'] .stat-value",    "level"),
    ]:
        el = page.query_selector(sel)
        if el:
            v = num(el.inner_text())
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

    # ── 5. BUILDINGS — all building levels via DOM ────────────────────────────
    page.goto(f"{BASE_URL}/buildings")
    page.wait_for_load_state("networkidle", timeout=10000)
    bldg_js = page.evaluate("""() => {
        const result = {};
        document.querySelectorAll('input[name="building_type_id"]').forEach(inp => {
            const id  = parseInt(inp.value);
            // Walk up the DOM to find the card containing the status-value
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
    id_to_bname = {v: k for k, v in BUILDING_TYPE_ID.items()}
    s["buildings"] = {}
    for id_str, info in bldg_js.items():
        bname = id_to_bname.get(int(id_str))
        if bname:
            s["buildings"][bname] = info["level"]
    # Fallbacks from overview text
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


def decide(s, cats, strategy=None):
    """
    Returns ordered list of actions to take this tick.
    Core principle: NEVER hold gold back. Gear existing army first, then train.

    strategy : key from STRATEGIES dict (default: load from user_config.json)

    Priority order:
      1. Repair fort
      2. Fill gear gaps on ALL existing units (weapon + armor)
      3. Upgrade ALL existing units to max available gear tier
      4. Train new citizens (weighted by strategy) + immediately gear them
      5. Mine / income building upgrade
      6. Other buildings
      7. Battle upgrades
    """
    gold      = s["gold"]
    income    = s["income"]
    level     = s["level"]
    builds    = s["buildings"]
    citizens  = s["citizens"]
    actions   = []
    gold_left = gold

    # ── Load strategy ─────────────────────────────────────────────────────────
    strat_key = strategy or load_strategy()
    strat     = STRATEGIES.get(strat_key, STRATEGIES[DEFAULT_STRATEGY])
    print(f"  📋 Strategy: {strat['label']}  ({strat['desc']})")

    COMBAT_TYPES = {"soldier", "guard", "spy", "sentry"}

    # ── 1. Fort repair ────────────────────────────────────────────────────────
    fort_dmg = s.get("fort_max_hp", 100) - s.get("fort_hp", 100)
    if fort_dmg > 0:
        repair_cost = int(fort_dmg * s.get("cost_per_hp", 16.75)) + 1
        if gold_left >= repair_cost:
            actions.append({"type":"REPAIR_FORT","damage":fort_dmg,"cost":repair_cost,
                             "reason":f"fort at {s.get('fort_pct',100)}% HP"})
            gold_left -= repair_cost
        else:
            actions.append({"type":"SAVE_FOR_REPAIR","cost":repair_cost,
                             "reason":f"fort damaged {fort_dmg} HP, need {repair_cost:,}g"})

    # ── Gear sort helper ──────────────────────────────────────────────────────
    gear_pri = strat["gear_pri"]
    def _gear_sort(c):
        try:    pri = gear_pri.index(c["unit"])
        except: pri = 99
        return (pri, c["score"])

    # ── 2. Fill gear gaps on existing units ───────────────────────────────────
    for cat in sorted(cats, key=_gear_sort):
        unit  = cat["unit"]
        units = cat["units"]
        if units == 0: continue
        for slot, gap, tier in [
            ("weapon", cat["w_gap"], cat["w_tier"]),
            ("armor",  cat["a_gap"], cat["a_tier"]),
        ]:
            if gap <= 0 or tier not in GEAR.get((unit, slot), {}):
                continue
            name, _, cost = GEAR[(unit, slot)][tier]
            total = cost * gap
            if gold_left >= total:
                actions.append({"type":"BUY_GEAR","unit":unit,"slot":slot,
                                 "qty":gap,"name":name,"tier":tier,"cost":cost,
                                 "total":total,"tab":ARMORY_TAB[unit],
                                 "reason":f"{gap} existing {unit}s missing {slot}"})
                gold_left -= total
            elif gold_left >= cost:
                can = gold_left // cost
                actions.append({"type":"BUY_GEAR","unit":unit,"slot":slot,
                                 "qty":can,"name":name,"tier":tier,"cost":cost,
                                 "total":cost*can,"tab":ARMORY_TAB[unit],
                                 "reason":f"partial fill: {can}/{gap} {unit}s"})
                gold_left -= cost * can

    # ── 3. Upgrade existing units to max available gear tier ──────────────────
    for cat in sorted(cats, key=_gear_sort):
        unit  = cat["unit"]
        units = cat["units"]
        if units == 0: continue
        for slot in ("weapon", "armor"):
            cur_t    = cat["w_tier"] if slot == "weapon" else cat["a_tier"]
            slot_max = cat["w_max_t"] if slot == "weapon" else cat["a_max_t"]
            while cur_t < slot_max:
                next_t = cur_t + 1
                while next_t <= slot_max and next_t not in GEAR.get((unit, slot), {}):
                    next_t += 1
                if next_t > slot_max:
                    break
                old_cost  = GEAR[(unit, slot)].get(cur_t, (None, None, 0))[2]
                new_cost  = GEAR[(unit, slot)].get(next_t, (None, None, 0))[2]
                upg_per   = new_cost - old_cost
                if upg_per <= 0:
                    cur_t = next_t
                    continue
                new_name = GEAR[(unit, slot)][next_t][0]
                can   = min(units, gold_left // upg_per)
                if can > 0:
                    total = upg_per * can
                    actions.append({"type":"UPGRADE_GEAR","unit":unit,"slot":slot,
                                    "qty":can,"name":new_name,"tier":next_t,
                                    "cost":upg_per,"total":total,"tab":ARMORY_TAB[unit],
                                    "reason":f"T{cur_t}→T{next_t} for {can}/{units} {unit}s"})
                    gold_left -= total
                if can < units:
                    break
                cur_t = next_t

    # ── 4. Train new citizens (weighted by strategy) + immediately gear them ──
    if citizens > 0:
        weights   = strat["weights"]
        active    = [(u, w) for u, w in weights.items() if w > 0]
        total_w   = sum(w for _, w in active)

        allocs = {}
        leftover = citizens
        for unit, w in active:
            allocs[unit] = int(citizens * w / total_w)
            leftover -= allocs[unit]
        for unit, _ in sorted(active, key=lambda x: -x[1]):
            if leftover <= 0:
                break
            allocs[unit] += 1
            leftover -= 1

        remaining_citizens = citizens
        for unit, alloc in allocs.items():
            if remaining_citizens <= 0 or gold_left <= 0:
                break
            alloc = min(alloc, remaining_citizens)
            if alloc <= 0:
                continue

            if unit in COMBAT_TYPES:
                wc, ac, mt_w, mt_a = _gear_cost_for_unit(s, unit)
                cost_per = UNIT_COST[unit] + wc + ac
            else:
                cost_per = UNIT_COST[unit]
                mt_w = mt_a = 0

            if cost_per <= 0:
                continue

            count = min(alloc, gold_left // cost_per)
            if count <= 0:
                # Can't afford train+gear together — train bare, gear next tick
                count = min(alloc, gold_left // UNIT_COST[unit])

            if count <= 0:
                continue

            remaining_citizens -= count
            train_gold = UNIT_COST[unit] * count
            actions.append({"type":"TRAIN","unit":unit,"count":count,"cost":train_gold,
                             "reason":f"{strat_key} ({count}/{alloc} slot)",
                             **({"w_max_t":mt_w,"a_max_t":mt_a} if unit in COMBAT_TYPES else {})})
            gold_left -= train_gold

            # Immediately buy max-tier gear for newly trained combat units
            if unit in COMBAT_TYPES:
                for slot, mt, slot_cost in [("weapon", mt_w, wc), ("armor", mt_a, ac)]:
                    if mt >= 1 and slot_cost > 0:
                        total = slot_cost * count
                        if gold_left >= total:
                            gear_name = GEAR[(unit, slot)].get(mt, (f"T{mt}", 0, 0))[0]
                            actions.append({"type":"BUY_GEAR","unit":unit,"slot":slot,
                                            "qty":count,"name":gear_name,"tier":mt,
                                            "cost":slot_cost,"total":total,
                                            "tab":ARMORY_TAB[unit],
                                            "reason":f"gear for {count} newly trained {unit}s"})
                            gold_left -= total
                        elif gold_left >= slot_cost:
                            can = gold_left // slot_cost
                            gear_name = GEAR[(unit, slot)].get(mt, (f"T{mt}", 0, 0))[0]
                            actions.append({"type":"BUY_GEAR","unit":unit,"slot":slot,
                                            "qty":can,"name":gear_name,"tier":mt,
                                            "cost":slot_cost,"total":slot_cost*can,
                                            "tab":ARMORY_TAB[unit],
                                            "reason":f"partial gear: {can}/{count} {unit}s"})
                            gold_left -= slot_cost * can

    # ── 5. Mine / income building upgrade (after gear is handled) ────────────
    for bname, req_lv, base_cost, max_lv, btype in BUILDINGS:
        if btype != "income": continue
        cur_lv = builds.get(bname, 0)
        if cur_lv >= max_lv or level < req_lv: continue
        prereq = BUILDING_PREREQ.get((bname, cur_lv + 1), {})
        if any(builds.get(b, 0) < req for b, req in prereq.items()):
            continue
        cost = base_cost * (cur_lv + 1)
        if gold_left >= cost:
            actions.append({"type":"BUILD","name":bname,"cost":cost,"lv":cur_lv+1,
                             "reason":f"+{income*0.10:.0f}/tick"})
            gold_left -= cost
        break

    # ── 6. Other buildings ────────────────────────────────────────────────────
    bld_skip = strat.get("bld_skip", [])
    for bname, req_lv, base_cost, max_lv, btype in BUILDINGS:
        if btype == "income": continue
        if btype in bld_skip: continue
        cur_lv = builds.get(bname, 0)
        if cur_lv >= max_lv or level < req_lv: continue
        prereq = BUILDING_PREREQ.get((bname, cur_lv + 1), {})
        if any(builds.get(b, 0) < req for b, req in prereq.items()):
            continue
        cost = base_cost * (cur_lv + 1)
        if gold_left >= cost:
            actions.append({"type":"BUILD","name":bname,"cost":cost,"lv":cur_lv+1,
                             "reason":f"Lv{cur_lv+1}"})
            gold_left -= cost
        break

    # ── 7. Battle upgrades when all gear fully maxed ──────────────────────────
    all_maxed = all(c["fully_maxed"] or c["units"] == 0 for c in cats)
    if all_maxed:
        for upg_name, upg_info in s.get("upgrades_buyable", {}).items():
            cost = upg_info.get("cost", 0) if isinstance(upg_info, dict) else upg_info
            if cost and gold_left >= cost:
                actions.append({"type":"BUY_UPGRADE","name":upg_name,"qty":1,
                                 "cost":cost,"total":cost,
                                 "reason":"all gear maxed — buying battle upgrade"})
                gold_left -= cost

    return actions, gold_left

# ── EXECUTE ACTIONS ────────────────────────────────────────────────────────────
def execute(page, actions, gold):
    for a in actions:
        t = a["type"]
        if t in ("SAVE_FOR_BUILD","SAVE_FOR_GEAR","SAVE_FOR_UPGRADE","SAVE_FOR_REPAIR"): continue

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
            try: data = json.load(f)
            except: data = []
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

        # Decide
        actions, gold_after = decide(s, cats)
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

        # Summary
        executed = [a for a in actions if not a["type"].startswith("SAVE_")]
        waiting  = [a for a in actions if a["type"].startswith("SAVE_")]
        if executed: print(f"  ✅ Executed: {len(executed)} action(s)")
        if waiting:
            w = waiting[0]
            print(f"  💾 Saving: {w.get('reason','')}")

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
    Scrape global leaderboards directly from the detail pages:
      /leaderboards/global/overall   → overall rank for every player
      /leaderboards/global/offense   → offense (ATK) rank
      /leaderboards/global/defense   → defense (DEF) rank

    Paginates through all pages (~50 rows each) until empty.
    Saves rank_map to private_rankings_snapshot.json for the estimator.
    """
    print("  🏆 Scraping global rankings...")

    # JS that extracts all .ranking-row entries from the current page
    EXTRACT_JS = r"""() => {
        const rows = [];
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


def _seed_model(points: list, base_k: float, label: str):
    """Fit or seed a model from a list of (rank, value) confirmed points.
    Returns (A, k). Uses two points for a full fit, one point for a seed."""
    points = sorted(p for p in points if p[0] > 0 and p[1] > 0)
    if len(points) >= 2:
        A, k = _fit_exponential(points[0][0], points[0][1],
                                  points[1][0], points[1][1])
        if A > 0 and k > 0:
            print(f"     📐 {label} calibrated:  A={A:,.0f} k={k:.4f} "
                  f"(pts: {points[0]}, {points[1]})")
            return A, k
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

    atk_pts, def_pts, spo_pts, spd_pts = [], [], [], []

    # Collect points from CONFIRMED_STATS (may be stale)
    for cname, cs in CONFIRMED_STATS.items():
        p = profiles.get(cname, {})
        ar  = p.get('off_rank',     0)
        dr  = p.get('def_rank',     0)
        sor = p.get('spy_off_rank', 0)
        sdr = p.get('spy_def_rank', 0)
        if ar  > 0 and cs.get('atk',     0): atk_pts.append((ar,  cs['atk']))
        if dr  > 0 and cs.get('def',     0): def_pts.append((dr,  cs['def']))
        if sor > 0 and cs.get('spy_off', 0): spo_pts.append((sor, cs['spy_off']))
        if sdr > 0 and cs.get('spy_def', 0): spd_pts.append((sdr, cs['spy_def']))

    # Add YOUR live stats as the most-reliable anchor point
    # (rank scraped this tick from the server, stat read directly from the game)
    if you:
        r_atk = you.get('rank_offense', 0)
        r_def = you.get('rank_defense', 0)
        if r_atk > 0 and you.get('atk', 0) > 0:
            atk_pts.append((r_atk, you['atk']))
            print(f"     📌 ATK anchor: rank #{r_atk} = {you['atk']:,}  (your live stats)")
        if r_def > 0 and you.get('def', 0) > 0:
            def_pts.append((r_def, you['def']))
            print(f"     📌 DEF anchor: rank #{r_def} = {you['def']:,}  (your live stats)")

    MAX_K = 0.05   # cap: prevents rank-1 estimates from being absurdly large

    A, k = _seed_model(atk_pts, ATK_RANK_K, 'ATK model')
    if A: ATK_RANK_A, ATK_RANK_K = A, min(k, MAX_K)

    A, k = _seed_model(def_pts, DEF_RANK_K, 'DEF model')
    if A: DEF_RANK_A, DEF_RANK_K = A, min(k, MAX_K)

    A, k = _seed_model(spo_pts, ATK_RANK_K, 'SpyOff model')
    if A: SPY_OFF_RANK_A, SPY_OFF_RANK_K = A, min(k, MAX_K)

    A, k = _seed_model(spd_pts, DEF_RANK_K, 'SpyDef model')
    if A: SPY_DEF_RANK_A, SPY_DEF_RANK_K = A, min(k, MAX_K)

# ── Helpers ────────────────────────────────────────────────────────────────────
def read_csv(path):
    if not os.path.isfile(path): return []
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f))

def num(s): return int(re.sub(r'\D','',str(s or '')) or 0)

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
CONFIRMED_STATS = {
    # Verified directly from in-game profile screenshots.
    # atk/def/spy_off/spy_def are exact values — no estimation needed for these players.
    # citizens_idle = idle citizens shown on their profile (not total population).
    'Ashcipher': {
        'level': 19, 'race': 'Human', 'cls': 'Fighter',
        'atk': 76_408, 'def': 41_965, 'spy_off': 162,   'spy_def': 445,
        'gold': 980_667, 'bank': 1_720_000, 'citizens_idle': 332,
    },
    # verified from in-game profile screenshot 2026-04-15
    'Mungus': {
        'level': 30, 'race': 'Undead', 'cls': 'Thief',
        'atk': 323_975, 'def': 96_973, 'spy_off': 210, 'spy_def': 910,
    },
    # verified from in-game profile screenshot 2026-04-15
    'Mettalica': {
        'level': 27, 'race': 'Goblin', 'cls': 'Cleric',
        'atk': 239_492, 'def': 38_442, 'spy_off': 26_130, 'spy_def': 22_880,
        'gold': 28_797, 'bank': 14_050_000, 'citizens_idle': 16,
    },
    'JT': {
        'level': 15, 'race': 'Goblin', 'cls': 'Thief',
        'atk': 19_145, 'def': 14_423, 'spy_off': 210,   'spy_def':  90,
        'gold':    782, 'bank': 1_010_000, 'citizens_idle': 1_385,
    },
    # verified from dashboard row 2026-04-15 (Fort HP 3000/3000 = Fort Lv2)
    'TGO Jasbob1989': {
        'level': 29, 'race': 'Undead', 'cls': 'Fighter',
        'atk': 394_000, 'def': 120_000,
    },
    # def_rank=1 on server — verified from profile screenshot 2026-04-08
    'Radagon Of The Golden Order': {
        'level': 18, 'race': 'Goblin', 'cls': 'Cleric',
        'atk':  9_611, 'def': 77_457, 'spy_off': 4_226, 'spy_def': 3_760,
    },
}

# ── Known players ─────────────────────────────────────────────────────────────
# Ranks from Global Rankings leaderboard screenshot (2026-04-08).
# Levels from Level leaderboard tab. Population from profile scrapes.
# Ranks updated: overall=#1-10 visible, offense #1-10, defense #1-10, level #1-10
# name, level, race, class, population, clan, overall, off_rank, def_rank
# NOTE: level/pop/ranks here are fallback defaults only.
# scrape_rankings() refreshes ranks every tick from the live leaderboard.
# private_latest.json always overrides YOUR stats.
# Format: (name, level, race, class, population, clan, overall_rank, off_rank, def_rank)
PLAYERS = [
    ('Ashcipher',                  19,'Human',  'Fighter', 2760,'RQUM', 99, 99, 99),
    ('TGO Jasbob1989',             29,'Undead', 'Fighter', 2757,'TGO',  99, 99, 99),
    ('Nerv',                       20,'Human',  'Fighter', 2754,'TGO',  99, 99, 99),
    ('Radagon Of The Golden Order',18,'Goblin', 'Cleric',  2600,'TGO',  99, 99, 99),
    ('sirclement_xxviii',          18,'Undead', 'Assassin',2647,'—',    99, 99, 99),
    ('Tycoon',                     15,'Goblin', 'Cleric',  2700,'RQUM', 99, 99, 99),
    ('TGO_Gaara',                  18,'—',      '—',       2500,'TGO',  99, 99, 99),
    ('Carrot',                     21,'Elf',    'Cleric',  2688,'—',    99, 99, 99),
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
    ('Chill',                      19,'—',      '—',       2500,'—',    99, 99, 99),
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

    # ── CONFIRMED: demographic data from profile screenshots ─────────────────
    # Level/race/cls are stable — use them.  Combat stats (atk/def/spy) may be
    # weeks old; compare them to the rank-calibrated model and use whichever is
    # HIGHER (players only get stronger over time, never weaker).
    # Population ceiling (see below) guards against impossibly large values.
    if clean in CONFIRMED_STATS:
        c = CONFIRMED_STATS[clean]
        # Stable demographic overrides
        level = c.get('level', level)
        race  = c.get('race',  race)
        cls   = c.get('cls',   cls)
        rb = RACE.get(race, RACE['Human'])
        cb = CLASS.get(cls,  CLASS['Fighter'])
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
        # Confirmed stats are authoritative — always use them when present.
        # Model fills in ONLY if the confirmed field is absent/zero.
        # (max() was tried here but caused the model to override fresh confirmed
        # data, e.g. showing 527k ATK when the real value is 394k.)
        # Stale confirmed data (e.g. Radagon April-8 DEF) is acceptable because
        # rank_snap now estimates all 259 server players, so even if a confirmed
        # player's value is stale the server-rank ordering is still correct.
        atk_v = c.get('atk',     0) or cal_atk
        def_v = c.get('def',     0) or cal_def
        spo_v = c.get('spy_off', 0) or cal_so
        spd_v = c.get('spy_def', 0) or cal_sd
        # Population ceiling: a player CANNOT have more stat than their ENTIRE
        # population fully equipped.  Caps unrealistic model outliers.
        gt = max_gear_tier(level);  ut = max_unit_tier(level)
        max_atk_pu = UNIT_OFF[ut] + WEAPON_STATS[gt] + ARMOR_STATS[gt]
        max_def_pu = UNIT_DEF[ut] + WEAPON_STATS[gt] + ARMOR_STATS[gt]
        atk_v = min(atk_v, int(pop * max_atk_pu * 1.10))
        def_v = min(def_v, int(pop * max_def_pu * 1.10))
        conf  = 'CONFIRMED'
        return {
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
        }

    if is_you:
        return {
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
        }

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

    return {
        'pop':        pop,       'workers':   workers,
        'off_u':      off_u,     'def_u':     def_u,
        'spy_u':      spy_u,     'sent_u':    sent_u,
        'atk':        atk,       'def':       def_,
        'spy_off':    spy_off,   'spy_def':   spy_def,
        'income':     income,    'mine_lv':   mine_lv,
        'gear_t':     gear_t,    'unit_t':    unit_t,
        'army_size':  army_size, 'upgrades':  max(0, building_upgrades),
        'conf':       conf,
    }

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
  const allSorted = (col) => [...RAW].sort((a,b) => (b[col]||0)-(a[col]||0));

  // Use actual server-wide ranks if available, else fall back to rank within tracked players
  const atkRank = SERVER_ATK_RANK > 0 ? SERVER_ATK_RANK
                : (you ? allSorted('EstATK').findIndex(r=>r.Player===you.Player)+1 : '?');
  const defRank = SERVER_DEF_RANK > 0 ? SERVER_DEF_RANK
                : (you ? allSorted('EstDEF').findIndex(r=>r.Player===you.Player)+1 : '?');
  const top1atk = RAW.reduce((a,b)=>(a.EstATK||0)>(b.EstATK||0)?a:b, RAW[0]);
  const top1def = RAW.reduce((a,b)=>(a.EstDEF||0)>(b.EstDEF||0)?a:b, RAW[0]);

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
    # or profile-scraped.  We know their server rank + level (often), so the model
    # can produce meaningful ATK/DEF estimates for them — critical for the TOP
    # ATK/DEF ranking to reflect the full server, not just the ~30-entry PLAYERS list.
    snap_known = known_names | {p[0] for p in extra_players}
    rank_snap_extra = []
    for pname, rs in rank_snap.items():
        if pname in snap_known or 'YOU' in pname:
            continue
        lv = rs.get('level', 0)
        if lv == 0:
            continue
        rank_snap_extra.append((
            pname,
            lv,
            rs.get('race',       '—'),
            rs.get('cls',        '—'),
            rs.get('population',   0),
            rs.get('clan',       '—'),
            rs.get('overall',    999),
            rs.get('off_rank',   999),
            rs.get('def_rank',   999),
        ))
    if rank_snap_extra:
        print(f"  ℹ️  {len(rank_snap_extra)} additional players injected from rankings snapshot")
    extra_players.extend(rank_snap_extra)

    print(f'\n{"="*150}')
    print(f'  PLAYER ESTIMATES — {ts}  (gear tiers from exact armory data)')
    print(f'{"="*150}')
    hdr = (f'{"Player":<26} {"Clan":<5} {"Lv":>3} {"Race":<8} {"Class":<10} '
           f'{"Pop":>5} {"Army":>6} {"Upg":>4} {"Off":>5} {"Def":>5} {"Spy":>5} '
           f'{"GearT":>6} {"UnitT":>6} '
           f'{"ATK":>9} {"DEF":>9} {"SpyOff":>8} {"SpyDef":>8} '
           f'{"Inc/tick":>10} {"Inc/day":>9} {"Conf":<10}')
    print(hdr)
    print('-' * 150)

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

        # CONFIRMED_STATS always override level/race/cls
        if clean in CONFIRMED_STATS:
            c = CONFIRMED_STATS[clean]
            if c.get('level', 0) > 0: level = c['level']
            if c.get('race',  ''):    race  = c['race']
            if c.get('cls',   ''):    cls   = c['cls']

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
                     units_trained    = ad.get('units_trained',      0))
        tag = '← YOU' if 'YOU' in name else ''
        fmt_u  = lambda v: f'{v:>5,}' if isinstance(v, int) else f'{"?":>5}'
        fmt_a  = lambda v: f'{v:>6,}' if v > 0 else f'{"—":>6}'
        fmt_up = lambda v: f'{v:>4}' if v >= 0 else f'{"?":>4}'
        disp_lv = CONFIRMED_STATS[clean]['level'] if clean in CONFIRMED_STATS else level
        print(
            f'{name:<26} {clan:<5} {disp_lv:>3} {race:<8} {cls:<10} '
            f'{e["pop"]:>5,} {fmt_a(e["army_size"])} {fmt_up(e["upgrades"])} '
            f'{fmt_u(e["off_u"])} {fmt_u(e["def_u"])} '
            f'{fmt_u(e["spy_u"])} {fmt_u(e["sent_u"])} '
            f'{e["gear_t"]:>6} {e["unit_t"]:>6} '
            f'{e["atk"]:>9,} {e["def"]:>9,} {e["spy_off"]:>8,} {e["spy_def"]:>8,} '
            f'{e["income"]:>10,} {e["income"]*TICKS/1e6:>8.1f}M  {e["conf"]:<10} {tag}'
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
        })

    # Save CSV — always overwrite so header never goes stale
    _unhide(OUTPUT)
    with open(OUTPUT, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    print(f'\n✅ Saved to {OUTPUT}')

    # Save HTML dashboard
    write_html_report(results, ts)
    print(f'✅ Saved to private_player_estimates.html')

    # Publish to GitHub Pages
    publish_estimates(ts)

    # Summary
    by_atk  = sorted(results, key=lambda x: -x['EstATK'])
    by_def  = sorted(results, key=lambda x: -x['EstDEF'])
    you_r   = next(r for r in results if 'Beginner' in r['Player'])
    print(f'\n  TOP ATK: ' + '  '.join(f'{r["Player"].split()[0]}={r["EstATK"]:,}' for r in by_atk[:5]))
    print(f'  TOP DEF: ' + '  '.join(f'{r["Player"].split()[0]}={r["EstDEF"]:,}' for r in by_def[:5]))
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
