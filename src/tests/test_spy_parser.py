"""
Test harness for optimizer.parse_spy_report() — rich-intel extraction.

Covers the three new sections added in Phase 1 (Armory Inventory, Buildings,
Battle Upgrades) plus regression coverage for the existing fields (combat
stats, army composition, fort HP, resources).

Fixtures are modelled on real spy reports — one HTML blob per target, where
the text content after _page_text() flattening matches what the game serves.

Run:  python test_spy_parser.py
"""
import sys, os

_HERE       = os.path.dirname(os.path.abspath(__file__))
_SRC        = os.path.dirname(_HERE)
_DARKTHRONE = os.path.dirname(_SRC)
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


class FakePage:
    """Minimal stand-in for a Playwright Page — parse_spy_report() only
    ever calls page.content() (via _page_text), so that's all we need."""
    def __init__(self, html: str):
        self._html = html
    def content(self) -> str:
        return self._html


def _build_spy_html(
    *, name: str, level: int, race: str, cls: str,
    fort_hp: int, fort_max: int,
    atk: int, deff: int, spy_off: int, spy_def: int,
    gold: int, bank: int, citizens: int,
    army: dict, armory: dict, buildings: dict, upgrades: dict,
) -> str:
    """Assemble a simplified spy-report HTML that flattens via _page_text
    to the same plain-text layout parse_spy_report() expects from the real
    game page.  Table rows become space-separated cells after tag-stripping."""
    def _armory_row(item, tier, category, qty, bonus, total):
        return f"<tr><td>T{tier} {item}</td><td>{category}</td><td>{qty:,}</td><td>{bonus}</td><td>{total:,}</td></tr>"
    def _army_row(unit, role, qty, bonus, total):
        return f"<tr><td>T1 {unit}</td><td>{role}</td><td>{qty:,}</td><td>{bonus}</td><td>{total:,}</td></tr>"
    def _bld(bname, lvl):
        return f"<span>{bname} Level {lvl}</span>"
    def _upg_row(uname, kind, qty, bonus):
        return f"<tr><td>{uname}</td><td>Battle Upgrades - {kind}</td><td>{qty:,}</td><td>{bonus}</td></tr>"

    return f"""
    <html><body>
      <section>
        <h2>PROFILE &amp; COMBAT STATS</h2>
        <div>Operation Successful</div>
        <div>Intel gathered successfully - {name} !</div>
        <span>Level {level} Race {race} Class {cls}</span>
        <span>Fort HP {fort_hp:,} / {fort_max:,}</span>
        <span>Total Offense {atk:,} Total Defense {deff:,} Spy Offense {spy_off:,} Spy Defense {spy_def:,}</span>
      </section>
      <section>
        <h2>Resources</h2>
        <span>Gold on Hand {gold:,} Gold in Bank {bank:,} Citizens {citizens:,}</span>
      </section>
      <section>
        <h2>Army Composition</h2>
        <table>
          {_army_row("Expert Miner",  "Workers",             army.get("workers",  0), 150, army.get("workers", 0) * 150)}
          {_army_row("Knight",        "Offensive Military",  army.get("soldiers", 0),  20, army.get("soldiers",0) * 20)}
          {_army_row("Archer",        "Defensive Military",  army.get("guards",   0),  20, army.get("guards",  0) * 20)}
          {_army_row("Spy",           "Spy Offense",         army.get("spies",    0),   5, army.get("spies",   0) * 5)}
          {_army_row("Sentry",        "Spy Defense",         army.get("sentries", 0),   5, army.get("sentries",0) * 5)}
        </table>
      </section>
      <section>
        <h2>Armory Inventory</h2>
        <table>
          {''.join(_armory_row(item, d["tier"], d["category"], d["qty"], d["bonus"], d["qty"] * d["bonus"]) for item, d in armory.items())}
        </table>
      </section>
      <section>
        <h2>Buildings</h2>
        {''.join(_bld(b, lv) for b, lv in buildings.items())}
      </section>
      <section>
        <h2>Battle Upgrades</h2>
        <table>
          <tr><th>UPGRADE</th><th>CATEGORY</th><th>QUANTITY</th><th>BONUS</th></tr>
          {''.join(_upg_row(u, d["kind"], d["qty"], d["bonus"]) for u, d in upgrades.items())}
        </table>
      </section>
    </body></html>
    """


