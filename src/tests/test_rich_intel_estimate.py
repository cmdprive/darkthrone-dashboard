"""
Phase 3 test harness — rich-intel precision in estimate().

Covers:
  1. _is_rich_intel_fresh: accepts captures within window, rejects old
  2. _exact_income_from_intel: exact gold/tick from mine + workers
  3. _reconstruct_stat_from_intel: ATK / DEF rebuilt from army + armory + upgrades
  4. _fort_damage_pct: 0.0 full HP, ~1.0 destroyed, None when unknown
  5. estimate() publishes exact income + building levels + fort state
     when rich_intel kwarg is fresh
  6. estimate() falls back cleanly when rich_intel is absent or stale
  7. End-to-end: parse Carrot's spy report → estimate() exposes precise fields

Run:  python test_rich_intel_estimate.py
"""
import sys, os, datetime

_HERE       = os.path.dirname(os.path.abspath(__file__))
_SRC        = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Stub playwright so importing optimizer doesn't require the real package.
import types
pw_mod = types.ModuleType('playwright')
pw_sync = types.ModuleType('playwright.sync_api')
pw_sync.sync_playwright = lambda: None
sys.modules['playwright'] = pw_mod
sys.modules['playwright.sync_api'] = pw_sync

import optimizer as opt


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _old_str(hours_ago):
    return (datetime.datetime.now() - datetime.timedelta(hours=hours_ago)
            ).strftime("%Y-%m-%d %H:%M:%S")


# ── Fresh Carrot intel fixture (matches screenshot from 2026-04-16 20:23) ──
CARROT_RICH = {
    "captured_at": None,   # filled in per-test with _now_str() / _old_str()
    "source":      "spy",
    "level": 29, "race": "Elf", "cls": "Cleric",
    "fort_hp": 2233, "fort_max": 3000,
    "atk": 188264, "def": 359970, "spy_off": 414, "spy_def": 2130,
    "gold": 100856, "bank": 0, "citizens": 44,
    "army": {"workers": 5200, "soldiers": 390, "guards": 695, "spies": 79, "sentries": 76},
    "army_detail": {
        "workers":  {"tier": 1, "unit": "Expert Miner", "qty": 5200, "bonus": 150, "total": 780000},
        "soldiers": {"tier": 2, "unit": "Knight",        "qty":  390, "bonus":  20, "total":   7800},
        "guards":   {"tier": 1, "unit": "Archer",        "qty":  695, "bonus":  20, "total":  13900},
        "spies":    {"tier": 1, "unit": "Spy",           "qty":   79, "bonus":   5, "total":    395},
        "sentries": {"tier": 1, "unit": "Sentry",        "qty":   76, "bonus":   5, "total":    380},
    },
    "armory": {
        "Short Sword":         {"tier": 5, "category": "Offense Weapons",     "qty": 301, "bonus": 200, "total":  60200},
        "Iron Chainmail":      {"tier": 5, "category": "Offense Armor",       "qty": 304, "bonus": 180, "total":  54720},
        "Javelin":             {"tier": 6, "category": "Defense Weapons",     "qty": 600, "bonus": 150, "total":  90000},
        "Bronze Chainmail":    {"tier": 6, "category": "Defense Armor",       "qty": 600, "bonus": 120, "total":  72000},
        "Mace":                {"tier": 3, "category": "Spy Defense Weapons", "qty":  23, "bonus":  50, "total":   1150},
        "Studded Guard Armor": {"tier": 3, "category": "Spy Defense Armor",   "qty":  12, "bonus":  50, "total":    600},
    },
    "buildings": {
        "fortification": 2, "armory": 1, "mine": 2, "spy_academy": 1,
        "barracks": 1, "housing": 2, "mercenary_camp": 2,
    },
    "upgrades": {
        "Steed":       {"kind": "Offense", "qty": 250, "bonus": 200},
        "Guard Tower": {"kind": "Defense", "qty": 505, "bonus": 200},
    },
}


def _fresh(base):
    b = dict(base)
    b["captured_at"] = _now_str()
    return b


