"""Token-bucket rate limiter. Deterministic — time is injected, never real."""

from __future__ import annotations

from lake.api.ratelimit import Limit, RateLimiter


def test_allows_up_to_capacity_then_blocks():
    rl = RateLimiter({"q": Limit(capacity=3, per_seconds=60)})
    for _ in range(3):
        ok, _ = rl.allow("1.2.3.4", "q", now=0.0)
        assert ok
    ok, retry = rl.allow("1.2.3.4", "q", now=0.0)
    assert not ok
    assert retry > 0


def test_refills_over_time():
    rl = RateLimiter({"q": Limit(capacity=60, per_seconds=60)})  # 1 token/sec
    for _ in range(60):
        rl.allow("c", "q", now=0.0)
    assert rl.allow("c", "q", now=0.0)[0] is False

    # after 1 second, one token is back
    assert rl.allow("c", "q", now=1.0)[0] is True
    # but only one
    assert rl.allow("c", "q", now=1.0)[0] is False


def test_burst_capacity_is_capped():
    """A long-idle client gets at most `capacity`, not unbounded accrual."""
    rl = RateLimiter({"q": Limit(capacity=5, per_seconds=5)})
    # idle for an hour
    granted = sum(rl.allow("c", "q", now=3600.0 + i * 0.0)[0] for i in range(100))
    assert granted == 5


def test_clients_are_isolated():
    rl = RateLimiter({"q": Limit(capacity=1, per_seconds=60)})
    assert rl.allow("a", "q", now=0.0)[0] is True
    assert rl.allow("a", "q", now=0.0)[0] is False
    assert rl.allow("b", "q", now=0.0)[0] is True  # b has its own bucket


def test_tiers_are_isolated():
    rl = RateLimiter({"ai": Limit(1, 60), "catalog": Limit(10, 60)})
    assert rl.allow("c", "ai", now=0.0)[0] is True
    assert rl.allow("c", "ai", now=0.0)[0] is False
    assert rl.allow("c", "catalog", now=0.0)[0] is True  # different bucket


def test_unknown_tier_is_never_limited():
    rl = RateLimiter({"q": Limit(1, 60)})
    for _ in range(100):
        assert rl.allow("c", "does-not-exist", now=0.0)[0] is True


def test_retry_after_is_a_sane_estimate():
    rl = RateLimiter({"q": Limit(capacity=1, per_seconds=10)})  # 0.1 tokens/sec
    rl.allow("c", "q", now=0.0)
    ok, retry = rl.allow("c", "q", now=0.0)
    assert not ok
    assert 9.0 <= retry <= 10.0  # ~10s to earn one token back


def test_idle_buckets_are_evicted():
    rl = RateLimiter({"q": Limit(1, 60)}, idle_evict_seconds=100)
    rl.allow("gone", "q", now=0.0)
    assert ("gone", "q") in rl._buckets
    # a request far in the future triggers a sweep
    rl.allow("here", "q", now=1000.0)
    assert ("gone", "q") not in rl._buckets


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeReq:
    def __init__(self, host, headers=None):
        self.client = _FakeClient(host)
        self.headers = headers or {}


def test_client_identity_uses_socket_peer_without_proxy():
    from lake.api.ratelimit import client_identity

    req = _FakeReq("203.0.113.9", {"x-forwarded-for": "1.1.1.1"})
    # peer is not a trusted proxy, so XFF is ignored (anti-spoofing)
    assert client_identity(req, trusted_proxies=frozenset()) == "203.0.113.9"


def test_client_identity_trusts_xff_only_from_a_known_proxy():
    from lake.api.ratelimit import client_identity

    req = _FakeReq("10.0.0.1", {"x-forwarded-for": "198.51.100.7, 10.0.0.1"})
    got = client_identity(req, trusted_proxies=frozenset({"10.0.0.1"}))
    assert got == "198.51.100.7"  # the original client, left-most
