"""
Local Worker Scoring Engine

Consumes BeamCore's 24h worker signals (reputation_score, delivery_ratio,
latency_p50_ms, failure_reasons, region) plus a short-term local failure
overlay to rank workers for chunk assignment.

Duck-typed — workers can be Worker dataclass instances or dict[str, Any].
Missing fields fall back to conservative defaults so the scorer stays
functional if BeamCore hasn't rolled out the new schema yet.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Cross-UID poison file. When an orchestrator observes a serious failure
# (stale_timeout, hash_mismatch), it writes the worker_id + timestamp to this
# shared file so the other orchestrators on the same host hard-exclude that
# worker on their next scoring pass. Breaks the "same bad worker fails on 3
# UIDs back-to-back" pattern that the per-process LocalFailureTracker can't.
POISON_FILE = os.environ.get("BEAM_WORKER_POISON_FILE", "/root/beam/worker_poison.json")
POISON_TTL = int(os.environ.get("BEAM_POISON_TTL", "86400"))
                          # 24 hours — matches BeamCore's delivery_ratio window
                          # so a stuck worker can't return until its 24h history
                          # is refreshed. 4h TTL was too lenient: poisoned worker
                          # came back inside the same day still failing.
POISON_CACHE_TTL = 10     # re-read the file at most every 10s to avoid IO churn


class _PoisonCache:
    """Lazy-loading shared poison list. File format: {worker_id: timestamp}."""

    def __init__(self) -> None:
        self._cache: Dict[str, float] = {}
        self._mtime: float = 0.0
        self._last_read: float = 0.0

    def _maybe_reload(self) -> None:
        now = time.time()
        if now - self._last_read < POISON_CACHE_TTL:
            return
        self._last_read = now
        try:
            if not os.path.exists(POISON_FILE):
                self._cache = {}
                return
            mt = os.path.getmtime(POISON_FILE)
            if mt == self._mtime:
                return
            with open(POISON_FILE) as f:
                raw = json.load(f) or {}
            # Normalize: floats only
            self._cache = {str(k): float(v) for k, v in raw.items()}
            self._mtime = mt
        except Exception as e:
            logger.debug(f"poison reload failed: {e}")

    def is_poisoned(self, worker_id: str) -> bool:
        if not worker_id:
            return False
        self._maybe_reload()
        ts = self._cache.get(worker_id)
        return bool(ts and (time.time() - ts) < POISON_TTL)


_poison_cache = _PoisonCache()


# Cross-UID "proven" list — the mirror of poison. When an orchestrator observes
# a worker successfully completing a chunk (task_result with matching bytes),
# it adds the worker_id here so all sibling orchestrators prefer this worker
# on future assignments. Written by orchestrator.py task-completion handler.
# Multiplier applied to the final score (not a hard pick), so if a proven
# worker's live signals tank (overloaded, stale) they can still be excluded.
PROVEN_FILE = os.environ.get("BEAM_WORKER_PROVEN_FILE", "/root/beam/worker_proven.json")
PROVEN_TTL = 86400                # 24h — matches BeamCore's delivery_ratio window
PROVEN_CACHE_TTL = 10
PROVEN_MULT = float(os.environ.get("BEAM_PROVEN_MULT", "3.0"))


class _ProvenCache:
    """Lazy-loading shared proven list. File format: {worker_id: timestamp}."""

    def __init__(self) -> None:
        self._cache: Dict[str, float] = {}
        self._mtime: float = 0.0
        self._last_read: float = 0.0

    def _maybe_reload(self) -> None:
        now = time.time()
        if now - self._last_read < PROVEN_CACHE_TTL:
            return
        self._last_read = now
        try:
            if not os.path.exists(PROVEN_FILE):
                self._cache = {}
                return
            mt = os.path.getmtime(PROVEN_FILE)
            if mt == self._mtime:
                return
            with open(PROVEN_FILE) as f:
                raw = json.load(f) or {}
            self._cache = {str(k): float(v) for k, v in raw.items()}
            self._mtime = mt
        except Exception as e:
            logger.debug(f"proven reload failed: {e}")

    def is_proven(self, worker_id: str) -> bool:
        if not worker_id:
            return False
        self._maybe_reload()
        ts = self._cache.get(worker_id)
        return bool(ts and (time.time() - ts) < PROVEN_TTL)


_proven_cache = _ProvenCache()


# Operator hard-block list — JSON list of worker_ids that are NEVER selected,
# regardless of stats or TTL. Used for known-bad workers we want to permanently
# avoid (e.g. seeded miner-grade poor delivery).
BLACKLIST_FILE = os.environ.get("BEAM_WORKER_BLACKLIST_FILE", "/root/beam/worker_blacklist.json")
# Auto-blacklist: a worker that triggers `stale_timeout` or `hash_mismatch`
# this many times in BLACKLIST_AUTO_WINDOW_S gets promoted from the 24h
# poison list to the permanent blacklist. Catches the "delivered yesterday,
# offline today, then back online to drop chunks again next day" cycle.
BLACKLIST_AUTO_FAILURES = int(os.environ.get("BEAM_BLACKLIST_AUTO_FAILURES", "3"))
BLACKLIST_AUTO_WINDOW_S = int(os.environ.get("BEAM_BLACKLIST_AUTO_WINDOW_S", str(24 * 3600)))


class _BlacklistCache:
    """File format: list of worker_ids, e.g. [\"worker_abc123\", \"worker_def456\"]."""

    def __init__(self) -> None:
        self._cache: set = set()
        self._mtime: float = 0.0

    def _maybe_reload(self) -> None:
        try:
            if not os.path.exists(BLACKLIST_FILE):
                self._cache = set()
                self._mtime = 0.0
                return
            mt = os.path.getmtime(BLACKLIST_FILE)
            if mt == self._mtime:
                return
            with open(BLACKLIST_FILE) as f:
                raw = json.load(f) or []
            if isinstance(raw, list):
                self._cache = {str(w) for w in raw}
            elif isinstance(raw, dict):
                self._cache = {str(w) for w in raw.keys()}
            else:
                self._cache = set()
            self._mtime = mt
        except Exception as e:
            logger.debug(f"blacklist reload failed: {e}")

    def is_blacklisted(self, worker_id: str) -> bool:
        if not worker_id:
            return False
        self._maybe_reload()
        return worker_id in self._cache


_blacklist_cache = _BlacklistCache()


# Tightened add-criteria: a single successful delivery isn't enough — workers
# with one lucky chunk but otherwise-bad delivery_ratio kept getting promoted
# and then dragging SLA down. Require either:
#   (a) BeamCore's 24h delivery_ratio >= MIN_DR, OR
#   (b) 2+ recent successes observed by THIS orch within RECENT_SUCCESS_WINDOW
PROVEN_MIN_DELIVERY_RATIO = float(os.environ.get("BEAM_PROVEN_MIN_DR", "0.5"))
PROVEN_RECENT_SUCCESSES_REQUIRED = int(os.environ.get("BEAM_PROVEN_RECENT_SUCCESSES", "2"))
PROVEN_RECENT_WINDOW_S = int(os.environ.get("BEAM_PROVEN_RECENT_WINDOW_S", "3600"))

# Hard-exclude any worker with non-trivial task history but poor delivery_ratio.
# Defaults: 10 tasks + dr<0.5 → exclude. Tighten for SLA-recovery UIDs by
# setting BEAM_SCORER_MIN_DR_SAMPLE=3 BEAM_SCORER_MIN_DR=0.6 in their env.
EXCLUDE_MIN_TASK_SAMPLE = int(os.environ.get("BEAM_SCORER_MIN_DR_SAMPLE", "10"))
EXCLUDE_MIN_DELIVERY_RATIO = float(os.environ.get("BEAM_SCORER_MIN_DR", "0.5"))
_recent_success_log: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=10))


