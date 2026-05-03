from app.middleware.rate_limit import TokenBucketLimiter


def test_rate_limiter_integration():
    """Rate limiter should block after exhausting tokens."""
    limiter = TokenBucketLimiter(rate=3, per_seconds=60)
    for _ in range(3):
        assert limiter.allow("test-user") is True
    assert limiter.allow("test-user") is False


def test_rate_limiter_different_tiers():
    """Different rate tiers for read vs write."""
    read_limiter = TokenBucketLimiter(rate=100, per_seconds=60)
    write_limiter = TokenBucketLimiter(rate=10, per_seconds=60)

    # Read should allow many
    for _ in range(50):
        assert read_limiter.allow("user1") is True

    # Write should block sooner
    for _ in range(10):
        assert write_limiter.allow("user1") is True
    assert write_limiter.allow("user1") is False
