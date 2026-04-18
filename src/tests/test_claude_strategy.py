"""
Phase "Claude" test harness — autonomous-Claude strategy engine.

Covers the non-network surface of claude_strategy.py:
  1. parse_response: extracts JSON even with markdown fences / leading prose
  2. validate_actions: rejects malformed entries, keeps well-formed ones,
     enforces per-type semantics (unit name, slot, tier range, positive qty)
  3. memo persistence: save → load round-trip preserves fields
  4. budget tracking: record_cost appends rows, budget_today_usd sums
  5. load_api_key: env var wins over file
  6. build_user_content: includes mechanics doc + dynamic state + memo
  7. decide_claude: raises on missing key / over budget / missing SDK
     (with anthropic import monkey-patched away)

No real API calls — we never hit the network in tests.

Run:  python test_claude_strategy.py
"""
import sys, os, json, datetime, tempfile, shutil, importlib

# Windows cp1252 stdout can't encode the check-mark glyphs in the pass
# lines — force UTF-8 so the test output prints instead of crashing.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC  = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Stub playwright (optimizer imports it transitively through nothing here,
# but we keep the pattern consistent with other tests).
import types as _types
pw_mod = _types.ModuleType('playwright')
pw_sync = _types.ModuleType('playwright.sync_api')
pw_sync.sync_playwright = lambda: None
sys.modules['playwright'] = pw_mod
sys.modules['playwright.sync_api'] = pw_sync

import claude_strategy as cs


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp():
    return tempfile.mkdtemp(prefix="dt_claude_test_")


def _with_cwd(tmp, fn):
    prev = os.getcwd()
    os.chdir(tmp)
    try:
        return fn()
    finally:
        os.chdir(prev)


# ── parse_response ──────────────────────────────────────────────────────────
def test_parse_response_clean_json():
    text = '{"situation": "ok", "actions": [], "memo_next_tick": {}}'
    out = cs.parse_response(text)
    _assert(out["situation"] == "ok", f"parse clean json: {out}")
    print("  ✅ parse_response: clean JSON")


def test_parse_response_with_markdown_fence():
    text = 'Here is my plan:\n```json\n{"situation": "ok", "actions": [{"type": "TRAIN", "unit": "soldier", "count": 5}]}\n```\n'
    out = cs.parse_response(text)
    _assert(out["actions"][0]["unit"] == "soldier", f"fenced: {out}")
    print("  ✅ parse_response: tolerates ```json``` fences + leading prose")


def test_parse_response_with_trailing_prose():
    text = '{"situation": "x", "actions": []}\n\nI hope this helps!'
    out = cs.parse_response(text)
    _assert(out["situation"] == "x", f"trailing prose: {out}")
    print("  ✅ parse_response: tolerates trailing prose")


def test_parse_response_errors():
    try:
        cs.parse_response("")
        _assert(False, "empty should raise")
    except ValueError:
        pass
    try:
        cs.parse_response("no braces here")
        _assert(False, "no-JSON should raise")
    except ValueError:
        pass
    try:
        cs.parse_response('{"bad": json]')
        _assert(False, "malformed should raise")
    except ValueError:
        pass
    print("  ✅ parse_response: raises cleanly on empty / no-JSON / malformed")


# ── validate_actions ────────────────────────────────────────────────────────
def test_validate_actions_happy_path():
    raw = [
        {"type": "TRAIN",    "unit": "soldier", "count": 10, "reason": "train some"},
        {"type": "BUY_GEAR", "unit": "soldier", "slot": "weapon", "tier": 5, "qty": 10, "reason": "gear up"},
        {"type": "BUILD",    "name": "Mine", "lv": 2, "reason": "income boost"},
        {"type": "BANK",     "amount": 100000, "reason": "safe keeping"},
    ]
    kept, rej = cs.validate_actions(raw, available_gold=1_000_000)
    _assert(len(kept) == 4, f"kept count: {len(kept)}, rejected: {rej}")
    _assert(len(rej)  == 0, f"no rejections expected: {rej}")
    print(f"  ✅ validate_actions: all 4 good actions pass")


def test_validate_actions_rejects_malformed():
    raw = [
        {"type": "TRAIN", "unit": "dragon", "count": 5},           # bad unit
        {"type": "BUY_GEAR", "unit": "soldier", "slot": "shield", "tier": 5, "qty": 3},  # bad slot
        {"type": "BUY_GEAR", "unit": "soldier", "slot": "weapon", "tier": 15, "qty": 3},  # tier out of range
        {"type": "TRAIN", "unit": "soldier", "count": -1},         # negative
        {"type": "UNKNOWN_TYPE", "foo": "bar"},                     # unknown type
        {"not a dict": True},                                        # non-dict
        "totally wrong",                                             # not even a dict
        {"type": "TRAIN"},                                           # missing required fields
        {"type": "BUILD", "name": "Mine", "lv": 0},                  # lv=0 invalid
        {"type": "BANK", "amount": 0},                               # amount=0 invalid
    ]
    kept, rej = cs.validate_actions(raw, available_gold=1_000_000)
    _assert(len(kept) == 0, f"all should be rejected, kept={kept}")
    _assert(len(rej) >= 8, f"expected many rejections, got {len(rej)}: {rej}")
    print(f"  ✅ validate_actions: rejected {len(rej)} malformed entries")