def _proven_write(worker_id: str, delivery_ratio: Optional[float] = None) -> bool:
    """Conditionally upsert worker into the proven file.

    Add only when delivery_ratio >= PROVEN_MIN_DELIVERY_RATIO OR we've seen
    PROVEN_RECENT_SUCCESSES_REQUIRED successes from this worker within
    PROVEN_RECENT_WINDOW_S. Single-success luck is not enough — that's how
    workers with delivery_ratio=0.15 ended up boosted.

    Returns True if the worker was added/refreshed in the proven file.
    """
    if not worker_id:
        return False
    now = time.time()
    # Record this success in the in-memory rolling window
    log = _recent_success_log[worker_id]
    log.append(now)
    cutoff_recent = now - PROVEN_RECENT_WINDOW_S
    # Prune in-place
    while log and log[0] < cutoff_recent:
        log.popleft()
    recent_successes = len(log)

    dr_ok = delivery_ratio is not None and delivery_ratio >= PROVEN_MIN_DELIVERY_RATIO
    recent_ok = recent_successes >= PROVEN_RECENT_SUCCESSES_REQUIRED
    if not (dr_ok or recent_ok):
        logger.debug(
            f"proven gated for {worker_id[:12]}: dr={delivery_ratio} "
            f"recent={recent_successes}/{PROVEN_RECENT_SUCCESSES_REQUIRED}"
        )
        return False

    try:
        data: Dict[str, float] = {}
        if os.path.exists(PROVEN_FILE):
            try:
                with open(PROVEN_FILE) as f:
                    data = {str(k): float(v) for k, v in (json.load(f) or {}).items()}
            except Exception:
                data = {}
        data[worker_id] = now
        cutoff = now - PROVEN_TTL
        data = {k: v for k, v in data.items() if v > cutoff}
        tmp = PROVEN_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, PROVEN_FILE)
        return True
    except Exception as e:
        logger.debug(f"proven write failed for {worker_id[:12]}: {e}")
        return False


