"""
Unit test for optimizer.pick_battle_targets — filter + sort logic.

Runs entirely in memory with hand-crafted rows and estimates.  No browser,
no file I/O.  Exercises every filter branch (in-range, daily cap, friends,
clan, bots, margin, spy gold/turns) and checks sort order.

Run:  python test_battle_pick.py
"""
import os, sys, types

# src/tests/test_*.py → src → darkthrone
_HERE       = os.path.dirname(os.path.abspath(__file__))
_SRC        = os.path.dirname(_HERE)
_DARKTHRONE = os.path.dirname(_SRC)
_DATA       = os.path.join(_DARKTHRONE, "data")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
os.makedirs(_DATA, exist_ok=True)
os.chdir(_DATA)

# Stub playwright so optimizer imports cleanly without the real package.
pw_mod  = types.ModuleType("playwright")
pw_sync = types.ModuleType("playwright.sync_api")
pw_sync.sync_playwright = lambda: None
sys.modules["playwright"]          = pw_mod
sys.modules["playwright.sync_api"] = pw_sync

import optimizer as opt

# ── Fixtures ──────────────────────────────────────────────────────────────────
ROWS = [
    # Safe: high gold, in range, low fort, count<5 → should be #1
    {"player_id": "100", "name": "RichWeakling", "gold": 500_000, "fort_pct": 20,
     "in_range": True, "is_bot": False, "is_clan": False, "is_friend": False,
     "is_hitlist": False, "attack_count": 0},
    # Safe but poorer
    {"player_id": "101", "name": "PoorWeakling", "gold": 50_000, "fort_pct": 30,
     "in_range": True, "is_bot": False, "is_clan": False, "is_friend": False,
     "is_hitlist": False, "attack_count": 0},
    # Exhausted — 5/5 today
    {"player_id": "102", "name": "MaxedOut", "gold": 700_000, "fort_pct": 0,
     "in_range": True, "is_bot": False, "is_clan": False, "is_friend": False,
     "is_hitlist": False, "attack_count": 5},
    # Out of range
    {"player_id": "103", "name": "Faraway", "gold": 900_000, "fort_pct": 0,
     "in_range": False, "is_bot": False, "is_clan": False, "is_friend": False,
     "is_hitlist": False, "attack_count": 0},
    # Friend (skip)
    {"player_id": "104", "name": "MyFriend", "gold": 800_000, "fort_pct": 10,
     "in_range": True, "is_bot": False, "is_clan": False, "is_friend": True,
     "is_hitlist": False, "attack_count": 0},
    # Clanmate (skip)
    {"player_id": "105", "name": "Clanmate", "gold": 800_000, "fort_pct": 10,
     "in_range": True, "is_bot": False, "is_clan": True, "is_friend": False,
     "is_hitlist": False, "attack_count": 0},
    # Too strong (margin fails)
    {"player_id": "106", "name": "Beefy", "gold": 1_000_000, "fort_pct": 80,
     "in_range": True, "is_bot": False, "is_clan": False, "is_friend": False,
     "is_hitlist": False, "attack_count": 0},
    # Bot — fair game by default
    {"player_id": "107", "name": "[bot] Filler", "gold": 200_000, "fort_pct": 0,
     "in_range": True, "is_bot": True, "is_clan": False, "is_friend": False,
     "is_hitlist": False, "attack_count": 1},
    # Unknown est_def (missing from estimates) — should be skipped
    {"player_id": "108", "name": "Stranger", "gold": 500_000, "fort_pct": 0,
     "in_range": True, "is_bot": False, "is_clan": False, "is_friend": False,
     "is_hitlist": False, "attack_count": 0},
]

# Estimates are keyed by player NAME (matches the CSV column `Player`).
ESTIMATES = {
    "RichWeakling":  {"est_def":  40_000, "est_spy_def":  2_000},
    "PoorWeakling":  {"est_def":  30_000, "est_spy_def":  1_500},
    "MaxedOut":      {"est_def":  30_000, "est_spy_def":  1_500},
    "Faraway":       {"est_def":  30_000, "est_spy_def":  1_500},
    "MyFriend":      {"est_def":  30_000, "est_spy_def":  1_500},
    "Clanmate":      {"est_def":  30_000, "est_spy_def":  1_500},
    "Beefy":         {"est_def": 300_000, "est_spy_def": 80_000},
    "[bot] Filler":  {"est_def":  20_000, "est_spy_def":   500},
    # "Stranger" intentionally missing
}

OUR_STATS = {"atk": 100_000, "spy_off": 15_000, "gold": 50_000, "turns": 500}

def _names(targets):
    return [t["name"] for t in targets]