def test_validate_actions_twenty_cap():
    raw = [
        {"type": "TRAIN", "unit": "soldier", "count": 1, "reason": f"row {i}"}
        for i in range(30)
    ]
    kept, rej = cs.validate_actions(raw, available_gold=999_999_999)
    _assert(len(kept) == 20, f"expected 20 kept (hard cap), got {len(kept)}")
    _assert(len(rej) == 10,  f"expected 10 rejected by cap, got {len(rej)}")
    print("  ✅ validate_actions: enforces 20-action hard cap per tick")


def test_validate_actions_string_ints_coerce():
    """JSON-from-LLM sometimes has numeric fields as strings; the validator
    should coerce them to int without rejecting."""
    raw = [{"type": "TRAIN", "unit": "soldier", "count": "5"}]
    kept, rej = cs.validate_actions(raw, available_gold=1_000_000)
    _assert(len(kept) == 1, f"coerce failed: kept={kept}, rej={rej}")
    _assert(kept[0]["count"] == 5 and isinstance(kept[0]["count"], int),
            f"coerced type: {type(kept[0]['count'])}, value: {kept[0]['count']}")
    print("  ✅ validate_actions: coerces numeric strings to int")


# ── normalize_claude_action (shape drift tolerance) ─────────────────────────
def test_normalize_canonical_is_noop():
    """Already-canonical actions pass through unchanged."""
    a = {"type": "REPAIR_FORT", "damage": 100, "reason": "fort low"}
    out = cs.normalize_claude_action(a)
    _assert(out == a, f"canonical changed: {out}")
    _assert(out is not a, "must return a new dict, not mutate input")
    print("  ✅ normalize: canonical shape is a no-op (returns copy)")


def test_normalize_drift_seen_in_live_run():
    """The exact drift observed in the 2026-04-17 live run:
       action_type / lowercase / params.amount / reasoning.
       Should normalize to canonical REPAIR_FORT shape."""
    drifted = {
        "action_type": "repair_fort",
        "params":      {"amount": 100},
        "reasoning":   "Fort at 2900/3000 HP, 100 × 33.5 = 3350g — cheap",
    }
    out = cs.normalize_claude_action(drifted)
    _assert(out["type"]   == "REPAIR_FORT", f"type: {out.get('type')}")
    _assert(out["damage"] == 100,           f"damage: {out.get('damage')}")
    _assert(out["reason"].startswith("Fort at"), f"reason: {out.get('reason')}")
    _assert("action_type" not in out, f"action_type should be removed: {out}")
    _assert("params"      not in out, f"params should be flattened: {out}")
    _assert("reasoning"   not in out, f"reasoning should be removed: {out}")
    _assert("amount"      not in out, f"amount should be renamed: {out}")
    print("  ✅ normalize: live-run drift (action_type/lowercase/params/reasoning) → canonical")


def test_normalize_train_count_aliases():
    for alias in ("qty", "quantity"):
        a = {"type": "train", "unit": "soldier", alias: 5}
        out = cs.normalize_claude_action(a)
        _assert(out["type"]  == "TRAIN", f"{alias}: type {out}")
        _assert(out["count"] == 5,       f"{alias}: count {out}")
        _assert(alias not in out,        f"{alias}: should be renamed: {out}")
    print("  ✅ normalize: TRAIN accepts qty/quantity as aliases for count")


def test_normalize_preserves_existing_canonical_fields():
    """When BOTH canonical and alias present, keep canonical (don't overwrite)."""
    a = {"type": "REPAIR_FORT", "damage": 50, "amount": 999, "reason": "r", "reasoning": "x"}
    out = cs.normalize_claude_action(a)
    _assert(out["damage"] == 50,  f"damage preserved: {out}")
    _assert(out["reason"] == "r", f"reason preserved: {out}")
    print("  ✅ normalize: canonical fields win over aliases when both present")


def test_validate_accepts_drifted_shape_via_normalizer():
    """End-to-end: drifted shape goes into validate_actions and comes out kept."""
    raw = [{
        "action_type": "repair_fort",
        "params":      {"amount": 100},
        "reasoning":   "fort at 97%",
    }]
    kept, rej = cs.validate_actions(raw, available_gold=1_000_000)
    _assert(len(kept) == 1, f"drifted should be kept: kept={kept}, rej={rej}")
    _assert(kept[0]["type"]   == "REPAIR_FORT", f"kept: {kept[0]}")
    _assert(kept[0]["damage"] == 100,           f"kept: {kept[0]}")
    print("  ✅ validate_actions: drifted shape is normalized + kept (live-run regression)")


# ── Memo persistence ────────────────────────────────────────────────────────
def test_memo_roundtrip():
    tmp = _tmp()
    def _inner():
        memo = {
            "initialized": True,
            "overarching_goal": "Rank 10 def in 7 days",
            "current_phase": "Early gear-up",
            "short_term_plan": ["Mine L2", "Fill Javelins", "Pivot to ATK"],
            "key_observations": ["Got attacked twice", "Jasbob 545k ATK"],
            "open_questions": ["Spy Radagon?"],
            "last_tick_actions": [],
            "tick_count": 5,
        }
        cs.save_memo(memo)
        loaded = cs.load_memo()
        _assert(loaded["overarching_goal"] == memo["overarching_goal"],
                f"overarching: {loaded['overarching_goal']}")
        _assert(loaded["short_term_plan"] == memo["short_term_plan"],
                f"plan: {loaded['short_term_plan']}")
        _assert(loaded["tick_count"] == 5,
                f"tick_count: {loaded['tick_count']}")
        # last_updated is set on save
        _assert("last_updated" in loaded and loaded["last_updated"],
                f"last_updated missing: {loaded}")
        print("  ✅ memo: save/load round-trip preserves fields + stamps last_updated")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_memo_missing_returns_empty():
    tmp = _tmp()
    def _inner():
        m = cs.load_memo()
        _assert(isinstance(m, dict), f"not dict: {m}")
        _assert(m.get("initialized") is False, f"should be fresh: {m}")
        _assert(m.get("tick_count") == 0, f"fresh tick_count: {m}")
        print("  ✅ memo: missing file returns a fresh empty memo")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


