"""
claude_strategy.py — Autonomous Claude decision engine for DarkThrone Suite.

Alternative to decide_v2.  Where decide_v2 codifies three human-written
strategies (Grow / Combat / Defend) and picks weights from them, this
engine ships raw game MECHANICS (facts: costs, formulas, tables) to the
Claude API each tick and lets the model generate its own reasoning,
goals, and actions.  The user-facing strategy profile is "claude-auto"
— select it in the GUI to route run_tick through this module.

Design principles
-----------------
* **Mechanics not playbooks** — Claude gets the rules (BASE_INC, tier
  unlocks, plunder maths).  It does NOT receive heuristics like "keep
  gold low" or "max def first".  Strategic judgement is Claude's job.
* **Continuity** — a JSON memo persists between ticks so Claude can
  pursue multi-tick goals instead of cold-starting every call.
* **Safety rails** — validator rejects malformed actions, total cost is
  capped at current gold, and any failure raises so run_tick falls back
  to decide_v2.  A daily USD budget hard-stops the strategy before
  runaway costs.
* **Observability** — every API call logs input/output tokens + cost;
  Claude's narrative reasoning is surfaced to the GUI log.

Runtime files (data dir):
  anthropic_key.txt       — fallback API key storage (env var wins)
  claude_memo.json        — per-tick strategy continuity
  claude_cost_log.csv     — token usage + cost per call

Callers:
  decide_claude(state, cats, rivals_top10, tick_num, log_fn)
      → (actions, memo, info) or raises RuntimeError on failure
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import re
from typing import Any


# ── Configuration ───────────────────────────────────────────────────────────
CLAUDE_MODEL            = "claude-sonnet-4-5"
CLAUDE_MAX_TOKENS_OUT   = 3000
CLAUDE_BUDGET_DAILY_USD = 5.0         # hard stop once daily spend exceeds this
CLAUDE_API_KEY_FILE     = "anthropic_key.txt"
CLAUDE_MEMO_FILE        = "claude_memo.json"
CLAUDE_COST_LOG         = "claude_cost_log.csv"
CLAUDE_NEXT_PLAN_FILE   = "claude_next_plan.json"

# Predictive-mode parameters.  The forward plan for next tick is saved at
# the END of each Claude decision — then when the next tick fires we
# execute those pre-decided actions BEFORE the slow scrape cycle, shrinking
# the gold-on-hand exposure window from 60-90s (reactive path) to 5-10s.
PLAN_STALE_HOURS         = 2.0     # >2h old = toss the plan (bot was probably paused)
PLAN_VARIANCE_THRESHOLD  = 0.30    # live gold differs from expected by > this → replan
                                    # 0.30 = 30%; tighter means more replans, more cost

# Per-tick caps — protect against runaway tool-use loops + runaway costs.
#
# MAX_TOOL_ITERATIONS: how many API roundtrips per tick.  Each iteration
#   may include multiple tool calls; loop exits when Claude returns text
#   (its final decision) instead of requesting more tools.
# PER_TICK_COST_CAP: dollars.  If a tick's cumulative cost crosses this,
#   we raise out and the caller falls back to decide_v2.  Sized so you
#   get ~3-4 "normal" ticks per dollar of daily budget.
# TOOL_RESULT_MAX_CHARS: each tool's JSON result is truncated to this
#   many characters before feeding it back to Claude — stops a tool that
#   pulls 200 rivals from eating the whole context window.
MAX_TOOL_ITERATIONS     = 8
PER_TICK_COST_CAP       = 0.50
TOOL_RESULT_MAX_CHARS   = 20_000
CLAUDE_COST_LOG_COLUMNS = [
    "Timestamp", "Model", "InputTokens", "OutputTokens",
    "CacheReads", "CacheWrites", "CostUSD", "Tick",
]

# Prices per million tokens, USD.  Update when Anthropic revises pricing.
CLAUDE_PRICING = {
    "claude-sonnet-4-5": {
        "input": 3.00, "output": 15.00,
        "cache_write": 3.75, "cache_read": 0.30,
    },
    "claude-opus-4-5": {
        "input": 15.00, "output": 75.00,
        "cache_write": 18.75, "cache_read": 1.50,
    },
}


# ── Tools (read-only info queries Claude can invoke mid-tick) ───────────────
# The initial context passed to Claude is deliberately compact.  If Claude
# needs more detail — "who is Jasbob specifically?", "what attacks did I
# run last night?", "who are all the lvl-25 Elves?" — it calls one of
# these tools.  Each is read-only: they touch local CSV/JSON files, never
# the game server.  For LIVE scraping (player profile refresh etc.) Claude
# can request a scrape that happens on the next tick via the memo's
# `open_questions` field; no sync game traffic during decision-making.
TOOLS = [
    {
        "name": "get_player_details",
        "description": (
            "Comprehensive intelligence on one specific player: current "
            "estimated stats (ATK/DEF/spy_off/spy_def) + confidence, rank "
            "positions, level/race/class/clan, per-stat confirmed values "
            "if we've directly spied them, rich intel snapshot (army "
            "composition, armory inventory, buildings, battle upgrades, "
            "fort HP) if available, and recent growth trajectory.  Use "
            "BEFORE attacking or spying a specific target, or when "
            "assessing a rival's real threat level."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Player name exactly as shown in-game."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_top_rivals",
        "description": (
            "Up to N players sorted by a stat, to survey the competitive "
            "landscape.  The initial prompt ships you top-10 by overall; "
            "use this when you need a different axis (e.g. top wealth, "
            "top spy offense) or a larger view."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sort_by":      {"type": "string",
                                 "enum": ["atk", "def", "overall", "wealth",
                                          "level", "spy_off", "spy_def"]},
                "limit":        {"type": "integer",
                                 "description": "default 15, max 50"},
                "include_bots": {"type": "boolean"},
            },
            "required": ["sort_by"],
        },
    },
    {
        "name": "search_players",
        "description": (
            "Find players matching filters.  All filters are AND.  Useful "
            "for building a targeted attack/spy list (e.g. 'lvl 20-30 "
            "Elves with DEF < 80k')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "level_min": {"type": "integer"},
                "level_max": {"type": "integer"},
                "atk_min":   {"type": "integer"},
                "atk_max":   {"type": "integer"},
                "def_min":   {"type": "integer"},
                "def_max":   {"type": "integer"},
                "race":      {"type": "string"},
                "class":     {"type": "string"},
                "clan":      {"type": "string"},
                "limit":     {"type": "integer",
                              "description": "default 20, max 50"},
            },
        },
    },
    {
        "name": "get_own_recent_history",
        "description": (
            "Your own last N ticks: gold / stats / army / action counts "
            "per tick.  Use to judge whether recent strategy is working "
            "(is ATK growing faster than rank?  did the last pivot help?)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticks": {"type": "integer",
                          "description": "default 10, max 50"},
            },
        },
    },
    {
        "name": "get_battle_log_recent",
        "description": (
            "Your recent attack + spy outcomes: target, result (win/loss/"
            "spy_ok/spy_fail), gold stolen, XP, losses.  Use to assess "
            "whether current ATK is enough to farm effectively, or which "
            "rivals are repeat-attacking you."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "default 20, max 100"},
            },
        },
    },
    {
        "name": "get_server_overview",
        "description": (
            "Server-wide summary: total tracked players, bot/non-bot "
            "split, your rank percentile, top 3 ATK + top 3 DEF, level "
            "distribution.  Quick sanity check of where you stand overall."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_action_costs",
        "description": (
            "Exact current-game prices for every action you can take.  "
            "Returns: per-unit training cost (worker/soldier/guard/spy/"
            "sentry), per-item gear cost for every (unit, slot, tier) "
            "combination with the item name and per-unit stat bonus, and "
            "next-level building costs already scraped live.  Use this "
            "BEFORE proposing BUY_GEAR or TRAIN actions — it gives you "
            "exact math instead of estimates, so you don't under- or "
            "over-budget.  You can optionally filter by unit_type to get "
            "a smaller result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_type": {
                    "type": "string",
                    "enum": ["worker","soldier","guard","spy","sentry"],
                    "description": "Filter gear prices to one unit only (saves tokens).",
                },
            },
        },
    },
    {
        "name": "get_incoming_attacks",
        "description": (
            "Recent attacks AGAINST you — who hit you, when, how much "
            "gold they plundered, your fort damage.  Use to assess "
            "whether you're being repeat-farmed and need defensive "
            "investment (fort, DEF gear, banking), or whether you're "
            "flying under the radar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "default 10, max 50"},
            },
        },
    },
]


# ── Tool executors ──────────────────────────────────────────────────────────
def _safe_read_csv(path: str) -> list:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _safe_read_json(path: str) -> Any:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _num(v, default=0) -> int:
    if v is None or v == "":
        return default
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def _tool_get_player_details(name: str) -> dict:
    """Assemble a dossier from every local data source we have for a player."""
    if not name:
        return {"error": "name required"}
    out: dict = {"name": name}
    name_lower = name.lower()

    # Estimates CSV — latest model prediction + confidence
    for r in _safe_read_csv("private_player_estimates.csv"):
        if (r.get("Player") or "").strip().lower() == name_lower:
            out["estimate"] = {
                "atk":       _num(r.get("EstATK")),
                "def":       _num(r.get("EstDEF")),
                "spy_off":   _num(r.get("EstSpyOff")),
                "spy_def":   _num(r.get("EstSpyDef")),
                "income":    _num(r.get("EstIncomeTick")),
                "level":     _num(r.get("Level")),
                "race":      (r.get("Race")  or "").strip(),
                "class":     (r.get("Class") or "").strip(),
                "clan":      (r.get("Clan")  or "").strip(),
                "conf_label":(r.get("Confidence") or "").strip(),
                "conf_score":float(r.get("ConfScore") or 0),
                "is_bot":    _num(r.get("IsBot")) == 1,
            }
            break

    # Ranking snapshot — live ranks from this tick's leaderboard scrape
    rank_snap = _safe_read_json("private_rankings_snapshot.json") or {}
    rm = (rank_snap.get("rank_map") or {})
    for key, entry in rm.items():
        if key.lower() == name_lower:
            out["ranks"] = {
                "overall":  entry.get("overall"),
                "offense":  entry.get("off_rank"),
                "defense":  entry.get("def_rank"),
                "clan":     entry.get("clan"),
                "player_id":entry.get("player_id"),
            }
            break

    # Rich per-target intel — spy-report-derived armory/buildings/upgrades
    rich = _safe_read_json("private_target_intel.json") or {}
    if isinstance(rich, dict) and name in rich:
        r = rich[name]
        out["rich_intel"] = {
            "captured_at": r.get("captured_at"),
            "source":      r.get("source"),
            "level":       r.get("level"),
            "race":        r.get("race"),
            "cls":         r.get("cls"),
            "combat": {
                "atk":     r.get("atk"),
                "def":     r.get("def"),
                "spy_off": r.get("spy_off"),
                "spy_def": r.get("spy_def"),
            },
            "resources": {
                "gold":     r.get("gold"),
                "bank":     r.get("bank"),
                "citizens": r.get("citizens"),
            },
            "fort":      {"hp": r.get("fort_hp"), "max": r.get("fort_max")},
            "army":      r.get("army"),
            "armory":    r.get("armory"),
            "buildings": r.get("buildings"),
            "upgrades":  r.get("upgrades"),
        }

    # Most-recent rows from raw intel CSV (history of spy captures)
    intel_rows = [r for r in _safe_read_csv("private_intel.csv")
                  if (r.get("Player") or "").strip().lower() == name_lower]
    if intel_rows:
        intel_rows.sort(key=lambda r: r.get("Timestamp", ""), reverse=True)
        out["intel_history"] = [{
            "ts":        r.get("Timestamp"),
            "source":    r.get("Source"),
            "atk":       _num(r.get("ATK")),
            "def":       _num(r.get("DEF")),
            "spy_off":   _num(r.get("SpyOff")),
            "spy_def":   _num(r.get("SpyDef")),
        } for r in intel_rows[:5]]

    # Growth trajectory (from player_growth.csv)
    growth_rows = [r for r in _safe_read_csv("private_player_growth.csv")
                   if (r.get("Player") or "").strip().lower() == name_lower]
    if growth_rows:
        growth_rows.sort(key=lambda r: r.get("Timestamp", ""))
        # Sample first + last + 1 midpoint to show trajectory without flooding
        samples = [growth_rows[0]]
        if len(growth_rows) > 2:
            samples.append(growth_rows[len(growth_rows)//2])
        if len(growth_rows) > 1:
            samples.append(growth_rows[-1])
        out["growth_samples"] = [{
            "ts":       r.get("Timestamp"),
            "level":    _num(r.get("Level")),
            "atk":      _num(r.get("EstATK")),
            "def":      _num(r.get("EstDEF")),
            "source":   r.get("Source", ""),
        } for r in samples]
        out["growth_observations"] = len(growth_rows)

    if len(out) == 1:   # only name — nothing found
        out["not_found"] = (
            f"No local data for '{name}' in estimates, ranks, intel, or growth. "
            "Spelling must match exactly as shown in-game."
        )
    return out


def _tool_list_top_rivals(sort_by: str = "overall",
                           limit: int = 15,
                           include_bots: bool = False) -> dict:
    rows = _safe_read_csv("private_player_estimates.csv")
    if not rows:
        return {"error": "no estimates available (run a tick first)"}

    def score(r: dict) -> int:
        atk = _num(r.get("EstATK"))
        d   = _num(r.get("EstDEF"))
        if sort_by == "atk":      return atk
        if sort_by == "def":      return d
        if sort_by == "overall":  return atk + d
        if sort_by == "wealth":   return _num(r.get("EstIncomeTick")) * 48
        if sort_by == "level":    return _num(r.get("Level"))
        if sort_by == "spy_off":  return _num(r.get("EstSpyOff"))
        if sort_by == "spy_def":  return _num(r.get("EstSpyDef"))
        return atk + d

    filtered = rows if include_bots else [r for r in rows if _num(r.get("IsBot")) != 1]
    # Also strip YOU so Claude doesn't inadvertently reason about self as rival
    filtered = [r for r in filtered if "(YOU)" not in (r.get("Player") or "")]

    filtered.sort(key=score, reverse=True)
    limit = max(1, min(int(limit or 15), 50))
    out = []
    for r in filtered[:limit]:
        out.append({
            "name":    (r.get("Player") or "").strip(),
            "level":   _num(r.get("Level")),
            "race":    (r.get("Race")  or "").strip(),
            "class":   (r.get("Class") or "").strip(),
            "clan":    (r.get("Clan")  or "").strip(),
            "atk":     _num(r.get("EstATK")),
            "def":     _num(r.get("EstDEF")),
            "spy_off": _num(r.get("EstSpyOff")),
            "spy_def": _num(r.get("EstSpyDef")),
            "income":  _num(r.get("EstIncomeTick")),
            "conf":    round(float(r.get("ConfScore") or 0), 2),
            "score":   score(r),
        })
    return {"sort_by": sort_by, "limit": limit, "results": out}


def _tool_search_players(**filters) -> dict:
    rows = _safe_read_csv("private_player_estimates.csv")
    if not rows:
        return {"error": "no estimates available"}
    want_race  = (filters.get("race")  or "").strip().lower()
    want_class = (filters.get("class") or "").strip().lower()
    want_clan  = (filters.get("clan")  or "").strip().lower()
    lmin = int(filters.get("level_min") or 0)
    lmax = int(filters.get("level_max") or 999)
    amin = int(filters.get("atk_min")   or 0)
    amax = int(filters.get("atk_max")   or 10**12)
    dmin = int(filters.get("def_min")   or 0)
    dmax = int(filters.get("def_max")   or 10**12)

    out = []
    for r in rows:
        name = (r.get("Player") or "").strip()
        if "(YOU)" in name:
            continue
        lvl = _num(r.get("Level"))
        if not (lmin <= lvl <= lmax):
            continue
        atk = _num(r.get("EstATK")); deff = _num(r.get("EstDEF"))
        if not (amin <= atk <= amax): continue
        if not (dmin <= deff <= dmax): continue
        if want_race  and (r.get("Race")  or "").strip().lower() != want_race:  continue
        if want_class and (r.get("Class") or "").strip().lower() != want_class: continue
        if want_clan  and (r.get("Clan")  or "").strip().lower() != want_clan:  continue
        out.append({
            "name":  name, "level": lvl,
            "race":  (r.get("Race")  or "").strip(),
            "class": (r.get("Class") or "").strip(),
            "clan":  (r.get("Clan")  or "").strip(),
            "atk":   atk, "def": deff,
            "spy_off": _num(r.get("EstSpyOff")),
            "spy_def": _num(r.get("EstSpyDef")),
        })
    limit = max(1, min(int(filters.get("limit") or 20), 50))
    return {"filters": filters, "match_count": len(out), "results": out[:limit]}


def _tool_get_own_recent_history(ticks: int = 10) -> dict:
    """Read last N tick snapshots from private_optimizer_growth.json."""
    g = _safe_read_json("private_optimizer_growth.json") or []
    if not isinstance(g, list) or not g:
        return {"error": "no own growth history yet"}
    ticks = max(1, min(int(ticks or 10), 50))
    recent = g[-ticks:]
    out = [{
        "tick":    r.get("tick"),
        "ts":      r.get("ts"),
        "level":   r.get("level"),
        "gold":    r.get("gold"),
        "bank":    r.get("bank"),
        "income":  r.get("income"),
        "atk":     r.get("atk"),
        "def":     r.get("def"),
        "spy_off": r.get("spy_off"),
        "spy_def": r.get("spy_def"),
        "army":    r.get("army"),
        "mine_lv": r.get("mine_lv"),
        "fort_lv": r.get("fort_lv"),
        "gold_spent": r.get("gold_spent"),
        "actions_summary": r.get("actions"),
    } for r in recent]
    # Deltas vs first snapshot, for quick trend assessment
    first = recent[0]; last = recent[-1]
    deltas = {
        "ticks_covered": len(recent),
        "atk_delta":  (last.get("atk", 0) or 0)  - (first.get("atk", 0) or 0),
        "def_delta":  (last.get("def", 0) or 0)  - (first.get("def", 0) or 0),
        "level_delta":(last.get("level", 0) or 0) - (first.get("level", 0) or 0),
        "gold_spent_total": sum(r.get("gold_spent", 0) or 0 for r in recent),
    }
    return {"recent": out, "deltas": deltas}


def _tool_get_battle_log_recent(limit: int = 20) -> dict:
    rows = _safe_read_csv("private_battle_log.csv")
    if not rows:
        return {"error": "no battle log yet"}
    rows.sort(key=lambda r: r.get("Timestamp", ""), reverse=True)
    limit = max(1, min(int(limit or 20), 100))
    out = []
    for r in rows[:limit]:
        out.append({
            "ts":         r.get("Timestamp"),
            "mode":       r.get("Mode"),
            "target":     r.get("Target"),
            "turns":      _num(r.get("Turns")),
            "gold_before":_num(r.get("GoldBefore")),
            "gold_after": _num(r.get("GoldAfter")),
            "gold_gained":_num(r.get("GoldGained")),
            "result":     r.get("Result"),
        })
    wins   = sum(1 for r in out if (r["result"] or "").lower() in ("win","spy_ok"))
    losses = sum(1 for r in out if (r["result"] or "").lower() in ("loss","spy_fail"))
    return {"recent": out, "wins": wins, "losses": losses,
            "total": len(out)}


def _tool_get_server_overview() -> dict:
    rows = _safe_read_csv("private_player_estimates.csv")
    if not rows:
        return {"error": "no estimates available"}
    total    = len(rows)
    bots     = sum(1 for r in rows if _num(r.get("IsBot")) == 1)
    non_bot  = total - bots
    # Top-3 ATK / DEF (non-bot, not YOU)
    def _score(r, key): return _num(r.get(key))
    eligible = [r for r in rows
                if _num(r.get("IsBot")) != 1 and "(YOU)" not in (r.get("Player") or "")]
    top_atk = sorted(eligible, key=lambda r: _score(r, "EstATK"), reverse=True)[:3]
    top_def = sorted(eligible, key=lambda r: _score(r, "EstDEF"), reverse=True)[:3]
    lv_dist: dict = {}
    for r in eligible:
        band = (_num(r.get("Level")) // 5) * 5
        lv_dist[band] = lv_dist.get(band, 0) + 1
    # YOU's rank percentile if findable
    you_pct = None
    for i, r in enumerate(sorted(eligible, key=lambda r: _score(r, "EstATK") + _score(r, "EstDEF"), reverse=True)):
        if "(YOU)" in (r.get("Player") or ""):
            you_pct = round(100 * (1 - i / max(1, len(eligible))), 1)
            break
    return {
        "tracked_players":    total,
        "bots":               bots,
        "non_bot":            non_bot,
        "top_atk":            [{"name": r.get("Player"), "atk": _num(r.get("EstATK")),
                                "level": _num(r.get("Level"))} for r in top_atk],
        "top_def":            [{"name": r.get("Player"), "def": _num(r.get("EstDEF")),
                                "level": _num(r.get("Level"))} for r in top_def],
        "level_distribution": dict(sorted(lv_dist.items())),
        "your_overall_pct":   you_pct,
    }


def _tool_get_action_costs(unit_filter: str = "") -> dict:
    """Exact current-game prices for every action Claude can take.  Pulls
    the cost tables straight from optimizer.py so there's no risk of
    drift between what Claude sees and what the runtime actually charges.
    Filterable by unit to cut tokens when Claude only cares about one."""
    # Lazy import — only when this tool is actually invoked.  Keeps
    # claude_strategy importable even when optimizer is mid-init.
    try:
        import optimizer as _opt
    except Exception as e:
        return {"error": f"optimizer module not importable: {e}"}

    u = (unit_filter or "").strip().lower()
    unit_cost = dict(getattr(_opt, "UNIT_COST", {}) or {})
    if u:
        unit_cost = {k: v for k, v in unit_cost.items() if k == u}

    gear: list = []
    GEAR = getattr(_opt, "GEAR", {}) or {}
    for (unit, slot), table in GEAR.items():
        if u and unit != u:
            continue
        for tier, row in table.items():
            name, per_unit_bonus, cost_each = row
            gear.append({
                "unit":            unit,
                "slot":            slot,
                "tier":            int(tier),
                "item_name":       name,
                "per_item_bonus":  int(per_unit_bonus),
                "cost_per_item":   int(cost_each),
            })
    gear.sort(key=lambda g: (g["unit"], g["slot"], g["tier"]))

    # Building next-level costs from the live /buildings scrape.
    # This requires having run a tick recently.  Safe to skip if missing.
    buildings_live = []
    try:
        # read the same buildings_meta exposed via _buildings_digest from state
        # Since this tool doesn't get the state, re-derive from private_latest
        # if the user cached it; otherwise skip.
        live = _safe_read_json("private_latest.json") or {}
        meta = live.get("buildings_meta") or {}
        for name, info in meta.items():
            buildings_live.append({
                "name":            name,
                "current_level":   info.get("level", 0),
                "next_level_cost": info.get("cost", 0),
                "upgradable_now":  bool(info.get("upgradable", False)),
                "locked":          bool(info.get("locked", False)),
                "requirements":    info.get("reqs", []) or [],
            })
    except Exception:
        pass

    return {
        "train_cost_per_unit":  unit_cost,
        "gear_catalog":         gear,
        "buildings_next_level": buildings_live,
        "note": "All values in gold.  Gear cost is per-item; total = qty × cost_per_item.",
    }


def _tool_get_incoming_attacks(limit: int = 10) -> dict:
    """Recent attacks AGAINST YOU — read from the fort-attacks log that the
    scraper writes.  If the file doesn't exist yet, returns empty list."""
    rows = _safe_read_csv("private_fort_attacks.csv")
    if not rows:
        return {
            "attackers_recent":  [],
            "note": (
                "No fort-attack history found yet.  Runs once scrape_fort_attacks "
                "executes (pulls the /fort page's 'Recent Attacks' section).  "
                "If this stays empty across ticks, you're genuinely not being "
                "attacked — no need to rush fort/DEF investment."
            ),
        }

    rows.sort(key=lambda r: r.get("Timestamp", ""), reverse=True)
    limit = max(1, min(int(limit or 10), 50))
    out = []
    for r in rows[:limit]:
        out.append({
            "ts":           r.get("Timestamp"),
            "attacker":     r.get("Attacker"),
            "result":       r.get("Result"),
            "gold_stolen":  _num(r.get("GoldStolen")),
            "damage":       _num(r.get("Damage")),
            "fort_hp_lost": _num(r.get("FortHpLost")),
            "xp_gained":    _num(r.get("AttackerXpGained")),
        })
    # Summary counts: how many attacks in last 24h, total gold lost, top aggressor
    import datetime as _dt
    cutoff_24h = _dt.datetime.now() - _dt.timedelta(hours=24)
    total_gold_lost_24h = 0
    attacks_24h = 0
    aggressor_counts: dict = {}
    for r in rows:
        ts_str = (r.get("Timestamp") or "").strip()
        try:
            row_dt = _dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                row_dt = _dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
            except ValueError:
                continue
        if row_dt >= cutoff_24h:
            attacks_24h += 1
            total_gold_lost_24h += _num(r.get("GoldStolen"))
            atk = r.get("Attacker") or "?"
            aggressor_counts[atk] = aggressor_counts.get(atk, 0) + 1
    worst = sorted(aggressor_counts.items(), key=lambda x: -x[1])[:3]
    return {
        "attackers_recent":        out,
        "attacks_last_24h":        attacks_24h,
        "gold_stolen_last_24h":    total_gold_lost_24h,
        "top_aggressors_last_24h": [{"name": n, "attacks": c} for n, c in worst],
    }


