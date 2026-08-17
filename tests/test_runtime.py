"""Tests for ref_checker.runtime: circuit breaker (SourceHealth),
duration formatting, and the reservation-style rate limiter.

Pure unit tests — no network, no stub_sources fixture needed.
"""
from __future__ import annotations

import threading

from ref_checker import runtime as runtime_mod


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


class TestSourceHealth:
    def test_error_increments_counter(self):
        h = runtime_mod.SourceHealth(threshold=3)
        h.record("openalex", "error")
        h.record("openalex", "error")
        assert not h.is_disabled("openalex")
        h.record("openalex", "error")
        assert h.is_disabled("openalex")

    def test_hit_resets_counter(self):
        h = runtime_mod.SourceHealth(threshold=3)
        h.record("openalex", "error")
        h.record("openalex", "error")
        h.record("openalex", "hit_id")
        h.record("openalex", "error")
        h.record("openalex", "error")
        assert not h.is_disabled("openalex")

    def test_not_found_resets_counter(self):
        h = runtime_mod.SourceHealth(threshold=3)
        h.record("openalex", "error")
        h.record("openalex", "error")
        h.record("openalex", "not_found")
        h.record("openalex", "error")
        assert not h.is_disabled("openalex")

    def test_all_scholarly_disabled_detects_full_outage(self):
        from ref_checker.sources.registry import scholarly_source_names

        h = runtime_mod.SourceHealth(threshold=1)
        for name in scholarly_source_names():
            h.record(name, "error")
        assert h.all_scholarly_disabled()

    def test_consecutive_rate_limit_disables_at_threshold(self):
        h = runtime_mod.SourceHealth(rate_limit_threshold=3)
        h.record("openalex", "rate_limited")
        h.record("openalex", "rate_limited")
        assert not h.is_disabled("openalex")
        h.record("openalex", "rate_limited")
        assert h.is_disabled("openalex")

    def test_rate_limit_counter_resets_on_hit(self):
        h = runtime_mod.SourceHealth(rate_limit_threshold=3)
        h.record("openalex", "rate_limited")
        h.record("openalex", "rate_limited")
        h.record("openalex", "hit_id")
        h.record("openalex", "rate_limited")
        h.record("openalex", "rate_limited")
        assert not h.is_disabled("openalex")

    def test_rate_limit_counter_resets_on_not_found(self):
        h = runtime_mod.SourceHealth(rate_limit_threshold=3)
        h.record("openalex", "rate_limited")
        h.record("openalex", "rate_limited")
        h.record("openalex", "not_found")
        h.record("openalex", "rate_limited")
        h.record("openalex", "rate_limited")
        assert not h.is_disabled("openalex")

    def test_rate_limit_does_not_advance_error_counter(self):
        h = runtime_mod.SourceHealth(threshold=3, rate_limit_threshold=10)
        h.record("openalex", "rate_limited")
        h.record("openalex", "rate_limited")
        h.record("openalex", "error")
        h.record("openalex", "error")
        assert not h.is_disabled("openalex")
        h.record("openalex", "error")
        assert h.is_disabled("openalex")

    def test_error_resets_rate_limit_counter(self):
        h = runtime_mod.SourceHealth(threshold=10, rate_limit_threshold=3)
        h.record("openalex", "rate_limited")
        h.record("openalex", "rate_limited")
        h.record("openalex", "error")
        h.record("openalex", "rate_limited")
        h.record("openalex", "rate_limited")
        assert not h.is_disabled("openalex")

    def test_should_log_first_rate_limit_returns_true_once(self):
        h = runtime_mod.SourceHealth()
        assert h.should_log_first_rate_limit("openalex") is True
        assert h.should_log_first_rate_limit("openalex") is False
        assert h.should_log_first_rate_limit("crossref") is True


# --------------------------------------------------------------------------
# _format_duration
# --------------------------------------------------------------------------


class TestFormatDuration:
    def test_seconds(self):
        assert runtime_mod._format_duration(12.3) == "12.3s"

    def test_zero(self):
        assert runtime_mod._format_duration(0.0) == "0.0s"

    def test_negative_clamped(self):
        assert runtime_mod._format_duration(-5.0) == "0.0s"

    def test_just_under_minute(self):
        assert runtime_mod._format_duration(59.9) == "59.9s"

    def test_minutes(self):
        assert runtime_mod._format_duration(222.0) == "3m 42s"

    def test_minute_boundary(self):
        assert runtime_mod._format_duration(60.0) == "1m 00s"

    def test_hours(self):
        assert runtime_mod._format_duration(15092.0) == "4h 11m"

    def test_hour_boundary(self):
        assert runtime_mod._format_duration(3600.0) == "1h 00m"


# --------------------------------------------------------------------------
# _RateLimiter
# --------------------------------------------------------------------------


class TestRateLimiter:
    def test_rate_limiter_strict_spacing_under_contention(self):
        import time

        rl = runtime_mod._RateLimiter({"openalex": 0.05})

        timestamps: list[float] = []
        lock = threading.Lock()

        def worker():
            rl.wait("openalex")
            with lock:
                timestamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        timestamps.sort()
        # Consecutive calls should be spaced at least ~delay apart.
        for a, b in zip(timestamps, timestamps[1:]):
            # small tolerance for scheduling jitter
            assert (b - a) >= 0.04, f"spacing {b - a:.4f} < 0.04"