# ── Cost tracking ───────────────────────────────────────────────────────────
def test_cost_log_sums_correctly():
    tmp = _tmp()
    def _inner():
        # No log yet → $0 spent
        _assert(cs.budget_today_usd() == 0.0, "empty start should be $0")
        # Record a few calls
        cost1 = cs.record_cost("claude-sonnet-4-5", 5000, 1000, 0, 0, tick_num=1)
        cost2 = cs.record_cost("claude-sonnet-4-5", 5000, 1500, 0, 0, tick_num=2)
        total = cs.budget_today_usd()
        expected = cost1 + cost2
        _assert(abs(total - expected) < 1e-6,
                f"total {total} != sum {expected}")
        spent, under = cs.check_budget()
        _assert(abs(spent - expected) < 1e-6,
                f"check_budget spent {spent} != {expected}")
        _assert(under is True, f"should be under budget: ${spent:.2f}")
        print(f"  ✅ cost log: 2 calls totaling ${expected:.4f}, budget check OK")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_cost_with_cache_reads():
    tmp = _tmp()
    def _inner():
        # Cache read tokens are priced at $0.30/M (10× cheaper than uncached
        # input).  Verify record_cost applies the discount.
        cost = cs.record_cost("claude-sonnet-4-5",
                              in_tokens=500,       # uncached
                              out_tokens=1000,
                              cache_reads=4500,     # cached prefix
                              cache_writes=0)
        # Expected: 500 × 3 / 1M  +  1000 × 15 / 1M  +  4500 × 0.3 / 1M
        expected = 500 * 3 / 1e6 + 1000 * 15 / 1e6 + 4500 * 0.3 / 1e6
        _assert(abs(cost - expected) < 1e-9,
                f"cache-read pricing: got {cost}, expected {expected}")
        print(f"  ✅ cost log: cache-read pricing (${cost:.6f})")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


# ── API key loading ─────────────────────────────────────────────────────────
def test_api_key_env_wins_over_file():
    tmp = _tmp()
    def _inner():
        # File-only
        cs.save_api_key("file-key-abc")
        saved_env = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            _assert(cs.load_api_key() == "file-key-abc",
                    "should read from file when env missing")
            # Now set env — should win
            os.environ["ANTHROPIC_API_KEY"] = "env-key-xyz"
            _assert(cs.load_api_key() == "env-key-xyz",
                    "env var should override file")
        finally:
            if saved_env is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved_env
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)
        print("  ✅ load_api_key: env var wins, file is fallback")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


# ── Prompt builder ──────────────────────────────────────────────────────────
def test_build_user_content_structure():
    state = {
        "level": 23, "gold": 1_000_000, "bank": 0, "turns": 150,
        "xp": 100, "xp_need": 500,
        "atk": 90000, "def": 127000, "spy_off": 13000, "spy_def": 7000,
        "income": 750000, "citizens": 44,
        "workers": 1000, "soldiers": 620, "guards": 520, "spies": 90, "sentries": 60,
        "fort_hp": 100, "fort_max_hp": 100,
        "rank_overall": 28, "rank_offense": 22, "rank_defense": 28, "rank_wealth": 30,
        "buildings": {"Fortification": 1, "Mine": 2},
    }
    cats = [
        {"unit": "soldier", "units": 620, "w_owned": 500, "w_tier": 5,
         "a_owned": 500, "a_tier": 5, "max_t": 5, "score": 0.4},
    ]
    rivals = [
        {"name": "Jasbob", "level": 29, "race": "Undead", "class": "Fighter",
         "atk": 555000, "def": 120000, "spy_off": 500, "spy_def": 800, "conf": 0.8, "score": 675000},
    ]
    memo = cs._empty_memo()
    memo["overarching_goal"] = "Reach rank 15"

    content = cs.build_user_content(state, cats, rivals, memo, tick_num=42)
    _assert(len(content) == 2, f"expected 2 content blocks, got {len(content)}")
    # First block is the cached mechanics doc
    _assert(content[0]["type"] == "text", "first block text")
    _assert("DarkThrone" in content[0]["text"], "mechanics text present")
    _assert(content[0].get("cache_control", {}).get("type") == "ephemeral",
            "mechanics block has cache_control")
    # Second block carries dynamic JSON
    second = content[1]["text"]
    _assert("tick 42" in second, f"tick number in prompt: {second[:200]}")
    _assert("Reach rank 15" in second, "memo flowed through")
    _assert("Jasbob" in second, "rival flowed through")
    _assert("1000" in second or '"workers": 1000' in second,
            "workers count in prompt")
    print("  ✅ build_user_content: mechanics cached + dynamic state/memo/rivals included")


