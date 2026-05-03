from app.middleware.rate_limit import TokenBucketLimiter


def test_rate_limiter_allows_under_limit():
    limiter = TokenBucketLimiter(rate=10, per_seconds=60)
    for _ in range(10):
        assert limiter.allow("user1") is True


def test_rate_limiter_blocks_over_limit():
    limiter = TokenBucketLimiter(rate=2, per_seconds=60)
    assert limiter.allow("user1") is True
    assert limiter.allow("user1") is True
    assert limiter.allow("user1") is False


def test_rate_limiter_separate_keys():
    limiter = TokenBucketLimiter(rate=1, per_seconds=60)
    assert limiter.allow("user1") is True
    assert limiter.allow("user2") is True
    assert limiter.allow("user1") is False


def test_rate_limiter_zero_rate():
    limiter = TokenBucketLimiter(rate=0, per_seconds=60)
    assert limiter.allow("user1") is False


def test_rate_limiter_large_burst():
    limiter = TokenBucketLimiter(rate=100, per_seconds=60)
    results = [limiter.allow("user1") for _ in range(100)]
    assert all(results)
    assert limiter.allow("user1") is False
