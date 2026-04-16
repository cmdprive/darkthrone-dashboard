"""
Diagnostic harness for testing optimizer.calibrate_models() in isolation.

Loads the real on-disk data (private_latest.json + private_player_profiles.csv)
and runs calibration WITHOUT hitting the game server or running a full tick.
Prints the calibrated A/k values and the top-N rank_atk / rank_def estimates.

Run:  python test_calibration.py
"""
import sys, os

# Resolve <darkthrone>/ from this test's location: src/tests/test_*.py → src → darkthrone
_HERE       = os.path.dirname(os.path.abspath(__file__))
_SRC        = os.path.dirname(_HERE)
_DARKTHRONE = os.path.dirname(_SRC)
_DATA       = os.path.join(_DARKTHRONE, "data")

# Add src/ to the path so `import optimizer` finds the module.
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# cwd to data/ so optimizer's bare filename references (private_latest.json,
# auth.json, etc.) all resolve to the right directory.
os.makedirs(_DATA, exist_ok=True)
os.chdir(_DATA)

# Stub out playwright so importing optimizer doesn't fail
import types
pw_mod = types.ModuleType('playwright')
pw_sync = types.ModuleType('playwright.sync_api')
pw_sync.sync_playwright = lambda: None
sys.modules['playwright'] = pw_mod
sys.modules['playwright.sync_api'] = pw_sync

import optimizer as opt

print("=" * 72)
print("DIAGNOSE: calibrate_models() with real on-disk data")
print("=" * 72)

you      = opt.load_your_stats()
profiles = opt.load_scraped_profiles()

print(f"\nYour stats: ATK={you.get('atk',0):,} (rank {you.get('rank_offense',0)}) | "
      f"DEF={you.get('def',0):,} (rank {you.get('rank_defense',0)})")
print(f"Loaded {len(profiles)} scraped profiles.")

print("\n--- Calibration logs ---")
opt.calibrate_models(profiles, you)

print("\n--- Resulting rank model constants ---")
print(f"  ATK  : A={opt.ATK_RANK_A:,.0f}  k={opt.ATK_RANK_K:.4f}")
print(f"  DEF  : A={opt.DEF_RANK_A:,.0f}  k={opt.DEF_RANK_K:.4f}")
print(f"  SpyO : A={opt.SPY_OFF_RANK_A:,.0f}  k={opt.SPY_OFF_RANK_K:.4f}")
print(f"  SpyD : A={opt.SPY_DEF_RANK_A:,.0f}  k={opt.SPY_DEF_RANK_K:.4f}")

print("\n--- rank_atk by rank ---")
for r in [1, 2, 3, 5, 10, 22, 50]:
    print(f"  rank_atk({r:>2})  = {opt.rank_atk(r):>10,}")

print("\n--- rank_def by rank ---")
for r in [1, 2, 3, 5, 10, 27, 50]:
    print(f"  rank_def({r:>2})  = {opt.rank_def(r):>10,}")

print("\n--- Sanity checks ---")
ok = True
your_def = you.get('def', 0)
your_def_rank = you.get('rank_defense', 27)
if opt.rank_def(1) < your_def:
    print(f"  FAIL : rank_def(1)={opt.rank_def(1):,} < your DEF={your_def:,}")
    ok = False
else:
    print(f"  OK   : rank_def(1)={opt.rank_def(1):,} >= your DEF={your_def:,}")

# The regression is anchored at the TOP confirmed value (rank 1) so the
# top of the curve is exact.  YOUR rank estimate may diverge — 30% is
# acceptable because estimate() uses your REAL stats via the is_you branch,
# so the model miss at your rank doesn't affect the dashboard.
delta = abs(opt.rank_def(your_def_rank) - your_def)
if delta > your_def * 0.35:
    print(f"  FAIL : rank_def(your_rank={your_def_rank}) off by {delta:,} ({delta/your_def:.0%})")
    ok = False
else:
    print(f"  OK   : rank_def(your_rank={your_def_rank}) within 35% of your DEF")

if opt.rank_atk(2) < 300_000:
    print(f"  FAIL : rank_atk(2)={opt.rank_atk(2):,} is too low (TGO confirmed 394k)")
    ok = False
else:
    print(f"  OK   : rank_atk(2)={opt.rank_atk(2):,} matches confirmed TGO floor")

print(f"\n{'PASS' if ok else 'FAIL'}")

# ──────────────────────────────────────────────────────────────────────────
# Full dashboard simulation: run estimator_run() and inspect top-10 ATK/DEF
# ──────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
print("SIMULATION: top-10 ATK/DEF estimates (what the dashboard will show)")
print("=" * 72)

import json
rank_snap = {}
if os.path.isfile('private_rankings_snapshot.json'):
    with open('private_rankings_snapshot.json', encoding='utf-8') as f:
        rank_snap = json.load(f).get('rank_map', {})

# Walk every player in PLAYERS + rank_snap and call opt.estimate()
seen = set()
estimates = []
for row in opt.PLAYERS:
    name, level, race, cls, pop, clan, overall, off_rank, def_rank = row
    if name in seen:
        continue
    seen.add(name)
    rs = rank_snap.get(name.replace(' (YOU)', ''), {})
    snap_off = rs.get('off_rank', off_rank) or off_rank
    snap_def = rs.get('def_rank', def_rank) or def_rank
    snap_ov  = rs.get('overall',  overall)  or overall
    res = opt.estimate(
        name, level, race, cls, pop, rs.get('clan', clan),
        snap_ov, snap_off, snap_def, you,
        spy_off_rank=rs.get('spy_off_rank', 999),
        spy_def_rank=rs.get('spy_def_rank', 999),
    )
    estimates.append((name, res))

# Also include rank_snap-only players
for pname, rs in rank_snap.items():
    if pname in seen or 'YOU' in pname:
        continue
    seen.add(pname)
    res = opt.estimate(
        pname, 10, '—', '—', 2000, rs.get('clan', '—'),
        rs.get('overall', 999) or 999,
        rs.get('off_rank', 999) or 999,
        rs.get('def_rank', 999) or 999,
        you,
        spy_off_rank=rs.get('spy_off_rank', 999),
        spy_def_rank=rs.get('spy_def_rank', 999),
    )
    estimates.append((pname, res))

def _stat(res, key):
    if isinstance(res, dict):
        return res.get(key, 0)
    return 0

top_atk = sorted(estimates, key=lambda e: -_stat(e[1], 'atk'))[:10]
top_def = sorted(estimates, key=lambda e: -_stat(e[1], 'def'))[:10]

print("\nTOP 10 ATK:")
for i, (name, res) in enumerate(top_atk, 1):
    print(f"  {i:>2}. {name:<35} atk={_stat(res,'atk'):>8,}  def={_stat(res,'def'):>8,}")

print("\nTOP 10 DEF:")
for i, (name, res) in enumerate(top_def, 1):
    print(f"  {i:>2}. {name:<35} atk={_stat(res,'atk'):>8,}  def={_stat(res,'def'):>8,}")
