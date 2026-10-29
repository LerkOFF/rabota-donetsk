import stat

from rabota.accounts import csrf

SECRET = b"s" * 40


def test_sign_verify_roundtrip():
    seed = csrf.new_seed()
    assert csrf.verify(seed, csrf.sign(seed, SECRET), SECRET)


def test_wrong_secret_or_token_fails():
    seed = csrf.new_seed()
    token = csrf.sign(seed, SECRET)
    assert not csrf.verify(seed, token, b"other" * 8)
    assert not csrf.verify(seed, token[:-1] + ("0" if token[-1] != "0" else "1"), SECRET)
    assert not csrf.verify(csrf.new_seed(), token, SECRET)


def test_missing_or_malformed_values_fail():
    seed = csrf.new_seed()
    assert not csrf.verify(None, csrf.sign(seed, SECRET), SECRET)
    assert not csrf.verify(seed, None, SECRET)
    assert not csrf.verify(seed, "", SECRET)
    assert not csrf.verify("short", csrf.sign("short", SECRET), SECRET)
    assert not csrf.valid_seed("<script>" + "a" * 40)


def test_seeds_are_unique():
    assert csrf.new_seed() != csrf.new_seed()
    assert csrf.valid_seed(csrf.new_seed())


def test_origin_check():
    assert csrf.same_origin(None, "site.test")
    assert csrf.same_origin("https://site.test", "site.test")
    assert not csrf.same_origin("https://evil.test", "site.test")
    assert not csrf.same_origin("null", "site.test")
    assert not csrf.same_origin("https://site.test.evil.test", "site.test")


def test_secret_explicit_wins(tmp_path):
    assert csrf.load_secret("abc", tmp_path) == b"abc"
    assert csrf.load_secret(b"raw", tmp_path) == b"raw"


def test_secret_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_KEY", "from-env-" + "e" * 30)
    assert csrf.load_secret(None, tmp_path) == ("from-env-" + "e" * 30).encode()


def test_secret_file_is_created_once_and_private(monkeypatch, tmp_path):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    first = csrf.load_secret(None, tmp_path)
    second = csrf.load_secret(None, tmp_path)
    assert first == second and len(first) >= 32
    mode = stat.S_IMODE((tmp_path / ".secret_key").stat().st_mode)
    assert mode == 0o600
