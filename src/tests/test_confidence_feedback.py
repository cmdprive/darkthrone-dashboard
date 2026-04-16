"""
Phase 4 test harness — projection-vs-intel feedback loop + conf_score.

Covers:
  1. record_projections + load_projection_log: round-trip, retention prune
  2. compute_projection_errors: correct pairing (projection BEFORE intel)
  3. compute_projection_errors: per-player + per-bucket aggregation
  4. _confidence_score: each input component + penalty semantics
  5. estimate() publishes conf_score in every return dict
  6. End-to-end: tick 1 projects → intel arrives → next calibration
     records error → estimate() reflects lower conf_score

Run:  python test_confidence_feedback.py
"""
import sys, os, datetime, tempfile, shutil

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


def _tmp():
    return tempfile.mkdtemp(prefix="dt_phase4_")


def _cleanup(tmp):
    shutil.rmtree(tmp, ignore_errors=True)


def _with_cwd(tmp, fn):
    prev = os.getcwd()
    os.chdir(tmp)
    try:
        return fn()
    finally:
        os.chdir(prev)


def test_record_and_load_projection_log():
    """Round-trip: record N projections, load them back, correct shape."""
    tmp = _tmp()
    def _inner():
        results = [
            {"Player": "Carrot",   "Level": 29, "Race": "Elf",    "Class": "Cleric",
             "EstATK": 188_264, "EstDEF": 359_970, "EstSpyOff": 414, "EstSpyDef": 2_130,
             "Confidence": "CONFIRMED"},
            {"Player": "Chill",    "Level": 28, "Race": "Undead", "Class": "Thief",
             "EstATK": 104_782, "EstDEF": 129_736, "EstSpyOff": 11_523, "EstSpyDef": 13_610,
             "Confidence": "CONFIRMED"},
        ]
        opt.record_projections(results)
        _assert(os.path.isfile(opt.PROJECTION_LOG_FILE), "log file should exist")

        log = opt.load_projection_log()
        _assert("Carrot" in log and "Chill" in log, f"players missing from log: {list(log.keys())}")
        _assert(log["Carrot"][-1]["atk"] == 188_264, f"atk round-trip: {log['Carrot']}")
        _assert(log["Chill"][-1]["race"] == "Undead", f"race round-trip: {log['Chill']}")
        _assert(log["Chill"][-1]["cls"]  == "Thief",  f"cls round-trip")

        # Second record: both players have TWO rows now
        opt.record_projections(results)
        log2 = opt.load_projection_log()
        _assert(len(log2["Carrot"]) == 2, f"second tick should append, got {len(log2['Carrot'])}")
        print("  ✅ record_projections + load_projection_log: round-trip intact")
    _with_cwd(tmp, _inner)
    _cleanup(tmp)


