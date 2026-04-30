"""Unit tests for the WorkerScorer."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from neurons.orchestrator.core.worker_scorer import (
    LocalFailureTracker,
    ScoreComponents,
    TaskContext,
    WorkerScorer,
    region_affinity,
)


def _settings(**overrides):
    defaults = dict(
        worker_scorer_zero_bytes_threshold=3,
        worker_scorer_rejected_threshold=20,
        worker_scorer_affiliated_mult=1.15,
        worker_scorer_explore_c=0.15,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _mk_worker(**kw):
    base = dict(
        worker_id=kw.pop("worker_id", "w-default"),
        is_affiliated=False,
        region="us-east",
        bandwidth_mbps=100.0,
        latency_p50_ms=100.0,
        reputation_score=0.8,
        delivery_ratio=0.9,
        failure_reasons={},
        total_tasks=50,
        load_factor=0.0,
    )
    base.update(kw)
    return base


def _scorer(**settings_overrides):
    return WorkerScorer(_settings(**settings_overrides), LocalFailureTracker())


def _ctx(source="us-east", dest="us-east"):
    return TaskContext(source_region=source, dest_region=dest)


# --- Hard excludes ---------------------------------------------------------

def test_hash_mismatch_excludes_even_at_one():
    sc = _scorer().score(_mk_worker(failure_reasons={"hash_mismatch": 1}), _ctx())
    assert sc.final == 0.0
    assert sc.excluded_reason == "hash_mismatch"


def test_zero_bytes_threshold():
    scorer = _scorer()
    assert scorer.score(_mk_worker(failure_reasons={"zero_bytes": 2}), _ctx()).excluded_reason is None
    assert scorer.score(_mk_worker(failure_reasons={"zero_bytes": 3}), _ctx()).excluded_reason == "zero_bytes"


def test_rejected_threshold_default_20():
    scorer = _scorer()
    assert scorer.score(_mk_worker(failure_reasons={"rejected": 19}), _ctx()).excluded_reason is None
    assert scorer.score(_mk_worker(failure_reasons={"rejected": 20}), _ctx()).excluded_reason == "rejected_flood"


def test_overloaded_excludes():
    sc = _scorer().score(_mk_worker(load_factor=2.0), _ctx())
    assert sc.excluded_reason == "overloaded"


def test_saturated_worker_not_excluded_but_penalized():
    scorer = _scorer()
    fresh = scorer.score(_mk_worker(load_factor=0.5), _ctx())
    saturated = scorer.score(_mk_worker(load_factor=1.0), _ctx())
    over = scorer.score(_mk_worker(load_factor=1.5), _ctx())
    assert saturated.excluded_reason is None
    assert over.excluded_reason is None
    assert fresh.final > saturated.final > over.final


# --- Monotonicity ----------------------------------------------------------

def test_reputation_is_monotonic():
    scorer = _scorer()
    ctx = _ctx()
    prev = -1.0
    for rep in [0.3, 0.5, 0.7, 0.9, 1.0]:
        score = scorer.score(_mk_worker(reputation_score=rep), ctx).final
        assert score >= prev
        prev = score


def test_delivery_ratio_is_monotonic():
    scorer = _scorer()
    ctx = _ctx()
    prev = -1.0
    for dr in [0.2, 0.5, 0.8, 1.0]:
        score = scorer.score(_mk_worker(delivery_ratio=dr), ctx).final
        assert score >= prev
        prev = score


def test_latency_is_inverse_monotonic():
    scorer = _scorer()
    ctx = _ctx()
    prev = float("inf")
    for p50 in [50, 100, 300, 1000, 5000]:
        score = scorer.score(_mk_worker(latency_p50_ms=p50), ctx).final
        assert score <= prev
        prev = score


# --- Multiplicative quality (no double-smoothing) --------------------------

def test_multiplicative_quality_punishes_disagreement():
    scorer = _scorer()
    ctx = _ctx()
    # (rep=0.9, dr=0.3) = 0.27 — recent regression
    a = scorer.score(_mk_worker(reputation_score=0.9, delivery_ratio=0.3), ctx).final
    # (rep=0.6, dr=0.6) = 0.36 — consistent mediocre
    b = scorer.score(_mk_worker(reputation_score=0.6, delivery_ratio=0.6), ctx).final
    assert a < b


# --- Local failure overlay -------------------------------------------------

def test_local_overlay_halves_per_failure():
    tracker = LocalFailureTracker()
    scorer = WorkerScorer(_settings(), tracker)
    ctx = _ctx()
    wid = "test_overlay_worker"
    # total_tasks=500 zeros out explore so halving is exact (explore is additive)
    base = scorer.score(_mk_worker(worker_id=wid, total_tasks=500), ctx).final
    tracker.record(wid, "timeout")
    after_one = scorer.score(_mk_worker(worker_id=wid, total_tasks=500), ctx).final
    tracker.record(wid, "worker_error")
    after_two = scorer.score(_mk_worker(worker_id=wid, total_tasks=500), ctx).final
    assert after_one < base
    assert after_two < after_one
    assert after_one == pytest.approx(base * 0.5, rel=1e-6)
    assert after_two == pytest.approx(base * 0.25, rel=1e-6)


def test_local_hash_mismatch_excludes_hard():
    tracker = LocalFailureTracker()
    tracker.record("w1", "hash_mismatch")
    scorer = WorkerScorer(_settings(), tracker)
    sc = scorer.score(_mk_worker(worker_id="w1"), _ctx())
    assert sc.excluded_reason == "local_hash"


def test_local_stale_timeout_excludes_hard():
    tracker = LocalFailureTracker()
    tracker.record("w1", "stale_timeout")
    scorer = WorkerScorer(_settings(), tracker)
    sc = scorer.score(_mk_worker(worker_id="w1"), _ctx())
    assert sc.excluded_reason == "local_timeout"
    # A different worker with no stale_timeout remains scoreable
    sc2 = scorer.score(_mk_worker(worker_id="w2"), _ctx())
    assert sc2.excluded_reason is None
    assert sc2.final > 0


def test_cross_uid_poison_excludes_hard(tmp_path, monkeypatch):
    """Worker on the shared poison list is hard-excluded on any orchestrator."""
    import json as _json
    import time as _time
    from neurons.orchestrator.core import worker_scorer as ws

    poison_path = tmp_path / "poison.json"
    poison_path.write_text(_json.dumps({"poisoned_worker": _time.time()}))
    # Point the poison cache at our temp file + reset cache state
    monkeypatch.setattr(ws, "POISON_FILE", str(poison_path))
    ws._poison_cache._cache = {}
    ws._poison_cache._mtime = 0.0
    ws._poison_cache._last_read = 0.0

    scorer = WorkerScorer(_settings(), LocalFailureTracker())
    sc = scorer.score(_mk_worker(worker_id="poisoned_worker"), _ctx())
    assert sc.excluded_reason == "cross_uid_poison"
    # Unpoisoned worker still scores normally
    sc2 = scorer.score(_mk_worker(worker_id="clean_worker"), _ctx())
    assert sc2.excluded_reason is None
    assert sc2.final > 0


def test_cross_uid_poison_expires(tmp_path, monkeypatch):
    """Poison entries older than POISON_TTL are ignored."""
    import json as _json
    import time as _time
    from neurons.orchestrator.core import worker_scorer as ws

    poison_path = tmp_path / "poison.json"
    # 25 hours ago — well past 24-hour TTL
    poison_path.write_text(_json.dumps({"old_worker": _time.time() - 90000}))
    monkeypatch.setattr(ws, "POISON_FILE", str(poison_path))
    ws._poison_cache._cache = {}
    ws._poison_cache._mtime = 0.0
    ws._poison_cache._last_read = 0.0

    scorer = WorkerScorer(_settings(), LocalFailureTracker())
    sc = scorer.score(_mk_worker(worker_id="old_worker"), _ctx())
    assert sc.excluded_reason is None  # expired, should not exclude


def test_record_writes_poison_for_serious_failures(tmp_path, monkeypatch):
    """LocalFailureTracker.record() shares stale_timeout / hash_mismatch."""
    import json as _json
    from neurons.orchestrator.core import worker_scorer as ws

    poison_path = tmp_path / "poison.json"
    monkeypatch.setattr(ws, "POISON_FILE", str(poison_path))

    tracker = LocalFailureTracker()
    tracker.record("w1", "timeout")  # NOT shared (soft only)
    assert not poison_path.exists() or "w1" not in _json.loads(poison_path.read_text())

    tracker.record("w2", "stale_timeout")
    data = _json.loads(poison_path.read_text())
    assert "w2" in data

    tracker.record("w3", "hash_mismatch")
    data = _json.loads(poison_path.read_text())
    assert "w2" in data and "w3" in data


# --- Live-signal hard excludes (p50 latency, pending load, stale heartbeat)

def test_high_latency_p50_excludes_hard():
    scorer = WorkerScorer(_settings(), LocalFailureTracker())
    sc = scorer.score(_mk_worker(latency_p50_ms=7976), _ctx())
    assert sc.excluded_reason == "slow_latency"
    # Just under the threshold still scores
    sc2 = scorer.score(_mk_worker(latency_p50_ms=4999), _ctx())
    assert sc2.excluded_reason is None


def test_high_pending_tasks_excludes_hard():
    scorer = WorkerScorer(_settings(), LocalFailureTracker())
    # Use a generous max_concurrent_tasks so load_factor stays under 1
    # and we isolate the `pending_tasks >= 20` check.
    sc = scorer.score(_mk_worker(pending_tasks=25, max_concurrent_tasks=50), _ctx())
    assert sc.excluded_reason == "overloaded_pending"
    sc2 = scorer.score(_mk_worker(pending_tasks=19, max_concurrent_tasks=50), _ctx())
    assert sc2.excluded_reason is None


def test_stale_last_seen_excludes_hard():
    import time
    from datetime import datetime, timezone, timedelta
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    scorer = WorkerScorer(_settings(), LocalFailureTracker())
    sc = scorer.score(_mk_worker(last_seen=old_ts), _ctx())
    assert sc.excluded_reason == "stale_heartbeat"
    # Fresh heartbeat scores fine
    fresh_ts = datetime.now(timezone.utc).isoformat()
    sc2 = scorer.score(_mk_worker(last_seen=fresh_ts), _ctx())
    assert sc2.excluded_reason is None


def test_local_overlay_prunes_old_entries():
    tracker = LocalFailureTracker(ttl_seconds=1)
    tracker.record("w1", "timeout")
    assert tracker.count("w1", window_s=1) == 1
    time.sleep(1.1)
    assert tracker.count("w1", window_s=1) == 0


# --- Region affinity -------------------------------------------------------

def test_region_affinity_exact_match_beats_other():
    assert region_affinity("us-east", "us-east", "us-east") == 1.25
    assert region_affinity("us-east", "eu-west", "eu-west") == 0.85


def test_region_affinity_same_continent():
    val = region_affinity("us-east", "us-west", "us-west")
    assert val == 1.0


def test_region_affinity_unknown_is_neutral():
    assert region_affinity("unknown", "us-east", "eu-west") == 1.0
    assert region_affinity("", "us-east", "eu-west") == 1.0
    assert region_affinity("us-east", "", "") == 1.0


# --- Graceful degradation --------------------------------------------------

def test_missing_new_fields_produces_nonzero_score():
    scorer = _scorer()
    minimal = {"worker_id": "w", "bandwidth_mbps": 50.0, "total_tasks": 5}
    sc = scorer.score(minimal, _ctx())
    assert sc.excluded_reason is None
    assert sc.final > 0


# --- Exploration bonus -----------------------------------------------------

def test_exploration_only_below_100_tasks():
    scorer = _scorer()
    ctx = _ctx()
    new_worker = _mk_worker(total_tasks=0, reputation_score=0.5, delivery_ratio=0.7)
    sc = scorer.score(new_worker, ctx, epoch_assignments=100)
    assert sc.explore > 0.0

    seasoned = _mk_worker(total_tasks=500, reputation_score=0.5, delivery_ratio=0.7)
    sc2 = scorer.score(seasoned, ctx, epoch_assignments=100)
    assert sc2.explore == 0.0


def test_exploration_disabled_in_retry_mode():
    scorer = _scorer()
    sc = scorer.score(_mk_worker(total_tasks=0), _ctx(), epoch_assignments=100, mode="retry")
    assert sc.explore == 0.0


def test_exploration_cannot_override_hard_exclude():
    scorer = _scorer()
    sc = scorer.score(
        _mk_worker(total_tasks=0, failure_reasons={"hash_mismatch": 1}),
        _ctx(),
        epoch_assignments=1000,
    )
    assert sc.final == 0.0


# --- Affiliated bonus ------------------------------------------------------

def test_affiliated_bonus_applied():
    scorer = _scorer()
    ctx = _ctx()
    a = scorer.score(_mk_worker(is_affiliated=True), ctx).final
    b = scorer.score(_mk_worker(is_affiliated=False), ctx).final
    assert a > b
    assert a == pytest.approx(b * 1.15, rel=0.01)


# --- Proven worker bonus ---------------------------------------------------

def test_proven_worker_gets_multiplier(tmp_path, monkeypatch):
    """Workers on the shared proven list get a score boost."""
    import json as _json
    import time as _time
    from neurons.orchestrator.core import worker_scorer as ws

    proven_path = tmp_path / "proven.json"
    proven_path.write_text(_json.dumps({"magic_worker": _time.time()}))
    monkeypatch.setattr(ws, "PROVEN_FILE", str(proven_path))
    monkeypatch.setattr(ws, "PROVEN_MULT", 3.0)
    ws._proven_cache._cache = {}
    ws._proven_cache._mtime = 0.0
    ws._proven_cache._last_read = 0.0

    scorer = WorkerScorer(_settings(), LocalFailureTracker())
    proven = scorer.score(_mk_worker(worker_id="magic_worker"), _ctx())
    normal = scorer.score(_mk_worker(worker_id="normal_worker"), _ctx())
    assert proven.proven_mult == 3.0
    assert normal.proven_mult == 1.0
    # Explore bonus is additive so the ratio isn't exactly 3x — compare the
    # multiplicative core via (final - explore) / (final - explore).
    proven_core = proven.final - proven.explore
    normal_core = normal.final - normal.explore
    assert proven_core == pytest.approx(normal_core * 3.0, rel=0.01)


def test_proven_write_blocked_on_low_dr_single_success(tmp_path, monkeypatch):
    """Single success with low delivery_ratio should NOT add to proven."""
    import json as _json
    from neurons.orchestrator.core import worker_scorer as ws
    proven_path = tmp_path / "proven.json"
    monkeypatch.setattr(ws, "PROVEN_FILE", str(proven_path))
    ws._recent_success_log.clear()
    # delivery_ratio=0.15 → fails dr_ok; single success → fails recent_ok
    ok = ws._proven_write("flaky_worker", delivery_ratio=0.15)
    assert ok is False
    assert not proven_path.exists()


def test_proven_write_admits_high_dr_single_success(tmp_path, monkeypatch):
    """delivery_ratio >= 0.5 with single success admits to proven."""
    import json as _json
    from neurons.orchestrator.core import worker_scorer as ws
    proven_path = tmp_path / "proven.json"
    monkeypatch.setattr(ws, "PROVEN_FILE", str(proven_path))
    ws._recent_success_log.clear()
    ok = ws._proven_write("solid_worker", delivery_ratio=0.75)
    assert ok is True
    data = _json.loads(proven_path.read_text())
    assert "solid_worker" in data


def test_proven_write_admits_two_recent_successes_low_dr(tmp_path, monkeypatch):
    """2 recent successes admit even if delivery_ratio is low/None."""
    import json as _json
    from neurons.orchestrator.core import worker_scorer as ws
    proven_path = tmp_path / "proven.json"
    monkeypatch.setattr(ws, "PROVEN_FILE", str(proven_path))
    ws._recent_success_log.clear()
    # 1st success — dr=0.10 (fails dr_ok), 1 recent (fails recent_ok)
    ok1 = ws._proven_write("recovering_worker", delivery_ratio=0.10)
    assert ok1 is False
    # 2nd success within window — now 2 recent successes, recent_ok kicks in
    ok2 = ws._proven_write("recovering_worker", delivery_ratio=0.10)
    assert ok2 is True


def test_proven_does_not_override_hard_exclude(tmp_path, monkeypatch):
    """Proven worker still gets hard-excluded on hash_mismatch."""
    import json as _json
    import time as _time
    from neurons.orchestrator.core import worker_scorer as ws

    proven_path = tmp_path / "proven.json"
    proven_path.write_text(_json.dumps({"magic_worker": _time.time()}))
    monkeypatch.setattr(ws, "PROVEN_FILE", str(proven_path))
    ws._proven_cache._cache = {}
    ws._proven_cache._mtime = 0.0
    ws._proven_cache._last_read = 0.0

    scorer = WorkerScorer(_settings(), LocalFailureTracker())
    sc = scorer.score(
        _mk_worker(worker_id="magic_worker", failure_reasons={"hash_mismatch": 1}),
        _ctx(),
    )
    assert sc.excluded_reason == "hash_mismatch"


# --- Ranking ---------------------------------------------------------------

def test_rank_orders_by_score_and_excludes():
    scorer = _scorer()
    workers = [
        _mk_worker(worker_id="good", reputation_score=0.95, delivery_ratio=0.98),
        _mk_worker(worker_id="bad-hash", failure_reasons={"hash_mismatch": 1}),
        _mk_worker(worker_id="mediocre", reputation_score=0.5, delivery_ratio=0.6),
    ]
    ranked = scorer.rank(workers, _ctx())
    picked = [w["worker_id"] for w, sc in ranked if sc.excluded_reason is None]
    assert "bad-hash" not in picked
    assert picked[0] == "good"
    assert picked[1] == "mediocre"