# ── decide_claude preflight failures ────────────────────────────────────────
def test_decide_claude_no_api_key():
    tmp = _tmp()
    def _inner():
        saved_env = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            try:
                cs.decide_claude({}, [], [], tick_num=1)
                _assert(False, "should have raised RuntimeError")
            except RuntimeError as e:
                _assert("no_api_key" in str(e), f"wrong error: {e}")
        finally:
            if saved_env is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved_env
        print("  ✅ decide_claude: raises no_api_key when neither env nor file set")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_decide_claude_over_budget():
    tmp = _tmp()
    def _inner():
        cs.save_api_key("fake-test-key")
        saved_env = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            # Inflate today's spend past the cap by manually injecting rows.
            with open(cs.CLAUDE_COST_LOG, "w", newline="", encoding="utf-8") as f:
                import csv as _csv
                w = _csv.writer(f)
                w.writerow(cs.CLAUDE_COST_LOG_COLUMNS)
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # $6 of cost today — over the $5 cap
                w.writerow([now, "claude-sonnet-4-5", 0, 0, 0, 0, "6.00", 999])

            try:
                cs.decide_claude({"gold": 100}, [], [], tick_num=1)
                _assert(False, "should have raised RuntimeError (over budget)")
            except RuntimeError as e:
                _assert("over_budget" in str(e), f"wrong error: {e}")
        finally:
            if saved_env is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved_env
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)
        print("  ✅ decide_claude: raises over_budget when daily cap exceeded")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_decide_claude_missing_sdk():
    """If the `anthropic` package isn't installed, decide_claude raises
    a clear error (not a generic ImportError) so run_tick knows to fall
    back cleanly."""
    tmp = _tmp()
    def _inner():
        cs.save_api_key("fake-key-for-sdk-test")
        os.environ["ANTHROPIC_API_KEY"] = "fake-env-key"

        # Force `from anthropic import Anthropic` inside decide_claude to fail
        saved = sys.modules.get("anthropic")
        sys.modules["anthropic"] = None   # assignment of None → ImportError
        try:
            try:
                cs.decide_claude({"gold": 100}, [], [], tick_num=1)
                _assert(False, "expected RuntimeError")
            except RuntimeError as e:
                _assert("anthropic_sdk_missing" in str(e), f"wrong error: {e}")
        finally:
            if saved is not None:
                sys.modules["anthropic"] = saved
            else:
                sys.modules.pop("anthropic", None)
            os.environ.pop("ANTHROPIC_API_KEY", None)
        print("  ✅ decide_claude: raises anthropic_sdk_missing when package unavailable")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


# ── Full mock-API round-trip ────────────────────────────────────────────────
class _TextBlock:
    def __init__(self, t):
        self.type = "text"
        self.text = t


class _ToolUseBlock:
    def __init__(self, name, input_args, block_id="t0"):
        self.type = "tool_use"
        self.name = name
        self.input = input_args
        self.id = block_id


class _Usage:
    def __init__(self, in_tok=1000, out_tok=200, c_read=4000, c_write=0):
        self.input_tokens = in_tok
        self.output_tokens = out_tok
        self.cache_read_input_tokens = c_read
        self.cache_creation_input_tokens = c_write


class _FakeMessage:
    """One full API response.  Supports text-only (stop_reason='end_turn')
    or tool-use responses (stop_reason='tool_use' with tool_use blocks)."""
    def __init__(self, content_blocks, stop_reason="end_turn",
                 in_tok=1000, out_tok=200, c_read=4000, c_write=0):
        self.content = content_blocks
        self.stop_reason = stop_reason
        self.usage = _Usage(in_tok, out_tok, c_read, c_write)


class _FakeMessages:
    """Accepts either:
      (a) a single string → always returns that as text (backward compat)
      (b) a list of _FakeMessage → returned in order across create() calls
          (for multi-turn tool-use testing)"""
    def __init__(self, response_spec):
        if isinstance(response_spec, str):
            self._queue = [_FakeMessage([_TextBlock(response_spec)])]
        else:
            self._queue = list(response_spec)
        self.calls = []
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._queue:
            raise RuntimeError("_FakeMessages: ran out of queued responses")
        return self._queue.pop(0)


class _FakeAnthropic:
    def __init__(self, response_spec):
        self.messages = _FakeMessages(response_spec)
    def __call__(self, *a, **kw):
        return self


_SAMPLE_STATE = {
    "level": 23, "gold": 1_000_000, "bank": 0, "turns": 100,
    "xp": 50, "xp_need": 500,
    "atk": 90000, "def": 127000, "spy_off": 13000, "spy_def": 7000,
    "income": 750000, "citizens": 10,
    "workers": 1000, "soldiers": 620, "guards": 520, "spies": 90, "sentries": 60,
    "fort_hp": 100, "fort_max_hp": 100,
    "rank_overall": 28, "rank_offense": 22, "rank_defense": 28, "rank_wealth": 30,
    "buildings": {"Fortification": 1},
}
_SAMPLE_CATS = [
    {"unit": "guard", "units": 520, "w_owned": 300, "w_tier": 5,
     "a_owned": 400, "a_tier": 4, "max_t": 5, "score": 0.3},
]


def _install_fake_anthropic(response_spec):
    """Install a fake anthropic module into sys.modules and return the
    _FakeAnthropic instance so tests can inspect calls[]."""
    fake_anthropic_mod = _types.ModuleType("anthropic")
    fake_client = _FakeAnthropic(response_spec)
    fake_anthropic_mod.Anthropic = lambda api_key=None: fake_client
    sys.modules["anthropic"] = fake_anthropic_mod
    return fake_client


