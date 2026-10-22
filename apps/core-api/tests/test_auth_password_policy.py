"""새 비밀번호 정책 순수 함수 — 경계 · 문자 종류 · 기호 목록 · 공백/비 ASCII (#492 PR 3)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[1]
PACKAGES = Path(__file__).resolve().parents[3] / "packages"
for p in (str(CORE_API), str(PACKAGES)):
    if p not in sys.path:
        sys.path.insert(0, p)

from auth.password_policy import PASSWORD_SYMBOLS, check_password

SYMBOLS = "^$*.[]{}()?\"!@#%&/\\,><':;|_~`=+-"


def pad(core: str, length: int) -> str:
    return core + "a" * (length - len(core))


def test_symbol_list_is_exactly_ascii_punctuation():
    import string

    assert set(SYMBOLS) == set(string.punctuation) == PASSWORD_SYMBOLS
    assert len(SYMBOLS) == 32


@pytest.mark.parametrize("length,ok", [(11, False), (12, True), (256, True), (257, False)])
def test_length_boundaries(length, ok):
    assert check_password(pad("Aa1-", length)) is ok


@pytest.mark.parametrize(
    "password",
    ["aaaa1111-bbbb", "AAAA1111-BBBB", "Aaaaaaaaaaa-b", "Aaaaaaaaaaa1b"],
    ids=["no-upper", "no-lower", "no-digit", "no-symbol"],
)
def test_every_missing_class_is_rejected(password):
    assert check_password(password) is False


@pytest.mark.parametrize("symbol", list(SYMBOLS))
def test_every_listed_symbol_satisfies_the_symbol_class(symbol):
    assert check_password(f"Abcdefgh123{symbol}") is True


@pytest.mark.parametrize(
    "password",
    [" Abcdefgh1-xyz", "Abcdefgh1-xyz ", "Abcdefgh 1-xyz", "Abcdefgh1\t-xyz", "Abcdefgh1\n-xyz"],
    ids=["leading", "trailing", "middle", "tab", "newline"],
)
def test_whitespace_anywhere_is_rejected_without_trimming(password):
    assert check_password(password) is False


@pytest.mark.parametrize(
    "password",
    ["Abcdefgh1-한글비밀번호", "Abcdefgh1-éé", "Abcdefgh1-\u00a0x", "Ａbcdefgh1-xyz", "Abcdefgh1-xyz\x00"],
    ids=["hangul", "latin1", "nbsp", "fullwidth", "control"],
)
def test_non_ascii_and_control_characters_are_rejected(password):
    assert check_password(password) is False


def test_hangul_cannot_stand_in_for_a_character_class():
    assert check_password("한글한글한글한글한글한글-1A") is False