# ── Fixture: Carrot spy report (2026-04-16 20:23) ──────────────────────────
CARROT = dict(
    name="Carrot",
    level=29, race="Elf", cls="Cleric",
    fort_hp=2_233, fort_max=3_000,
    atk=188_264, deff=359_970, spy_off=414, spy_def=2_130,
    gold=100_856, bank=0, citizens=44,
    army=dict(workers=5_200, soldiers=390, guards=695, spies=79, sentries=76),
    armory={
        "Short Sword":         {"tier": 5, "category": "Offense Weapons",      "qty": 301, "bonus": 200},
        "Iron Chainmail":      {"tier": 5, "category": "Offense Armor",        "qty": 304, "bonus": 180},
        "Javelin":             {"tier": 6, "category": "Defense Weapons",      "qty": 600, "bonus": 150},
        "Bronze Chainmail":    {"tier": 6, "category": "Defense Armor",        "qty": 600, "bonus": 120},
        "Mace":                {"tier": 3, "category": "Spy Defense Weapons",  "qty":  23, "bonus":  50},
        "Studded Guard Armor": {"tier": 3, "category": "Spy Defense Armor",    "qty":  12, "bonus":  50},
    },
    buildings={
        "Fortification":   2, "Armory":     1, "Mine":    2,
        "Spy Academy":     1, "Barracks":   1, "Housing": 2,
        "Mercenary Camp":  2,
    },
    upgrades={
        "Steed":       {"kind": "Offense", "qty": 250, "bonus": 200},
        "Guard Tower": {"kind": "Defense", "qty": 505, "bonus": 200},
    },
)


