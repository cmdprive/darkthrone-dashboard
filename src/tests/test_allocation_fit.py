"""
Phase 2 test harness — empirical allocation-fraction fitting.

Covers:
  1. _theoretical_stat_rate: positive, monotonic in level/pop
  2. compute_allocation_observations: correct bucketing + ratio clamping
  3. fit_allocation_fractions: median-of-ratios, min-samples gate
  4. derive_growth_rate: fitted-value path + default fallback
  5. End-to-end: synthetic growth_rates → fit → derive_growth_rate picks it up

Run:  python test_allocation_fit.py
"""
import sys, os

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


def test_theoretical_rate_positive_and_scales():
    """Theoretical rate at 100% allocation should be positive for any sensible
    player, and should increase with level + population."""
    r1 = opt._theoretical_stat_rate(10, 'Goblin', 'Cleric', 1000, 'def')
    r2 = opt._theoretical_stat_rate(25, 'Goblin', 'Cleric', 2500, 'def')
    r3 = opt._theoretical_stat_rate(25, 'Goblin', 'Cleric', 5000, 'def')
    _assert(r1 > 0,    f"low-level rate should be positive: {r1}")
    _assert(r2 > r1,   f"higher level must give higher rate: r1={r1} r2={r2}")
    _assert(r3 > r2,   f"higher pop must give higher rate: r2={r2} r3={r3}")
    # All 4 stat types should produce a positive value
    for stat in ('atk', 'def', 'spy_off', 'spy_def'):
        v = opt._theoretical_stat_rate(25, 'Goblin', 'Cleric', 2500, stat)
        _assert(v > 0, f"stat={stat} returned {v}")
    # Invalid inputs return 0
    _assert(opt._theoretical_stat_rate(0,  'Goblin', 'Cleric', 2500, 'def')  == 0, "level=0 should give 0")
    _assert(opt._theoretical_stat_rate(25, 'Goblin', 'Cleric', 0,    'def')  == 0, "pop=0 should give 0")
    _assert(opt._theoretical_stat_rate(25, 'Goblin', 'Cleric', 2500, 'nope') == 0, "unknown stat should give 0")
    print("  ✅ _theoretical_stat_rate: positive, monotonic, validates inputs")


def test_compute_allocation_observations_bucketing():
    """Players with the same (race, class, stat) land in the same bucket.
    Each bucket collects the observed/theoretical RATIOS, one per player."""
    # Express observed rates as explicit fractions of theoretical so the
    # test is robust against future changes to income/gear cost tables.
    theo_gc_29_def = opt._theoretical_stat_rate(29, 'Goblin', 'Cleric', 2500, 'def')
    theo_gc_30_def = opt._theoretical_stat_rate(30, 'Goblin', 'Cleric', 2700, 'def')
    theo_gc_28_def = opt._theoretical_stat_rate(28, 'Goblin', 'Cleric', 2600, 'def')
    theo_gc_30_atk = opt._theoretical_stat_rate(30, 'Goblin', 'Cleric', 2700, 'atk')
    theo_ec_27_def = opt._theoretical_stat_rate(27, 'Elf',    'Cleric', 2400, 'def')

    growth = {
        # Three Goblin Clerics with def growth — 3 observations in one bucket
        'Carrot':    {'atk_per_tick': 0,                 'def_per_tick': theo_gc_29_def * 0.60, 'spy_off_per_tick': 0, 'spy_def_per_tick': 0},
        'Mettalica': {'atk_per_tick': theo_gc_30_atk*0.4,'def_per_tick': theo_gc_30_def * 0.30, 'spy_off_per_tick': 0, 'spy_def_per_tick': 0},
        'Radagon':   {'atk_per_tick': 0,                 'def_per_tick': theo_gc_28_def * 0.80, 'spy_off_per_tick': 0, 'spy_def_per_tick': 0},
        # One Elf Cleric — different race bucket
        'ElfFriend': {'atk_per_tick': 0,                 'def_per_tick': theo_ec_27_def * 0.50, 'spy_off_per_tick': 0, 'spy_def_per_tick': 0},
        # Unknown demographics — should be skipped
        'UnknownBob':{'atk_per_tick': 500,               'def_per_tick': 0, 'spy_off_per_tick': 0, 'spy_def_per_tick': 0},
    }
    demos = {
        'Carrot':    (29, 'Goblin', 'Cleric', 2500),
        'Mettalica': (30, 'Goblin', 'Cleric', 2700),
        'Radagon':   (28, 'Goblin', 'Cleric', 2600),
        'ElfFriend': (27, 'Elf',    'Cleric', 2400),
        # UnknownBob: missing from demos → skipped
    }
    buckets = opt.compute_allocation_observations(growth, demos)

    gc_def = buckets.get(('Goblin', 'Cleric', 'def'), [])
    _assert(len(gc_def) == 3, f"Goblin/Cleric/def should have 3 observations: {gc_def}")
    gc_atk = buckets.get(('Goblin', 'Cleric', 'atk'), [])
    _assert(len(gc_atk) == 1, f"Goblin/Cleric/atk should have 1 observation (only Mettalica has +atk): {gc_atk}")
    ec_def = buckets.get(('Elf', 'Cleric', 'def'), [])
    _assert(len(ec_def) == 1, f"Elf/Cleric/def should have 1 observation: {ec_def}")

    # UnknownBob missing demographics → no bucket entries for him
    total = sum(len(v) for v in buckets.values())
    _assert(total == 5, f"total observations: {total} — should exclude UnknownBob")

    # All ratios should be in [0, 1] after clamping
    for k, ratios in buckets.items():
        for r in ratios:
            _assert(0.0 <= r <= opt.ALLOCATION_MAX_FRACTION,
                    f"ratio out of bounds for {k}: {r}")

    print("  ✅ compute_allocation_observations: bucketing, skip-unknown, ratio clamp")