def run():
    ok = True
    def check(label, cond, hint=""):
        nonlocal ok
        status = "OK  " if cond else "FAIL"
        print(f"  [{status}] {label}{(' — ' + hint) if hint else ''}")
        if not cond:
            ok = False

    cfg = {
        "margin": 1.2, "skip_friends": True, "skip_clan": True,
        "skip_bots": False, "max_per_pass": 10,
    }

    print("=== Attack mode — default skip (friends+clan on, bots off) ===")
    t = opt.pick_battle_targets(ROWS, ESTIMATES, OUR_STATS, cfg, mode="attack")
    names = _names(t)
    print(f"  result: {names}")
    check("RichWeakling is #1 by gold", names and names[0] == "RichWeakling")
    check("PoorWeakling is included",   "PoorWeakling" in names)
    check("Bot [bot] Filler is included (skip_bots=False)", "[bot] Filler" in names)
    check("Exhausted target filtered",   "MaxedOut"  not in names)
    check("Out-of-range target filtered","Faraway"   not in names)
    check("Friend filtered",             "MyFriend"  not in names)
    check("Clanmate filtered",           "Clanmate"  not in names)
    check("Too-strong target filtered",  "Beefy"     not in names)
    check("Unknown-estimate target filtered", "Stranger" not in names)

    print("\n=== Attack mode — skip_bots=True ===")
    cfg2 = dict(cfg, skip_bots=True)
    t = opt.pick_battle_targets(ROWS, ESTIMATES, OUR_STATS, cfg2, mode="attack")
    check("Bot excluded", "[bot] Filler" not in _names(t))

    print("\n=== Attack mode — tight margin 2.0× excludes low-margin targets ===")
    cfg3 = dict(cfg, margin=2.0)
    t = opt.pick_battle_targets(ROWS, ESTIMATES, OUR_STATS, cfg3, mode="attack")
    # our_atk=100k, need est_def <= 50k → RichWeakling(40k) and bot(20k) and PoorWeakling(30k) all pass.
    check("Safe targets still pass 2.0× margin", len(_names(t)) >= 2)
    cfg4 = dict(cfg, margin=3.0)
    t = opt.pick_battle_targets(ROWS, ESTIMATES, OUR_STATS, cfg4, mode="attack")
    # need est_def <= 33k → PoorWeakling(30k) and bot(20k) pass, RichWeakling(40k) fails
    check("3.0× margin drops RichWeakling", "RichWeakling" not in _names(t))

    print("\n=== Spy mode — needs 3000 gold + 2 turns + spy_off margin ===")
    OUR_SPY = {"atk": 100_000, "spy_off": 15_000, "gold": 50_000, "turns": 500}
    t = opt.pick_battle_targets(ROWS, ESTIMATES, OUR_SPY, cfg, mode="spy")
    print(f"  result: {_names(t)}")
    check("Spy mode finds targets", len(t) > 0)
    check("Beefy (80k spy_def) excluded", "Beefy" not in _names(t))

    OUR_BROKE = {"atk": 100_000, "spy_off": 15_000, "gold": 1_000, "turns": 500}
    t = opt.pick_battle_targets(ROWS, ESTIMATES, OUR_BROKE, cfg, mode="spy")
    check("Spy mode with <3000 gold returns []", t == [])

    OUR_NOTURNS = {"atk": 100_000, "spy_off": 15_000, "gold": 50_000, "turns": 1}
    t = opt.pick_battle_targets(ROWS, ESTIMATES, OUR_NOTURNS, cfg, mode="spy")
    check("Spy mode with <2 turns returns []", t == [])

    print("\n=== Daily cap (5/5) always respected ===")
    rows_all_maxed = [dict(r, attack_count=5) for r in ROWS]
    t = opt.pick_battle_targets(rows_all_maxed, ESTIMATES, OUR_STATS, cfg, mode="attack")
    check("All targets at 5/5 returns []", t == [])

    print("\n=== Sort: gold desc, then fort_pct desc ===")
    rows_sort = [
        {"player_id":"200","name":"A","gold":100,"fort_pct":90,"in_range":True,
         "is_bot":False,"is_clan":False,"is_friend":False,"is_hitlist":False,"attack_count":0},
        {"player_id":"201","name":"B","gold":500,"fort_pct":10,"in_range":True,
         "is_bot":False,"is_clan":False,"is_friend":False,"is_hitlist":False,"attack_count":0},
        {"player_id":"202","name":"C","gold":500,"fort_pct":90,"in_range":True,
         "is_bot":False,"is_clan":False,"is_friend":False,"is_hitlist":False,"attack_count":0},
    ]
    ests_sort = {
        "A":{"est_def":1_000,"est_spy_def":100},
        "B":{"est_def":1_000,"est_spy_def":100},
        "C":{"est_def":1_000,"est_spy_def":100},
    }
    t = opt.pick_battle_targets(rows_sort, ests_sort, OUR_STATS, cfg, mode="attack")
    order = _names(t)
    check("C (gold=500, fort=90) first",  order[0] == "C")
    check("B (gold=500, fort=10) second", order[1] == "B")
    check("A (gold=100) last",            order[2] == "A")

    print(f"\n{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(run())