def test_freshness_gate():
    fresh = _fresh(CARROT_RICH)
    stale = dict(CARROT_RICH)
    stale["captured_at"] = _old_str(100)   # 100 hours ago — stale
    _assert(opt._is_rich_intel_fresh(fresh),          "fresh should pass")
    _assert(not opt._is_rich_intel_fresh(stale),      "100h-old should fail")
    _assert(not opt._is_rich_intel_fresh(None),        "None should fail")
    _assert(not opt._is_rich_intel_fresh({}),          "empty dict should fail")
    _assert(not opt._is_rich_intel_fresh({"captured_at": "bogus"}),
            "malformed timestamp should fail")
    print("  ✅ _is_rich_intel_fresh: accepts fresh, rejects stale/empty/malformed")


def test_exact_income_from_intel():
    fresh = _fresh(CARROT_RICH)
    exact = opt._exact_income_from_intel(fresh, 'Elf', 'Cleric')
    _assert(exact is not None and exact > 0,
            f"exact income should be positive: {exact}")
    # Manually compute to cross-check
    rb = opt.RACE['Elf']; cb = opt.CLASS['Cleric']
    expected = int((opt.BASE_INC + 5200 * opt.WORKER_GOLD)
                   * opt.mine_mult(2)
                   * (1 + rb.get('income', 0) + cb.get('income', 0)))
    _assert(exact == expected, f"exact income mismatch: got {exact} want {expected}")

    # Missing pieces → None
    no_army = dict(fresh); no_army['army'] = {}
    _assert(opt._exact_income_from_intel(no_army, 'Elf', 'Cleric') is None,
            "no workers → None")
    no_bld  = dict(fresh); no_bld['buildings'] = {}
    _assert(opt._exact_income_from_intel(no_bld, 'Elf', 'Cleric') is None,
            "no mine level → None")
    print(f"  ✅ _exact_income_from_intel: {exact:,} gold/tick (Elf Cleric, mine L2, 5200 workers)")


def test_reconstruct_atk():
    fresh = _fresh(CARROT_RICH)
    atk = opt._reconstruct_stat_from_intel(fresh, 'Elf', 'Cleric', 'atk')
    # Expected:
    #   soldiers 390 × bonus 20 = 7,800  (Knight base)
    # + weapons equipped = min(301, 390) = 301 × 200 = 60,200
    # + armor equipped   = min(304, 390) = 304 × 180 = 54,720
    # + Steed upgrade    = 250 × 200 = 50,000
    # sum = 172,720
    # × (1 + 0 + 0) for Elf (no atk bonus) / Cleric (no atk bonus)
    # = 172,720
    expected = 7_800 + 60_200 + 54_720 + 50_000
    _assert(atk == expected, f"atk reconstruction mismatch: got {atk} want {expected}")
    # Compare to reported 188,264 — reconstruction is a sanity floor; they
    # diverge by ~9% which is game-side mechanics we don't model (enchantments,
    # battle-upgrade synergies, etc.).  The test asserts our math, not parity
    # with the live value.
    print(f"  ✅ _reconstruct_stat_from_intel atk: {atk:,} (soldiers + gear + Steed)")


def test_reconstruct_def():
    fresh = _fresh(CARROT_RICH)
    d = opt._reconstruct_stat_from_intel(fresh, 'Elf', 'Cleric', 'def')
    # Expected:
    #   guards 695 × 20 = 13,900
    # + Javelin equipped = min(600, 695) = 600 × 150 = 90,000
    # + Bronze armor     = min(600, 695) = 600 × 120 = 72,000
    # + Guard Tower      = 505 × 200 = 101,000
    # sum = 276,900
    # × (1 + 0.05[Elf def] + 0.05[Cleric def]) = × 1.10
    # = 304,590
    raw = 13_900 + 90_000 + 72_000 + 101_000
    expected = int(raw * (1 + 0.05 + 0.05))
    _assert(d == expected, f"def reconstruction mismatch: got {d} want {expected}")
    print(f"  ✅ _reconstruct_stat_from_intel def: {d:,} (guards + gear + tower + 10% bonus)")


def test_fort_damage_pct():
    fresh = _fresh(CARROT_RICH)   # fort_hp=2233, fort_max=3000
    pct = opt._fort_damage_pct(fresh)
    expected = 1.0 - 2233/3000
    _assert(abs(pct - expected) < 1e-9, f"fort damage: got {pct} want {expected}")
    # Full HP
    full = dict(fresh); full['fort_hp'] = 3000
    _assert(opt._fort_damage_pct(full) == 0.0, "full HP should be 0% damage")
    # Destroyed
    dead = dict(fresh); dead['fort_hp'] = 0
    _assert(opt._fort_damage_pct(dead) == 1.0, "0 HP should be 100% damage")
    # Missing fields
    _assert(opt._fort_damage_pct({}) is None, "no fields → None")
    print(f"  ✅ _fort_damage_pct: Carrot at {pct:.1%} damage (2233/3000)")


