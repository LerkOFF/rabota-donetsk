"""Пароли: хеш через scrypt из стандартной библиотеки, проверка в постоянное время, правила надёжности."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from functools import cache
from itertools import pairwise

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32
MIN_LENGTH = 8
MAX_LENGTH = 128  # длинный пароль нельзя отдавать в scrypt: это способ нагрузить сервер

_SCHEME = "scrypt"

COMMON_PASSWORDS = frozenset("""
12345678 123456789 1234567890 12345678910 0123456789 987654321 87654321 11111111 00000000 22222222
123123123 112233445 11223344 1q2w3e4r 1q2w3e4r5t 1qaz2wsx 1qazxsw2 qwertyui qwertyuiop qwerty123
qwerty12 qwerty1234 qwertyuiop123 asdfghjk asdfghjkl asdf1234 zxcvbnm1 zxcvbnm123 q1w2e3r4 q1w2e3r4t5
password password1 password12 password123 password1234 passw0rd p@ssw0rd p@ssword pa$$w0rd
iloveyou iloveyou1 letmein1 welcome1 welcome123 admin123 administrator abc12345 abcd1234 abcdefgh
abcdefg1 trustno1 monkey123 dragon123 football football1 baseball master123 superman sunshine1
princess1 shadow123 michael1 charlie1 qazwsxedc qazwsx123 zaq12wsx 1234qwer 12341234 123456qwe
123qweasd 123qwe123 qweasdzxc qweqweqwe qwe12345 qwe123456 a1234567 a12345678 aa123456 aaaaaaaa
aaaaaaaaa 99999999 88888888 77777777 66666666 55555555 44444444 33333333 12121212 13131313 14141414
147258369 159753456 741852963 789456123 963852741 1357924680 24681012 121212121 1111111111
пароль парольпароль пароль1 пароль12 пароль123 пароль1234 привет123 привет1234 йцукенгш йцукенгшщ
йцукен123 йцукенгшщз qwertyйцукен 12345678q 1234567q q1234567 q12345678 qq123456 qq123456789
donetsk donetsk123 donetsk1 donbass donbass123 rabota123 rabota1234 rabota12 работа123 работа1234
vacancy123 12345qwert 12345qwerty qwert12345 qwerty12345 qwerty123456 password!
""".split())  # noqa: SIM905


def hash_password(password: str) -> str:
    """Строка вида scrypt$N$r$p$соль$хеш (соль и хеш в base64). Параметры хранятся в строке, их можно усиливать."""
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join((_SCHEME, str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P), _b64(salt), _b64(key)))


def verify_password(password: str, stored: str) -> bool:
    """Сравнение в постоянное время. Любой мусор вместо хеша даёт False, а не исключение."""
    try:
        scheme, n, r, p, salt_b64, key_b64 = stored.split("$")
        if scheme != _SCHEME:
            return False
        salt, expected = _unb64(salt_b64), _unb64(key_b64)
        actual = _derive(password, salt, int(n), int(r), int(p), len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    """True, если хеш сделан слабее текущих параметров."""
    try:
        _, n, r, p, *_ = stored.split("$")
        return (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
    except ValueError:
        return True


# Хеш для сравнения, когда пользователя с такой почтой нет: время ответа не выдаёт, есть ли почта.
@cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def burn_verify(password: str) -> None:
    """Потратить столько же времени, сколько тратит настоящая проверка."""
    verify_password(password, _dummy_hash())


def validate_password(password: str, email: str = "") -> str | None:
    """Текст ошибки по-русски или None, если пароль подходит."""
    if len(password) < MIN_LENGTH:
        return f"Пароль не короче {MIN_LENGTH} знаков."
    if len(password) > MAX_LENGTH:
        return f"Пароль не длиннее {MAX_LENGTH} знаков."
    low = password.casefold()
    mail = email.strip().casefold()
    if mail and (low == mail or low == mail.split("@")[0]):
        return "Пароль не должен совпадать с почтой."
    if low in COMMON_PASSWORDS or len(set(low)) == 1 or _is_sequence(low):
        return "Этот пароль слишком простой. Придумайте другой: из слов, цифр и знаков."
    if low.isdigit() and len(set(low)) <= 3:
        return "Этот пароль слишком простой. Придумайте другой: из слов, цифр и знаков."
    return None


def _is_sequence(s: str) -> bool:
    """Цифры или буквы строго подряд: 12345678, 87654321, abcdefgh."""
    if len(s) < MIN_LENGTH:
        return False
    steps = {ord(b) - ord(a) for a, b in pairwise(s)}
    return steps in ({1}, {-1})


def _derive(password: str, salt: bytes, n: int, r: int, p: int, dklen: int = KEY_BYTES) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=256 * 1024 * 1024
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)
