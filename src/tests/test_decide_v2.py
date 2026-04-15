"""
Unit test for the marginal-value decision engine (optimizer.decide_v2).

Handcrafts a minimal state dict + cats list and verifies that the scoring
engine picks sensible actions for each strategy profile.

Run:  python src/tests/test_decide_v2.py
"""
import os, sys, types

# src/tests/test_decide_v2.py → src → darkthrone
_HERE       = os.path.dirname(os.path.abspath(__file__))
_SRC        = os.path.dirname(_HERE)
_DARKTHRONE = os.path.dirname(_SRC)
_DATA       = os.path.join(_DARKTHRONE, "data")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
os.makedirs(_DATA, exist_ok=True)
os.chdir(_DATA)

# Stub playwright so optimizer imports cleanly
pw_mod  = types.ModuleType("playwright")
pw_sync = types.ModuleType("playwright.sync_api")
pw_sync.sync_playwright = lambda: None
sys.modules["playwright"]          = pw_mod
sys.modules["playwright.sync_api"] = pw_sync

import optimizer as opt


def _mk_state(**overrides):
    """Build a plausible state dict.  Defaults mirror a Level-15 player
    with a modest army, some citizens, and enough gold to spend."""
    base = {
        "gold":         500_000,
        "bank":         0,
        "income":       300_000,
        "level":        15,
        "citizens":     50,
        "atk":          50_000,
        "def":          100_000,
        "spy_off":      10_000,
        "spy_def":      5_000,
        "workers":      3_000,
        "soldiers":     500,
        "guards":       500,
        "spies":        50,
        "sentries":     30,
        "mine_lv":      1,
        "fort_hp":      1000,
        "fort_max_hp":  1000,
        "fort_pct":     100,
        "cost_per_hp":  16.75,
        "buildings": {"Mine": 1, "Housing": 0, "Spy Academy": 1,
                      "Mercenary Camp": 0, "Barracks": 0,
                      "Fortification": 1, "Armory": 1},
        "upgrades_buyable": {},
        "upgrades_owned":   {},
        "deposits":     0,
        "xp":           0,
        "xp_need":      10_000,
        "xp_pct":       0,
        # gear / gear_tier / max_buyable_tier populated below
    }
    base["gear"] = {
        ("soldier","weapon"): (500, 500),
        ("soldier","armor" ): (500, 500),
        ("guard",  "weapon"): (500, 500),
        ("guard",  "armor" ): (500, 500),
        ("spy",    "weapon"): (50, 50),
        ("spy",    "armor" ): (50, 50),
        ("sentry", "weapon"): (30, 30),
        ("sentry", "armor" ): (30, 30),
    }
    base["gear_tier"] = {
        ("soldier","weapon"): 3, ("soldier","armor"): 3,
        ("guard",  "weapon"): 3, ("guard",  "armor"): 3,
        ("spy",    "weapon"): 3, ("spy",    "armor"): 3,
        ("sentry", "weapon"): 3, ("sentry", "armor"): 3,
    }
    base["max_buyable_tier"] = {
        ("soldier","weapon"): 5, ("soldier","armor"): 5,
        ("guard",  "weapon"): 5, ("guard",  "armor"): 5,
        ("spy",    "weapon"): 5, ("spy",    "armor"): 5,
        ("sentry", "weapon"): 5, ("sentry", "armor"): 5,
    }
    base.update(overrides)
    return base


def _action_types(actions):
    return [a["type"] for a in actions]


