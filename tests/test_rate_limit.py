from concurrent.futures import ThreadPoolExecutor

from coordinator.app.ratelimit import RateLimiter


def test_allows_limit_then_blocks_until_window_passes():
    t = [1000.0]
    rl = RateLimiter(3, clock=lambda: t[0])
    assert [rl.check("a") for _ in range(3)] == [0.0, 0.0, 0.0]
    assert 59.0 < rl.check("a") <= 60.0
    assert rl.check("b") == 0.0
    t[0] += 60.0
    assert rl.check("a") == 0.0


def test_zero_disables_limit():
    rl = RateLimiter(0)
    assert all(rl.check("a") == 0.0 for _ in range(1000))


def test_concurrent_checks_never_exceed_limit():
    rl = RateLimiter(50)
    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(lambda _: rl.check("k"), range(400)))
    assert results.count(0.0) == 50


from grid_helpers import ADMIN, USER, auth, enroll


def test_authenticated_identities_have_separate_buckets(make_api):
    api = make_api(rate_limit_per_minute=3)
    token = enroll(api, "w")  # admin bucket: 1
    codes = [api.get("/api/balance", headers=auth(USER)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert int(api.get("/api/balance", headers=auth(USER)).headers["Retry-After"]) >= 1
    assert api.post("/api/worker/claim", headers=auth(token)).status_code == 200
    assert api.get("/api/admin/workers", headers=auth(ADMIN)).status_code == 200  # admin bucket: 2


def test_invalid_tokens_do_not_consume_real_users_budget(make_api):
    api = make_api(rate_limit_per_minute=2)
    for _ in range(5):
        assert api.get("/api/balance", headers=auth("wrong-token")).status_code == 403
    assert api.get("/api/balance", headers=auth(USER)).status_code == 200
