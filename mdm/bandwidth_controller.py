"""
Bandwidth controller for My Download Manager (Stage 5).
Implements a thread-safe token-bucket algorithm supporting global and per-download rate limiting.
Enforces limits collectively across all simultaneous downloads and segments.
"""
import time
import threading
from typing import Optional, Dict, Callable, Any

from mdm.downloader import DownloadCancelledError, DownloadPausedError


class TokenBucket:
    """Thread-safe token bucket rate limiter with burst suppression and reservation."""

    def __init__(self, rate_bps: int):
        self.rate_bps = max(0, int(rate_bps))
        # Keep burst capacity proportional to configured bandwidth (~200ms of tokens, min 512 bytes)
        self.capacity = max(512, int(self.rate_bps * 0.20)) if self.rate_bps > 0 else 0
        self.tokens = float(self.capacity)
        self.last_update = time.monotonic()

    def set_rate(self, rate_bps: int):
        now = time.monotonic()
        self.update(now)
        self.rate_bps = max(0, int(rate_bps))
        if self.rate_bps > 0:
            self.capacity = max(512, int(self.rate_bps * 0.20))
            self.tokens = min(self.tokens, float(self.capacity))
        else:
            self.capacity = 0
            self.tokens = 0.0

    def reset_debt(self):
        """Clears negative tokens/stale debt."""
        if self.tokens < 0:
            self.tokens = 0.0

    def update(self, now: float):
        if self.rate_bps <= 0:
            return
        elapsed = now - self.last_update
        if elapsed > 0:
            self.tokens = min(float(self.capacity), self.tokens + elapsed * self.rate_bps)
            self.last_update = now

    def reserve(self, amount: int, now: float) -> float:
        """
        Updates tokens and reserves `amount`.
        Returns wait_time in seconds until the reserved amount is satisfied.
        Handles chunk sizes larger than capacity by allowing tokens to go negative.
        """
        if self.rate_bps <= 0:
            return 0.0
        self.update(now)
        if self.tokens < amount:
            needed = amount - self.tokens
            wait_time = needed / self.rate_bps
            self.tokens -= amount
            return wait_time
        else:
            self.tokens -= amount
            return 0.0

    def refund(self, amount: int):
        if self.rate_bps > 0:
            self.tokens = min(float(self.capacity), self.tokens + amount)


class GlobalReservation:
    """Tracks an outstanding global bandwidth reservation for exact accounting and reconciliation."""

    def __init__(self, amount: int, generation: int):
        self.amount = int(amount)
        self.generation = int(generation)
        self.invalidated: bool = False
        self.reconciled: bool = False