def _poison_write(worker_id: str) -> None:
    """Atomic upsert of worker_id into the poison file."""
    if not worker_id:
        return
    try:
        data: Dict[str, float] = {}
        if os.path.exists(POISON_FILE):
            try:
                with open(POISON_FILE) as f:
                    data = {str(k): float(v) for k, v in (json.load(f) or {}).items()}
            except Exception:
                data = {}
        data[worker_id] = time.time()
        # Prune expired entries to keep file bounded
        cutoff = time.time() - POISON_TTL
        data = {k: v for k, v in data.items() if v > cutoff}
        tmp = POISON_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, POISON_FILE)
    except Exception as e:
        logger.debug(f"poison write failed for {worker_id[:12]}: {e}")


def _blacklist_write(worker_id: str) -> None:
    """Atomic upsert of worker_id into the blacklist file.

    Used by LocalFailureTracker.record() to auto-promote chronic offenders
    from poison (24h) to blacklist (permanent until manual removal).
    """
    if not worker_id:
        return
    try:
        existing: list = []
        if os.path.exists(BLACKLIST_FILE):
            try:
                with open(BLACKLIST_FILE) as f:
                    raw = json.load(f) or []
                existing = list(raw) if isinstance(raw, list) else list(raw.keys())
            except Exception:
                existing = []
        if worker_id in existing:
            return  # already blacklisted
        existing.append(worker_id)
        tmp = BLACKLIST_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(existing, f)
        os.replace(tmp, BLACKLIST_FILE)
        logger.warning(f"AUTO-BLACKLIST: added {worker_id} (chronic failures)")
    except Exception as e:
        logger.debug(f"blacklist write failed for {worker_id[:12]}: {e}")


DEFAULT_REPUTATION = 0.5
DEFAULT_DELIVERY_RATIO = 0.7
DEFAULT_LATENCY_MS = 500.0

LOCAL_FAILURE_TTL = 300          # 5 minutes for soft local overlay
LOCAL_HASH_MISMATCH_TTL = 1800   # 30 minutes hard exclude after local hash fail
LOCAL_TIMEOUT_TTL = 180          # 3 minutes hard exclude after local timeout
DEQUE_MAX = 20

