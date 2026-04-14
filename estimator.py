"""
DarkThrone — Player Stat Estimator
====================================
All unlock requirements confirmed from in-game buildings screen screenshots.

CONFIRMED BUILDING REQUIREMENTS (from in-game screenshots 2026-04-08):
  Building        Lv1 req           Lv2 req                          Lv2 cost   Bonus/level
  ─────────────────────────────────────────────────────────────────────────────────────────
  Mine            Player Level 3    Player Level 12 + Fort Lv1       3.50M gold  +10% income/lv (cumulative)
  Housing         Player Level 3    Player Level 13 + Fort Lv1       3.00M gold  +10 citizens/midnight/lv
  Spy Academy     Player Level 5    (not confirmed)                  250k×lv     +5% spy offense/lv
  Mercenary Camp  Player Level ?    (not confirmed, max Lv3)         200k×lv     20/30/40 mercs/day
  Barracks        Player Level 8    (not confirmed)                  400k×lv     +2 citizens/tick/lv
  Fortification   Player Level 10   (not confirmed)                  500k×lv     +1000 fort HP/lv
  Armory          Player Level 10   Player Level 30 + Fort Lv1 ←CONFIRMED 750k+  unlocks gear tiers

CONFIRMED GEAR UNLOCK CHAIN (from in-game Armory upgrade screen):
  "Each level unlocks 2 new equipment tiers."
  Gear tier = min(armory_gate, fort_gate) — BOTH buildings must be levelled.

  Armory Lv  →  Unlocks    →  Player Level Req  →  Fort Req  →  Cost
  ──────────────────────────────────────────────────────────────────────
  Lv0 (base) →  T1–T3      →  —                 →  —         →  —
  Lv1        →  T4–T5      →  Player Level 10   →  none      →  ~750k
  Lv2        →  T6–T7      →  Player Level 30   →  Fort Lv1  →  7.00M  ← CONFIRMED
  Lv3        →  T8–T9      →  ~Player Level 50? →  Fort Lv2? →  est.
  Lv4        →  T10        →  ~Player Level 70? →  Fort Lv3? →  est.
  Lv5        →  (maxed)    →  ~Player Level 90? →  Fort Lv4? →  est.

  KEY IMPLICATION: As of 2026-04-08 (server max level ≈21), NO player on the
  server has unlocked T6+ gear. All level 10–29 players are capped at T5.

INCOME FORMULA (confirmed from your data):
  Income = (1000 + workers × 65) × (1 + mine_lv × 0.10) × (1 + income_bonus)
  income_bonus: Thief class = +5% (confirmed from game UI 2026-04-08).
  Stacks multiplicatively with mine bonus.

ESTIMATION APPROACH:
  We know: population, level, race, class, leaderboard rank.
  We DON'T know: worker/military split, gear count per unit.
  Strategy: derive max mine level from player level,
            derive max gear tier from armory level (which needs player level 10),
            use 15% military fraction (conservative real-world estimate),
            label ALL non-YOU estimates as UPPER BOUND.
"""

import csv, json, math, re, os, sys, datetime, subprocess