def test_fit_median_with_min_samples_gate():
    """Buckets with fewer than ALLOCATION_MIN_SAMPLES should be dropped.
    Buckets at or above that threshold should get the median of their ratios."""
    buckets = {
        ('Goblin', 'Cleric', 'def'):  [0.10, 0.30, 0.50, 0.70, 0.90],  # median = 0.50
        ('Goblin', 'Cleric', 'atk'):  [0.05, 0.10, 0.15],              # median = 0.10
        ('Elf',    'Cleric', 'def'):  [0.20, 0.40],                    # 2 < min → drop
        ('Human',  'Fighter','atk'):  [0.80],                          # 1 < min → drop
        ('Undead', 'Thief',  'atk'):  [0.05, 0.15, 0.25, 0.35],        # even count, median = 0.20
    }
    fitted = opt.fit_allocation_fractions(buckets, min_samples=3)

    _assert(fitted.get(('Goblin', 'Cleric', 'def'))  == 0.50, f"GC/def: {fitted}")
    _assert(fitted.get(('Goblin', 'Cleric', 'atk'))  == 0.10, f"GC/atk: {fitted}")
    _assert(('Elf', 'Cleric', 'def')   not in fitted, f"Elf/Cleric/def should be dropped (2 samples): {fitted}")
    _assert(('Human','Fighter','atk')  not in fitted, f"Human/Fighter/atk should be dropped (1 sample): {fitted}")
    # Even-count median of [0.05,0.15,0.25,0.35] = (0.15+0.25)/2 = 0.20
    _assert(abs(fitted.get(('Undead','Thief','atk')) - 0.20) < 1e-9,
            f"Undead/Thief/atk median: {fitted}")
    print("  ✅ fit_allocation_fractions: median, even-count, min-samples gate")


def test_derive_growth_rate_uses_fitted_when_available():
    """derive_growth_rate() should read FITTED_ALLOCATIONS for the
    (race, class, stat) key when present, and fall back otherwise."""
    # Save + reset the module state so this test is self-contained
    saved = dict(opt.FITTED_ALLOCATIONS)
    try:
        # Clean slate — should use DEFAULT_ALLOCATION_FRACTION (0.5)
        opt.FITTED_ALLOCATIONS = {}
        default_def = opt.derive_growth_rate(29, 'Goblin', 'Cleric', 2500, 'def')
        _assert(default_def > 0, f"default rate should be positive: {default_def}")

        # Theoretical (100%) value
        theo_def = opt._theoretical_stat_rate(29, 'Goblin', 'Cleric', 2500, 'def')
        # With default 0.5 fraction, derive should return ≈ theo × 0.5
        _assert(abs(default_def - theo_def * 0.5) < 1e-6,
                f"default frac 0.5 check: derive={default_def} theo*.5={theo_def*0.5}")

        # Inject a fitted value — should win
        opt.FITTED_ALLOCATIONS = {('Goblin', 'Cleric', 'def'): 0.85}
        fitted_def = opt.derive_growth_rate(29, 'Goblin', 'Cleric', 2500, 'def')
        _assert(abs(fitted_def - theo_def * 0.85) < 1e-6,
                f"fitted frac 0.85 check: derive={fitted_def} theo*.85={theo_def*0.85}")
        _assert(fitted_def > default_def,
                f"fitted 0.85 > default 0.5: fitted={fitted_def} default={default_def}")

        # Miss: different class, should fall back to default
        miss = opt.derive_growth_rate(29, 'Goblin', 'Fighter', 2500, 'def')
        miss_theo = opt._theoretical_stat_rate(29, 'Goblin', 'Fighter', 2500, 'def')
        _assert(abs(miss - miss_theo * 0.5) < 1e-6,
                f"unfitted bucket should use default: {miss} vs {miss_theo*0.5}")

        print("  ✅ derive_growth_rate: fitted value wins, fallback to default on miss")
    finally:
        opt.FITTED_ALLOCATIONS = saved