# Live-signal hard-exclude thresholds (from BeamCore worker record).
# Catches overloaded / slow / stale workers whose 24h counters haven't
# caught up yet. These are the ones that show up "active" in BeamCore
# but fail chunk delivery — e.g. p50=7975ms with pending_tasks=25.
MAX_LATENCY_P50_MS = 10000       # > 10s p50 → hard-exclude (matches realistic chunk-delivery budget)
MAX_PENDING_TASKS = 20           # ≥ 20 queued → overloaded, will time out tail assignments
MAX_LAST_SEEN_SECONDS = 90       # 3× heartbeat interval (30s) — tolerate 1 missed beat
# Hoarding-no-delivery: worker holds pending assignments but has never
# completed a single chunk (total_tasks == 0). Catches eu-west-style
# stragglers that BeamCore still flags "active" while they hoard 17+
# chunks without ever delivering. Existing low_delivery_ratio rule needs
# total_tasks ≥ 10 to fire, so this fills the cold-start gap.
HOARDING_PENDING_THRESHOLD = int(os.environ.get("BEAM_SCORER_HOARDING_PENDING", "5"))
# Pending+stale: worker has pending assignments AND missed >1 heartbeat.
# Catches "previously-good worker went offline mid-chunk" — the pattern
# where a worker delivered yesterday but went silent today, still showing
# is_affiliated/active in BeamCore. Distinct from `hoarding_no_delivery`
# (which only fires for total_tasks==0) and `stale_heartbeat` (no pending
# requirement). Together they prevent a previously-good worker from
# silently swallowing chunks after it goes down.
STALE_WITH_PENDING_THRESHOLD_S = int(os.environ.get("BEAM_SCORER_STALE_PENDING_S", "30"))
STALE_WITH_PENDING_MIN = int(os.environ.get("BEAM_SCORER_STALE_PENDING_MIN", "2"))


CONTINENT: Dict[str, str] = {
    "us-east": "na", "us-west": "na", "us-central": "na", "ca-central": "na",
    "us": "na", "na": "na",
    "eu-west": "eu", "eu-central": "eu", "eu-north": "eu", "eu-south": "eu",
    "eu": "eu",
    "ap-south": "as", "ap-east": "as", "ap-southeast": "as", "ap-northeast": "as",
    "ap": "as", "as": "as",
    "sa-east": "sa", "sa": "sa",
    "af-south": "af", "af": "af",
    "me-south": "me", "me": "me",
    "au-east": "oc", "oc": "oc",
    "local": "local",
}

_REGION_WARNED: set = set()


def _continent(region: str) -> str:
    if not region:
        return ""
    r = region.lower()
    if r in CONTINENT:
        return CONTINENT[r]
    if r not in _REGION_WARNED:
        logger.warning(f"worker_scorer: unknown region code '{region}' — treating as neutral")
        _REGION_WARNED.add(r)
    return ""


def region_affinity(worker_region: str, source_region: str, dest_region: str) -> float:
    """
    Return multiplier in [0.85, 1.25].
    Unknown worker region → neutral 1.0.
    Missing task regions → neutral 1.0 (no signal to act on).
    """
    if not worker_region or worker_region.lower() in ("unknown", "", "0.0.0.0"):
        return 1.0
    endpoints = [e for e in (source_region, dest_region) if e]
    if not endpoints:
        return 1.0
    best = 0.85
    wc = _continent(worker_region)
    for endpoint in endpoints:
        if endpoint.lower() == worker_region.lower():
            return 1.25
        if wc and _continent(endpoint) == wc:
            best = max(best, 1.0)
    return best


@dataclass
class TaskContext:
    source_region: str = ""
    dest_region: str = ""
    chunk_size: int = 0


@dataclass
class ScoreComponents:
    quality: float = 0.0
    perf: float = 0.0
    soft_penalty: float = 1.0
    local_penalty: float = 1.0
    region_mult: float = 1.0
    load_mult: float = 1.0
    explore: float = 0.0
    aff_mult: float = 1.0
    proven_mult: float = 1.0
    final: float = 0.0
    excluded_reason: Optional[str] = None


