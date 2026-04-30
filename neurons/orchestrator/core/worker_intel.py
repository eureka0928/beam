"""
API-driven worker intelligence layer.

Polls /orchestrators/workers periodically and builds:
  1. IP-correlation map — when one worker from an IP triggers a serious
     failure (stale_timeout, hash_mismatch), all siblings from the same
     IP get poisoned. Catches multi-worker-per-IP operators where one
     bad worker is a strong predictor that the rest are also bad.
  2. total_tasks history — workers that hold pending_tasks for an
     extended window without their total_tasks growing are "stagnant"
     (accepted chunks but never delivers). They get auto-poisoned.

The poison is the existing 24h cross-orchestrator file (worker_poison.json),
so any orchestrator instance using the same scorer benefits immediately.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable, Deque, Dict, List, Set, Tuple

logger = logging.getLogger(__name__)


# Stagnation detection: a worker holding ≥STAGNANT_MIN_PENDING tasks for
# STAGNANT_WINDOW_S without total_tasks incrementing is stagnant.
STAGNANT_WINDOW_S = 3600          # 1 hour — generous, only catches truly stuck
STAGNANT_MIN_PENDING = 1
STAGNANT_MIN_SAMPLES = 3          # need ≥3 datapoints to call stagnation
HISTORY_DEQUE_MAX = 30            # ~2.5h at 5min poll interval

PoisonFn = Callable[[str], None]
ListWorkersFn = Callable[[], Awaitable[List[Dict[str, Any]]]]


class WorkerIntel:
    def __init__(self, list_workers_fn: ListWorkersFn, poison_fn: PoisonFn) -> None:
        self._list_workers = list_workers_fn
        self._poison = poison_fn
        # IP correlation maps (rebuilt every refresh)
        self._ip_to_workers: Dict[str, Set[str]] = defaultdict(set)
        self._worker_to_ip: Dict[str, str] = {}
        # total_tasks history per worker_id
        self._history: Dict[str, Deque[Tuple[float, int, int]]] = defaultdict(
            lambda: deque(maxlen=HISTORY_DEQUE_MAX)
        )
        # Already-stagnation-poisoned to avoid re-poisoning churn
        self._stagnation_poisoned: Set[str] = set()
        # Counters for observability
        self.refresh_count = 0
        self.stagnation_poison_count = 0
        self.ip_sibling_poison_count = 0

    async def refresh(self) -> None:
        try:
            workers = await self._list_workers()
        except Exception as e:
            logger.debug(f"WorkerIntel refresh failed: {e}")
            return
        if not workers:
            return
        new_ip_to_workers: Dict[str, Set[str]] = defaultdict(set)
        new_worker_to_ip: Dict[str, str] = {}
        now = time.time()
        for w in workers:
            wid = w.get("worker_id")
            if not wid:
                continue
            ip = w.get("ip")
            if ip and ip not in ("0.0.0.0", "127.0.0.1", ""):
                new_ip_to_workers[ip].add(wid)
                new_worker_to_ip[wid] = ip
            try:
                tt = int(w.get("total_tasks", 0) or 0)
                pt = int(w.get("pending_tasks", 0) or 0)
            except (TypeError, ValueError):
                continue
            self._history[wid].append((now, tt, pt))
        self._ip_to_workers = new_ip_to_workers
        self._worker_to_ip = new_worker_to_ip
        self.refresh_count += 1
        self._detect_stagnant()

    def _detect_stagnant(self) -> None:
        """Scan history; poison any worker that's stagnant in the window."""
        now = time.time()
        cutoff = now - STAGNANT_WINDOW_S
        for wid, hist in list(self._history.items()):
            if wid in self._stagnation_poisoned:
                continue
            in_window = [s for s in hist if s[0] >= cutoff]
            if len(in_window) < STAGNANT_MIN_SAMPLES:
                continue
            first_tt = in_window[0][1]
            last_tt = in_window[-1][1]
            if last_tt > first_tt:
                continue  # tasks grew → worker is alive
            held_pending = all(s[2] >= STAGNANT_MIN_PENDING for s in in_window)
            if not held_pending:
                continue
            logger.warning(
                f"WorkerIntel STAGNANT: {wid} — total_tasks={first_tt} flat "
                f"for {len(in_window)} samples with pending≥{STAGNANT_MIN_PENDING}. "
                f"Poisoning (24h)."
            )
            try:
                self._poison(wid)
            except Exception as e:
                logger.debug(f"stagnation poison failed for {wid[:16]}: {e}")
            self._stagnation_poisoned.add(wid)
            self.stagnation_poison_count += 1

    def poison_ip_siblings(self, worker_id: str) -> List[str]:
        """Poison all workers sharing the failing worker's IP.

        Returns the list of poisoned siblings (excluding the worker_id
        itself, which the caller has already poisoned). No-op if we have
        no IP info for the worker.
        """
        ip = self._worker_to_ip.get(worker_id)
        if not ip:
            return []
        siblings = list(self._ip_to_workers.get(ip, set()) - {worker_id})
        for sib in siblings:
            try:
                self._poison(sib)
            except Exception as e:
                logger.debug(f"IP-sibling poison failed for {sib[:16]}: {e}")
        if siblings:
            self.ip_sibling_poison_count += len(siblings)
            logger.warning(
                f"WorkerIntel IP-CORRELATION: failure on {worker_id[:16]} "
                f"(ip={ip}) → poisoned {len(siblings)} siblings: "
                f"{[s[:16] for s in siblings[:5]]}{'...' if len(siblings) > 5 else ''}"
            )
        return siblings

    def stats(self) -> Dict[str, int]:
        return {
            "refresh_count": self.refresh_count,
            "stagnation_poison_count": self.stagnation_poison_count,
            "ip_sibling_poison_count": self.ip_sibling_poison_count,
            "tracked_workers": len(self._history),
            "tracked_ips": len(self._ip_to_workers),
        }