def test_estimate_with_fresh_rich_intel():
    """estimate() surfaces rich fields when a fresh snapshot is passed."""
    fresh = _fresh(CARROT_RICH)
    you = {'level': 28, 'atk': 100_000, 'def': 100_000, 'spy_off': 1_000, 'spy_def': 1_000,
           'population': 2500, 'workers': 2000, 'off_units': 50, 'def_units': 100,
           'spy_units': 10, 'sent_units': 10, 'income': 50_000, 'mine_lv': 1,
           'rank_offense': 28, 'rank_defense': 28, 'rank_spy_off': 0, 'rank_spy_def': 0}

    e = opt.estimate("Carrot", 29, "Elf", "Cleric", 2688, "RQUM",
                     7, 13, 7, you, rich_intel=fresh)

    # Exact income should be overridden
    expected_income = opt._exact_income_from_intel(fresh, 'Elf', 'Cleric')
    _assert(e["income"] == expected_income,
            f"estimate should publish exact income: got {e['income']} want {expected_income}")
    # Mine level
    _assert(e["mine_lv"] == 2, f"mine_lv should be 2: {e.get('mine_lv')}")
    # Building levels exposed
    _assert(e.get("fortification_lv")   == 2, f"fortification_lv: {e.get('fortification_lv')}")
    _assert(e.get("housing_lv")         == 2, f"housing_lv: {e.get('housing_lv')}")
    _assert(e.get("mercenary_camp_lv")  == 2, f"mercenary_camp_lv: {e.get('mercenary_camp_lv')}")
    _assert(e.get("spy_academy_lv")     == 1, f"spy_academy_lv: {e.get('spy_academy_lv')}")
    # Fort state
    _assert(e.get("fort_hp")    == 2233, f"fort_hp: {e.get('fort_hp')}")
    _assert(e.get("fort_max")   == 3000, f"fort_max: {e.get('fort_max')}")
    _assert(abs(e.get("fort_damage_pct", 0) - (1 - 2233/3000)) < 1e-3,
            f"fort_damage_pct: {e.get('fort_damage_pct')}")
    # Reconstructions
    _assert(e.get("atk_reconstructed") > 0, f"atk_reconstructed missing")
    _assert(e.get("def_reconstructed") > 0, f"def_reconstructed missing")
    # Army detail passed through
    _assert(e.get("army_detail", {}).get("soldiers", {}).get("bonus") == 20,
            f"army_detail missing: {e.get('army_detail')}")
    print("  ✅ estimate() + fresh rich_intel: exact income, building levels, fort state all surfaced")


def test_estimate_falls_back_when_stale():
    """Stale rich intel is ignored entirely — no rich fields leak through."""
    stale = dict(CARROT_RICH); stale["captured_at"] = _old_str(100)   # 100h
    you = {'level': 28, 'atk': 100_000, 'def': 100_000, 'spy_off': 1_000, 'spy_def': 1_000,
           'population': 2500, 'workers': 2000, 'off_units': 50, 'def_units': 100,
           'spy_units': 10, 'sent_units': 10, 'income': 50_000, 'mine_lv': 1,
           'rank_offense': 28, 'rank_defense': 28, 'rank_spy_off': 0, 'rank_spy_def': 0}
    e = opt.estimate("Carrot", 29, "Elf", "Cleric", 2688, "RQUM",
                     7, 13, 7, you, rich_intel=stale)

    # NONE of the rich fields should be present when intel is stale
    for k in ("fortification_lv", "housing_lv", "mercenary_camp_lv",
              "fort_hp", "fort_max", "fort_damage_pct",
              "atk_reconstructed", "def_reconstructed", "army_detail"):
        _assert(k not in e, f"stale intel should NOT leak {k}: {e.get(k)!r}")

    # Baseline fields still there (tempting to assert value but conf path
    # depends on CS entry state in this test env — just confirm not empty)
    _assert("income" in e, "baseline income should still be present")
    _assert("mine_lv" in e, "baseline mine_lv should still be present")
    print("  ✅ estimate() + stale rich_intel: rich fields correctly withheld, baseline preserved")