_TOOL_EXECUTORS = {
    "get_player_details":      lambda args: _tool_get_player_details(args.get("name", "")),
    "list_top_rivals":         lambda args: _tool_list_top_rivals(
                                                args.get("sort_by", "overall"),
                                                args.get("limit", 15),
                                                args.get("include_bots", False)),
    "search_players":          lambda args: _tool_search_players(**(args or {})),
    "get_own_recent_history":  lambda args: _tool_get_own_recent_history(
                                                args.get("ticks", 10)),
    "get_battle_log_recent":   lambda args: _tool_get_battle_log_recent(
                                                args.get("limit", 20)),
    "get_server_overview":     lambda args: _tool_get_server_overview(),
    "get_action_costs":        lambda args: _tool_get_action_costs(args.get("unit_type", "")),
    "get_incoming_attacks":    lambda args: _tool_get_incoming_attacks(args.get("limit", 10)),
}


def _execute_tool(name: str, args: dict) -> dict:
    """Dispatch a single tool call.  Wrapped in try/except so a crashing
    tool returns `{error: ...}` to Claude instead of aborting the tick."""
    fn = _TOOL_EXECUTORS.get(name)
    if not fn:
        return {"error": f"unknown tool {name!r}"}
    try:
        return fn(args or {})
    except Exception as e:
        return {"error": f"tool {name} failed: {e}"}