# ── Fixture: Tycoon (4 combat stats only, sparser report) ─────────────────
TYCOON = dict(
    name="Tycoon",
    level=28, race="Goblin", cls="Cleric",
    fort_hp=2_363, fort_max=3_000,
    atk=281_610, deff=388_584, spy_off=22_018, spy_def=31_370,
    gold=0, bank=0, citizens=0,
    army=dict(workers=0, soldiers=0, guards=0, spies=0, sentries=0),
    armory={},
    buildings={},
    upgrades={},
)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_carrot_full_extraction():
    """Every field from a rich spy report round-trips through the parser."""
    html = _build_spy_html(**CARROT)
    out = opt.parse_spy_report(FakePage(html))

    # Basic metadata
    _assert(out["result"] == "spy_ok",               f"result: {out.get('result')}")
    _assert(out["target_name"] == "Carrot",          f"name: {out.get('target_name')}")
    _assert(out["target_level"] == 29,               f"level: {out.get('target_level')}")
    _assert(out["target_race"] == "Elf",             f"race: {out.get('target_race')}")
    _assert(out["target_class"] == "Cleric",         f"class: {out.get('target_class')}")

    # Fort
    _assert(out["target_fort_hp"]  == 2_233,         f"fort_hp: {out.get('target_fort_hp')}")
    _assert(out["target_fort_max"] == 3_000,         f"fort_max: {out.get('target_fort_max')}")

    # Combat stats
    _assert(out["target_atk"]     == 188_264,        f"atk: {out.get('target_atk')}")
    _assert(out["target_def"]     == 359_970,        f"def: {out.get('target_def')}")
    _assert(out["target_spy_off"] ==     414,        f"spy_off: {out.get('target_spy_off')}")
    _assert(out["target_spy_def"] ==   2_130,        f"spy_def: {out.get('target_spy_def')}")

    # Resources
    _assert(out["target_gold"]     == 100_856,       f"gold: {out.get('target_gold')}")
    _assert(out["target_bank"]     ==       0,       f"bank: {out.get('target_bank')}")
    _assert(out["target_citizens"] ==      44,       f"citizens: {out.get('target_citizens')}")

    # Army composition
    _assert(out["target_workers"]  == 5_200,         f"workers: {out.get('target_workers')}")
    _assert(out["target_soldiers"] ==   390,         f"soldiers: {out.get('target_soldiers')}")
    _assert(out["target_guards"]   ==   695,         f"guards: {out.get('target_guards')}")
    _assert(out["target_spies"]    ==    79,         f"spies: {out.get('target_spies')}")
    _assert(out["target_sentries"] ==    76,         f"sentries: {out.get('target_sentries')}")

    # Armory — 6 items, each with tier/category/qty/bonus
    armory = out.get("target_armory", {})
    _assert(len(armory) == 6, f"armory count: {len(armory)} want 6, got {list(armory.keys())}")
    _assert(armory["Short Sword"]["tier"]     == 5,                   f"Short Sword tier: {armory['Short Sword']}")
    _assert(armory["Short Sword"]["category"] == "Offense Weapons",   f"Short Sword category")
    _assert(armory["Short Sword"]["qty"]      == 301,                 f"Short Sword qty")
    _assert(armory["Short Sword"]["bonus"]    == 200,                 f"Short Sword bonus")
    _assert(armory["Javelin"]["tier"]     == 6,                       f"Javelin tier")
    _assert(armory["Javelin"]["category"] == "Defense Weapons",       f"Javelin category")
    _assert(armory["Javelin"]["qty"]      == 600,                     f"Javelin qty")
    _assert(armory["Studded Guard Armor"]["category"] == "Spy Defense Armor",
            f"Studded Guard Armor category: {armory.get('Studded Guard Armor')}")
    _assert(armory["Mace"]["category"] == "Spy Defense Weapons",
            f"Mace category: {armory.get('Mace')}")

    # Buildings — snake_case keys, integer values
    bld = out.get("target_buildings", {})
    _assert(bld.get("fortification")   == 2, f"fortification: {bld.get('fortification')}")
    _assert(bld.get("armory")          == 1, f"armory: {bld.get('armory')}")
    _assert(bld.get("mine")            == 2, f"mine: {bld.get('mine')}")
    _assert(bld.get("spy_academy")     == 1, f"spy_academy: {bld.get('spy_academy')}")
    _assert(bld.get("barracks")        == 1, f"barracks: {bld.get('barracks')}")
    _assert(bld.get("housing")         == 2, f"housing: {bld.get('housing')}")
    _assert(bld.get("mercenary_camp")  == 2, f"mercenary_camp: {bld.get('mercenary_camp')}")
    _assert(len(bld) == 7, f"buildings count: {len(bld)} want 7")

    # Battle Upgrades
    upg = out.get("target_upgrades", {})
    _assert(len(upg) == 2,                              f"upgrades count: {len(upg)}")
    _assert(upg["Steed"]["kind"]  == "Offense",         f"Steed kind")
    _assert(upg["Steed"]["qty"]   == 250,               f"Steed qty")
    _assert(upg["Steed"]["bonus"] == 200,               f"Steed bonus")
    _assert(upg["Guard Tower"]["kind"]  == "Defense",   f"Guard Tower kind")
    _assert(upg["Guard Tower"]["qty"]   == 505,         f"Guard Tower qty")

    print("  ✅ Carrot full-extraction: all 30+ fields round-trip correctly")