def test_end_to_end_fit_affects_derive():
    """Integration: synthetic growth_rates + demographics → fit →
    derive_growth_rate immediately sees the new fraction."""
    saved = dict(opt.FITTED_ALLOCATIONS)
    try:
        opt.FITTED_ALLOCATIONS = {}

        # 4 synthetic players — fixed growth rates.  Ratios at lvl 29 / 2500 pop
        # will land in the Goblin/Cleric/def bucket.
        theo = opt._theoretical_stat_rate(29, 'Goblin', 'Cleric', 2500, 'def')
        # Build observed rates such that observed/theo = 0.20, 0.30, 0.40, 0.50
        growth = {
            'p1': {'atk_per_tick': 0, 'def_per_tick': theo * 0.20, 'spy_off_per_tick': 0, 'spy_def_per_tick': 0},
            'p2': {'atk_per_tick': 0, 'def_per_tick': theo * 0.30, 'spy_off_per_tick': 0, 'spy_def_per_tick': 0},
            'p3': {'atk_per_tick': 0, 'def_per_tick': theo * 0.40, 'spy_off_per_tick': 0, 'spy_def_per_tick': 0},
            'p4': {'atk_per_tick': 0, 'def_per_tick': theo * 0.50, 'spy_off_per_tick': 0, 'spy_def_per_tick': 0},
        }
        demos = {n: (29, 'Goblin', 'Cleric', 2500) for n in growth}

        buckets = opt.compute_allocation_observations(growth, demos)
        fitted  = opt.fit_allocation_fractions(buckets)
        opt.FITTED_ALLOCATIONS = fitted

        # Median of 4 values [0.20,0.30,0.40,0.50] = (0.30+0.40)/2 = 0.35
        gc_def = fitted.get(('Goblin', 'Cleric', 'def'))
        _assert(gc_def is not None, f"should have fitted Goblin/Cleric/def: {fitted}")
        _assert(abs(gc_def - 0.35) < 1e-6, f"median should be 0.35, got {gc_def}")

        # derive_growth_rate now uses 0.35, not 0.5
        derived = opt.derive_growth_rate(29, 'Goblin', 'Cleric', 2500, 'def')
        _assert(abs(derived - theo * 0.35) < 1e-6,
                f"derive should use fitted 0.35: got {derived} expected {theo*0.35}")

        print("  ✅ End-to-end: synthetic growth → fit → derive uses fitted fraction")
    finally:
        opt.FITTED_ALLOCATIONS = saved


def test_ratio_clamp_drops_outliers():
    """observed >> theoretical (ratio > 1.5) is dropped entirely — one bad
    sample (from a lvl-up artifact) shouldn't pull a median past 1.0."""
    theo = opt._theoretical_stat_rate(25, 'Human', 'Fighter', 2000, 'atk')
    growth = {
        'ok1':  {'atk_per_tick': theo * 0.30, 'def_per_tick': 0, 'spy_off_per_tick': 0, 'spy_def_per_tick': 0},
        'ok2':  {'atk_per_tick': theo * 0.40, 'def_per_tick': 0, 'spy_off_per_tick': 0, 'spy_def_per_tick': 0},
        'ok3':  {'atk_per_tick': theo * 0.50, 'def_per_tick': 0, 'spy_off_per_tick': 0, 'spy_def_per_tick': 0},
        'bad':  {'atk_per_tick': theo * 5.0,  'def_per_tick': 0, 'spy_off_per_tick': 0, 'spy_def_per_tick': 0},
    }
    demos = {n: (25, 'Human', 'Fighter', 2000) for n in growth}
    buckets = opt.compute_allocation_observations(growth, demos)
    hf_atk = buckets.get(('Human', 'Fighter', 'atk'), [])
    _assert(len(hf_atk) == 3, f"outlier should be dropped; got {hf_atk}")
    for r in hf_atk:
        _assert(r <= opt.ALLOCATION_MAX_FRACTION, f"clamp failed: {r}")
    print("  ✅ Ratio clamp: 5× outlier dropped, 3 clean observations kept")


def main():
    print("=" * 72)
    print("test_allocation_fit.py — Phase 2 empirical growth coefficients")
    print("=" * 72)
    test_theoretical_rate_positive_and_scales()
    test_compute_allocation_observations_bucketing()
    test_fit_median_with_min_samples_gate()
    test_derive_growth_rate_uses_fitted_when_available()
    test_end_to_end_fit_affects_derive()
    test_ratio_clamp_drops_outliers()
    print()
    print("✅ All 6 test cases passed.")


if __name__ == "__main__":
    main()