def check(label, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    print(f"  [{tag}] {label}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def test_legacy_key_normalization():
    print("\n=== test_legacy_key_normalization ===")
    ok = True
    ok &= check("balanced → grow",  opt._normalize_strategy_key("balanced") == "grow")
    ok &= check("economy → grow",   opt._normalize_strategy_key("economy")  == "grow")
    ok &= check("attack → combat",  opt._normalize_strategy_key("attack")   == "combat")
    ok &= check("spy → combat",     opt._normalize_strategy_key("spy")      == "combat")
    ok &= check("defense → defend", opt._normalize_strategy_key("defense")  == "defend")
    ok &= check("hybrid → defend",  opt._normalize_strategy_key("hybrid")   == "defend")
    ok &= check("grow stays grow",  opt._normalize_strategy_key("grow")     == "grow")
    ok &= check("unknown → grow",   opt._normalize_strategy_key("xyz")      == "grow")
    ok &= check("None → grow",      opt._normalize_strategy_key(None)       == "grow")
    return ok


def test_score_sign_convention():
    print("\n=== test_score_sign_convention ===")
    w = {"w_income": 10, "w_atk": 2, "w_def": 1, "w_xp": 3}
    ok = True
    # Cheap high-income option should score higher than expensive low-income option.
    cheap_income = {"cost": 100_000,
                    "benefits": {"income_delta": 500, "atk_delta": 0,
                                 "def_delta": 0, "xp_delta": 0}}
    expensive_income = {"cost": 1_000_000,
                        "benefits": {"income_delta": 500, "atk_delta": 0,
                                     "def_delta": 0, "xp_delta": 0}}
    s_cheap = opt.score_option(cheap_income, w)
    s_exp   = opt.score_option(expensive_income, w)
    ok &= check(f"cheap income ({s_cheap:.2f}) > expensive income ({s_exp:.2f})",
                s_cheap > s_exp)
    # Zero-cost option gets large score (we guard divzero, not the value)
    zero_cost = {"cost": 0, "benefits": {"income_delta": 10, "atk_delta": 0,
                                          "def_delta": 0, "xp_delta": 0}}
    ok &= check("zero-cost doesn't crash", opt.score_option(zero_cost, w) > 0)
    return ok


def test_fort_repair_always_first():
    print("\n=== test_fort_repair_always_first ===")
    s = _mk_state(fort_hp=500, fort_max_hp=1000, fort_pct=50)
    cats = opt.analyse(s)
    actions, _ = opt.decide_v2(s, cats, strategy="grow")
    ok = check("first action is REPAIR_FORT",
               bool(actions) and actions[0]["type"] == "REPAIR_FORT")
    return ok


def test_grow_prefers_income():
    print("\n=== test_grow_prefers_income ===")
    # No fort damage, mine can be upgraded, lots of gear gaps.
    # With Grow weights (w_income=10), Mine upgrade should score very highly.
    s = _mk_state(gold=5_000_000)  # enough for many spends
    cats = opt.analyse(s)
    actions, _ = opt.decide_v2(s, cats, strategy="grow")
    types = _action_types(actions)
    has_mine = any(a["type"] == "BUILD" and a.get("name") == "Mine" for a in actions)
    ok = check("Grow strategy picks Mine upgrade", has_mine,
               f"actions={types[:8]}")
    return ok


def test_combat_prefers_gear():
    print("\n=== test_combat_prefers_gear ===")
    # Same state but Combat strategy should lean toward gear/training.
    s = _mk_state(gold=5_000_000,
                  gear={
                    ("soldier","weapon"): (100, 500),  # big gap
                    ("soldier","armor" ): (100, 500),
                    ("guard",  "weapon"): (500, 500),
                    ("guard",  "armor" ): (500, 500),
                    ("spy",    "weapon"): (50, 50),
                    ("spy",    "armor" ): (50, 50),
                    ("sentry", "weapon"): (30, 30),
                    ("sentry", "armor" ): (30, 30),
                  })
    cats = opt.analyse(s)
    actions, _ = opt.decide_v2(s, cats, strategy="combat")
    types = _action_types(actions)
    gear_actions = [a for a in actions if a["type"] in ("BUY_GEAR", "UPGRADE_GEAR", "TRAIN")]
    ok = check("Combat strategy picks at least one gear/train action",
               len(gear_actions) >= 1, f"actions={types[:8]}")
    return ok


def test_no_targets_no_crash():
    print("\n=== test_no_targets_no_crash ===")
    # Empty army, no citizens, no gold — should not crash, returns empty list.
    s = _mk_state(gold=100, citizens=0, workers=0, soldiers=0, guards=0,
                  spies=0, sentries=0,
                  gear={("soldier","weapon"): (0,0), ("soldier","armor"): (0,0),
                        ("guard","weapon"):   (0,0), ("guard","armor"):   (0,0),
                        ("spy","weapon"):     (0,0), ("spy","armor"):     (0,0),
                        ("sentry","weapon"):  (0,0), ("sentry","armor"):  (0,0)})
    cats = opt.analyse(s)
    actions, gold_left = opt.decide_v2(s, cats, strategy="grow")
    ok = check("empty state returns list", isinstance(actions, list))
    ok &= check("gold_left is int", isinstance(gold_left, int))
    return ok


def test_option_dict_shape():
    print("\n=== test_option_dict_shape ===")
    # Verify every emitted option has the fields execute() needs for its type.
    s = _mk_state(gold=5_000_000)
    cats = opt.analyse(s)
    actions, _ = opt.decide_v2(s, cats, strategy="grow")
    ok = True
    for a in actions:
        t = a["type"]
        if t == "REPAIR_FORT":
            ok &= check("REPAIR_FORT has damage+cost", "damage" in a and "cost" in a)
        elif t == "BUILD":
            ok &= check(f"BUILD has name+lv+cost ({a.get('name')})",
                        "name" in a and "lv" in a and "cost" in a)
        elif t in ("BUY_GEAR", "UPGRADE_GEAR"):
            ok &= check(f"{t} has unit/slot/tier/qty/total/tab/name",
                        all(k in a for k in ("unit","slot","tier","qty","total","tab","name")))
        elif t == "TRAIN":
            ok &= check(f"TRAIN {a.get('unit')} has count+cost",
                        "unit" in a and "count" in a and "cost" in a)
        elif t == "BANK":
            ok &= check("BANK has amount", "amount" in a)
        elif t == "BUY_UPGRADE":
            ok &= check("BUY_UPGRADE has name+qty+total", "name" in a and "qty" in a)
    return ok


if __name__ == "__main__":
    results = [
        test_legacy_key_normalization(),
        test_score_sign_convention(),
        test_fort_repair_always_first(),
        test_grow_prefers_income(),
        test_combat_prefers_gear(),
        test_no_targets_no_crash(),
        test_option_dict_shape(),
    ]
    passed = sum(1 for r in results if r)
    total  = len(results)
    print(f"\n{passed}/{total} passed")
    raise SystemExit(0 if passed == total else 1)