class BandwidthController:
    """
    Shared bandwidth controller for global and individual download speed limits.
    Thread-safe and supports dynamic limit adjustments, cancellation, and pausing.
    """

    def __init__(
        self,
        global_limit: Optional[int] = None,
        global_enabled: bool = False,
    ):
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self.global_enabled = global_enabled
        self.global_generation = 0
        self.global_bucket = TokenBucket(global_limit or 0)
        self.item_buckets: Dict[str, TokenBucket] = {}
        self._active_global_reservations: set = set()

    @property
    def global_limit(self) -> int:
        with self._lock:
            return self.global_bucket.rate_bps

    @global_limit.setter
    def global_limit(self, rate_bps: int):
        self.set_global_limit(rate_bps)

    def _reconcile_and_invalidate_global_reservations(self):
        """
        Reconciles accounting for all outstanding global reservations and increments generation.
        Ensures:
        - Charged positive tokens are refunded exactly once.
        - Unpaid debt cleared by reset_debt() is NOT refunded (avoiding artificial inflation).
        - Outstanding reservations are marked invalidated and reconciled.
        """
        now = time.monotonic()
        self.global_bucket.update(now)
        tokens = self.global_bucket.tokens
        total_reserved = sum(r.amount for r in self._active_global_reservations)

        if total_reserved > 0:
            if tokens < 0:
                debt = -tokens
                debt_forgiven = min(float(total_reserved), debt)
                refund = float(total_reserved) - debt_forgiven
                self.global_bucket.tokens = 0.0
                if refund > 0:
                    if self.global_bucket.capacity > 0:
                        self.global_bucket.tokens = min(float(self.global_bucket.capacity), refund)
                    else:
                        self.global_bucket.tokens = 0.0
            else:
                refund = float(total_reserved)
                self.global_bucket.reset_debt()
                if refund > 0:
                    if self.global_bucket.capacity > 0:
                        self.global_bucket.tokens = min(float(self.global_bucket.capacity), self.global_bucket.tokens + refund)

            for r in self._active_global_reservations:
                r.invalidated = True
                r.reconciled = True
            self._active_global_reservations.clear()
        else:
            self.global_bucket.reset_debt()

        self.global_generation += 1

    def set_global_limit(self, rate_bps: Optional[int], enabled: Optional[bool] = None):
        """Sets the global bandwidth limit in bytes per second."""
        with self._cond:
            rate = max(0, int(rate_bps)) if rate_bps else 0
            old_rate = self.global_bucket.rate_bps
            was_enabled = self.global_enabled

            turning_off = (enabled is False and was_enabled) or (rate == 0 and old_rate > 0 and was_enabled)
            if turning_off:
                self._reconcile_and_invalidate_global_reservations()

            self.global_bucket.set_rate(rate)
            if enabled is not None:
                self.global_enabled = enabled
            if not self.global_enabled or rate == 0:
                self.global_bucket.reset_debt()
            self._cond.notify_all()

    def set_global_enabled(self, enabled: bool):
        """Enables or disables global speed limiting."""
        with self._cond:
            if not enabled and self.global_enabled:
                self._reconcile_and_invalidate_global_reservations()
            self.global_enabled = enabled
            if not enabled:
                self.global_bucket.reset_debt()
            self._cond.notify_all()

    def invalidate_global_reservations(self):
        """Increments global generation and reconciles accounting for outstanding global reservations."""
        with self._cond:
            self._reconcile_and_invalidate_global_reservations()
            self._cond.notify_all()

    def is_global_enabled(self) -> bool:
        with self._lock:
            return self.global_enabled

    def get_global_limit(self) -> int:
        with self._lock:
            return self.global_bucket.rate_bps

    def set_individual_limit(self, item_id: str, rate_bps: Optional[int]):
        """Sets individual speed limit for a download in bytes per second (0 or None = unlimited)."""
        with self._cond:
            rate = max(0, int(rate_bps)) if rate_bps else 0
            if rate > 0:
                if item_id in self.item_buckets:
                    self.item_buckets[item_id].set_rate(rate)
                else:
                    self.item_buckets[item_id] = TokenBucket(rate)
            else:
                if item_id in self.item_buckets:
                    b = self.item_buckets.pop(item_id)
                    b.reset_debt()
                    b.set_rate(0)
            self._cond.notify_all()

    def get_individual_limit(self, item_id: str) -> int:
        with self._lock:
            bucket = self.item_buckets.get(item_id)
            return bucket.rate_bps if bucket else 0

    def remove_individual_limit(self, item_id: str):
        """Removes the individual speed limit for a download."""
        with self._cond:
            b = self.item_buckets.pop(item_id, None)
            if b:
                b.reset_debt()
                b.set_rate(0)
            self._cond.notify_all()

    def get_effective_chunk_size(
        self,
        arg1: Any = None,
        arg2: Any = None,
    ) -> int:
        """Calculates a chunk size suitable for current rate limits to prevent choppy transfers."""
        if isinstance(arg1, int):
            default_chunk_size = arg1
            item_id = arg2
        elif isinstance(arg1, str):
            item_id = arg1
            default_chunk_size = arg2 if isinstance(arg2, int) else 65536
        else:
            item_id = None
            default_chunk_size = 65536

        with self._lock:
            limits = []
            if self.global_enabled and self.global_bucket.rate_bps > 0:
                limits.append(self.global_bucket.rate_bps)
            if item_id and item_id in self.item_buckets:
                rate = self.item_buckets[item_id].rate_bps
                if rate > 0:
                    limits.append(rate)

            if not limits:
                return default_chunk_size

            min_limit = min(limits)
            # Sample ~5 to 10 times per second for smooth rate limiting without excessive chunks
            target = max(1024, min_limit // 10)
            return min(default_chunk_size, target)

    def acquire(
        self,
        amount: int,
        item_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        is_paused: Optional[Callable[[], bool]] = None,
    ):
        """
        Acquires bandwidth tokens for `amount` bytes.
        Blocks for the required delay if rate limited, checking cancellation, pause,
        and adapting promptly to dynamic rate adjustments.
        """
        if amount <= 0:
            return

        with self._cond:
            now = time.monotonic()
            global_res: Optional[GlobalReservation] = None
            if self.global_enabled and self.global_bucket.rate_bps > 0:
                self.global_bucket.reserve(amount, now)
                global_res = GlobalReservation(amount, self.global_generation)
                self._active_global_reservations.add(global_res)

            reserved_item_bucket: Optional[TokenBucket] = None
            if item_id and item_id in self.item_buckets:
                curr_bucket = self.item_buckets[item_id]
                if curr_bucket.rate_bps > 0:
                    curr_bucket.reserve(amount, now)
                    reserved_item_bucket = curr_bucket

            # Fast path if neither limit is active
            if not global_res and not reserved_item_bucket:
                return

            try:
                while True:
                    # 0. Check if global reservation was invalidated by generation change
                    if global_res and (global_res.invalidated or global_res.generation != self.global_generation):
                        if global_res in self._active_global_reservations:
                            self._active_global_reservations.remove(global_res)
                        global_res = None

                    # 1. Cancellation check
                    if is_cancelled and is_cancelled():
                        if global_res:
                            if global_res in self._active_global_reservations:
                                self._active_global_reservations.remove(global_res)
                            if not global_res.reconciled:
                                self.global_bucket.refund(global_res.amount)
                                global_res.reconciled = True
                            global_res = None
                        if reserved_item_bucket:
                            reserved_item_bucket.refund(amount)
                            reserved_item_bucket = None
                        raise DownloadCancelledError("Download cancelled while waiting for bandwidth.")

                    # 2. Pause check
                    if is_paused and is_paused():
                        if global_res:
                            if global_res in self._active_global_reservations:
                                self._active_global_reservations.remove(global_res)
                            if not global_res.reconciled:
                                self.global_bucket.refund(global_res.amount)
                                global_res.reconciled = True
                            global_res = None
                        if reserved_item_bucket:
                            reserved_item_bucket.refund(amount)
                            reserved_item_bucket = None
                        raise DownloadPausedError("Download paused while waiting for bandwidth.")

                    cur_now = time.monotonic()

                    # 3. Re-evaluate Global Limit
                    rem_global = 0.0
                    if self.global_enabled and self.global_bucket.rate_bps > 0:
                        if global_res is None:
                            self.global_bucket.reserve(amount, cur_now)
                            global_res = GlobalReservation(amount, self.global_generation)
                            self._active_global_reservations.add(global_res)
                        self.global_bucket.update(cur_now)
                        if self.global_bucket.tokens < 0:
                            rem_global = (-self.global_bucket.tokens) / self.global_bucket.rate_bps
                    else:
                        if global_res is not None:
                            if global_res in self._active_global_reservations:
                                self._active_global_reservations.remove(global_res)
                            if not global_res.reconciled:
                                self.global_bucket.refund(global_res.amount)
                                global_res.reconciled = True
                            global_res = None
                        rem_global = 0.0

                    # 4. Re-evaluate Individual Limit
                    rem_item = 0.0
                    active_item_bucket = self.item_buckets.get(item_id) if item_id else None
                    if active_item_bucket and active_item_bucket.rate_bps > 0:
                        if reserved_item_bucket is not active_item_bucket:
                            if reserved_item_bucket is not None:
                                reserved_item_bucket.refund(amount)
                            active_item_bucket.reserve(amount, cur_now)
                            reserved_item_bucket = active_item_bucket
                        active_item_bucket.update(cur_now)
                        if active_item_bucket.tokens < 0:
                            rem_item = (-active_item_bucket.tokens) / active_item_bucket.rate_bps
                    else:
                        if reserved_item_bucket is not None:
                            reserved_item_bucket.refund(amount)
                            reserved_item_bucket = None
                        rem_item = 0.0

                    remaining = max(rem_global, rem_item)
                    if remaining <= 0.001:
                        # Successfully acquired bandwidth: tokens legitimately consumed for transmission
                        if global_res:
                            if global_res in self._active_global_reservations:
                                self._active_global_reservations.remove(global_res)
                            global_res = None
                        break

                    # Sleep on condition variable up to 50ms, wake immediately on rate changes
                    self._cond.wait(timeout=min(0.05, remaining))
            finally:
                if global_res:
                    if global_res in self._active_global_reservations:
                        self._active_global_reservations.remove(global_res)
                    if not global_res.reconciled:
                        self.global_bucket.refund(global_res.amount)
                        global_res.reconciled = True