# ── System prompt ───────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are the autonomous strategic brain of a DarkThrone player.
Each tick (30 minutes) you receive:
  * The game's mechanics and formulas (rulebook)
  * Your current state, army, armory, buildings, ranks
  * The top rival players' estimated stats
  * A memo you wrote last tick with your own goals + observations

Your job: reason about the situation from first principles, decide what
to do this tick, and update your memo so the NEXT tick's you has context.

Do NOT rely on canned strategies like "Grow" / "Combat" / "Defend" —
those are simplistic human heuristics.  Reason from the mechanics: what
are your bottlenecks, what are your opportunities, what would a
thoughtful player do?  If the situation is unusual, respond unusually.

## Tools — investigate before deciding
You can call tools to pull additional information:
  - get_action_costs(unit_type?): EXACT in-game prices for every
    TRAIN / BUY_GEAR / BUILD action.  **Use this if any action you're
    considering involves gear or training** — cost math from memory is
    where you go wrong.  Filter by unit_type to save tokens.
  - get_incoming_attacks(limit): who's attacked YOU recently + how much
    gold you've lost.  Critical for deciding whether to fort + bank.
  - get_player_details(name): deep dossier on one player (ranks,
    estimated stats, confirmed intel, growth trajectory).
  - list_top_rivals(sort_by, limit): rivals by ATK/DEF/wealth/spy/level.
  - search_players(filters): find players by level/stat/race/class/clan.
  - get_own_recent_history(ticks): your own stat + action trajectory.
  - get_battle_log_recent(limit): your recent attack/spy outcomes.
  - get_server_overview(): percentile position + level distribution.

