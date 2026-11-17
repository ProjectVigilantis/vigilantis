"""#589: 활동 로그 계약과 `apps/web/src/types/api.ts` 가 맞는지 본다.

`api.ts` 는 계약(`schemas.platform_events` · `schemas.api.platform_events`)을 보고 손으로 맞추는 파일이다.
행동을 더하거나 응답 칸을 바꾸는 PR 이 `api.ts` 를 빠뜨려도 다른 검사는 잡지 못한다 — 타입스크립트는 자기
파일 안에서만 맞으면 통과하고, 서버 테스트는 `api.ts` 를 읽지 않는다. 이 파일이 그 사이를 잇는다.

원천은 파이썬 계약이다. `api.ts` 의 활동 로그 구역(머리 주석에 `api/platform_events.py` 가 있는 구역)을
글자로 읽어 견준다 — 값과 순서, 칸 이름과 순서, null 허용, 쪽 크기 상수.

보지 않는 것: `details` 의 행동별 칸(`api.ts` 에서는 `Record<string, unknown>` 이고 설명은 주석이다) · 주석 문장.
"""

from __future__ import annotations

import re
import types
import typing
from enum import Enum
from pathlib import Path

import pytest
from schemas import platform_events as record
from schemas.api import platform_events as api
from schemas.api.auth import AccountType, PlatformRole

API_TS = Path(__file__).resolve().parents[3] / "apps" / "web" / "src" / "types" / "api.ts"
SECTION_MARK = "api/platform_events.py"


def _source() -> str:
    assert API_TS.is_file(), f"{API_TS} 가 없습니다 — 이 테스트는 저장소 안에서 돕니다"
    return API_TS.read_text(encoding="utf-8")


def _section() -> str:
    """활동 로그 구역 — 머리 주석부터 다음 구역 머리 주석 앞까지."""
    source = _source()
    assert source.count(SECTION_MARK) >= 1, f"api.ts 에 {SECTION_MARK} 구역 머리가 없습니다"
    start = source.index(SECTION_MARK)
    following = re.search(r"(?m)^(?:/\*|//) ─{10,}", source[start:])
    return source[start : start + following.start()] if following else source[start:]


def _without_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)//.*$", "", text)


def _union(name: str) -> list[str]:
    """`export type 이름 = | 'A' | 'B';` 의 값 — 적힌 순서대로."""
    found = re.search(rf"export type {name} =([^;]*);", _without_comments(_section()))
    assert found, f"api.ts 활동 로그 구역에 타입 {name} 이 없습니다"
    return re.findall(r"'([^']+)'", found.group(1))


def _interface(name: str) -> list[tuple[str, str]]:
    """`export interface 이름 { … }` 의 (칸 이름, 타입 글자) — 적힌 순서대로. 선택 칸(`?:`)은 이름 뒤에 `?` 를 남긴다."""
    found = re.search(rf"export interface {name} \{{(.*?)\n\}}", _section(), flags=re.S)
    assert found, f"api.ts 활동 로그 구역에 인터페이스 {name} 이 없습니다"
    # 칸은 한 줄에 하나다 — 타입 글자 안에 `;` 가 있을 수 있어(`{ type: …; id: … } | null`) 줄 끝의 `;` 로 끊는다
    return re.findall(r"(?m)^\s{2}(\w+\??):\s*(.+);\s*$", _without_comments(found.group(1)))


def _actor_branches() -> list[list[tuple[str, str]]]:
    """`PlatformEventActor` 의 갈래별 (칸 이름, 타입 글자)."""
    found = re.search(r"export type PlatformEventActor =(.*?)\n\n", _section(), flags=re.S)
    assert found, "api.ts 활동 로그 구역에 타입 PlatformEventActor 가 없습니다"
    return [
        re.findall(r"(\w+\??):\s*([^;]+);", body)
        for body in re.findall(r"\{(.*?)\}", _without_comments(found.group(1)), flags=re.S)
    ]


def _nullable(annotation) -> bool:
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return type(None) in typing.get_args(annotation)
    return False


def _contract_fields(model) -> list[tuple[str, bool]]:
    return [(name, _nullable(field.annotation)) for name, field in model.model_fields.items()]


def _difference(contract: list[str], web: list[str]) -> str:
    return f"계약에만 있음={sorted(set(contract) - set(web))} · api.ts 에만 있음={sorted(set(web) - set(contract))}"


# ----- enum 다섯 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "contract",
    [
        record.PlatformEventCategory,
        record.PlatformEventAction,
        record.PlatformEventOutcome,
        record.PlatformActorKind,
        record.PlatformEventTargetType,
    ],
    ids=lambda enum: enum.__name__,
)
def test_enum_values_and_their_order_match(contract: type[Enum]):
    values = [member.value for member in contract]
    web = _union(contract.__name__)
    assert set(web) == set(values), _difference(values, web)
    assert len(web) == len(values), f"api.ts 의 {contract.__name__} 에 같은 값이 두 번 있습니다"
    # 순서까지 같게 둔다 — 화면의 필터 목록이 이 순서를 그대로 쓰고, 새 값이 어디에 들어갔는지 눈으로 견주기 쉽다
    assert web == values