def test_tycoon_sparse_report():
    """Sparse reports (no armory/buildings/upgrades) still parse cleanly."""
    html = _build_spy_html(**TYCOON)
    out = opt.parse_spy_report(FakePage(html))

    _assert(out["result"] == "spy_ok",               f"result: {out.get('result')}")
    _assert(out["target_name"] == "Tycoon",          f"name: {out.get('target_name')}")
    _assert(out["target_atk"] == 281_610,            f"atk: {out.get('target_atk')}")
    _assert(out["target_def"] == 388_584,            f"def: {out.get('target_def')}")

    # Optional sections absent → dict keys should NOT be set (not empty-dict)
    _assert("target_armory" not in out,              "target_armory should be absent when empty")
    _assert("target_buildings" not in out,           "target_buildings should be absent when empty")
    _assert("target_upgrades" not in out,            "target_upgrades should be absent when empty")

    print("  ✅ Tycoon sparse-report: combat stats OK, optional sections absent")


def test_building_name_disambiguation():
    """The 'Armory Inventory' section header contains the word 'Armory'.
    The Buildings parser must NOT grab that as a building match.  We test
    this by giving the player a real Armory Level in the Buildings section
    and ensuring only that value is returned."""
    html = _build_spy_html(**CARROT)
    out = opt.parse_spy_report(FakePage(html))
    # Carrot has Armory Level 1 in the real screenshot
    _assert(out["target_buildings"]["armory"] == 1,
            f"armory level should be 1, got {out['target_buildings'].get('armory')}")
    print("  ✅ Building-name disambiguation: 'Armory Inventory' header doesn't pollute Buildings output")


def test_realistic_armory_with_column_headers():
    """The real game renders armory/upgrades tables with column-header rows
    ('ITEM CATEGORY QUANTITY BONUS TOTAL' / 'UPGRADE CATEGORY QUANTITY BONUS').
    My simplified fixture omits them from the armory — real pages include them
    and the parser must skip over the header row without false-matching."""
    # Build HTML manually with explicit <th> headers on every table
    html = """
    <html><body>
      <div>Operation Successful</div>
      <div>Intel gathered successfully - TestPlayer !</div>
      <span>Level 25 Race Human Class Fighter</span>
      <span>Fort HP 1,500 / 2,000</span>
      <span>Total Offense 150,000 Total Defense 120,000 Spy Offense 500 Spy Defense 400</span>
      <span>Gold on Hand 5,000 Gold in Bank 0 Citizens 100</span>
      <h2>Army Composition</h2>
      <table>
        <tr><th>UNIT</th><th>TYPE</th><th>QUANTITY</th><th>BONUS</th><th>TOTAL</th></tr>
        <tr><td>Expert Miner</td><td>Workers</td><td>1,000</td><td>150</td><td>150,000</td></tr>
      </table>
      <h2>Armory Inventory</h2>
      <table>
        <tr><th>ITEM</th><th>CATEGORY</th><th>QUANTITY</th><th>BONUS</th><th>TOTAL</th></tr>
        <tr><td>T3 Dagger</td><td>Offense Weapons</td><td>50</td><td>100</td><td>5,000</td></tr>
        <tr><td>T2 Leather</td><td>Defense Armor</td><td>75</td><td>60</td><td>4,500</td></tr>
      </table>
      <h2>Buildings</h2>
      <div>Fortification Level 1 Mine Level 1</div>
      <h2>Battle Upgrades</h2>
      <table>
        <tr><th>UPGRADE</th><th>CATEGORY</th><th>QUANTITY</th><th>BONUS</th></tr>
        <tr><td>Steed</td><td>Battle Upgrades - Offense</td><td>10</td><td>200</td></tr>
      </table>
    </body></html>
    """
    out = opt.parse_spy_report(FakePage(html))
    _assert(out["result"] == "spy_ok", f"result: {out.get('result')}")

    # Armory: 2 items, no phantom matches on 'ITEM CATEGORY' header
    armory = out.get("target_armory", {})
    _assert(len(armory) == 2, f"armory count: {len(armory)} want 2: {list(armory.keys())}")
    _assert("Dagger" in armory,  f"Dagger missing: {list(armory.keys())}")
    _assert("Leather" in armory, f"Leather missing: {list(armory.keys())}")
    _assert(armory["Dagger"]["tier"] == 3, f"Dagger tier")

    # Buildings: 2 items, no phantom match on 'CATEGORY' or 'ITEM'
    bld = out.get("target_buildings", {})
    _assert(bld.get("fortification") == 1, f"fortification: {bld.get('fortification')}")
    _assert(bld.get("mine") == 1,          f"mine: {bld.get('mine')}")

    # Upgrades: 1 item, header row skipped cleanly
    upg = out.get("target_upgrades", {})
    _assert(len(upg) == 1, f"upgrades count: {len(upg)}: {list(upg.keys())}")
    _assert("Steed" in upg, f"Steed missing: {list(upg.keys())}")
    _assert(upg["Steed"]["kind"] == "Offense", f"Steed kind")

    print("  ✅ Realistic HTML with <th> column headers: no false matches on header text")