def _final_json_text():
    return json.dumps({
        "situation":     "Level 23, rank 28 def. 1M gold sitting on hand.",
        "strategy_now":  "Bank the gold before next attack window.",
        "strategy_multi_tick": "Build fort, then pivot to T5 gear.",
        "actions": [
            {"type": "BANK", "amount": 500000, "reason": "protect gold"},
            {"type": "BUY_GEAR", "unit": "guard", "slot": "weapon",
             "tier": 5, "qty": 20, "reason": "fill defense gap"},
        ],
        "memo_next_tick": {
            "overarching_goal": "Rank 15 def in 7 days",
            "current_phase":    "Fort + def gear",
            "short_term_plan":  ["Fort L2", "20 more Javelins"],
            "key_observations": ["Gold-on-hand attracts attacks"],
            "open_questions":   ["Spy Jasbob?"],
        },
    })


def test_decide_claude_full_roundtrip_no_tools():
    """Single-response path: Claude answers immediately with the decision,
    no tool calls invoked."""
    tmp = _tmp()
    def _inner():
        os.environ["ANTHROPIC_API_KEY"] = "fake-key"
        try:
            # Single end_turn response containing the JSON action
            _install_fake_anthropic([
                _FakeMessage([_TextBlock(_final_json_text())], stop_reason="end_turn"),
            ])

            actions, memo, info = cs.decide_claude(
                _SAMPLE_STATE, _SAMPLE_CATS, rivals_top10=[], tick_num=100
            )
            _assert(len(actions) == 2, f"expected 2 actions, got {len(actions)}")
            _assert(actions[0]["type"] == "BANK",     f"first: {actions[0]}")
            _assert(actions[1]["type"] == "BUY_GEAR", f"second: {actions[1]}")
            _assert(memo["overarching_goal"] == "Rank 15 def in 7 days",
                    f"memo goal: {memo.get('overarching_goal')}")
            _assert(info["cost_usd"] > 0, f"cost logged: {info}")
            _assert(len(info.get("tool_calls", [])) == 0,
                    f"no tools should have been called: {info['tool_calls']}")
            print(f"  ✅ decide_claude no-tools path: 2 actions, ${info['cost_usd']:.4f}")
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            sys.modules.pop("anthropic", None)
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_decide_claude_tool_use_multi_turn():
    """Two-turn tool flow: Claude first requests get_player_details +
    list_top_rivals, then returns the JSON decision on the second turn."""
    tmp = _tmp()
    def _inner():
        os.environ["ANTHROPIC_API_KEY"] = "fake-key"
        try:
            # Seed a minimal estimates CSV so the tools have data to return
            with open("private_player_estimates.csv", "w", newline="", encoding="utf-8") as f:
                import csv as _csv
                w = _csv.writer(f)
                w.writerow([
                    "Timestamp","Player","Clan","Level","Race","Class",
                    "Population","EstWorkers","ArmySize","BuildingUpgrades",
                    "EstOffUnits","EstDefUnits","EstSpyUnits","EstSentryUnits",
                    "GearTier","UnitTier","EstATK","EstDEF","EstSpyOff","EstSpyDef",
                    "EstIncomeTick","EstIncomeDay","MineLv","Confidence","ConfScore","IsBot",
                ])
                w.writerow([
                    "2026-04-17 02:00","Jasbob","TGO",29,"Undead","Fighter",
                    2757,2000,800,-1,600,200,50,50,
                    5,5,555000,120000,500,800,
                    750000,36000000,2,"CONFIRMED",0.85,0,
                ])
                w.writerow([
                    "2026-04-17 02:00","Radagon","—",18,"Goblin","Cleric",
                    2500,2000,100,-1,20,480,100,100,
                    5,5,10000,530000,4000,3800,
                    500000,24000000,1,"CONFIRMED",0.70,0,
                ])

            # Turn 1: Claude requests two tools
            turn1 = _FakeMessage(
                [
                    _TextBlock("Let me check the landscape first."),
                    _ToolUseBlock("get_player_details", {"name": "Jasbob"}, block_id="t1"),
                    _ToolUseBlock("list_top_rivals", {"sort_by": "def", "limit": 5}, block_id="t2"),
                ],
                stop_reason="tool_use",
            )
            # Turn 2: Claude returns final decision
            turn2 = _FakeMessage(
                [_TextBlock(_final_json_text())],
                stop_reason="end_turn",
            )

            fake = _install_fake_anthropic([turn1, turn2])

            actions, memo, info = cs.decide_claude(
                _SAMPLE_STATE, _SAMPLE_CATS, rivals_top10=[], tick_num=101
            )
            _assert(len(actions) == 2, f"actions count: {len(actions)}")
            # Two tools were invoked
            tools_used = [tc["name"] for tc in info.get("tool_calls", [])]
            _assert(tools_used == ["get_player_details", "list_top_rivals"],
                    f"tool calls: {tools_used}")
            # Two API calls were made (turn 1 with tool_use, turn 2 with final text)
            _assert(len(fake.messages.calls) == 2,
                    f"expected 2 API calls, got {len(fake.messages.calls)}")
            # Somewhere in the conversation, tool_result blocks were sent back
            # (the mock captures by reference so the final list is what we see).
            all_messages = fake.messages.calls[-1]["messages"]
            tool_result_msgs = [m for m in all_messages
                                if m["role"] == "user"
                                and isinstance(m["content"], list)
                                and any(isinstance(b, dict)
                                         and b.get("type") == "tool_result"
                                         for b in m["content"])]
            _assert(len(tool_result_msgs) >= 1,
                    f"tool_result message not present in conversation")
            # And the last message is the assistant's final decision
            _assert(all_messages[-1]["role"] == "assistant",
                    f"last role: {all_messages[-1]['role']}")
            print(f"  ✅ decide_claude tool-use multi-turn: 2 tools invoked → final decision, "
                  f"total ${info['cost_usd']:.4f}")
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            sys.modules.pop("anthropic", None)
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_decide_claude_tool_loop_hits_max_iterations():
    """If Claude never stops requesting tools, we abort after MAX_TOOL_ITERATIONS."""
    tmp = _tmp()
    def _inner():
        os.environ["ANTHROPIC_API_KEY"] = "fake-key"
        try:
            # Repeat tool_use 10× — more than MAX_TOOL_ITERATIONS (8)
            queue = []
            for i in range(10):
                queue.append(_FakeMessage(
                    [_ToolUseBlock("get_server_overview", {}, block_id=f"t{i}")],
                    stop_reason="tool_use",
                ))
            _install_fake_anthropic(queue)
            try:
                cs.decide_claude(_SAMPLE_STATE, _SAMPLE_CATS,
                                 rivals_top10=[], tick_num=102)
                _assert(False, "should have raised tool_loop_max_iterations")
            except RuntimeError as e:
                _assert("tool_loop_max_iterations_exceeded" in str(e),
                        f"wrong error: {e}")
            print("  ✅ decide_claude: aborts after max tool iterations (fallback-safe)")
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            sys.modules.pop("anthropic", None)
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_decide_claude_per_tick_cost_cap():
    """Per-tick cost cap trips → RuntimeError so caller falls back cleanly."""
    tmp = _tmp()
    def _inner():
        os.environ["ANTHROPIC_API_KEY"] = "fake-key"
        try:
            # Each fake response reports a huge token count → cost > $0.50 cap
            expensive_response = _FakeMessage(
                [_ToolUseBlock("get_server_overview", {}, block_id="exp")],
                stop_reason="tool_use",
                in_tok=200_000, out_tok=50_000,
                c_read=0, c_write=0,
            )
            _install_fake_anthropic([expensive_response, expensive_response])
            try:
                cs.decide_claude(_SAMPLE_STATE, _SAMPLE_CATS,
                                 rivals_top10=[], tick_num=103)
                _assert(False, "should have raised per_tick_cost_cap")
            except RuntimeError as e:
                _assert("per_tick_cost_cap_exceeded" in str(e),
                        f"wrong error: {e}")
            print("  ✅ decide_claude: aborts when per-tick cost cap exceeded")
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            sys.modules.pop("anthropic", None)
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