class LocalFailureTracker:
    """
    Per-worker deque of (timestamp, failure_kind) bounded at DEQUE_MAX.

    Asyncio single-threaded, so no lock needed. `prune()` is called
    opportunistically by the scorer before counting — cheap O(N) over a
    bounded deque.
    """

    def __init__(self, ttl_seconds: int = LOCAL_FAILURE_TTL,
                 hash_mismatch_ttl: int = LOCAL_HASH_MISMATCH_TTL) -> None:
        self._events: Dict[str, Deque[Tuple[float, str]]] = defaultdict(
            lambda: deque(maxlen=DEQUE_MAX)
        )
        self.ttl = ttl_seconds
        self.hash_ttl = hash_mismatch_ttl
        # Optional callback set by Orchestrator after construction:
        # `fn(worker_id) -> List[str]` poisons siblings sharing the worker's
        # IP. Wired to WorkerIntel.poison_ip_siblings.
        self._ip_sibling_poison_cb: Optional[Any] = None

    def set_ip_sibling_poison_cb(self, cb: Any) -> None:
        """Inject the IP-correlation callback. None disables the feature."""
        self._ip_sibling_poison_cb = cb

    def record(self, worker_id: str, kind: str) -> None:
        if not worker_id:
            return
        self._events[worker_id].append((time.time(), kind))
        # Share serious failures with sibling orchestrators via the poison
        # file so they hard-exclude on their next score pass.
        if kind in ("stale_timeout", "hash_mismatch"):
            _poison_write(worker_id)
            # Auto-blacklist after BLACKLIST_AUTO_FAILURES serious events in
            # BLACKLIST_AUTO_WINDOW_S. Counts stale_timeout+hash_mismatch from
            # the bounded deque (max 20 events). Chronic offenders graduate
            # from 24h poison to permanent blacklist.
            horizon = time.time() - BLACKLIST_AUTO_WINDOW_S
            chronic = sum(1 for ts, k in self._events[worker_id]
                          if ts >= horizon and k in ("stale_timeout", "hash_mismatch"))
            if chronic >= BLACKLIST_AUTO_FAILURES:
                _blacklist_write(worker_id)
            # IP-correlation: poison all siblings sharing this worker's IP.
            # Catches multi-worker-per-IP operators where one bad worker
            # strongly predicts the rest.
            if self._ip_sibling_poison_cb:
                try:
                    self._ip_sibling_poison_cb(worker_id)
                except Exception as e:
                    logger.debug(f"ip-sibling cb failed for {worker_id[:16]}: {e}")
        elif kind == "worker_error":
            # 3+ worker_error in last 30min → cross-UID poison
            horizon = time.time() - 1800
            recent = sum(1 for ts, k in self._events[worker_id]
                         if ts >= horizon and k == "worker_error")
            if recent >= 3:
                _poison_write(worker_id)

    def _prune_one(self, worker_id: str, horizon: float) -> Deque[Tuple[float, str]]:
        dq = self._events.get(worker_id)
        if not dq:
            return deque()
        while dq and dq[0][0] < horizon:
            dq.popleft()
        return dq

    def count(self, worker_id: str, window_s: int = LOCAL_FAILURE_TTL) -> int:
        horizon = time.time() - window_s
        dq = self._prune_one(worker_id, horizon)
        return len(dq)

    def had_hash_mismatch(self, worker_id: str, ttl: int = LOCAL_HASH_MISMATCH_TTL) -> bool:
        horizon = time.time() - ttl
        dq = self._events.get(worker_id)
        if not dq:
            return False
        return any(ts >= horizon and kind == "hash_mismatch" for ts, kind in dq)

    def had_recent_timeout(self, worker_id: str, ttl: int = LOCAL_TIMEOUT_TTL) -> bool:
        """True if we observed a stale-task timeout on this worker within ttl.

        Uses the `stale_timeout` kind (distinct from the soft-overlay
        `timeout` kind) so recording a single stale-task event produces a
        hard exclude without interfering with the 24h soft-penalty path.
        """
        horizon = time.time() - ttl
        dq = self._events.get(worker_id)
        if not dq:
            return False
        return any(ts >= horizon and kind == "stale_timeout" for ts, kind in dq)

    def stats(self) -> Dict[str, int]:
        return {wid: len(dq) for wid, dq in self._events.items() if dq}


