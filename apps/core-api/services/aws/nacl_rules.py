"""번호 선택·가드레일·실행 직전 검증이 공유하는 인바운드 우선순위."""

from collections.abc import Mapping
from typing import Any


def inbound_rule_numbers(acl: Mapping[str, Any]) -> set[int]:
    return {
        entry["RuleNumber"] for entry in acl.get("Entries", [])
        if not entry.get("Egress", False)
    }


def first_inbound_allow_rule_number(acl: Mapping[str, Any]) -> int | None:
    return min(
        (entry["RuleNumber"] for entry in acl.get("Entries", [])
         if not entry.get("Egress", False) and entry.get("RuleAction") == "allow"),
        default=None,
    )


def select_nacl_deny_rule_number(acl: Mapping[str, Any]) -> int | None:
    used = inbound_rule_numbers(acl)
    first_allow = first_inbound_allow_rule_number(acl)
    upper = min(first_allow, 32767) if first_allow is not None else 32767
    return next((number for number in range(1, upper) if number not in used), None)