def _unhide(path):
    if sys.platform == "win32" and os.path.isfile(path):
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)
        except Exception:
            pass

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
FORT_LV_TO_UNIT_TIER   = {0:1, 1:2, 2:3, 3:4, 4:4, 5:4}

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
    No player on this server (max Lv21 as of 2026-04-08) has Armory Lv2."""
    if player_lv < 10: return 0
    if player_lv < 30: return 1   # T4–T5 cap for ALL current players
    if player_lv < 50: return 2   # T6–T7  (Player Lv30 confirmed)
    if player_lv < 70: return 3   # T8–T9  (estimated)
    if player_lv < 90: return 4   # T10    (estimated)
    return 5

def est_fort_lv(player_lv):
    """Fortification level tracks armory (fort is cheaper so reaches each
    level slightly earlier, but confirmed Fort Lv1 is req. for Armory Lv2)."""
    if player_lv < 10: return 0
    if player_lv < 30: return 1   # matches armory gate
    if player_lv < 50: return 2
    if player_lv < 70: return 3
    if player_lv < 90: return 4
    return 5

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

# ── Population mechanics ───────────────────────────────────────────────────────
# Each day at 00:00 server time players can recruit up to 300 new citizens.
# These are added to TOTAL POPULATION (shown on /game/player/{id} profile page).
# Total population = workers + soldiers + guards + spies + sentries + idle citizens.
# Idle citizens (shown in header bar) = population - all_trained_units.
MAX_DAILY_RECRUIT = 300

def est_days_active(population: int) -> int:
    """Estimate days a player has been active based on their total population.
    Assumes they recruit the full 300/day every day. Minimum 1 day."""
    return max(1, round(population / MAX_DAILY_RECRUIT))

def recruit_efficiency(population: int, level: int) -> str:
    """Rate how consistently a player recruits based on population vs level.
    Higher level players have had more time — compare pop to expected max."""
    expected = level * MAX_DAILY_RECRUIT * 10  # rough: ~10 days per level
    if population <= 0 or expected <= 0:
        return '?'
    pct = population / expected * 100
    if pct >= 90: return 'HIGH'
    if pct >= 60: return 'MED'
    return 'LOW'

# ── Rank-calibrated exponential decay models ──────────────────────────────────
# stat(rank) = A * exp(-k * rank)
# Models are pre-seeded from CONFIRMED data and re-calibrated at runtime
# whenever profiles have been scraped and ranks are known.
#
# ATK — Ashcipher(off_rank=1,ATK=76408) + Mungus(off_rank=6,ATK=49041)
#   k = ln(76408/49041)/5 = 0.0885   A = 76408/exp(-0.0885) ≈ 83,484
ATK_RANK_A = 83_484.0
ATK_RANK_K = 0.0885

# DEF — Radagon(def_rank=1,DEF=77457) — one confirmed point, same k as ATK
#   A = 77457/exp(-0.0885*1) ≈ 84,647
DEF_RANK_A = 84_647.0
DEF_RANK_K = 0.0885

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


def calibrate_models(profiles: dict):
    """Re-calibrate all four stat models using CONFIRMED_STATS cross-referenced
    with scraped profile ranks. Call once per run after profiles are loaded."""
    global ATK_RANK_A, ATK_RANK_K
    global DEF_RANK_A, DEF_RANK_K
    global SPY_OFF_RANK_A, SPY_OFF_RANK_K
    global SPY_DEF_RANK_A, SPY_DEF_RANK_K

    atk_pts, def_pts, spo_pts, spd_pts = [], [], [], []

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

    A, k = _seed_model(atk_pts, ATK_RANK_K, 'ATK model')
    if A: ATK_RANK_A, ATK_RANK_K = A, k

    A, k = _seed_model(def_pts, DEF_RANK_K, 'DEF model')
    if A: DEF_RANK_A, DEF_RANK_K = A, k

    A, k = _seed_model(spo_pts, ATK_RANK_K, 'SpyOff model')
    if A: SPY_OFF_RANK_A, SPY_OFF_RANK_K = A, k

    A, k = _seed_model(spd_pts, DEF_RANK_K, 'SpyDef model')
    if A: SPY_DEF_RANK_A, SPY_DEF_RANK_K = A, k

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
    'Mungus': {
        'level': 21, 'race': 'Undead', 'cls': 'Thief',
        'atk': 49_041, 'def':  5_720, 'spy_off':  90,   'spy_def':  70,
        'gold':  21_370, 'bank': 1_800_000, 'citizens_idle': 49,
    },
    'JT': {
        'level': 15, 'race': 'Goblin', 'cls': 'Thief',
        'atk': 19_145, 'def': 14_423, 'spy_off': 210,   'spy_def':  90,
        'gold':    782, 'bank': 1_010_000, 'citizens_idle': 1_385,
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
    ('TGO Jasbob1989',             19,'Undead', 'Fighter', 2757,'TGO',  99, 99, 99),
    ('Nerv',                       20,'Human',  'Fighter', 2754,'TGO',  99, 99, 99),
    ('Radagon Of The Golden Order',18,'Goblin', 'Cleric',  2600,'TGO',  99, 99, 99),
    ('sirclement_xxviii',          18,'Undead', 'Assassin',2647,'—',    99, 99, 99),
    ('Tycoon',                     15,'Goblin', 'Cleric',  2700,'RQUM', 99, 99, 99),
    ('TGO_Gaara',                  18,'—',      '—',       2500,'TGO',  99, 99, 99),
    ('Carrot',                     21,'Elf',    'Cleric',  2688,'—',    99, 99, 99),
    ('NapoleonBorntoparty',        13,'Goblin', 'Thief',   2500,'TGO',  99, 99, 99),
    ('Hesiana',                    18,'—',      '—',       2500,'—',    99, 99, 99),
    ('Mungus',                     21,'Undead', 'Thief',   2556,'RQUM', 99, 99, 99),
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

    # ── CONFIRMED: real stats from profile screenshot ─────────────────────────
    if clean in CONFIRMED_STATS:
        c = CONFIRMED_STATS[clean]
        # Override level/race/cls with confirmed values
        level = c.get('level', level)
        race  = c.get('race',  race)
        cls   = c.get('cls',   cls)
        rb = RACE.get(race, RACE['Human'])
        cb = CLASS.get(cls,  CLASS['Fighter'])
        mine_lv = est_mine_lv(level)
        workers = int(pop * 0.80)
        income  = int((BASE_INC + workers * WORKER_GOLD) * mine_mult(mine_lv)
                      * (1 + rb.get('income', 0) + cb.get('income', 0)))
        ad = kwargs  # army kwargs available in CONFIRMED path too
        return {
            'pop':       pop,     'workers':   workers,
            'off_u':     '?',     'def_u':     '?',
            'spy_u':     '?',     'sent_u':    '?',
            'atk':       c['atk'], 'def':      c['def'],
            'spy_off':   c['spy_off'], 'spy_def': c['spy_def'],
            'income':    income,  'mine_lv':   mine_lv,
            'gear_t':    max_gear_tier(level),
            'unit_t':    FORT_LV_TO_UNIT_TIER[est_fort_lv(level)],
            'army_size': ad.get('army_size', 0),
            'upgrades':  max(0, ad.get('building_upgrades', -1)),
            'conf':      'CONFIRMED',
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
def run():
    you      = load_your_stats()
    profiles = load_scraped_profiles()
    ts       = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    results  = []

    # Auto-calibrate DEF model (and verify ATK model) using confirmed players
    # whose off_rank / def_rank are now known from scraped profiles
    calibrate_models(profiles)

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
    tier_rows = [(2,'T1 unit + T3 gear'),(10,'T2 unit + T5 gear'),
                 (13,'T3 unit + T7 gear'),(16,'T4 unit + T9 gear'),(18,'T4 unit + T10 gear')]
    print(f'\n  GEAR TIER SUMMARY (stat per fully-geared unit):')
    for lv, label in tier_rows:
        you_tag = '  ← YOU' if lv <= your_lv < (tier_rows[tier_rows.index((lv,label))+1][0]
                                                  if tier_rows.index((lv,label)) < len(tier_rows)-1
                                                  else 999) else ''
        print(f'  Lv{lv:<3} ({label}): {stat_per_unit(lv):,}/unit{you_tag}')

    return results

if __name__ == '__main__':
    run()
