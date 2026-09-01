# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# 요약 3줄이 입력에 없는 값을 말하는지 대조합니다. (Issue #237)
#
# _validate_output_contract(ai/agent.py)는 형식만 본다 — 요약이 정확히 3줄인지, 후보가
# 계약을 지키는지. 문장이 **입력에 없는 사실을 지어냈는지**는 아무도 보지 않는다.
# 여기서 보는 것이 그것이며, 정답 문장이 없어도 돌아간다 — 대조 상대가 "좋은 요약"이
# 아니라 **모델에게 실제로 나간 페이로드의 값 집합**이기 때문이다.
#
# 토큰을 네 갈래로 나누고, 그중 둘만 실패로 센다.
#   ① 식별자(FAIL) — 자원 ID·ARN·IP. 입력에 없으면 지어낸 것이다. 파생될 수 없다.
#   ② 숫자(FAIL) — 입력 값 집합에도, 아래 ④의 파생 집합에도 없는 수치. 걸린 토큰을
#      그대로 돌려주므로 사람이 환각인지 산문인지 가른다.
#   ③ 인스턴스 타입(보고만) — **실패로 세지 않는다.** 다운사이징 권고의 대상 타입은
#      정의상 입력에 없는 값이라(그것이 이 런북이 하는 일이다) 실패로 세면 정상 응답이
#      전부 FAIL이 된다. 대신 목록으로 남겨, 현재 타입을 잘못 말한 경우를 사람이 본다.
#   ④ 파생 수치(보고만) — 입력의 두 시각 차이처럼 **입력에서 문서화된 연산으로 나오는
#      값.** 처음에는 이것도 FAIL로 셌는데, 그러면 지표가 정확성이 아니라 "덜 말하는
#      쪽"을 상위로 올린다. 실측(#237)에서 gpt-5.6-luna의 위반 12건이 전부 `14`
#      하나였고 그것은 window_start·window_end의 차이를 정확히 계산한 관측 기간이었다.
#      같은 표의 gpt-4o는 관측 창을 24회 내내 언급조차 하지 않아 만점을 받고 있었다.
#
# 앞 갈래가 먹은 자리는 뒤 갈래가 보지 않는다 — 그러지 않으면 `t3.xlarge`가 숫자 3을,
# `i-0a1b2c3d`가 숫자 0을 만들어 낸다.
#
# **입력과 요약을 같은 규칙으로 쪼갠다.** 한쪽에만 적용되는 표기 규칙이 있으면 그것이
# 그대로 오탐이 된다 — 검사기가 잡는 것이 환각이 아니라 표기 차이가 되고, 자세히 쓰는
# 모델일수록 나쁜 점수를 받는다. 표기를 맞추는 정규화는 _canonicalize()에 모은다.
# ==============================================================================

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import combinations
from typing import Any

# 경계를 \b로 잡지 않는다 — 한글이 \w에 들어가서 `t3.medium으로`·`i-0abc…의`처럼
# 조사가 붙은 자리에서 \b가 성립하지 않는다(요약은 한국어 문장이다). ASCII 낱말 문자만
# 경계로 보는 lookaround를 쓴다.
_L = r"(?<![0-9A-Za-z_.\-])"
_R = r"(?![0-9A-Za-z_\-])"

# ARN은 허용 문자로만 이어 붙이고 마지막 글자를 제한해, 문장 끝 마침표를 삼키지 않는다
_ARN = r"arn:aws:[a-z0-9\-]+:[a-z0-9\-]*:[0-9]*:[A-Za-z0-9:/_+=.@\-]*[A-Za-z0-9/_+=@\-]"
# AWS 자원 ID. 16진 6자 이상이라 일반 낱말과 겹치지 않는다
_RESOURCE_ID = _L + r"(?:i|sg|vol|acl|vpc|subnet|lt|eni|ami|snap|tg)-[0-9a-f]{6,}" + _R
_IPV4 = _L + r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}" + r"(?![0-9A-Za-z_.\-])"
# t3.xlarge · m5.2xlarge · c6g.medium 같은 형태
_INSTANCE_TYPE = (
    _L
    + r"[a-z]{1,4}[0-9][a-z]*\.(?:nano|micro|small|medium|large|[0-9]{0,2}xlarge|metal)"
    + r"(?![0-9A-Za-z_])"
)
_NUMBER = r"(?<![0-9A-Za-z_.])[0-9]+(?:\.[0-9]+)?" + r"(?![0-9A-Za-z_])"

