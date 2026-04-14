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

import re, time, datetime, csv, os, json, subprocess, sys
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

    sent_w_count = gear_count(r'Hatchet x(\d+)') + gear_count(r'Mace x(\d+).*?SPY DEF')
    s["_gear_owned"] = {
        ("soldier","weapon"): gear_count(r'Quarterstaff x(\d+)'),
        ("soldier","armor"):  gear_count(r'Studded Leather Armor x(\d+)\s+\+\d+ ATK'),
        ("guard",  "weapon"): gear_count(r'Spear x(\d+)'),
        ("guard",  "armor"):  gear_count(r'Studded Leather Armor x(\d+)\s+\+\d+ DEF'),
        ("spy",    "weapon"): gear_count(r'Blowgun x(\d+)'),
        ("spy",    "armor"):  gear_count(r'Infiltrator Garb x(\d+)'),
        ("sentry", "weapon"): sent_w_count,
        ("sentry", "armor"):  gear_count(r'Studded Guard Armor x(\d+)'),
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

        # ── Read own server-wide ranks from /stats profile page ──────────────
        try:
            import scraper_private as _scp
            _own_ranks = _scp.read_own_ranks(page)
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

        # ── Scrape rankings (single page) — refreshes all players' ranks ──────
        try:
            _scp.scrape_rankings(page, _ts)
        except Exception as _e:
            print(f"  ⚠️ Rankings scrape error: {_e}")

        # ── Scrape public attack list + update dashboard ──────────────────────
        try:
            import scraper as _sc
            _sc.scrape_with_page(page, max_pages=50)
        except Exception as _e:
            print(f"  ⚠️ Scraper error: {_e}")

        # ── Run estimator (writes fresh private_player_estimates.csv) ───────
        try:
            import estimator as _est
            print("  🔍 Running player estimates...")
            _est.run()
        except Exception as _e:
            print(f"  ⚠️ Estimator error: {_e}")

        # ── Re-run dashboard update so fresh estimates are in the publish ─────
        # (scraper.scrape_with_page ran BEFORE the estimator wrote the CSV, so
        #  we call update_dashboard() once more to inject the up-to-date data.)
        try:
            _sc.update_dashboard()
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