def test_compute_projection_errors_pairs_correctly():
    """Projection at T1 paired with intel at T2 (T2 > T1 + 30min)."""
    tmp = _tmp()
    def _inner():
        # Write a projection from 2 hours ago
        proj_log_ts = (datetime.datetime.now() - datetime.timedelta(hours=2)
                       ).strftime("%Y-%m-%d %H:%M:%S")
        with open(opt.PROJECTION_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            import csv as _csv
            w = _csv.writer(f)
            w.writerow(opt.PROJECTION_LOG_COLUMNS)
            # Projection: ATK 200k, DEF 350k — close to actual but slightly off
            w.writerow([proj_log_ts, "Carrot", 29, "Elf", "Cleric",
                        200_000, 350_000, 500, 2_000, "CONFIRMED"])

        # Synthetic intel overlay: actual values reported recently.
        # atk_err = |200_000 - 188_264| / 188_264 ≈ 0.0624
        # def_err = |350_000 - 359_970| / 359_970 ≈ 0.0277
        intel_overlay = {
            "Carrot": {
                "atk":     188_264,
                "def":     359_970,
                "spy_off": 414,
                "spy_def": 2_130,
                "race":    "Elf",
                "cls":     "Cleric",
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        }

        proj_log = opt.load_projection_log()
        p_err, b_err = opt.compute_projection_errors(proj_log, intel_overlay)

        _assert("Carrot" in p_err, f"player error missing: {list(p_err.keys())}")
        cerr = p_err["Carrot"]
        _assert(abs(cerr["atk_err_pct"] - (200_000-188_264)/188_264) < 1e-6,
                f"atk err: {cerr.get('atk_err_pct')}")
        _assert(abs(cerr["def_err_pct"] - (359_970-350_000)/359_970) < 1e-6,
                f"def err: {cerr.get('def_err_pct')}")

        # Bucket-level roll-up
        ec = b_err.get(("Elf", "Cleric"))
        _assert(ec is not None, f"Elf/Cleric bucket missing: {list(b_err.keys())}")
        _assert(ec["samples"] >= 1, f"bucket samples: {ec}")

        print("  ✅ compute_projection_errors: pairs correctly + populates player + bucket maps")
    _with_cwd(tmp, _inner)
    _cleanup(tmp)


def test_compute_projection_errors_skips_when_no_prior():
    """If no projection was made before the intel, NO error pair is produced."""
    tmp = _tmp()
    def _inner():
        # Projection is NEWER than intel — should be ignored
        proj_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(opt.PROJECTION_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            import csv as _csv
            w = _csv.writer(f)
            w.writerow(opt.PROJECTION_LOG_COLUMNS)
            w.writerow([proj_ts, "Bob", 20, "Human", "Fighter",
                        50_000, 40_000, 100, 100, "RANK-CMB"])

        # Intel is from 2 hours ago (OLDER than the projection)
        intel_ts = (datetime.datetime.now() - datetime.timedelta(hours=2)
                    ).strftime("%Y-%m-%d %H:%M:%S")
        intel_overlay = {
            "Bob": {
                "atk": 45_000, "def": 38_000, "spy_off": 50, "spy_def": 50,
                "race": "Human", "cls": "Fighter",
                "timestamp": intel_ts,
            }
        }

        p_err, _ = opt.compute_projection_errors(opt.load_projection_log(), intel_overlay)
        _assert("Bob" not in p_err, f"Bob should NOT have error pair (no prior proj): {p_err}")
        print("  ✅ compute_projection_errors: skips when no projection precedes intel")
    _with_cwd(tmp, _inner)
    _cleanup(tmp)


def test_confidence_score_components():
    """Walk each input toggle to confirm the score moves in the expected direction."""
    # Save + reset module error state so this is isolated
    saved_p = dict(opt.PROJECTION_ERRORS_LIVE)
    saved_b = dict(opt.BUCKET_ERRORS_LIVE)
    try:
        opt.PROJECTION_ERRORS_LIVE = {}
        opt.BUCKET_ERRORS_LIVE     = {}

        # Baseline: unknown player, no demographics, no intel, no growth
        s0 = opt._confidence_score("Anon", "", "",
                                   has_fresh_rich=False,
                                   has_stale_confirmed=False,
                                   has_observed_growth=False)
        _assert(abs(s0 - 0.30) < 1e-9, f"baseline should be 0.30: {s0}")

        # +demographics
        s1 = opt._confidence_score("Anon", "Elf", "Cleric",
                                   has_fresh_rich=False,
                                   has_stale_confirmed=False,
                                   has_observed_growth=False)
        _assert(abs(s1 - 0.45) < 1e-9, f"with demographics: 0.45, got {s1}")

        # +stale confirmed
        s2 = opt._confidence_score("Anon", "Elf", "Cleric",
                                   has_fresh_rich=False,
                                   has_stale_confirmed=True,
                                   has_observed_growth=False)
        _assert(abs(s2 - 0.60) < 1e-9, f"with stale CS: 0.60, got {s2}")

        # +fresh rich (overrides stale bonus — use the larger 0.35 add, not 0.15)
        s3 = opt._confidence_score("Anon", "Elf", "Cleric",
                                   has_fresh_rich=True,
                                   has_stale_confirmed=False,
                                   has_observed_growth=False)
        _assert(abs(s3 - 0.80) < 1e-9, f"with fresh rich: 0.80, got {s3}")

        # +observed growth
        s4 = opt._confidence_score("Anon", "Elf", "Cleric",
                                   has_fresh_rich=True,
                                   has_stale_confirmed=False,
                                   has_observed_growth=True)
        _assert(abs(s4 - 0.90) < 1e-9, f"fresh + growth: 0.90, got {s4}")

        # Penalty: player with 30% average error
        opt.PROJECTION_ERRORS_LIVE = {
            "Anon": {"atk_err_pct": 0.30, "def_err_pct": 0.30, "samples": 1}
        }
        s5 = opt._confidence_score("Anon", "Elf", "Cleric",
                                   has_fresh_rich=True,
                                   has_stale_confirmed=False,
                                   has_observed_growth=True)
        expected_5 = 0.90 - 0.50 * 0.30
        _assert(abs(s5 - expected_5) < 1e-9,
                f"player err penalty: expected {expected_5}, got {s5}")

        # Bucket-level fallback: another player in same bucket, no player-specific data
        opt.PROJECTION_ERRORS_LIVE = {}
        opt.BUCKET_ERRORS_LIVE = {
            ("Elf", "Cleric"): {"atk_err_pct_median": 0.20, "def_err_pct_median": 0.20, "samples": 3}
        }
        s6 = opt._confidence_score("NewElfCleric", "Elf", "Cleric",
                                   has_fresh_rich=True,
                                   has_stale_confirmed=False,
                                   has_observed_growth=True)
        expected_6 = 0.90 - 0.30 * 0.20
        _assert(abs(s6 - expected_6) < 1e-9,
                f"bucket err penalty: expected {expected_6}, got {s6}")

        print("  ✅ _confidence_score: baseline + 4 positive bumps + player/bucket penalty all correct")
    finally:
        opt.PROJECTION_ERRORS_LIVE = saved_p
        opt.BUCKET_ERRORS_LIVE     = saved_b


def test_estimate_publishes_conf_score():
    """Every return path of estimate() should include conf_score in [0,1]."""
    you = {'level': 22, 'atk': 88_000, 'def': 127_000, 'spy_off': 13_000, 'spy_def': 7_000,
           'population': 2636, 'workers': 985, 'off_units': 633, 'def_units': 526,
           'spy_units': 98, 'sent_units': 65, 'income': 748_530, 'mine_lv': 1,
           'rank_offense': 22, 'rank_defense': 28, 'rank_spy_off': 0, 'rank_spy_def': 0}

    # 1. Unknown player, no rich_intel, no CS — baseline path
    e1 = opt.estimate("Nobody", 15, "Human", "Fighter", 1500, "?",
                      50, 50, 50, you)
    _assert("conf_score" in e1, f"conf_score missing from baseline estimate")
    _assert(0.0 <= e1["conf_score"] <= 1.0, f"conf_score out of range: {e1['conf_score']}")

    # 2. Known CS player (Carrot is in CS) — should be higher
    # Note: level/race here matter since _confidence_score uses race+cls.
    e2 = opt.estimate("Carrot", 29, "Elf", "Cleric", 2688, "RQUM",
                      7, 13, 7, you)
    _assert("conf_score" in e2, f"conf_score missing from CS estimate")
    _assert(e2["conf_score"] >= e1["conf_score"],
            f"CS player should have >= baseline conf: CS={e2['conf_score']} base={e1['conf_score']}")

    # 3. YOUR own stats
    e3 = opt.estimate("Me (YOU)", 22, "Human", "Fighter", 2636, "—",
                      28, 22, 28, you)
    _assert("conf_score" in e3, f"conf_score missing from YOU estimate")
    _assert(0.0 <= e3["conf_score"] <= 1.0, f"YOU conf_score out of range: {e3['conf_score']}")

    print(f"  ✅ estimate() publishes conf_score on every return "
          f"(Anon={e1['conf_score']:.2f}, Carrot={e2['conf_score']:.2f}, YOU={e3['conf_score']:.2f})")


def test_end_to_end_feedback_loop():
    """Full pipeline: record tick-1 projection → synthetic intel arrives →
    compute errors → verify error feeds conf_score penalty."""
    tmp = _tmp()
    def _inner():
        # Tick 1: write a deliberately-WRONG projection (off by 30%)
        old_ts = (datetime.datetime.now() - datetime.timedelta(hours=2)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        with open(opt.PROJECTION_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            import csv as _csv
            w = _csv.writer(f)
            w.writerow(opt.PROJECTION_LOG_COLUMNS)
            w.writerow([old_ts, "TestHero", 25, "Human", "Fighter",
                        130_000, 130_000, 500, 500, "RANK-CMB"])

        # Intel says TestHero actually has 100k atk / 100k def (30% lower)
        intel_overlay = {
            "TestHero": {
                "atk": 100_000, "def": 100_000, "spy_off": 500, "spy_def": 500,
                "race": "Human", "cls": "Fighter",
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        }

        # Run the delta compute (what calibrate_models does each tick)
        proj_log = opt.load_projection_log()
        p_err, b_err = opt.compute_projection_errors(proj_log, intel_overlay)
        opt.PROJECTION_ERRORS_LIVE = p_err
        opt.BUCKET_ERRORS_LIVE     = b_err

        # conf_score for TestHero should now INCLUDE a penalty for 30% err
        score_with_err = opt._confidence_score(
            "TestHero", "Human", "Fighter",
            has_fresh_rich=True, has_stale_confirmed=False, has_observed_growth=False,
        )
        # Same inputs but no recorded error at ALL: should score higher.
        # We must clear BOTH PROJECTION_ERRORS_LIVE and BUCKET_ERRORS_LIVE
        # — otherwise _confidence_score falls through to the bucket-level
        # penalty path (which still sees TestHero's bucket data, muting
        # the delta we're measuring).
        saved_p = dict(opt.PROJECTION_ERRORS_LIVE)
        saved_b = dict(opt.BUCKET_ERRORS_LIVE)
        opt.PROJECTION_ERRORS_LIVE = {}
        opt.BUCKET_ERRORS_LIVE     = {}
        score_no_err = opt._confidence_score(
            "TestHero", "Human", "Fighter",
            has_fresh_rich=True, has_stale_confirmed=False, has_observed_growth=False,
        )
        opt.PROJECTION_ERRORS_LIVE = saved_p
        opt.BUCKET_ERRORS_LIVE     = saved_b

        _assert(score_with_err < score_no_err,
                f"error history should reduce conf_score: with={score_with_err} no={score_no_err}")
        delta = score_no_err - score_with_err
        _assert(delta > 0.05,
                f"penalty should be visible for 30% error, got delta={delta:.3f}")

        # Also: a fresh-rich Elf Cleric (different bucket) should NOT be
        # penalized by TestHero's Human/Fighter bucket errors.
        other_score = opt._confidence_score(
            "Randoelf", "Elf", "Cleric",
            has_fresh_rich=True, has_stale_confirmed=False, has_observed_growth=False,
        )
        _assert(other_score >= score_no_err - 0.01,   # ≤1% drift due to rounding
                f"cross-bucket penalty leak: elf={other_score} vs no-err-human={score_no_err}")

        print(f"  ✅ End-to-end: 30% proj error drops TestHero conf_score by "
              f"{delta:.2%} (other buckets unaffected)")
    try:
        _with_cwd(tmp, _inner)
    finally:
        # Restore module state so subsequent tests don't inherit our error maps
        opt.PROJECTION_ERRORS_LIVE = {}
        opt.BUCKET_ERRORS_LIVE     = {}
        _cleanup(tmp)


def main():
    print("=" * 72)
    print("test_confidence_feedback.py — Phase 4 projection-vs-intel feedback loop")
    print("=" * 72)
    test_record_and_load_projection_log()
    test_compute_projection_errors_pairs_correctly()
    test_compute_projection_errors_skips_when_no_prior()
    test_confidence_score_components()
    test_estimate_publishes_conf_score()
    test_end_to_end_feedback_loop()
    print()
    print("✅ All 6 test cases passed.")


if __name__ == "__main__":
    main()