Aim for 0-3 tool calls per tick — every call costs API tokens.  Don't
re-fetch data already in your initial context.

## Decision framework — walk through this before emitting actions
1. **Budget sanity check** — total cost of proposed actions must be
   LESS than `gold_on_hand`.  If you're unsure about a cost (especially
   BUILD, BUY_GEAR × tier), call `get_action_costs` first.
2. **Bottleneck identification** — what's preventing your stats from
   growing?  Under-geared units?  Under-trained army?  Locked building?
   Fix the binding constraint, not the "nice to have".
3. **Plunder risk** — if gold on hand > ~500k AND fort damage > 20% AND
   you've been attacked recently (check get_incoming_attacks), you're
   bleeding.  Bank or fortify FIRST.  Gold sitting attracts more attacks.
4. **Multi-tick horizon** — your income accumulates over ticks.  If a
   high-value target (Fort Lv2, Mine Lv3) needs 2M gold and you have 1M,
   bank 900k and plan to finish it in 1-2 ticks.  Don't make do with a
   small BUILD now just because you can afford it.
5. **Every action spent well** — you have limited action slots (20
   max).  Spending 15 of them on small gear buys when 1 BUILD would
   unlock a tier is waste.

## Common failure modes to avoid
- Proposing BUILD at a cost you can't afford → runtime drops it, you
  lose the slot.  Always verify cost ≤ gold first.
- Proposing BUY_GEAR when the target slot is already full.  **HARD
  RULE**: if `armory[unit].equip_gap` is 0 for a (unit, slot), DO NOT
  emit BUY_GEAR for that slot — the DarkThrone server silently refuses
  and the tick is wasted.  To upgrade tier on a saturated slot you must
  first TRAIN more of that unit (creating unequipped slots), or switch
  to a different unit/slot.  There is no SELL_GEAR action.
- Training units when you don't have the citizens for them (citizens
  field is below what's needed).  Use what's already trained before
  doubling the army.
- Banking gold when fort is intact and nobody's attacking — loses a
  deposit slot for no protection.

## Action shape — emit EXACTLY this, no drift
The validator is strict about shape.  These formats are DROPPED:
  BAD: {"action_type": "repair_fort", "params": {"amount": 100}, "reasoning": "..."}
  BAD: {"type": "repair_fort", "damage": 100, "reason": "..."}    # lowercase
  BAD: {"type": "REPAIR_FORT", "amount": 100, "reason": "..."}    # wrong field
The canonical shape is ALWAYS:
  GOOD: {"type": "REPAIR_FORT", "damage": 100, "reason": "..."}
Use UPPERCASE type names.  Flat fields — NEVER nested "params".  Field
names are exactly those listed in the schema below (e.g. "damage" for
REPAIR_FORT, "count" for TRAIN, "qty" for BUY_GEAR / BUY_UPGRADE,
"amount" for BANK).  Use "reason" not "reasoning".

## Predictive planning — think TWO ticks ahead every call
The runtime uses your response as follows: the `actions` list runs THIS
tick if the saved pre-plan from last tick isn't usable.  But
`plan_next_tick` is the star of the show — it's the action queue that
FAST-EXECUTES the instant the next tick fires, before scraping, before
any other API call.  That drops the gold-on-hand exposure window from
60-90s (reactive path) to 5-10s.  Predict the gold Claude-you will see
at the next tick fire, plan exact actions, and trust the runtime to
validate and execute.

For `plan_next_tick`:
  * `expected_start_gold` = (current gold-on-hand) + income_per_tick
    (plus small slack for attacks / timing).  Be accurate; if your
    forecast differs from reality by >30% the plan is discarded and the
    bot falls back to a synchronous re-plan.
  * `actions` = moves to run immediately when the next tick fires.  Same
    schema as `actions` above — TRAIN, BUY_GEAR, BUILD, BUY_UPGRADE,
    REPAIR_FORT, BANK.  The runtime decorates them with exact costs and
    caps cumulative spend at live gold.

## Output (after any tool calls)
When you have enough information, respond with ONE JSON object — no
prose outside the JSON, no markdown fences — exactly matching this schema:

{
  "situation": "1-3 sentences describing where you are right now",
  "strategy_now": "1-3 sentences on what you're trying to do THIS tick",
  "strategy_multi_tick": "1-3 sentences on the 3-10 tick horizon",
  "actions": [
    {"type": "TRAIN",       "unit": "soldier|guard|spy|sentry|worker", "count": N,          "reason": "..."},
    {"type": "BUY_GEAR",    "unit": "soldier|guard|spy|sentry",        "slot": "weapon|armor", "tier": 1-10, "qty": N, "reason": "..."},
    {"type": "BUILD",       "name": "Mine|Housing|...", "lv": 1-5,                          "reason": "..."},
    {"type": "BUY_UPGRADE", "name": "Steed|Guard Tower|...",            "qty": N,           "reason": "..."},
    {"type": "REPAIR_FORT", "damage": N,                                                    "reason": "..."},
    {"type": "BANK",        "amount": N,                                                    "reason": "..."}
  ],
  "plan_next_tick": {
    "expected_start_gold": N,
    "rationale":           "why you expect this gold-on-hand when next tick fires",
    "actions":             [ ...same action schema as above, for NEXT tick... ],
    "fallback_note":       "1 sentence on what should happen if gold differs from forecast"
  },
  "memo_next_tick": {
    "overarching_goal":  "what you're building toward (days/weeks)",
    "current_phase":     "label for this stretch of play",
    "short_term_plan":   ["next few expected moves, ordered"],
    "key_observations":  ["game-state insights you want to remember"],
    "open_questions":    ["things you'd like to learn; don't need all of these"]
  }
}

Constraints:
  * The runtime computes exact costs from your (unit, slot, tier) or (name, lv).
    You don't need to fill in cost fields.
  * Total spent cannot exceed your current gold.  The runtime will drop
    actions from the tail of your list until the budget fits.
  * Up to 20 actions per tick.
  * If there is literally nothing productive to do, return actions: [].
  * "workers" is trained like any other unit; "qty" ≥ 1.
  * BANK action amount is in gold; will be capped at the daily deposit limit.
"""


# ── Game mechanics doc (the rulebook, cached across ticks) ──────────────────
# This is the slow-changing knowledge Claude needs.  Keeping it in one
# string lets Anthropic prompt-caching cut ~90% of input costs on re-reads
# within the 5-minute cache window.
GAME_MECHANICS_DOC = """# DarkThrone Game Mechanics Reference

## Core resources
- **Gold**: primary currency.  Earned per tick.  Gold ON HAND can be
  plundered by attackers; gold IN BANK is safe.
- **Bank**: safe storage.  Limited deposits per day (default 6).
- **Turns**: consumed by attacks (usually 5/attack) and spies (2/spy).
  Regenerate slowly over time.
- **XP**: gained from winning attacks (scales with target level diff
  within the ±10 level range).  Level up unlocks new tiers.
- **Citizens**: idle population not yet assigned to workers/military.