def test_spy_defense_category_priority():
    """'Spy Defense Armor' must match before the shorter 'Defense Armor'
    pattern — otherwise Studded Guard Armor would be classified as Defense
    Armor, which is wrong and would corrupt downstream DEF estimates."""
    html = _build_spy_html(**CARROT)
    out = opt.parse_spy_report(FakePage(html))
    sga = out["target_armory"]["Studded Guard Armor"]
    _assert(sga["category"] == "Spy Defense Armor",
            f"Studded Guard Armor should be Spy Defense Armor, got {sga['category']}")
    mace = out["target_armory"]["Mace"]
    _assert(mace["category"] == "Spy Defense Weapons",
            f"Mace should be Spy Defense Weapons, got {mace['category']}")
    print("  ✅ Category priority: Spy-prefixed categories match before bare Defense/Offense")


def test_snapshot_round_trip(tmp_dir):
    """parse_spy_report → record_target_intel → load_target_intel_snapshot:
    every rich field survives the JSON round-trip."""
    prev_cwd = os.getcwd()
    os.chdir(tmp_dir)
    try:
        html = _build_spy_html(**CARROT)
        entry = opt.parse_spy_report(FakePage(html))
        opt.record_target_intel(entry)

        snap = opt.load_target_intel_snapshot()
        _assert("Carrot" in snap, f"Carrot missing from snapshot: {list(snap.keys())}")
        rec = snap["Carrot"]

        # Scalar fields
        _assert(rec.get("level") == 29,           f"level: {rec.get('level')}")
        _assert(rec.get("race")  == "Elf",        f"race: {rec.get('race')}")
        _assert(rec.get("atk")   == 188_264,      f"atk: {rec.get('atk')}")
        _assert(rec.get("def")   == 359_970,      f"def: {rec.get('def')}")
        _assert(rec.get("fort_hp")  == 2_233,     f"fort_hp: {rec.get('fort_hp')}")
        _assert(rec.get("fort_max") == 3_000,     f"fort_max: {rec.get('fort_max')}")
        _assert(rec.get("gold") == 100_856,       f"gold: {rec.get('gold')}")
        _assert(rec.get("citizens") == 44,        f"citizens: {rec.get('citizens')}")
        _assert("captured_at" in rec,             "captured_at missing")

        # Nested dicts
        army = rec.get("army", {})
        _assert(army.get("workers")  == 5_200,    f"army.workers: {army}")
        _assert(army.get("soldiers") ==   390,    f"army.soldiers: {army}")
        _assert(army.get("sentries") ==    76,    f"army.sentries: {army}")

        armory = rec.get("armory", {})
        _assert(len(armory) == 6,                 f"armory count: {len(armory)}")
        _assert(armory["Javelin"]["qty"] == 600,  f"Javelin qty after round-trip")
        _assert(armory["Short Sword"]["category"] == "Offense Weapons", "Short Sword category")

        bld = rec.get("buildings", {})
        _assert(bld.get("mercenary_camp") == 2,   f"mercenary_camp level: {bld}")
        _assert(len(bld) == 7,                    f"buildings count: {len(bld)}")

        upg = rec.get("upgrades", {})
        _assert(upg["Steed"]["qty"] == 250,       f"Steed qty after round-trip")
        _assert(upg["Guard Tower"]["bonus"] == 200, "Guard Tower bonus")

        print("  ✅ Snapshot round-trip: every rich field survives parse → record → load")
    finally:
        os.chdir(prev_cwd)