def test_estimate_no_rich_intel_passthrough():
    """rich_intel=None (caller didn't pass one) must not break estimate()."""
    you = {'level': 28, 'atk': 100_000, 'def': 100_000, 'spy_off': 1_000, 'spy_def': 1_000,
           'population': 2500, 'workers': 2000, 'off_units': 50, 'def_units': 100,
           'spy_units': 10, 'sent_units': 10, 'income': 50_000, 'mine_lv': 1,
           'rank_offense': 28, 'rank_defense': 28, 'rank_spy_off': 0, 'rank_spy_def': 0}
    e = opt.estimate("UnknownPlayer", 15, "Human", "Fighter", 1500, "?",
                     50, 50, 50, you)   # no rich_intel kwarg
    _assert("income" in e, "baseline income should be present")
    _assert("atk" in e, "baseline atk should be present")
    _assert("army_detail" not in e, "army_detail should be absent without rich intel")
    print("  ✅ estimate() without rich_intel kwarg: falls back cleanly")


def test_end_to_end_parser_to_estimate():
    """Full pipeline: Carrot screenshot → parse → record → load → estimate()."""
    # Build HTML via the Phase 1 fixture helper
    sys.path.insert(0, _HERE)
    from test_spy_parser import _build_spy_html, CARROT, FakePage

    import tempfile, shutil
    tmp = tempfile.mkdtemp(prefix="dt_phase3_e2e_")
    prev_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        html = _build_spy_html(**CARROT)
        entry = opt.parse_spy_report(FakePage(html))
        # Parser should have populated army_detail (Phase 3 extension)
        _assert("target_army_detail" in entry,
                f"parser should produce target_army_detail: keys={list(entry.keys())}")
        _assert(entry["target_army_detail"]["soldiers"]["bonus"] == 20,
                f"soldiers bonus should be 20: {entry['target_army_detail'].get('soldiers')}")

        # Full record → snapshot round-trip
        opt.record_target_intel(entry)
        snap = opt.load_target_intel_snapshot()
        _assert("Carrot" in snap,                f"Carrot missing from snap: {list(snap.keys())}")
        _assert("army_detail" in snap["Carrot"], f"army_detail missing after round-trip")
        _assert(snap["Carrot"]["army_detail"]["soldiers"]["bonus"] == 20,
                f"soldiers bonus missing: {snap['Carrot']['army_detail'].get('soldiers')}")

        # Feed into estimate()
        rich = snap["Carrot"]
        # snapshot's captured_at is 'now' (just written), so should be fresh
        you = {'level': 28, 'atk': 100_000, 'def': 100_000, 'spy_off': 1_000, 'spy_def': 1_000,
               'population': 2500, 'workers': 2000, 'off_units': 50, 'def_units': 100,
               'spy_units': 10, 'sent_units': 10, 'income': 50_000, 'mine_lv': 1,
               'rank_offense': 28, 'rank_defense': 28, 'rank_spy_off': 0, 'rank_spy_def': 0}
        e = opt.estimate("Carrot", 29, "Elf", "Cleric", 2688, "RQUM",
                         7, 13, 7, you, rich_intel=rich)
        _assert(e.get("fort_hp") == 2233,   f"fort_hp after round-trip: {e.get('fort_hp')}")
        _assert(e.get("mine_lv") == 2,      f"mine_lv after round-trip: {e.get('mine_lv')}")
        _assert(e.get("housing_lv") == 2,   f"housing_lv after round-trip: {e.get('housing_lv')}")
        _assert(e.get("atk_reconstructed") is not None,
                f"atk_reconstructed should be set after round-trip")

        print("  ✅ End-to-end: HTML → parse → record → load → estimate exposes rich fields")
    finally:
        os.chdir(prev_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 72)
    print("test_rich_intel_estimate.py — Phase 3 precision in estimate()")
    print("=" * 72)
    test_freshness_gate()
    test_exact_income_from_intel()
    test_reconstruct_atk()
    test_reconstruct_def()
    test_fort_damage_pct()
    test_estimate_with_fresh_rich_intel()
    test_estimate_falls_back_when_stale()
    test_estimate_no_rich_intel_passthrough()
    test_end_to_end_parser_to_estimate()
    print()
    print("✅ All 9 test cases passed.")


if __name__ == "__main__":
    main()