## Income formula
```
income_per_tick = (BASE_INC + workers * WORKER_GOLD)
                * mine_multiplier[mine_lv]
                * (1 + race_income_bonus + class_income_bonus)
```
- BASE_INC = 1000
- WORKER_GOLD = 5 (gold/worker/tick before multipliers)
- Workers are capped at ~80% of total population.
- mine_multiplier: Lv0=1.0, Lv1=1.5, Lv2=2.5, Lv3=4.0, Lv4=6.0, Lv5=8.0

## Race bonuses (exact values)
- **Human**:  +5% Offense (ATK)
- **Goblin**: +5% Defense (DEF)
- **Elf**:    +5% Defense (DEF)
- **Undead**: +5% Offense (ATK)

## Class bonuses (exact values)
- **Fighter**:  +5% Offense (ATK)
- **Cleric**:   +5% Defense (DEF)
- **Thief**:    +5% Income  (gold only)
- **Assassin**: +5% Spy OFF + Spy DEF

Bonuses stack: Goblin Cleric = +10% DEF.  Undead Thief = +5% ATK, +5% income.

## Buildings (name, max_lv, base_cost)
| Building        | Max Lv | Base cost | Function                              |
|-----------------|--------|-----------|---------------------------------------|
| Mine            | 3      | 150,000   | Gold income multiplier                |
| Housing         | 3      | 100,000   | Citizens / population cap             |
| Spy Academy     | 5      | 250,000   | Max spy-unit tier                     |
| Mercenary Camp  | 7      | 200,000   | Unlocks merc troop hire               |
| Barracks        | 8      | 400,000   | Citizen-growth rate (pop)             |
| Fortification   | 10     | 500,000   | Fort HP + max unit tier               |
| Armory          | 10     | 750,000   | Max gear (weapon/armor) tier          |

**Cost scaling** — costs are not strictly linear.  Each tick you
receive a `buildings` array in your state showing the LIVE next-level
cost for every building (from the game itself), plus whether it's
upgradable right now.  Use those exact numbers when budgeting a BUILD
action; don't guess with base × level.  Example of what you'll see:

```
"buildings": [
  {"name": "Mine",         "current_level": 2, "next_level": 3,
   "next_level_cost": 450000, "upgradable_now": false,
   "requirements": ["Fortification Lv2 required"]},
  {"name": "Barracks",     "current_level": 1, "next_level": 2,
   "next_level_cost": 1600000, "upgradable_now": true}
]
```

When `upgradable_now` is true and you have `next_level_cost` gold in
hand, submitting BUILD with `{"name": "Barracks", "lv": 2}` will work.
When it's false (locked or priced out), skip it.

Prerequisites: Mine Lv2 needs Fort Lv1; Mine Lv3 needs Fort Lv2.
Armory and Fortification both require player Level 10 to START.
Prereqs show up in the `requirements` list when unmet.

## Unit tiers (base stat per unit, before gear)
| Tier | Soldier(ATK) | Guard(DEF) | Spy(SpyOff) | Sentry(SpyDef) |
|------|--------------|------------|-------------|----------------|
| T1   | 5            | 5          | 5           | 5              |
| T2   | 20           | 20         | ~           | ~              |
| T3   | 40           | 40         | ~           | ~              |
| T4   | 80           | 80         | ~           | ~              |
| T5   | 160          | 160        | 40          | 40             |
| T6   | 320          | 320        | ~           | ~              |
(Spy/Sentry unit stat scaling is slower than military.)

Unit tier cap = min(fortification-level-cap, player-level-cap).  Below
Level 10 you are gated at T3 for MILITARY regardless of fort.

## Unit training costs (gold per unit, flat across tiers — tier is set
   by Fortification, not by what you train)
- worker:  2,000 gold
- soldier: 1,500 gold
- guard:   1,500 gold
- spy:     2,500 gold
- sentry:  2,500 gold

So training 50 soldiers = 50 × 1,500 = 75,000 gold.  Training 200 workers
= 200 × 2,000 = 400,000 gold.  Use `get_action_costs` tool for the exact
gear prices at each (unit, slot, tier) combination.

## Gear tiers (per-item bonus, before qty)
| Tier | Weapon/Armor bonus | Typical cost / item |
|------|---------------------|---------------------|
| T1   | 20                  | 1,000               |
| T2   | 40                  | 5,000               |
| T3   | 60                  | 25,000              |
| T4   | 100                 | 100,000             |
| T5   | 180-200             | 200,000             |
| T6   | 280                 | 400,000             |

Each unit can equip 1 weapon + 1 armor in its slot (offense vs defense).
Buying more items than units gives no benefit until you train more
units.  Gear carries forward if units die.

## Battle upgrades (permanent)
| Upgrade     | Side    | Per-item bonus | Notes                        |
|-------------|---------|----------------|------------------------------|
| Steed       | Offense | 200            | Multiplicative with soldiers |
| Guard Tower | Defense | 200            | Multiplicative with guards   |
| (others)    |         |                |                              |

## Combat formula (rough)
```
effective_atk = soldiers * tier_stat + equipped_weapons_stat + equipped_armor_stat
              + Σ(battle_upgrade_qty * bonus)
              (× (1 + race_atk + class_atk))
```
Same shape for DEF with guards / defense gear / Guard Tower.

## Fort HP by level
- Lv0: 100 HP
- Lv1: 1,000 HP
- Lv2: 3,000 HP
- Lv3+: doubles-ish per level
Repair cost: ~16.75 gold per HP.  When fort is damaged, each attack
plunders more of your gold-on-hand.

## Attack mechanics
- Range: targets within ±10 player levels
- Cost: 5 turns per attack (configurable 1-10)
- Daily cap: 5 attacks per target per day (server-side)
- On win: you steal a % of target's gold-on-hand + gain XP
- On loss: target's fort absorbs damage; you lose some units/gear
- Attacker must have ATK > Defender's DEF × safety_margin (bot uses 1.2)

## Spy mechanics
- Cost: 2 turns + 3,000 gold per recon
- Daily cap: 5 spies per target per day
- Spy success: SpyOff must overcome target's SpyDef
- Success reveals: combat stats, army composition, armory, buildings,
  battle upgrades, gold/bank/citizens, fort state.  All intel is stored
  and improves estimates for every player in the same (race, class) bucket.

## Rank system
Separate rank boards for Overall / Offense / Defense / Wealth.  Computed
from stats AT THE MOMENT the server sampled (rank snapshots update
server-wide each tick).  Ranks are what opponents see on leaderboards.
Top-20 places are hotly contested; breaking into top-20 gets you
attacked harder.

## Plunder economics
- Attacker steals some % of your gold-on-hand on a successful attack.
  Exact formula is server-side but roughly scales with fort damage %.
- Gold in bank is ALWAYS safe.
- Banking costs a deposit slot (daily-limited).  Players usually bank
  several hundred thousand gold at key moments, then drain on big plays.
- If you leave multi-million gold on hand and are in a contested rank,
  expect to lose 10-50% of it to plunder in a tick or two.

## Typical gear-up sequence from scratch
At each tier unlock you ideally buy enough weapons+armor to equip 100%
of your corresponding unit count.  Overshooting wastes gold; undershooting
wastes the tier bonus on unequipped units.

## Daily schedule
- Server tick fires approximately every 30 minutes (48 ticks/day).
- Daily reset (attack/spy/deposit counters) happens at a fixed UTC time.
- Most players are most active in their evening 18:00-00:00 local.

## What the optimizer executes
Your actions are dispatched by a Python runtime that:
  * Navigates to the right in-game page
  * Submits the right form
  * Re-reads live gold to verify the server actually applied the action
  * Retries the planning loop up to 3 times per tick if gold isn't spent
  * All wrapped in jittered delays to look human-ish

You can trust that, if the runtime accepts an action, it'll execute it —
but the server can silently reject for reasons we can't predict.  The
retry loop handles that; you just need to specify WHAT to do.