# ----- 응답의 칸 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model", [api.PlatformEventItem, api.PlatformEventsResponse], ids=lambda model: model.__name__
)
def test_response_fields_match_in_name_order_and_nullability(model):
    contract = _contract_fields(model)
    web = _interface(model.__name__)
    assert [name for name, _ in web] == [name for name, _ in contract], _difference(
        [name for name, _ in contract], [name for name, _ in web]
    )
    # 계약의 칸은 모두 필수다(없으면 null 로 온다) — api.ts 에 선택 칸(`?:`)이 생기면 위 이름 비교에서 걸린다
    assert not [name for name, field in model.model_fields.items() if not field.is_required()]
    web_nullable = [(name, bool(re.search(r"\|\s*null\b", kind))) for name, kind in web]
    assert web_nullable == contract


def test_enum_typed_fields_point_at_the_same_named_types():
    kinds = dict(_interface("PlatformEventItem"))
    for field, contract in {
        "category": record.PlatformEventCategory,
        "action": record.PlatformEventAction,
        "outcome": record.PlatformEventOutcome,
        "actor": api.PlatformEventActor,
    }.items():
        assert kinds[field].strip() == contract.__name__


def test_target_is_a_type_and_an_id_or_null():
    contract = _contract_fields(api.PlatformEventTarget)
    assert contract == [("type", False), ("id", False)]
    kind = dict(_interface("PlatformEventItem"))["target"]
    assert re.fullmatch(r"\{\s*type:\s*PlatformEventTargetType;\s*id:\s*string\s*\}\s*\|\s*null", kind.strip()), kind


def test_page_size_constants_match():
    section = _without_comments(_section())
    for name in ("PLATFORM_EVENTS_DEFAULT_LIMIT", "PLATFORM_EVENTS_MAX_LIMIT"):
        found = re.search(rf"export const {name} = (\d+);", section)
        assert found, f"api.ts 활동 로그 구역에 상수 {name} 이 없습니다"
        assert int(found.group(1)) == getattr(api, name)


# ----- 행위자 -----------------------------------------------------------------------------


def test_actor_branches_carry_the_contract_fields_and_cover_every_kind():
    fields = [name for name, _ in _contract_fields(api.PlatformEventActor)]
    branches = _actor_branches()
    assert len(branches) == 2, "사람(USER)과 사람이 아닌 행위자, 두 갈래입니다"
    for branch in branches:
        assert [name for name, _ in branch] == fields
    kinds = [kind for branch in branches for kind in re.findall(r"'([A-Z_]+)'", dict(branch)["kind"])]
    assert sorted(kinds) == sorted(member.value for member in record.PlatformActorKind), kinds


def test_only_a_person_has_an_identity():
    person, other = sorted(_actor_branches(), key=lambda branch: "'USER'" not in dict(branch)["kind"])
    assert re.findall(r"'([A-Z_]+)'", dict(person)["kind"]) == [record.PlatformActorKind.USER.value]
    # 사람이 아니면 네 칸이 모두 null 이다(계약의 _person_only 와 같다)
    assert {name: kind.strip() for name, kind in other if name != "kind"} == {
        "user_id": "null", "email": "null", "account_type": "null", "platform_role": "null",
    }
    person_kinds = {name: kind.strip() for name, kind in person}
    # 사람은 user_id · email 이 반드시 있다. 플랫폼 등급은 PLATFORM 계정만 있어 null 일 수 있다
    assert (person_kinds["user_id"], person_kinds["email"]) == ("string", "string")
    assert re.fullmatch(r"PlatformRole\s*\|\s*null", person_kinds["platform_role"])


def test_person_account_type_accepts_what_the_contract_can_send():
    person = next(branch for branch in _actor_branches() if "'USER'" in dict(branch)["kind"])
    kind = dict(person)["account_type"]
    source = _without_comments(_source())
    accepted = set(re.findall(r"'([A-Z_]+)'", kind))
    for alias in re.findall(r"\b([A-Z]\w+)\b", re.sub(r"'[^']*'", "", kind)):
        found = re.search(rf"export type {alias} =([^;]*);", source)
        assert found, f"api.ts 에 타입 {alias} 이 없습니다"
        accepted |= set(re.findall(r"'([A-Z_]+)'", found.group(1)))
    assert accepted == {member.value for member in AccountType}
    role = re.search(r"export type PlatformRole =([^;]*);", source)
    assert set(re.findall(r"'([A-Z_]+)'", role.group(1))) == {member.value for member in PlatformRole}