# ── Tool executor tests (without API) ───────────────────────────────────────
def test_tool_get_player_details_not_found():
    tmp = _tmp()
    def _inner():
        out = cs._tool_get_player_details("NobodyHere")
        _assert("not_found" in out, f"expected not_found key: {out}")
        print("  ✅ get_player_details: not_found on unknown player")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_tool_list_top_rivals_basic():
    tmp = _tmp()
    def _inner():
        # Seed estimates CSV
        with open("private_player_estimates.csv", "w", newline="", encoding="utf-8") as f:
            import csv as _csv
            w = _csv.writer(f)
            w.writerow(["Timestamp","Player","Clan","Level","Race","Class",
                        "Population","EstWorkers","ArmySize","BuildingUpgrades",
                        "EstOffUnits","EstDefUnits","EstSpyUnits","EstSentryUnits",
                        "GearTier","UnitTier","EstATK","EstDEF","EstSpyOff","EstSpyDef",
                        "EstIncomeTick","EstIncomeDay","MineLv","Confidence","ConfScore","IsBot"])
            for atk, name in [(555000, "Jasbob"), (400000, "Mungus"), (300000, "Someone")]:
                w.writerow(["2026-04-17 02:00",name,"clan",25,"Undead","Fighter",
                            2500,2000,500,-1,500,100,50,50,5,5,atk,100000,500,500,
                            500000,24000000,1,"OK",0.5,0])

        out = cs._tool_list_top_rivals("atk", 5, False)
        _assert("error" not in out, f"unexpected error: {out}")
        _assert(out["results"][0]["name"] == "Jasbob",
                f"expected Jasbob top: {out['results']}")
        _assert(out["results"][1]["name"] == "Mungus",
                f"expected Mungus second: {out['results']}")
        _assert(len(out["results"]) == 3, f"wrong count: {len(out['results'])}")
        print("  ✅ list_top_rivals: sorted by ATK correctly")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_tool_search_players_filters():
    tmp = _tmp()
    def _inner():
        with open("private_player_estimates.csv", "w", newline="", encoding="utf-8") as f:
            import csv as _csv
            w = _csv.writer(f)
            w.writerow(["Timestamp","Player","Clan","Level","Race","Class",
                        "Population","EstWorkers","ArmySize","BuildingUpgrades",
                        "EstOffUnits","EstDefUnits","EstSpyUnits","EstSentryUnits",
                        "GearTier","UnitTier","EstATK","EstDEF","EstSpyOff","EstSpyDef",
                        "EstIncomeTick","EstIncomeDay","MineLv","Confidence","ConfScore","IsBot"])
            players = [
                ("ElfCleric1", 23, "Elf",  "Cleric",  80_000, 100_000),
                ("ElfCleric2", 29, "Elf",  "Cleric",  150_000, 250_000),
                ("GoblinFight", 25, "Goblin","Fighter", 200_000,  50_000),
            ]
            for name, lv, race, cls, atk, deff in players:
                w.writerow(["2026-04-17",name,"clan",lv,race,cls,
                            2500,2000,100,-1,100,100,50,50,5,5,atk,deff,500,500,
                            500000,24000000,1,"OK",0.5,0])

        out = cs._tool_search_players(race="Elf", class_="Cleric",
                                       level_min=25, level_max=30)
        # Note: "class" is a Python keyword so tool accepts **kwargs; we used
        # the same name in schema.  Validator receives whatever Claude sent.
        # For this test we pass via kwargs using `class` literally:
        out = cs._tool_search_players(race="Elf", **{"class": "Cleric"},
                                       level_min=25, level_max=30)
        _assert(out["match_count"] == 1, f"expected 1 match: {out}")
        _assert(out["results"][0]["name"] == "ElfCleric2",
                f"expected ElfCleric2: {out['results']}")
        print("  ✅ search_players: level + race + class filters applied")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_execute_tool_unknown_returns_error():
    out = cs._execute_tool("nonexistent_tool", {})
    _assert("error" in out, f"should have error key: {out}")
    print("  ✅ _execute_tool: unknown tool returns error, doesn't crash")