## Output format
See SYSTEM prompt for the exact JSON schema.  No prose outside the JSON.
"""


# ── State digest helpers ────────────────────────────────────────────────────
def _armory_analysis(cats: list) -> list:
    """Condense the `cats` list from optimizer.analyse() to a compact form
    Claude can reason over.  Each entry: what unit type, how many units,
    how well-geared, what tier cap."""
    out = []
    for c in (cats or []):
        out.append({
            "unit":      c.get("unit"),
            "units":     c.get("units", 0),
            "weapon_qty":  c.get("w_owned", 0),
            "weapon_tier": c.get("w_tier", 0),
            "armor_qty":   c.get("a_owned", 0),
            "armor_tier":  c.get("a_tier", 0),
            "tier_cap":  c.get("max_t", 1),
            "equip_gap": max(0, c.get("units", 0) - c.get("w_owned", 0)),
            "score":     round(float(c.get("score", 0) or 0), 3),
        })
    return out


def _buildings_digest(state: dict) -> list:
    """Per-building next-level state: current level, next-level cost (live
    from the game), and whether the upgrade is available right now.  This
    replaces Claude's need to compute base × level — the game has already
    told us the exact cost for the next upgrade."""
    meta = state.get("buildings_meta") or {}
    owned = state.get("buildings") or {}
    rows = []
    # Stable order: same as on the /buildings page
    order = ["Mine", "Housing", "Spy Academy", "Mercenary Camp",
             "Barracks", "Fortification", "Armory"]
    for name in order:
        info = meta.get(name, {})
        current_lv = int(info.get("level", owned.get(name, 0)) or 0)
        cost       = int(info.get("cost", 0) or 0)
        max_lv     = int(info.get("max", 0) or 0)
        rows.append({
            "name":            name,
            "current_level":   current_lv,
            "max_level":       max_lv,
            "next_level":      current_lv + 1 if current_lv < max_lv else None,
            "next_level_cost": cost if cost > 0 else None,
            "upgradable_now":  bool(info.get("upgradable", False)),
            "locked":          bool(info.get("locked", False)),
            "needs_gold":      bool(info.get("needs_gold", False)),
            "requirements":    info.get("reqs", []) or [],
        })
    return rows


def _own_digest(state: dict) -> dict:
    """Flatten read_state() output into the subset Claude cares about."""
    pop_total = sum(int(state.get(k, 0) or 0)
                    for k in ("workers", "soldiers", "guards", "spies", "sentries"))
    pop_total += int(state.get("citizens", 0) or 0)
    return {
        "level":     state.get("level", 0),
        "xp":        state.get("xp", 0),
        "xp_need":   state.get("xp_need", 0),
        "xp_to_next": max(0, int(state.get("xp_need", 0) or 0)
                             - int(state.get("xp", 0) or 0)),
        "gold":      state.get("gold", 0),
        "bank":      state.get("bank", 0),
        "turns":     state.get("turns", 0),
        "citizens":  state.get("citizens", 0),
        "population_total": pop_total,
        "army": {
            "workers":  state.get("workers",  0),
            "soldiers": state.get("soldiers", 0),
            "guards":   state.get("guards",   0),
            "spies":    state.get("spies",    0),
            "sentries": state.get("sentries", 0),
        },
        "stats": {
            "atk":     state.get("atk", 0),
            "def":     state.get("def", 0),
            "spy_off": state.get("spy_off", 0),
            "spy_def": state.get("spy_def", 0),
            "income_per_tick": state.get("income", 0),
        },
        "rank": {
            "overall": state.get("rank_overall", 0),
            "offense": state.get("rank_offense", 0),
            "defense": state.get("rank_defense", 0),
            "wealth":  state.get("rank_wealth", 0),
        },
        "buildings": _buildings_digest(state),
        "fort": {
            "hp":  state.get("fort_hp", 0),
            "max": state.get("fort_max_hp", 0),
            "damage_pct": (
                round(1 - float(state.get("fort_hp", 0) or 0)
                        / max(1, float(state.get("fort_max_hp", 1) or 1)), 3)
            ),
        },
        "deposits_used_today": state.get("deposits", 0),
    }


# ── API key + budget ────────────────────────────────────────────────────────
def load_api_key() -> str:
    """Env var ANTHROPIC_API_KEY wins if set; otherwise read the data-dir file."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(CLAUDE_API_KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def save_api_key(key: str) -> None:
    """Persist API key so it survives reboots.  GUI writes this; env var
    can still override at runtime."""
    key = (key or "").strip()
    if not key:
        return
    with open(CLAUDE_API_KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key)