_IDENTIFIER_PATTERNS = (_ARN, _RESOURCE_ID, _IPV4)

# ISO 타임스탬프의 T·Z는 낱말 문자라 숫자 경계가 성립하지 않는다. 입력의
# `2026-08-19T06:00:00Z`에서 일(19)과 시(06)가 추출되지 않아, 요약이 같은 날짜를
# `2026-08-19`로 쓰면 입력에 없는 값으로 잡혔다(실측: gpt-5.6-luna·terra·5.4-nano).
_ISO_SEPARATOR = re.compile(r"(?<=[0-9])[tz](?![0-9a-z])|(?<=[0-9])t(?=[0-9])")

# 천 단위 쉼표. 요약은 `1,024`로 쓰고 입력은 정수 1024라, 쪼개진 `1`과 `024`가 그대로
# 위반이 됐다. 세 자리 묶음일 때만 지운다 — `4.9, 10.0` 같은 나열 쉼표는 건드리지 않는다.
_THOUSANDS_COMMA = re.compile(r"(?<=[0-9]),(?=[0-9]{3}(?![0-9]))")

# 문장 안의 열거 표시 `(1)`·`(2)`. 데이터 값이 아니라 글의 구조라 대조 대상이 아니다 —
# 값으로 세면 목록으로 쓴 요약이 그 표시 때문에 위반으로 잡힌다(실측: gpt-5.4-nano).
# 괄호가 숫자 하나만 감쌀 때로 한정한다.
_ENUM_MARKER = re.compile(r"\(\s*[0-9]{1,2}\s*\)")

# 파생 수치를 뽑을 시각. 스칼라 전체와 정확히 일치할 때만 시각으로 본다 — 문장 속
# 날짜까지 시각으로 세면 요약이 스스로 만든 날짜로 파생 집합을 넓히게 된다.
_ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+\-]\d{2}:?\d{2})?")


@dataclass(frozen=True)
class FactCheckResult:
    """요약 3줄 대조 결과. 걸린 토큰을 그대로 담는다 — 개수만으로는 판정할 수 없다."""

    identifier_violations: tuple[str, ...] = ()
    number_violations: tuple[str, ...] = ()
    instance_types_outside_input: tuple[str, ...] = ()
    # 입력에서 계산해 낸 값. 실패가 아니라 "이 수치는 인용이 아니라 산출"이라는 표시다
    derived_numbers: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.identifier_violations and not self.number_violations


def _normalize_number(text: str) -> str:
    """4.90·4.9·10.0을 같은 값으로 본다. 계정 ID 같은 큰 정수도 정밀도를 잃지 않는다."""
    try:
        value = Decimal(text)
    except InvalidOperation:
        return text
    if value == value.to_integral_value():
        return str(int(value))
    return str(value.normalize())


def _canonicalize(text: str) -> str:
    """입력과 요약에 **똑같이** 적용하는 표기 정규화. 값을 바꾸지 않고 경계만 맞춘다."""
    lowered = _ENUM_MARKER.sub(" ", text.lower())
    return _THOUSANDS_COMMA.sub("", _ISO_SEPARATOR.sub(" ", lowered))