# ── Predictive mode: plan persistence + staleness + variance ───────────────
def test_plan_save_and_load_roundtrip():
    tmp = _tmp()
    def _inner():
        plan = {
            "expected_start_gold": 850000,
            "rationale": "Current gold 0 (just banked) + income 850k = ~850k next tick",
            "actions": [
                {"type": "TRAIN",    "unit": "soldier", "count": 400, "reason": "fill army"},
                {"type": "BUY_GEAR", "unit": "soldier", "slot": "armor",
                 "tier": 5, "qty": 20, "reason": "close armor gap"},
            ],
            "fallback_note": "if attacked, skip TRAIN and bank remainder",
        }
        cs.save_next_plan(plan)
        loaded = cs.load_next_plan()
        _assert(loaded is not None, f"load returned None")
        _assert(loaded["expected_start_gold"] == 850000,
                f"gold forecast: {loaded.get('expected_start_gold')}")
        _assert(len(loaded["actions"]) == 2,
                f"actions count: {len(loaded['actions'])}")
        _assert("created_at" in loaded, f"timestamp missing")
        print("  ✅ save_next_plan + load_next_plan: round-trip preserves fields")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_plan_stale_discards_itself():
    tmp = _tmp()
    def _inner():
        # Write a plan with a timestamp 3 hours in the past (> PLAN_STALE_HOURS=2)
        old_ts = (datetime.datetime.now() - datetime.timedelta(hours=3)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        stale_plan = {
            "expected_start_gold": 100000,
            "actions": [{"type": "BANK", "amount": 50000}],
            "created_at": old_ts,
        }
        with open(cs.CLAUDE_NEXT_PLAN_FILE, "w", encoding="utf-8") as f:
            json.dump(stale_plan, f)
        out = cs.load_next_plan()
        _assert(out is None, f"stale plan should be rejected, got {out}")
        print(f"  ✅ load_next_plan: rejects plan older than "
              f"{cs.PLAN_STALE_HOURS}h (pauses / bot restarts)")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_plan_variance_ok_accepts_and_rejects():
    """Variance within threshold → ok; outside → reject.  Uses
    PLAN_VARIANCE_THRESHOLD (default 0.30)."""
    threshold = cs.PLAN_VARIANCE_THRESHOLD
    plan = {"expected_start_gold": 1_000_000}
    # 10% below expected → accept
    ok, var = cs.plan_variance_ok(plan, 900_000)
    _assert(ok is True and var <= threshold,
            f"10% variance should pass: ok={ok}, var={var}")
    # 50% below expected → reject (way outside 30% band)
    ok, var = cs.plan_variance_ok(plan, 500_000)
    _assert(ok is False and var > threshold,
            f"50% variance should reject: ok={ok}, var={var}")
    # Missing expected_start_gold → reject
    ok, var = cs.plan_variance_ok({}, 900_000)
    _assert(ok is False, f"empty plan should reject, ok={ok}")
    # expected_start_gold = 0 → reject (degenerate)
    ok, var = cs.plan_variance_ok({"expected_start_gold": 0}, 500_000)
    _assert(ok is False, f"zero-gold forecast should reject")
    print("  ✅ plan_variance_ok: accepts within threshold, rejects outside / degenerate")


def test_invalidate_next_plan_removes_file():
    tmp = _tmp()
    def _inner():
        cs.save_next_plan({"expected_start_gold": 1000,
                           "actions": [{"type": "BANK", "amount": 500}]})
        _assert(os.path.isfile(cs.CLAUDE_NEXT_PLAN_FILE), "file should exist")
        cs.invalidate_next_plan()
        _assert(not os.path.isfile(cs.CLAUDE_NEXT_PLAN_FILE),
                "invalidate should remove the file")
        # Calling again is a no-op (no file) — shouldn't raise
        cs.invalidate_next_plan()
        print("  ✅ invalidate_next_plan: removes file cleanly, idempotent")
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_decide_claude_saves_plan_when_present():
    """When Claude's response includes plan_next_tick, decide_claude
    persists it to disk via save_next_plan."""
    tmp = _tmp()
    def _inner():
        os.environ["ANTHROPIC_API_KEY"] = "fake-key"
        try:
            # Fake response carries both current-tick actions AND next-tick plan
            response_json = json.dumps({
                "situation":          "Level 23, low gold — fresh post-spend.",
                "strategy_now":       "Nothing to do this tick.",
                "strategy_multi_tick": "Next tick plan the gear push.",
                "actions":            [],
                "plan_next_tick": {
                    "expected_start_gold": 850000,
                    "rationale":           "0 gold + 850k income",
                    "actions": [
                        {"type": "TRAIN", "unit": "soldier", "count": 400, "reason": "fill army"},
                    ],
                    "fallback_note": "if attacked, bank remainder",
                },
                "memo_next_tick": {"overarching_goal": "Rank 15 def"},
            })
            _install_fake_anthropic([
                _FakeMessage([_TextBlock(response_json)], stop_reason="end_turn"),
            ])

            actions, memo, info = cs.decide_claude(
                _SAMPLE_STATE, _SAMPLE_CATS, rivals_top10=[], tick_num=200
            )
            # Plan should now be on disk
            loaded = cs.load_next_plan()
            _assert(loaded is not None, "plan not persisted")
            _assert(loaded["expected_start_gold"] == 850000,
                    f"plan gold wrong: {loaded.get('expected_start_gold')}")
            _assert(len(loaded["actions"]) == 1,
                    f"plan actions: {loaded['actions']}")
            print("  ✅ decide_claude persists plan_next_tick when Claude emits one")
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            sys.modules.pop("anthropic", None)
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def test_decide_claude_invalidates_old_plan_when_empty():
    """If Claude forgets to include plan_next_tick (or emits empty actions),
    decide_claude clears any stale plan on disk — next tick won't execute
    an old prediction against fresh state."""
    tmp = _tmp()
    def _inner():
        # Seed a stale plan
        cs.save_next_plan({"expected_start_gold": 999999,
                           "actions": [{"type": "BANK", "amount": 500000}]})
        _assert(cs.load_next_plan() is not None, "seed plan should exist")

        os.environ["ANTHROPIC_API_KEY"] = "fake-key"
        try:
            response_no_plan = json.dumps({
                "situation": "x", "strategy_now": "y", "strategy_multi_tick": "z",
                "actions": [{"type": "BANK", "amount": 100000, "reason": "small"}],
                # NO plan_next_tick field emitted
                "memo_next_tick": {},
            })
            _install_fake_anthropic([
                _FakeMessage([_TextBlock(response_no_plan)], stop_reason="end_turn"),
            ])
            cs.decide_claude(_SAMPLE_STATE, _SAMPLE_CATS,
                             rivals_top10=[], tick_num=201)
            loaded = cs.load_next_plan()
            _assert(loaded is None,
                    f"stale plan should have been invalidated, got {loaded}")
            print("  ✅ decide_claude invalidates stale plan when Claude emits no forecast")
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            sys.modules.pop("anthropic", None)
    _with_cwd(tmp, _inner)
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 72)
    print("test_claude_strategy.py — autonomous-Claude engine")
    print("=" * 72)
    # Parse / validation / memo / cost / API-key — runtime-independent
    test_parse_response_clean_json()
    test_parse_response_with_markdown_fence()
    test_parse_response_with_trailing_prose()
    test_parse_response_errors()
    test_validate_actions_happy_path()
    test_validate_actions_rejects_malformed()
    test_validate_actions_twenty_cap()
    test_validate_actions_string_ints_coerce()
    # normalize_claude_action — shape drift tolerance (live-run regression)
    test_normalize_canonical_is_noop()
    test_normalize_drift_seen_in_live_run()
    test_normalize_train_count_aliases()
    test_normalize_preserves_existing_canonical_fields()
    test_validate_accepts_drifted_shape_via_normalizer()
    test_memo_roundtrip()
    test_memo_missing_returns_empty()
    test_cost_log_sums_correctly()
    test_cost_with_cache_reads()
    test_api_key_env_wins_over_file()
    test_build_user_content_structure()
    # Preflight failure modes
    test_decide_claude_no_api_key()
    test_decide_claude_over_budget()
    test_decide_claude_missing_sdk()
    # Tool executors (standalone)
    test_tool_get_player_details_not_found()
    test_tool_list_top_rivals_basic()
    test_tool_search_players_filters()
    test_execute_tool_unknown_returns_error()
    # Full API flows (mocked)
    test_decide_claude_full_roundtrip_no_tools()
    test_decide_claude_tool_use_multi_turn()
    test_decide_claude_tool_loop_hits_max_iterations()
    test_decide_claude_per_tick_cost_cap()
    # Predictive mode — plan persistence + staleness + variance
    test_plan_save_and_load_roundtrip()
    test_plan_stale_discards_itself()
    test_plan_variance_ok_accepts_and_rejects()
    test_invalidate_next_plan_removes_file()
    test_decide_claude_saves_plan_when_present()
    test_decide_claude_invalidates_old_plan_when_empty()
    print()
    print("✅ All 36 test cases passed.")


if __name__ == "__main__":
    main()
