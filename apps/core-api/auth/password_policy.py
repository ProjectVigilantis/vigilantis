"""새 비밀번호 정책 (#492 PR 3) — Cognito 호출 전에 서버가 확인하는 순수 함수.

길이(스키마 상수), 대문자 · 소문자 · 숫자 · 기호 각 1자 이상. 기호는 AWS 공식 목록(ASCII 구두점 32자)뿐이다.
User Pool 정책보다 좁은 허용 목록이다: 공백 · 비 ASCII 문자는 모두 거절하며, 입력을 다듬거나 정규화하지 않는다.
"""

from __future__ import annotations

import string

from schemas.api.auth import NEW_PASSWORD_MIN_LENGTH as PASSWORD_MIN_LENGTH
from schemas.api.auth import PASSWORD_MAX_LENGTH

#: ^ $ * . [ ] { } ( ) ? " ! @ # % & / \ , > < ' : ; | _ ~ ` = + -
PASSWORD_SYMBOLS = frozenset("^$*.[]{}()?\"!@#%&/\\,><':;|_~`=+-")

_UPPER = frozenset(string.ascii_uppercase)
_LOWER = frozenset(string.ascii_lowercase)
_DIGITS = frozenset(string.digits)


def check_password(password: str) -> bool:
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        return False
    chars = set(password)
    if not chars <= _UPPER | _LOWER | _DIGITS | PASSWORD_SYMBOLS:
        return False
    return all(chars & group for group in (_UPPER, _LOWER, _DIGITS, PASSWORD_SYMBOLS))