def _tokens(text: str) -> tuple[set[str], set[str], set[str]]:
    """(식별자, 인스턴스 타입, 숫자). 앞 갈래가 먹은 자리는 공백으로 지우고 넘긴다."""
    remaining = _canonicalize(text)
    identifiers: set[str] = set()
    for pattern in _IDENTIFIER_PATTERNS:
        for match in re.finditer(pattern, remaining):
            token = match.group(0)
            identifiers.add(token)
            # ARN 안의 자원 ID도 인용 가능한 값이다. 입력은 자원을 ARN 통째로 담는데
            # 요약은 짧은 ID(`sg-0a1b…`)로 쓰므로, 여기서 함께 꺼내지 않으면 정확한
            # 인용이 위반이 된다 — ARN 패턴이 먼저 먹어 짧은 ID가 남지 않기 때문이다
            identifiers.update(inner.group(0) for inner in re.finditer(_RESOURCE_ID, token))
        remaining = re.sub(pattern, " ", remaining)

    instance_types = {match.group(0) for match in re.finditer(_INSTANCE_TYPE, remaining)}
    remaining = re.sub(_INSTANCE_TYPE, " ", remaining)

    numbers = {_normalize_number(match.group(0)) for match in re.finditer(_NUMBER, remaining)}
    return identifiers, instance_types, numbers


def _walk(value: Any, sink: list[str]) -> None:
    """페이로드의 스칼라만 모은다. dict 키는 필드 이름이라 대조 대상이 아니다."""
    if isinstance(value, Mapping):
        for item in value.values():
            _walk(item, sink)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk(item, sink)
    elif isinstance(value, bool) or value is None:
        return  # bool은 int의 하위 타입이라 숫자로 새지 않게 먼저 거른다
    else:
        sink.append(str(value))


def derivable_numbers(payload: Mapping[str, Any]) -> set[str]:
    """입력의 시각들에서 나오는 기간(일). 요약이 관측 창을 사람 말로 옮길 때 쓰는 값이다.

    허용하는 연산을 **두 시각의 차이(일 단위 정수) 하나로 한정한다.** 넓힐수록 검사기가
    잡을 수 있는 환각이 줄기 때문이다. 시각 쌍을 전부 도는 것은 어느 필드에서 왔는지에
    검사기를 묶지 않기 위해서다 — 페이로드 구성이 바뀌어도 규칙이 그대로 선다.
    """
    scalars: list[str] = []
    _walk(payload, scalars)
    stamps: list[datetime] = []
    for scalar in scalars:
        text = scalar.strip()
        if not _ISO_DATETIME.fullmatch(text):
            continue
        try:
            stamps.append(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            continue

    derived: set[str] = set()
    for first, second in combinations(stamps, 2):
        seconds = abs((first - second).total_seconds())
        days, remainder = divmod(seconds, 86_400)
        if remainder == 0 and days:
            derived.add(str(int(days)))
    return derived


def allowed_tokens(payload: Mapping[str, Any]) -> tuple[set[str], set[str], set[str]]:
    """모델에게 나간 페이로드에서 인용 가능한 값 집합을 뽑는다."""
    scalars: list[str] = []
    _walk(payload, scalars)
    identifiers: set[str] = set()
    instance_types: set[str] = set()
    numbers: set[str] = set()
    for scalar in scalars:
        found_ids, found_types, found_numbers = _tokens(scalar)
        identifiers |= found_ids
        instance_types |= found_types
        numbers |= found_numbers
    return identifiers, instance_types, numbers


def check_summary_facts(
    payload: Mapping[str, Any], summary_lines: Sequence[str]
) -> FactCheckResult:
    """요약 3줄이 인용한 값이 입력에 실재하는지 본다.

    payload는 **요약 노드에 실제로 나간 값**(ai/agent.py의 _incident_payload)을 그대로
    넘긴다. 다른 것을 넘기면 모델이 보지 못한 값을 근거로 통과시키게 된다.
    """
    allowed_ids, allowed_types, allowed_numbers = allowed_tokens(payload)

    identifiers: set[str] = set()
    instance_types: set[str] = set()
    numbers: set[str] = set()
    for line in summary_lines:
        found_ids, found_types, found_numbers = _tokens(line)
        identifiers |= found_ids
        instance_types |= found_types
        numbers |= found_numbers

    unquoted = numbers - allowed_numbers
    derivable = derivable_numbers(payload)
    return FactCheckResult(
        identifier_violations=tuple(sorted(identifiers - allowed_ids)),
        number_violations=tuple(sorted(unquoted - derivable)),
        instance_types_outside_input=tuple(sorted(instance_types - allowed_types)),
        derived_numbers=tuple(sorted(unquoted & derivable)),
    )