def _getf(w: Any, key: str, default: float) -> float:
    if isinstance(w, dict):
        v = w.get(key)
    else:
        v = getattr(w, key, None)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _geti(w: Any, key: str, default: int = 0) -> int:
    if isinstance(w, dict):
        v = w.get(key, default)
    else:
        v = getattr(w, key, default)
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return default


def _gets(w: Any, key: str, default: str = "") -> str:
    if isinstance(w, dict):
        v = w.get(key, default)
    else:
        v = getattr(w, key, default)
    return str(v) if v is not None else default


def _getb(w: Any, key: str, default: bool = False) -> bool:
    if isinstance(w, dict):
        v = w.get(key, default)
    else:
        v = getattr(w, key, default)
    return bool(v)


def _getd(w: Any, key: str) -> Dict[str, int]:
    if isinstance(w, dict):
        v = w.get(key) or {}
    else:
        v = getattr(w, key, None) or {}
    if not isinstance(v, dict):
        return {}
    out: Dict[str, int] = {}
    for k, val in v.items():
        try:
            out[str(k)] = int(val)
        except (TypeError, ValueError):
            continue
    return out


def _last_seen_age_seconds(w: Any) -> float:
    """Seconds since worker's last heartbeat per BeamCore's `last_seen`.

    BeamCore returns ISO timestamps (may or may not include tz). Treat
    naive as UTC. Missing/unparseable → return 0 (don't hard-exclude
    on missing data; fall back to other signals).
    """
    from datetime import datetime, timezone
    ts = _gets(w, "last_seen")
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return 0.0