def budget_today_usd() -> float:
    """Sum up today's spend from the cost log."""
    if not os.path.isfile(CLAUDE_COST_LOG):
        return 0.0
    today = datetime.date.today().isoformat()
    total = 0.0
    try:
        with open(CLAUDE_COST_LOG, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ts = (row.get("Timestamp") or "")[:10]
                if ts == today:
                    try:
                        total += float(row.get("CostUSD", 0) or 0)
                    except ValueError:
                        pass
    except Exception:
        pass
    return total


def check_budget() -> tuple[float, bool]:
    """Return (today_spent, under_budget)."""
    spent = budget_today_usd()
    return spent, spent < CLAUDE_BUDGET_DAILY_USD


def record_cost(model: str, in_tokens: int, out_tokens: int,
                cache_reads: int, cache_writes: int,
                tick_num: int | None = None) -> float:
    """Append a cost log entry + return the dollar amount for this call."""
    pricing = CLAUDE_PRICING.get(model, CLAUDE_PRICING["claude-sonnet-4-5"])
    cost = (
        in_tokens    * pricing["input"]       / 1_000_000
        + out_tokens   * pricing["output"]      / 1_000_000
        + cache_writes * pricing["cache_write"] / 1_000_000
        + cache_reads  * pricing["cache_read"]  / 1_000_000
    )
    new_file = not os.path.isfile(CLAUDE_COST_LOG)
    try:
        with open(CLAUDE_COST_LOG, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(CLAUDE_COST_LOG_COLUMNS)
            w.writerow([
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                model, int(in_tokens), int(out_tokens),
                int(cache_reads), int(cache_writes),
                f"{cost:.6f}", tick_num if tick_num is not None else "",
            ])
    except Exception:
        pass   # never block the tick on a logging failure
    return cost


# ── Memo persistence ────────────────────────────────────────────────────────
def _empty_memo() -> dict:
    return {
        "initialized":      False,
        "overarching_goal": None,
        "current_phase":    None,
        "short_term_plan":  [],
        "key_observations": [],
        "open_questions":   [],
        "last_tick_actions": [],
        "last_strategy_notes": "",
        "tick_count":       0,
        "last_updated":     None,
    }


def load_memo() -> dict:
    if not os.path.isfile(CLAUDE_MEMO_FILE):
        return _empty_memo()
    try:
        with open(CLAUDE_MEMO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return _empty_memo()


def save_memo(memo: dict) -> None:
    if not isinstance(memo, dict):
        return
    memo["last_updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(CLAUDE_MEMO_FILE, "w", encoding="utf-8") as f:
            json.dump(memo, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ── Next-tick plan persistence (predictive mode) ────────────────────────────
def save_next_plan(plan: dict) -> None:
    """Persist Claude's plan_next_tick forecast so the NEXT tick's
    run_tick can load + fast-execute it before the slow scrape cycle.

    Stamped with created_at for staleness detection; a plan older than
    PLAN_STALE_HOURS is discarded by load_next_plan() rather than
    executed against a state that's drifted too far."""
    if not isinstance(plan, dict):
        return
    plan = dict(plan)   # shallow copy so caller's dict isn't mutated
    plan["created_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(CLAUDE_NEXT_PLAN_FILE, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def load_next_plan() -> dict | None:
    """Return the saved next-tick plan, or None if:
        * file is missing
        * file is unparseable
        * plan is older than PLAN_STALE_HOURS

    Returning None lets run_tick cleanly fall back to synchronous
    decide_claude for this tick with no special-case handling."""
    if not os.path.isfile(CLAUDE_NEXT_PLAN_FILE):
        return None
    try:
        with open(CLAUDE_NEXT_PLAN_FILE, "r", encoding="utf-8") as f:
            plan = json.load(f)
        if not isinstance(plan, dict):
            return None
    except Exception:
        return None

    ts_str = (plan.get("created_at") or "").strip()
    if ts_str:
        try:
            created = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            age = (datetime.datetime.now() - created).total_seconds() / 3600.0
            if age > PLAN_STALE_HOURS:
                return None
        except ValueError:
            return None
    else:
        # No timestamp → treat as unusable (corrupt save)
        return None
    return plan


def invalidate_next_plan() -> None:
    """Explicitly discard the saved plan — e.g., after we detect state
    divergence so the next tick doesn't see a stale file."""
    try:
        if os.path.isfile(CLAUDE_NEXT_PLAN_FILE):
            os.remove(CLAUDE_NEXT_PLAN_FILE)
    except Exception:
        pass


def plan_variance_ok(plan: dict, live_gold: int) -> tuple[bool, float]:
    """Check whether the saved plan's gold forecast is close enough to
    actual live gold to be trusted.  Returns (ok, variance_ratio).
    Variance > PLAN_VARIANCE_THRESHOLD → plan is rejected."""
    try:
        expected = int((plan or {}).get("expected_start_gold", 0) or 0)
    except (TypeError, ValueError):
        return False, 1.0
    if expected <= 0:
        # Plan forecasts zero gold — trusting it would mean spending
        # nothing, which is never optimal; better to re-plan.
        return False, 1.0
    variance = abs(live_gold - expected) / max(expected, 1)
    return variance <= PLAN_VARIANCE_THRESHOLD, variance


# ── Response parsing + validation ───────────────────────────────────────────
# Valid action types and their required / optional fields.  Used by the
# validator below to reject nonsense without losing a whole tick.
_VALID_ACTION_SHAPES: dict[str, dict[str, type]] = {
    # type → {required_field: expected_type}
    "TRAIN":       {"unit": str, "count": int},
    "BUY_GEAR":    {"unit": str, "slot": str, "tier": int, "qty": int},
    "BUILD":       {"name": str, "lv": int},
    "BUY_UPGRADE": {"name": str, "qty": int},
    "REPAIR_FORT": {"damage": int},
    "BANK":        {"amount": int},
}
_VALID_UNITS = {"worker", "soldier", "guard", "spy", "sentry"}
_VALID_SLOTS = {"weapon", "armor"}


def normalize_claude_action(a: dict) -> dict:
    """Pull Claude's occasional format drift back into the canonical shape.

    Live runs have shown Claude sometimes emits actions like
        {"action_type": "repair_fort", "params": {"amount": 100},
         "reasoning": "..."}
    instead of the documented
        {"type": "REPAIR_FORT", "damage": 100, "reason": "..."}.

    Rather than drop the whole action (and waste a tick), normalize the
    common drifts here:
      * "action_type" → "type"
      * lowercase type → UPPERCASE ("repair_fort" → "REPAIR_FORT")
      * nested "params" dict → flattened into the top level
      * "reasoning" → "reason"
      * REPAIR_FORT:  "amount" → "damage" (a common model-written alias)
      * TRAIN:        "quantity" / "qty" → "count"

    Returns a NEW dict — never mutates the input.  Safe to call on
    already-canonical actions (no-op).
    """
    if not isinstance(a, dict):
        return a
    out = dict(a)

    if "type" not in out and "action_type" in out:
        out["type"] = out.pop("action_type")
    t = out.get("type")
    if isinstance(t, str):
        out["type"] = t.strip().upper()

    params = out.pop("params", None)
    if isinstance(params, dict):
        for k, v in params.items():
            out.setdefault(k, v)

    if "reason" not in out and "reasoning" in out:
        out["reason"] = out.pop("reasoning")

    atype = out.get("type")
    if atype == "REPAIR_FORT" and "damage" not in out and "amount" in out:
        out["damage"] = out.pop("amount")
    if atype == "TRAIN" and "count" not in out:
        if "qty" in out:
            out["count"] = out.pop("qty")
        elif "quantity" in out:
            out["count"] = out.pop("quantity")

    return out


def parse_response(text: str) -> dict:
    """Extract the JSON object from Claude's response.  Tolerates leading
    or trailing prose (even though the system prompt forbids it — models
    occasionally add a sentence), and tolerates markdown code fences."""
    if not text:
        raise ValueError("empty response")
    # Strip ```json fences if present
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if fenced:
        text = fenced.group(1).strip()
    # Find the largest {...} block
    start = text.find("{")
    end   = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found in response")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}")


def validate_actions(raw_actions: list, available_gold: int) -> tuple[list, list]:
    """Accept only actions matching the schema.  Drop tail actions if the
    cumulative cost would exceed available_gold (safety — Claude can't
    reason perfectly about exact prices).  Returns (kept, rejected_notes)."""
    kept: list = []
    rejected: list = []
    running_cost = 0

    for idx, a in enumerate(raw_actions or []):
        if not isinstance(a, dict):
            rejected.append(f"#{idx}: not an object")
            continue
        a = normalize_claude_action(a)
        atype = a.get("type")
        if atype not in _VALID_ACTION_SHAPES:
            rejected.append(f"#{idx}: unknown type {atype!r}")
            continue
        shape = _VALID_ACTION_SHAPES[atype]
        # Check required fields
        ok = True
        for field, exp_type in shape.items():
            v = a.get(field)
            if v is None:
                rejected.append(f"#{idx} {atype}: missing {field}")
                ok = False
                break
            # Allow int-like strings ("5" → 5); reject floats for counts.
            if exp_type is int:
                try:
                    a[field] = int(v)
                except (TypeError, ValueError):
                    rejected.append(f"#{idx} {atype}: {field}={v!r} not int")
                    ok = False
                    break
                if a[field] < 0:
                    rejected.append(f"#{idx} {atype}: {field}={a[field]} negative")
                    ok = False
                    break
        if not ok:
            continue

        # Type-specific semantics
        if atype == "TRAIN":
            if a["unit"] not in _VALID_UNITS:
                rejected.append(f"#{idx} TRAIN: unit={a['unit']!r} not in {_VALID_UNITS}")
                continue
            if a["count"] <= 0:
                rejected.append(f"#{idx} TRAIN: count must be ≥1")
                continue
        elif atype == "BUY_GEAR":
            if a["unit"] not in _VALID_UNITS:
                rejected.append(f"#{idx} BUY_GEAR: unit={a['unit']!r} invalid")
                continue
            if a["slot"] not in _VALID_SLOTS:
                rejected.append(f"#{idx} BUY_GEAR: slot={a['slot']!r} not in {_VALID_SLOTS}")
                continue
            if not (1 <= a["tier"] <= 10):
                rejected.append(f"#{idx} BUY_GEAR: tier={a['tier']} out of [1,10]")
                continue
            if a["qty"] <= 0:
                rejected.append(f"#{idx} BUY_GEAR: qty must be ≥1")
                continue
        elif atype == "BUILD":
            if not a["name"]:
                rejected.append(f"#{idx} BUILD: missing name")
                continue
            if not (1 <= a["lv"] <= 10):
                rejected.append(f"#{idx} BUILD: lv={a['lv']} out of [1,10]")
                continue
        elif atype == "BUY_UPGRADE":
            if not a["name"]:
                rejected.append(f"#{idx} BUY_UPGRADE: missing name")
                continue
            if a["qty"] <= 0:
                rejected.append(f"#{idx} BUY_UPGRADE: qty must be ≥1")
                continue
        elif atype == "REPAIR_FORT":
            if a["damage"] <= 0:
                rejected.append(f"#{idx} REPAIR_FORT: damage must be ≥1")
                continue
        elif atype == "BANK":
            if a["amount"] <= 0:
                rejected.append(f"#{idx} BANK: amount must be ≥1")
                continue

        kept.append(a)
        if len(kept) >= 20:
            # Hard cap per tick.  Remaining get rejected.
            rejected.extend(f"#{j}: over 20-action cap" for j in range(idx + 1, len(raw_actions)))
            break

    # Note: we do NOT enforce running-cost ≤ available_gold here.  The
    # caller (optimizer.py) looks up real prices from the GEAR / BUILDINGS
    # / UNIT_COST tables and does its own cost-capped ordering.  We only
    # prune structurally-invalid actions at this layer.
    _ = running_cost
    return kept, rejected


# ── Main entry ──────────────────────────────────────────────────────────────
def build_user_content(state: dict, cats: list, rivals_top10: list,
                        memo: dict, tick_num: int,
                        recent_growth: list | None = None) -> list:
    """Build the Anthropic `content` list for a user message, with the
    mechanics doc marked for prompt caching."""
    own = _own_digest(state)
    armory = _armory_analysis(cats)
    dynamic_ctx = {
        "tick_number":   tick_num,
        "own":           own,
        "armory":        armory,
        "rivals_top10":  rivals_top10 or [],
        "recent_growth": recent_growth or [],
        "memo_from_last_tick": memo or {},
    }

    return [
        {
            "type": "text",
            "text": GAME_MECHANICS_DOC,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": (
                "## Current situation (tick " + str(tick_num) + ")\n"
                "```json\n" + json.dumps(dynamic_ctx, indent=2, ensure_ascii=False) + "\n```\n\n"
                "Decide your actions for this tick and update your memo.  "
                "Return the JSON object per the schema in the system prompt."
            ),
        },
    ]


def decide_claude(state: dict, cats: list,
                  rivals_top10: list | None = None,
                  tick_num: int = 0,
                  recent_growth: list | None = None,
                  log_fn=None) -> tuple[list, dict, dict]:
    """Ask Claude to choose actions for this tick.  Returns
    (actions, memo, info).  Raises RuntimeError on any failure so the
    caller can fall back to decide_v2."""
    # Pre-flight checks — fail fast with clear reasons
    api_key = load_api_key()
    if not api_key:
        raise RuntimeError("no_api_key")
    spent, under = check_budget()
    if not under:
        raise RuntimeError(
            f"over_budget (today=${spent:.2f} / cap=${CLAUDE_BUDGET_DAILY_USD:.2f})"
        )

    try:
        from anthropic import Anthropic  # lazy — only when strategy is active
    except ImportError:
        raise RuntimeError("anthropic_sdk_missing — pip install anthropic")

    memo = load_memo()
    memo["tick_count"] = int(memo.get("tick_count", 0) or 0) + 1

    user_content = build_user_content(
        state, cats, rivals_top10 or [], memo, tick_num, recent_growth or []
    )

    # ── Tool-use loop ────────────────────────────────────────────────────
    # Each iteration = one API call.  If Claude requests tools, we execute
    # them locally and feed results back.  Loop ends when Claude responds
    # with text only (stop_reason="end_turn") — that's the final decision.
    #
    # Per-tick cost cap stops runaway loops; MAX_TOOL_ITERATIONS is a
    # belt-and-braces failsafe.  Both trip → RuntimeError → caller falls
    # back to decide_v2 for this tick.
    try:
        client = Anthropic(api_key=api_key)
    except Exception as e:
        raise RuntimeError(f"client_init_failed: {e}")

    messages = [{"role": "user", "content": user_content}]
    tick_cost       = 0.0
    tick_in_tok     = 0
    tick_out_tok    = 0
    tick_cache_read = 0
    tick_cache_write= 0
    tool_calls_log: list = []   # what tools Claude actually invoked this tick
    resp = None

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS_OUT,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
        except Exception as e:
            raise RuntimeError(f"api_error: {e}")

        usage = getattr(resp, "usage", None)
        iter_in  = getattr(usage, "input_tokens",                0) or 0
        iter_out = getattr(usage, "output_tokens",               0) or 0
        iter_cr  = getattr(usage, "cache_read_input_tokens",     0) or 0
        iter_cw  = getattr(usage, "cache_creation_input_tokens", 0) or 0
        iter_cost = record_cost(CLAUDE_MODEL, iter_in, iter_out, iter_cr, iter_cw,
                                tick_num=tick_num)
        tick_cost       += iter_cost
        tick_in_tok     += iter_in
        tick_out_tok    += iter_out
        tick_cache_read += iter_cr
        tick_cache_write+= iter_cw

        if tick_cost > PER_TICK_COST_CAP:
            raise RuntimeError(
                f"per_tick_cost_cap_exceeded: ${tick_cost:.4f} > ${PER_TICK_COST_CAP:.2f} "
                f"after {iteration + 1} iterations, {len(tool_calls_log)} tools"
            )

        # Record assistant's turn so the next roundtrip has context
        messages.append({"role": "assistant", "content": resp.content})

        stop_reason = getattr(resp, "stop_reason", "") or ""
        if stop_reason != "tool_use":
            break   # Claude returned its final decision

        # Execute every tool_use block in this turn, package results back.
        tool_results = []
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_name = getattr(block, "name", "") or ""
            tool_id   = getattr(block, "id", "")   or ""
            tool_args = getattr(block, "input", {}) or {}
            result = _execute_tool(tool_name, tool_args)
            tool_calls_log.append({"name": tool_name, "args": tool_args})
            # Clip oversize tool results so one chatty tool can't blow context.
            result_json = json.dumps(result, ensure_ascii=False)
            if len(result_json) > TOOL_RESULT_MAX_CHARS:
                result_json = (
                    result_json[:TOOL_RESULT_MAX_CHARS]
                    + f'... [truncated at {TOOL_RESULT_MAX_CHARS} chars]"}}'
                )
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": tool_id,
                "content":     result_json,
            })

        if not tool_results:
            # Claude said stop_reason=tool_use but we found zero tool_use
            # blocks — treat as final response.
            break

        messages.append({"role": "user", "content": tool_results})
    else:
        # for-else: loop exhausted all iterations without Claude emitting text
        raise RuntimeError(
            f"tool_loop_max_iterations_exceeded ({MAX_TOOL_ITERATIONS}) — "
            f"Claude kept asking for tools; last call: "
            f"{tool_calls_log[-1] if tool_calls_log else 'none'}"
        )

    # Extract final text response (the JSON decision)
    if resp is None:
        raise RuntimeError("no_response_produced")
    try:
        text_blocks = [b for b in resp.content if getattr(b, "type", None) == "text"]
        text = text_blocks[0].text if text_blocks else ""
    except Exception as e:
        raise RuntimeError(f"response_shape_unexpected: {e}")
    if not text.strip():
        raise RuntimeError("empty_final_response_after_tool_loop")

    # Publish the cumulative cost tally for the caller's info dict (legacy
    # variable names preserved for downstream log strings).
    in_tok  = tick_in_tok
    out_tok = tick_out_tok
    c_read  = tick_cache_read
    c_write = tick_cache_write
    cost    = tick_cost

    try:
        parsed = parse_response(text)
    except ValueError as e:
        raise RuntimeError(f"parse_failed: {e}\n--- raw ---\n{text[:500]}")

    # Validate actions — keeps only well-formed entries.  Claude may return
    # up to 20; validator enforces per-type shape rules.
    raw_actions = parsed.get("actions") or []
    gold = int(state.get("gold", 0) or 0)
    actions, rejected = validate_actions(raw_actions, gold)

    # Predictive-mode: save Claude's next-tick forecast so the NEXT
    # run_tick can load + fast-execute it before scraping.  The plan's
    # own action schema is validated the same way as `actions` above
    # (via validate_actions in the runtime decorate step).  We don't
    # validate here — the next-tick execute path does that with live
    # gold-on-hand as context.
    next_plan = parsed.get("plan_next_tick") or {}
    if isinstance(next_plan, dict) and next_plan.get("actions"):
        save_next_plan(next_plan)
    else:
        # Claude skipped the forecast — discard any stale plan on disk
        # so next tick doesn't execute an ancient prediction.
        invalidate_next_plan()

    # Update memo with Claude's emitted plan so the next tick inherits it.
    emitted_memo = parsed.get("memo_next_tick") or {}
    memo["initialized"]       = True
    memo["overarching_goal"]  = emitted_memo.get("overarching_goal")   or memo.get("overarching_goal")
    memo["current_phase"]     = emitted_memo.get("current_phase")      or memo.get("current_phase")
    memo["short_term_plan"]   = emitted_memo.get("short_term_plan")    or []
    memo["key_observations"]  = emitted_memo.get("key_observations")   or memo.get("key_observations", [])
    memo["open_questions"]    = emitted_memo.get("open_questions")     or []
    memo["last_tick_actions"] = [
        {"type": a.get("type"), "reason": a.get("reason", "")[:120]}
        for a in actions
    ]
    memo["last_strategy_notes"] = (parsed.get("strategy_now") or "")[:500]
    memo["last_situation"]      = (parsed.get("situation") or "")[:500]
    memo["last_multi_tick_plan"] = (parsed.get("strategy_multi_tick") or "")[:500]
    save_memo(memo)

    info = {
        "cost_usd":        cost,
        "total_today_usd": spent + cost,
        "input_tokens":    in_tok,
        "output_tokens":   out_tok,
        "cache_reads":     c_read,
        "cache_writes":    c_write,
        "rejected_count":  len(rejected),
        "rejected_notes":  rejected,
        "tool_calls":      tool_calls_log,   # [{name, args}, ...]
        "raw_situation":       parsed.get("situation", "") or "",
        "raw_strategy_now":    parsed.get("strategy_now", "") or "",
        "raw_strategy_multi":  parsed.get("strategy_multi_tick", "") or "",
    }

    if log_fn:
        sit = (info["raw_situation"] or "")[:180]
        plan = (info["raw_strategy_now"] or "")[:180]
        log_fn(f"  🧠 Claude situation: {sit}", "dim")
        log_fn(f"  🧠 Claude strategy:  {plan}", "battle")
        if tool_calls_log:
            tool_names = [tc["name"] for tc in tool_calls_log]
            log_fn(f"  🔧 Claude tools invoked ({len(tool_names)}): "
                   f"{', '.join(tool_names)}", "dim")
        log_fn(
            f"  💸 API cost: ${cost:.4f}  "
            f"(today ${info['total_today_usd']:.2f}/${CLAUDE_BUDGET_DAILY_USD:.2f})  "
            f"tokens in={in_tok} out={out_tok} cache_read={c_read}",
            "dim",
        )
        if rejected:
            log_fn(f"  ⚠️  {len(rejected)} action(s) rejected at validation "
                   f"(e.g. {rejected[0]})", "dim")

    return actions, memo, info


# ── Helper: build rivals_top10 from private_player_estimates.csv ────────────
def load_rivals_top10(est_csv_path: str = "private_player_estimates.csv",
                      limit: int = 10) -> list:
    """Pull a short list of top rivals for Claude's situational context.
    Sorted by estimated overall (ATK + DEF).  Excludes bots and YOU."""
    if not os.path.isfile(est_csv_path):
        return []
    rows = []
    try:
        with open(est_csv_path, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                name = (r.get("Player") or "").strip()
                if not name or "(YOU)" in name:
                    continue
                if int(r.get("IsBot", 0) or 0) == 1:
                    continue
                try:
                    atk = int(r.get("EstATK", 0) or 0)
                    deff = int(r.get("EstDEF", 0) or 0)
                    conf = float(r.get("ConfScore", 0) or 0)
                except (TypeError, ValueError):
                    continue
                rows.append({
                    "name":     name,
                    "level":    int(r.get("Level", 0) or 0),
                    "race":     (r.get("Race")  or "").strip(),
                    "class":    (r.get("Class") or "").strip(),
                    "atk":      atk,
                    "def":      deff,
                    "spy_off":  int(r.get("EstSpyOff", 0) or 0),
                    "spy_def":  int(r.get("EstSpyDef", 0) or 0),
                    "conf":     round(conf, 2),
                    "score":    atk + deff,
                })
    except Exception:
        return []
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:limit]
