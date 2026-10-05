from agent.budget import Budget, BudgetTracker, CallGuard, Usage


def test_cost_uses_model_prices_and_cache_rates():
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_write_tokens=0, cache_read_tokens=1_000_000)
    # sonnet-5-5: $2 in, $10 out, $0.20 cache read
    assert round(u.cost_usd("claude-sonnet-5-5"), 4) == round(2.0 + 1.0 + 0.2, 4)
    assert round(Usage(cache_write_tokens=1_000_000).cost_usd("claude-sonnet-5-5"), 4) == 2.5


def test_usage_adds():
    assert (Usage(1, 2, 3, 4) + Usage(10, 20, 30, 40)).total_tokens == 110


def test_cost_budget_trips():
    t = BudgetTracker(Budget(max_cost_usd=0.01), "claude-sonnet-5-5")
    assert t.exceeded() is None
    t.record(Usage(input_tokens=3000, output_tokens=500))  # $0.011
    assert t.exceeded().startswith("max_cost_usd")


def test_step_and_time_budgets():
    now = [0.0]
    t = BudgetTracker(Budget(max_steps=2, max_wall_s=10), "claude-sonnet-5-5", clock=lambda: now[0])
    t.steps = 2
    assert t.exceeded().startswith("max_steps")
    t.steps = 0
    now[0] = 11.0
    assert t.exceeded().startswith("max_wall_s")


def test_call_guard_blocks_then_aborts():
    g = CallGuard(block_at=3, abort_at=5)
    verdicts = [g.check("run_sql", {"sql": "SELECT 1"}) for _ in range(5)]
    assert verdicts == ["ok", "ok", "block", "block", "abort"]
    assert g.check("run_sql", {"sql": "SELECT 2"}) == "ok"  # a different input is a different call


def test_call_guard_ignores_key_order():
    g = CallGuard(block_at=2)
    g.check("t", {"a": 1, "b": 2})
    assert g.check("t", {"b": 2, "a": 1}) == "block"