def test_sparse_report_preserves_prior_rich_data(tmp_dir):
    """If Carrot is spied fully (rich intel), then ATTACKED later (sparse
    entry — no armory/buildings/upgrades), the snapshot must RETAIN the
    earlier rich data rather than clobber it with blanks.  This matters
    because full spy reports are expensive; a quick attack shouldn't
    nuke the richer state we previously harvested."""
    prev_cwd = os.getcwd()
    os.chdir(tmp_dir)
    try:
        # 1. Full spy — capture everything
        html1 = _build_spy_html(**CARROT)
        entry1 = opt.parse_spy_report(FakePage(html1))
        opt.record_target_intel(entry1)

        # 2. Sparse attack-style entry — only combat stats, no rich fields
        entry2 = {
            "source":      "attack",
            "target_name": "Carrot",
            "target_def":  370_000,   # slightly higher — Carrot grew
            "target_level": 29,
        }
        opt.record_target_intel(entry2)

        snap = opt.load_target_intel_snapshot()
        rec = snap["Carrot"]

        # Combat stat was updated
        _assert(rec["def"] == 370_000, f"def should reflect attack update: {rec['def']}")

        # Rich data from the earlier spy SURVIVES
        _assert(rec.get("armory", {}).get("Javelin", {}).get("qty") == 600,
                f"armory should survive sparse update: {rec.get('armory')}")
        _assert(rec.get("buildings", {}).get("mine") == 2,
                f"buildings should survive sparse update: {rec.get('buildings')}")
        _assert(rec.get("upgrades", {}).get("Steed", {}).get("qty") == 250,
                f"upgrades should survive sparse update: {rec.get('upgrades')}")
        print("  ✅ Sparse follow-up preserves prior rich data (merge semantics)")
    finally:
        os.chdir(prev_cwd)


def test_load_intel_overlay_merges_rich(tmp_dir):
    """load_intel_overlay() should merge rich JSON snapshot data into its
    return dict — otherwise the nested fields from spy reports never reach
    calibrate_models / estimate()."""
    prev_cwd = os.getcwd()
    os.chdir(tmp_dir)
    try:
        # Record a full Carrot spy — populates both CSV and JSON
        html = _build_spy_html(**CARROT)
        entry = opt.parse_spy_report(FakePage(html))
        opt.record_intel(entry)   # writes private_intel.csv + private_target_intel.json

        overlay = opt.load_intel_overlay()
        _assert("Carrot" in overlay, f"Carrot missing from overlay: {list(overlay.keys())}")
        rec = overlay["Carrot"]

        # Scalar fields from CSV path
        _assert(rec["level"] == 29,         f"level: {rec.get('level')}")
        _assert(rec["atk"]   == 188_264,    f"atk: {rec.get('atk')}")
        _assert(rec["def"]   == 359_970,    f"def: {rec.get('def')}")

        # Fort state — newly surfaced scalars that the old loader dropped
        _assert(rec["fort_hp"]  == 2_233,   f"fort_hp: {rec.get('fort_hp')}")
        _assert(rec["fort_max"] == 3_000,   f"fort_max: {rec.get('fort_max')}")

        # Flat army counts from CSV
        _assert(rec["workers"]  == 5_200,   f"workers: {rec.get('workers')}")
        _assert(rec["soldiers"] ==   390,   f"soldiers: {rec.get('soldiers')}")

        # Nested dicts from rich JSON — should be present after merge
        _assert("armory" in rec,            f"armory nested dict missing from overlay row")
        _assert(rec["armory"]["Javelin"]["qty"] == 600, "Javelin.qty via overlay")
        _assert("buildings" in rec,         f"buildings dict missing from overlay row")
        _assert(rec["buildings"]["mine"] == 2, f"mine level via overlay")
        _assert("upgrades" in rec,          f"upgrades dict missing from overlay row")
        _assert(rec["upgrades"]["Steed"]["qty"] == 250, "Steed via overlay")

        print("  ✅ load_intel_overlay: scalar (CSV) + nested (JSON) data merged cleanly")
    finally:
        os.chdir(prev_cwd)


