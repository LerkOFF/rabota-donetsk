from rabota.accounts.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(limits=None):
    clock = FakeClock()
    return RateLimiter({"b": (3, 60), **(limits or {})}, clock=clock), clock


def test_allows_up_to_limit_then_refuses():
    rl, _ = make()
    assert [rl.allow("b", "k") for _ in range(5)] == [True, True, True, False, False]


def test_keys_are_independent():
    rl, _ = make()
    for _ in range(3):
        rl.allow("b", "a")
    assert not rl.allow("b", "a")
    assert rl.allow("b", "other")


def test_window_slides():
    rl, clock = make()
    for _ in range(3):
        rl.allow("b", "k")
    clock.t += 61
    assert rl.allow("b", "k")


def test_refused_hits_do_not_extend_the_block():
    rl, clock = make()
    for _ in range(3):
        rl.allow("b", "k")
    clock.t += 30
    assert not rl.allow("b", "k")
    clock.t += 31  # первые события вышли из окна
    assert rl.allow("b", "k")


def test_blocked_record_reset():
    rl, _ = make()
    assert not rl.blocked("b", "k")
    for _ in range(3):
        rl.record("b", "k")
    assert rl.blocked("b", "k")
    rl.reset("b", "k")
    assert not rl.blocked("b", "k")


def test_unknown_or_zero_bucket_is_unlimited():
    rl, _ = make({"z": (0, 10)})
    assert all(rl.allow("missing", "k") for _ in range(100))
    assert all(rl.allow("z", "k") for _ in range(100))
    assert not rl.blocked("z", "k")


def test_sweep_drops_stale_keys():
    rl, clock = make()
    for i in range(50):
        rl.allow("b", f"k{i}")
    assert rl.size() == 50
    clock.t += 120
    rl.sweep()
    assert rl.size() == 0


def test_automatic_cleanup_keeps_memory_bounded():
    rl, clock = make()
    for i in range(400):
        rl.allow("b", f"k{i}")
    clock.t += 120
    for i in range(200):  # сработает периодическая очистка
        rl.allow("b", f"new{i}")
    assert rl.size() <= 200 + 1


def test_default_limits_cover_all_flows():
    rl = RateLimiter()
    for bucket in ("login_ip", "login_email", "register_ip", "register_email", "resend_ip", "resend_email",
                   "forgot_ip", "forgot_email", "token_ip", "change_email_user", "password_user", "delete_user"):
        assert bucket in rl.limits