class WorkerScorer:
    """
    Ranks workers for chunk assignment using BeamCore 24h signals and a
    short-term local failure overlay.
    """

    def __init__(self, settings: Any, failure_tracker: LocalFailureTracker) -> None:
        self._tracker = failure_tracker
        self.zero_bytes_threshold = int(
            getattr(settings, "worker_scorer_zero_bytes_threshold", 3)
        )
        self.rejected_threshold = int(
            getattr(settings, "worker_scorer_rejected_threshold", 20)
        )
        self.aff_mult = float(getattr(settings, "worker_scorer_affiliated_mult", 1.15))
        self.explore_c = float(getattr(settings, "worker_scorer_explore_c", 0.15))
        self.exclude_counts: Dict[str, int] = defaultdict(int)
        self.explored_new_workers = 0

    def score(
        self,
        worker: Any,
        task_ctx: TaskContext,
        epoch_assignments: int = 0,
        mode: str = "chunked",
    ) -> ScoreComponents:
        sc = ScoreComponents()

        worker_id = _gets(worker, "worker_id")
        fr = _getd(worker, "failure_reasons")
        load = _getf(worker, "load_factor", 0.0)

        # Load factor for dict workers: load_factor is a @property on the
        # dataclass but absent from dict form. Derive from pending_tasks /
        # max_concurrent_tasks when coming from the BeamCore dict.
        if isinstance(worker, dict) and load == 0.0:
            max_tasks = max(1, _geti(worker, "max_concurrent_tasks", 10))
            pending = max(
                _geti(worker, "pending_tasks", 0),
                _geti(worker, "active_tasks", 0),
            )
            load = pending / max_tasks

        # Hard excludes — return final=0.0 with reason.
        # Owner (2026-04-23) confirmed: score-reset tested real selection
        # quality; a 3rd-party team was explicitly seeded with bad workers.
        # Being strict on any early failure signal is correct here.
        if worker_id and _blacklist_cache.is_blacklisted(worker_id):
            sc.excluded_reason = "blacklist"
            self.exclude_counts["blacklist"] += 1
            return sc
        if fr.get("hash_mismatch", 0) >= 1:
            sc.excluded_reason = "hash_mismatch"
            self.exclude_counts["hash_mismatch"] += 1
            return sc
        # zero_bytes hard-exclude: only when the worker's overall delivery_ratio
        # is also poor. A high-dr worker with 14 zero_bytes events out of 100+
        # tasks (still 87% delivery) is a useful worker — don't shut it out.
        zb = fr.get("zero_bytes", 0)
        dr_for_zb = _getf(worker, "delivery_ratio", DEFAULT_DELIVERY_RATIO)
        if zb >= self.zero_bytes_threshold and dr_for_zb < 0.7:
            sc.excluded_reason = "zero_bytes"
            self.exclude_counts["zero_bytes"] += 1
            return sc
        if fr.get("rejected", 0) >= self.rejected_threshold:
            sc.excluded_reason = "rejected_flood"
            self.exclude_counts["rejected_flood"] += 1
            return sc
        # Delivery-ratio hard exclude: worker with meaningful task history
        # but poor completion rate is a seeded-bad-miner pattern.
        dr_live = _getf(worker, "delivery_ratio", DEFAULT_DELIVERY_RATIO)
        total_live = _geti(worker, "total_tasks", 0)
        if total_live >= EXCLUDE_MIN_TASK_SAMPLE and dr_live < EXCLUDE_MIN_DELIVERY_RATIO:
            sc.excluded_reason = "low_delivery_ratio"
            self.exclude_counts["low_delivery_ratio"] += 1
            return sc
        # Reputation hard exclude: BeamCore's own EMA signal.
        rep_live = _getf(worker, "reputation_score", DEFAULT_REPUTATION)
        if rep_live < 0.35:
            sc.excluded_reason = "low_reputation"
            self.exclude_counts["low_reputation"] += 1
            return sc

        # Live-signal hard excludes (catch overloaded/slow/stale workers
        # whose 24h counters haven't caught up). These came from real
        # BeamCore records we saw in prod: e.g. p50=7975ms, pending=25.
        p50_live = _getf(worker, "latency_p50_ms", 0.0)
        if p50_live > MAX_LATENCY_P50_MS:
            sc.excluded_reason = "slow_latency"
            self.exclude_counts["slow_latency"] += 1
            return sc
        pending_live = _geti(worker, "pending_tasks", 0)
        if pending_live >= HOARDING_PENDING_THRESHOLD and total_live == 0:
            sc.excluded_reason = "hoarding_no_delivery"
            self.exclude_counts["hoarding_no_delivery"] += 1
            return sc
        if pending_live >= MAX_PENDING_TASKS:
            sc.excluded_reason = "overloaded_pending"
            self.exclude_counts["overloaded_pending"] += 1
            return sc
        last_seen_age = _last_seen_age_seconds(worker)
        if last_seen_age > MAX_LAST_SEEN_SECONDS:
            sc.excluded_reason = "stale_heartbeat"
            self.exclude_counts["stale_heartbeat"] += 1
            return sc
        # Pending+stale: worker has open assignments AND hasn't been seen in
        # >1 heartbeat. Catches the "delivered yesterday, offline today, still
        # accepting chunks" silent-failure pattern that hurts compliance.
        if (pending_live >= STALE_WITH_PENDING_MIN
                and last_seen_age > STALE_WITH_PENDING_THRESHOLD_S):
            sc.excluded_reason = "stale_with_pending"
            self.exclude_counts["stale_with_pending"] += 1
            return sc
        if worker_id and self._tracker.had_hash_mismatch(worker_id):
            sc.excluded_reason = "local_hash"
            self.exclude_counts["local_hash"] += 1
            return sc
        if worker_id and self._tracker.had_recent_timeout(worker_id):
            sc.excluded_reason = "local_timeout"
            self.exclude_counts["local_timeout"] += 1
            return sc
        # Cross-UID poison: failure observed by another sibling orchestrator
        if worker_id and _poison_cache.is_poisoned(worker_id):
            sc.excluded_reason = "cross_uid_poison"
            self.exclude_counts["cross_uid_poison"] += 1
            return sc
        if load >= 2.0:
            sc.excluded_reason = "overloaded"
            self.exclude_counts["overloaded"] += 1
            return sc

        # Quality: rep × delivery_ratio (both in [0,1], multiplicative)
        rep = max(0.0, min(1.0, _getf(worker, "reputation_score", DEFAULT_REPUTATION)))
        dr = max(0.0, min(1.0, _getf(worker, "delivery_ratio", DEFAULT_DELIVERY_RATIO)))
        sc.quality = rep * dr

        # Performance: bandwidth + latency
        bw = max(0.1, _getf(worker, "bandwidth_mbps", 0.0))
        p50 = max(1.0, _getf(worker, "latency_p50_ms", DEFAULT_LATENCY_MS))
        bw_score = min(1.0, math.log10(1.0 + bw) / math.log10(1001.0))
        lat_score = 200.0 / (200.0 + p50)
        sc.perf = 0.6 * bw_score + 0.4 * lat_score

        # Soft 24h failure penalty
        soft_events = fr.get("timeout", 0) + fr.get("worker_error", 0)
        sc.soft_penalty = max(0.3, 0.97 ** soft_events)

        # Local 5-min overlay
        local_fails = self._tracker.count(worker_id) if worker_id else 0
        sc.local_penalty = 0.5 ** local_fails

        # Region affinity
        sc.region_mult = region_affinity(
            _gets(worker, "region"),
            task_ctx.source_region,
            task_ctx.dest_region,
        )

        # Load headroom — penalizes saturated workers without hard-excluding them
        # until load >= 2.0. Below 1.0 the penalty is ≤0.5; between 1.0 and 2.0
        # it ramps from 0.5 down to 0.2 so overloaded workers stay last-resort.
        if load <= 1.0:
            sc.load_mult = 1.0 - 0.5 * max(0.0, load)
        else:
            sc.load_mult = max(0.2, 0.5 - 0.3 * (load - 1.0))

        # Affiliated bonus
        sc.aff_mult = self.aff_mult if _getb(worker, "is_affiliated") else 1.0

        # Proven bonus — workers observed successfully delivering a chunk in
        # the last 24h get a multiplicative boost. Survives restarts because
        # the proven file is shared across sibling orchestrators.
        if worker_id and _proven_cache.is_proven(worker_id):
            sc.proven_mult = PROVEN_MULT

        # Exploration bonus — chunked mode only, for total_tasks < 100.
        # Threshold raised from 20 to 100 because the external-operator pool
        # is mostly "seasoned" (>20 tasks); the bonus was firing for almost
        # nobody. Wider window lets us occasionally pick workers who had a
        # bad streak but may have recovered.
        total = _geti(worker, "total_tasks", 0)
        if mode == "chunked" and total < 100:
            denom = total + 1
            n = max(1, epoch_assignments)
            sc.explore = min(0.20, self.explore_c * math.sqrt(math.log(max(2, n)) / denom))
            if total < 5:
                self.explored_new_workers += 1

        sc.final = (
            sc.aff_mult
            * sc.proven_mult
            * sc.quality
            * sc.perf
            * sc.soft_penalty
            * sc.local_penalty
            * sc.region_mult
            * sc.load_mult
        ) + sc.explore
        return sc

    def rank(
        self,
        workers: List[Any],
        task_ctx: TaskContext,
        epoch_assignments: int = 0,
        mode: str = "chunked",
    ) -> List[Tuple[Any, ScoreComponents]]:
        scored: List[Tuple[Any, ScoreComponents]] = []
        for w in workers:
            try:
                sc = self.score(w, task_ctx, epoch_assignments, mode)
            except Exception as e:
                logger.warning(f"worker_scorer: error scoring {_gets(w, 'worker_id')[:12]}: {e}")
                sc = ScoreComponents(excluded_reason="scorer_error")
            scored.append((w, sc))
        scored.sort(key=lambda t: t[1].final, reverse=True)
        return scored

    def counter_snapshot(self) -> Dict[str, int]:
        snap = dict(self.exclude_counts)
        snap["explored_new_workers"] = self.explored_new_workers
        return snap