class FakePageWithNav:
    """Richer Page stub that supports goto() + evaluate() + wait_for_load_state.
    Used to simulate scrape_spy_logs() walking /spy/logs → /spy/log/{id}.
    `log_pages` maps URL → HTML string; missing URLs raise to simulate
    404-style failures that the harvester should survive."""
    def __init__(self, log_pages: dict, log_ids: list):
        self._pages    = log_pages         # {url: html}
        self._log_ids  = log_ids           # what /spy/logs evaluate() returns
        self._current  = ""
        self.goto_calls = []
    def goto(self, url):
        self.goto_calls.append(url)
        self._current = url
        if url not in self._pages:
            raise RuntimeError(f"FakePageWithNav: no fixture for {url}")
    def wait_for_load_state(self, *a, **kw):
        pass
    def evaluate(self, js):
        # scrape_spy_logs() only calls evaluate() on the /spy/logs list page
        # to get the array of log IDs.  Return our fixture.
        return list(self._log_ids)
    def content(self):
        return self._pages.get(self._current, "")


def test_spy_logs_harvest_dedup(tmp_dir):
    """End-to-end harvest: first run grabs both logs; second run is a no-op
    because the seen-ids state file blocks re-fetching."""
    prev_cwd = os.getcwd()
    os.chdir(tmp_dir)
    try:
        # Two fixture spy reports — Carrot + Tycoon
        carrot_html = _build_spy_html(**CARROT)
        tycoon_html = _build_spy_html(**TYCOON)
        base = opt.BASE_URL

        log_pages = {
            f"{base}/spy/logs":      "<html><body>(list page — parsed via JS)</body></html>",
            f"{base}/spy/log/1001":  carrot_html,
            f"{base}/spy/log/1002":  tycoon_html,
        }
        page = FakePageWithNav(log_pages, log_ids=["1001", "1002"])

        stats1 = opt.scrape_spy_logs(page, "2026-04-16 22:00", log_fn=lambda m,t="info": None)
        _assert(stats1["new"] == 2,                f"first pass: {stats1}")
        _assert(stats1["seen_before"] == 0,        f"seen_before should be 0 on first pass: {stats1}")

        # Both log IDs now in the seen file
        seen = opt._load_seen_spy_logs()
        _assert("1001" in seen and "1002" in seen, f"seen_ids not persisted: {seen}")

        # Both players landed in the rich snapshot
        snap = opt.load_target_intel_snapshot()
        _assert("Carrot" in snap, f"Carrot missing from snapshot: {list(snap.keys())}")
        _assert("Tycoon" in snap, f"Tycoon missing from snapshot: {list(snap.keys())}")
        _assert(snap["Carrot"]["source"] == "spy_log_history",
                f"source should mark spy_log_history: {snap['Carrot'].get('source')}")

        # Second pass — same page, same IDs: nothing new.
        page2 = FakePageWithNav(log_pages, log_ids=["1001", "1002"])
        stats2 = opt.scrape_spy_logs(page2, "2026-04-16 22:30", log_fn=lambda m,t="info": None)
        _assert(stats2["new"] == 0,              f"second pass should find nothing new: {stats2}")
        _assert(stats2["seen_before"] == 2,      f"seen_before should be 2 on second pass: {stats2}")
        # Only the /spy/logs list page was hit — no per-log fetches
        per_log = [u for u in page2.goto_calls if "/spy/log/" in u]
        _assert(per_log == [], f"no per-log fetches expected on re-run, got: {per_log}")

        print("  ✅ Spy-log harvest: first-pass grabs 2, second-pass dedups to 0 fetches")
    finally:
        os.chdir(prev_cwd)


