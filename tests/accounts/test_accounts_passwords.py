import hmac

import pytest

from rabota.accounts import passwords


def test_hash_has_scheme_params_salt_and_key():
    h = passwords.hash_password("tulip-garden-77")
    scheme, n, r, p, salt, key = h.split("$")
    assert scheme == "scrypt"
    assert (int(n), int(r), int(p)) == (passwords.SCRYPT_N, passwords.SCRYPT_R, passwords.SCRYPT_P)
    assert salt and key
    assert "tulip" not in h


def test_same_password_gives_different_hashes():
    assert passwords.hash_password("tulip-garden-77") != passwords.hash_password("tulip-garden-77")


def test_verify_accepts_right_and_rejects_wrong():
    h = passwords.hash_password("tulip-garden-77")
    assert passwords.verify_password("tulip-garden-77", h)
    assert not passwords.verify_password("tulip-garden-78", h)
    assert not passwords.verify_password("", h)


def test_unicode_password_roundtrip():
    h = passwords.hash_password("Пароль-со-словами-42")
    assert passwords.verify_password("Пароль-со-словами-42", h)
    assert not passwords.verify_password("пароль-со-словами-42", h)


@pytest.mark.parametrize("junk", ["", "plain", "scrypt$1$2$3", "md5$1$1$1$aa$bb", "scrypt$x$8$1$AAAA$AAAA", "scrypt$16384$8$1$!!$!!"])
def test_verify_garbage_hash_is_false_not_error(junk):
    assert passwords.verify_password("anything", junk) is False


def test_verify_uses_constant_time_compare(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(passwords.hmac, "compare_digest", spy)
    h = passwords.hash_password("tulip-garden-77")
    assert passwords.verify_password("tulip-garden-77", h)
    assert not passwords.verify_password("nope-nope-nope", h)
    assert calls == [(bytes, bytes), (bytes, bytes)]


def test_stored_params_are_honoured_on_verify():
    salt = b"0123456789abcdef"
    key = passwords._derive("pw-for-test-1", salt, 2**10, 8, 1)
    stored = "$".join(("scrypt", "1024", "8", "1", passwords._b64(salt), passwords._b64(key)))
    assert passwords.verify_password("pw-for-test-1", stored)
    assert passwords.needs_rehash(stored)
    assert not passwords.needs_rehash(passwords.hash_password("x" * 10))
    assert passwords.needs_rehash("junk")


def test_burn_verify_does_not_raise():
    passwords.burn_verify("whatever-value")


@pytest.mark.parametrize("pw", ["short1", "1234567", "", "a" * 7])
def test_too_short_rejected(pw):
    assert "не короче" in passwords.validate_password(pw)


def test_too_long_rejected():
    assert "не длиннее" in passwords.validate_password("a1" * 80)


@pytest.mark.parametrize("pw", [
    "password", "Password123", "12345678", "qwertyuiop", "QWERTY123", "11111111", "йцукенгшщ", "Пароль123",
    "abcdefgh", "87654321", "aaaaaaaaaa", "1q2w3e4r", "donetsk123",
])
def test_common_passwords_rejected(pw):
    assert "простой" in passwords.validate_password(pw)


def test_equal_to_email_or_its_local_part_rejected():
    assert "совпадать" in passwords.validate_password("ivan.petrov@example.test", "Ivan.Petrov@example.test")
    assert "совпадать" in passwords.validate_password("ivan.petrov", "ivan.petrov@example.test")


@pytest.mark.parametrize("pw", ["tulip-garden-77", "Кофе и булочки 2026", "x7!kQ9#mZp2w", "correct horse battery"])
def test_good_passwords_accepted(pw):
    assert passwords.validate_password(pw, "ivan@example.test") is None