def test_spy_logs_per_tick_cap(tmp_dir):
    """If the listing returns more IDs than SPY_LOGS_MAX_PER_TICK, only the
    newest N are fetched this pass — remaining IDs catch up next tick."""
    prev_cwd = os.getcwd()
    os.chdir(tmp_dir)
    try:
        base = opt.BASE_URL
        cap  = opt.SPY_LOGS_MAX_PER_TICK
        # Build cap+5 fake IDs (2000..2000+cap+4), each pointing at Carrot fixture.
        ids = [str(2000 + i) for i in range(cap + 5)]
        carrot_html = _build_spy_html(**CARROT)
        log_pages = {f"{base}/spy/logs": "<html></html>"}
        for x in ids:
            log_pages[f"{base}/spy/log/{x}"] = carrot_html

        page = FakePageWithNav(log_pages, log_ids=ids)
        stats = opt.scrape_spy_logs(page, "2026-04-16 22:45", log_fn=lambda m,t="info": None)

        _assert(stats["new"] == cap,
                f"first pass should fetch exactly {cap}, got {stats['new']}")

        seen = opt._load_seen_spy_logs()
        _assert(len(seen) == cap,
                f"seen file should have exactly {cap} entries, got {len(seen)}")

        # Newest-first ordering: the seen set should contain the top `cap`
        # numeric IDs, not the lowest ones.
        seen_nums = sorted((int(s) for s in seen), reverse=True)
        expected_top = sorted((int(x) for x in ids), reverse=True)[:cap]
        _assert(seen_nums == expected_top,
                f"newest-first ordering broken:\n  got={seen_nums}\n  want={expected_top}")

        print(f"  ✅ Spy-log per-tick cap: fetched top {cap} newest IDs, deferred rest")
    finally:
        os.chdir(prev_cwd)


def _make_tmp_dir():
    import tempfile
    return tempfile.mkdtemp(prefix="dt_spy_parser_test_")


def main():
    print("=" * 72)
    print("test_spy_parser.py — parse_spy_report() rich-intel extraction")
    print("=" * 72)
    test_carrot_full_extraction()
    test_tycoon_sparse_report()
    test_building_name_disambiguation()
    test_spy_defense_category_priority()
    test_realistic_armory_with_column_headers()

    # Round-trip tests need a temp dir for JSON writes (don't pollute cwd).
    tmp = _make_tmp_dir()
    try:
        test_snapshot_round_trip(tmp)
        test_sparse_report_preserves_prior_rich_data(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # Separate tmp for overlay test (it writes both CSV and JSON)
    tmp2 = _make_tmp_dir()
    try:
        test_load_intel_overlay_merges_rich(tmp2)
    finally:
        import shutil
        shutil.rmtree(tmp2, ignore_errors=True)

    # Fresh tmp dirs for each harvest test to isolate their state files.
    tmp3 = _make_tmp_dir()
    try:
        test_spy_logs_harvest_dedup(tmp3)
    finally:
        import shutil
        shutil.rmtree(tmp3, ignore_errors=True)

    tmp4 = _make_tmp_dir()
    try:
        test_spy_logs_per_tick_cap(tmp4)
    finally:
        import shutil
        shutil.rmtree(tmp4, ignore_errors=True)

    print()
    print("✅ All 10 test cases passed.")


if __name__ == "__main__":
    main()
