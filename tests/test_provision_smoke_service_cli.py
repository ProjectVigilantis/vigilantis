"""scripts/provision_smoke_aws.py의 service 명령(파싱·위임·가드)과 #446 status 귀속 — 대역만 쓴다(실 AWS·DB 없음)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "provision_smoke_aws.py"
_spec = importlib.util.spec_from_file_location("provision_smoke_aws_cli", SCRIPT)
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = smoke
_spec.loader.exec_module(smoke)

ACCOUNT = "123456789012"


class FakeDb:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeSts:
    def __init__(self, account=ACCOUNT):
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account, "Arn": "arn:aws:iam::x:user/admin"}


class Backend:
    def __init__(self, result=None, exc=None):
        self.result = {"success": True} if result is None else result
        self.exc = exc
        self.calls: list[dict] = []

    def run_service_command(self, db, **kwargs):
        self.calls.append({"db": db, **kwargs})
        if self.exc:
            raise self.exc
        return self.result


@pytest.fixture
def env(monkeypatch):
    state = {"backend": Backend(), "dbs": [], "sts": FakeSts(), "clients_calls": []}

    def open_db():
        db = FakeDb()
        state["dbs"].append(db)
        return db

    def clients(region, profile):
        state["clients_calls"].append((region, profile))
        return {"ec2": "ec2", "elbv2": "elbv2", "asg": "asg", "sts": state["sts"]}

    monkeypatch.setattr(smoke, "_import_service_transitions", lambda: state["backend"])
    monkeypatch.setattr(smoke, "_open_db_session", open_db)
    monkeypatch.setattr(smoke, "_service_clients", clients)
    monkeypatch.setattr(smoke, "endpoint_url", lambda: None)
    monkeypatch.setattr(smoke, "default_region", lambda: "ap-northeast-2")
    return state


def run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["provision_smoke_aws.py", *argv])
    monkeypatch.setattr(sys.stdout, "reconfigure", lambda **_: None, raising=False)
    monkeypatch.setattr(sys.stderr, "reconfigure", lambda **_: None, raising=False)
    with pytest.raises(SystemExit) as exc:
        smoke.main()
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err


def test_schema_runs_offline_without_account_db_or_clients(env, monkeypatch, capsys):
    code, out, _ = run(monkeypatch, capsys, "service", "--operation", "schema")
    assert code == 0
    (call,) = env["backend"].calls
    assert call["db"] is None and call["clients"] == {} and call["command"] == "schema"
    assert env["dbs"] == [] and env["clients_calls"] == []
    assert json.loads(out) == {"success": True}


def test_schema_rejects_account_or_profile(env, monkeypatch, capsys):
    assert (
        run(
            monkeypatch,
            capsys,
            "service",
            "--operation",
            "schema",
            "--account",
            ACCOUNT,
        )[0]
        == 2
    )
    assert (
        run(monkeypatch, capsys, "service", "--operation", "schema", "--profile", "p")[
            0
        ]
        == 2
    )
    assert env["backend"].calls == []


def test_non_schema_requires_account_and_operation(env, monkeypatch, capsys):
    assert run(monkeypatch, capsys, "service", "--operation", "plan")[0] == 2
    assert run(monkeypatch, capsys, "service")[0] == 2
    assert env["backend"].calls == [] and env["dbs"] == []


@pytest.mark.parametrize("op", ["prepare", "cutover", "return", "cleanup"])
def test_mutating_operations_need_apply_and_never_reach_backend_without_it(
    env, monkeypatch, capsys, op
):
    code, _, err = run(
        monkeypatch, capsys, "service", "--operation", op, "--account", ACCOUNT
    )
    assert code == 2 and "--apply" in err
    assert (
        env["backend"].calls == [] and env["dbs"] == [] and env["clients_calls"] == []
    )


@pytest.mark.parametrize("op", ["plan", "status", "verify", "cleanup-plan"])
def test_apply_is_rejected_on_non_mutating_operations(env, monkeypatch, capsys, op):
    assert (
        run(
            monkeypatch,
            capsys,
            "service",
            "--operation",
            op,
            "--account",
            ACCOUNT,
            "--apply",
        )[0]
        == 2
    )
    assert env["backend"].calls == []


@pytest.mark.parametrize(
    "op",
    [
        "prepare",
        "cutover",
        "return",
        "cleanup",
        "cleanup-plan",
        "plan",
        "status",
        "verify",
    ],
)
def test_operation_is_delegated_verbatim_with_ids_options_and_closed_session(
    env, monkeypatch, capsys, op
):
    extra = ["--apply"] if op in smoke.SERVICE_MUTATING else []
    code, _out, _ = run(
        monkeypatch,
        capsys,
        "service",
        "--operation",
        op,
        "--account",
        ACCOUNT,
        "--execution-id",
        "ex-1",
        "--transition-id",
        "tr-1",
        "--options-json",
        '{"alb_arn": "x", "n": 2}',
        "--profile",
        "admin",
        *extra,
    )
    assert code == 0
    (call,) = env["backend"].calls
    assert (
        call["command"] == op
        and call["execution_id"] == "ex-1"
        and call["transition_id"] == "tr-1"
    )
    assert call["options"] == {"alb_arn": "x", "n": 2}
    assert call["clients"].keys() == {"ec2", "elbv2", "asg", "sts"}
    assert call["db"] is env["dbs"][0] and env["dbs"][0].closed
    assert env["clients_calls"] == [("ap-northeast-2", "admin")]


def test_options_default_to_none_and_ids_to_none(env, monkeypatch, capsys):
    run(monkeypatch, capsys, "service", "--operation", "status", "--account", ACCOUNT)
    (call,) = env["backend"].calls
    assert (
        call["options"] is None
        and call["execution_id"] is None
        and call["transition_id"] is None
    )


def test_options_file_is_an_explicit_option(env, monkeypatch, capsys, tmp_path):
    path = tmp_path / "opts.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    assert (
        run(
            monkeypatch,
            capsys,
            "service",
            "--operation",
            "plan",
            "--account",
            ACCOUNT,
            "--options-file",
            str(path),
        )[0]
        == 0
    )
    assert env["backend"].calls[0]["options"] == {"a": 1}


@pytest.mark.parametrize(
    "bad",
    [
        "[1]",
        '"s"',
        "5",
        "null",
        "{bad",
        '{"a":1,"a":2}',
        '{"a": NaN}',
        "",
        str(Path("opts.json")),
    ],
)
def test_invalid_options_json_fails_before_anything_runs(env, monkeypatch, capsys, bad):
    code, _, _ = run(
        monkeypatch,
        capsys,
        "service",
        "--operation",
        "plan",
        "--account",
        ACCOUNT,
        "--options-json",
        bad,
    )
    assert code == 2
    assert (
        env["backend"].calls == [] and env["dbs"] == [] and env["clients_calls"] == []
    )


def test_options_json_and_file_conflict_and_missing_file(
    env, monkeypatch, capsys, tmp_path
):
    base = ("service", "--operation", "plan", "--account", ACCOUNT)
    assert (
        run(monkeypatch, capsys, *base, "--options-json", "{}", "--options-file", "x")[
            0
        ]
        == 2
    )
    assert (
        run(
            monkeypatch, capsys, *base, "--options-file", str(tmp_path / "missing.json")
        )[0]
        == 2
    )
    assert env["backend"].calls == []


def test_account_mismatch_stops_before_db_and_backend(env, monkeypatch, capsys):
    env["sts"] = FakeSts("999999999999")
    code, _, err = run(
        monkeypatch, capsys, "service", "--operation", "status", "--account", ACCOUNT
    )
    assert code not in (0, None) and "호출 주체의 계정" in str(code) + err
    assert env["backend"].calls == [] and env["dbs"] == []


def test_endpoint_override_stops_service_commands(env, monkeypatch, capsys):
    monkeypatch.setattr(smoke, "endpoint_url", lambda: "http://localhost:4566")
    code, _, _ = run(
        monkeypatch, capsys, "service", "--operation", "status", "--account", ACCOUNT
    )
    assert code not in (0, None)
    assert env["backend"].calls == [] and env["dbs"] == []


def test_backend_exception_exits_nonzero_and_closes_session(env, monkeypatch, capsys):
    env["backend"] = Backend(exc=ValueError("옵션 누락"))
    code, out, err = run(
        monkeypatch, capsys, "service", "--operation", "plan", "--account", ACCOUNT
    )
    assert code == 1 and "옵션 누락" in err and out == ""
    assert env["dbs"][0].closed


@pytest.mark.parametrize(
    "result",
    [
        {"success": False, "reason": "blocked"},
        {"state": "unknown"},
        ["x"],
        None,
        {"success": 0},
        {"success": ""},
        {"success": "false"},
        {"success": "true"},
        {"success": []},
        {"success": 1},
        {"success": None},
    ],
)
def test_blocked_unknown_or_malformed_results_never_exit_zero(
    env, monkeypatch, capsys, result
):
    env["backend"] = Backend()
    env["backend"].result = result
    code, _, _ = run(
        monkeypatch, capsys, "service", "--operation", "verify", "--account", ACCOUNT
    )
    assert code == 1
    assert env["dbs"][0].closed


@pytest.mark.parametrize(
    "result",
    [{"success": 0}, {"success": "true"}, {"success": None}, {"success": False}],
)
def test_schema_allows_a_missing_success_key_but_not_a_non_true_one(
    env, monkeypatch, capsys, result
):
    env["backend"].result = result
    assert run(monkeypatch, capsys, "service", "--operation", "schema")[0] == 1
    env["backend"].result = {"fields": {}}
    assert run(monkeypatch, capsys, "service", "--operation", "schema")[0] == 0


def test_options_are_parsed_once_and_the_same_object_is_delegated(
    env, monkeypatch, capsys, tmp_path
):
    path = tmp_path / "opts.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    real, calls = smoke._service_options, []

    def counting(args):
        calls.append(1)
        parsed = real(args)
        path.write_text(
            '{"a": 2}', encoding="utf-8"
        )  # 파싱 뒤 파일이 바뀌어도 위임 값은 처음 읽은 것
        return parsed

    monkeypatch.setattr(smoke, "_service_options", counting)
    assert (
        run(
            monkeypatch,
            capsys,
            "service",
            "--operation",
            "plan",
            "--account",
            ACCOUNT,
            "--options-file",
            str(path),
        )[0]
        == 0
    )
    assert len(calls) == 1 and env["backend"].calls[0]["options"] == {"a": 1}


def test_real_facade_is_importable_and_schema_runs_offline():
    module = smoke._import_service_transitions()
    result = module.run_service_command(None, command="schema", clients={})
    assert isinstance(result, dict)


def test_facade_import_targets_the_root_module(monkeypatch):
    import types

    fake = types.ModuleType("service_transitions")
    monkeypatch.setitem(sys.modules, "service_transitions", fake)
    assert smoke._import_service_transitions() is fake


def test_legacy_commands_keep_requiring_account_and_reject_service_flags(
    env, monkeypatch, capsys
):
    assert run(monkeypatch, capsys, "status")[0] == 2
    assert (
        run(monkeypatch, capsys, "status", "--account", ACCOUNT, "--operation", "plan")[
            0
        ]
        == 2
    )
    assert run(monkeypatch, capsys, "down", "--account", ACCOUNT, "--apply")[0] == 2
    assert env["backend"].calls == []


def test_service_clients_use_an_explicit_session_without_touching_environment(
    monkeypatch,
):
    import boto3

    created = {}

    class FakeSession:
        def __init__(self, profile_name=None, region_name=None):
            created["session"] = (profile_name, region_name)

        def client(self, name, config=None):
            created.setdefault("clients", []).append(name)
            created.setdefault("configs", []).append(config)
            return name

    monkeypatch.setattr(boto3, "Session", FakeSession)
    before = dict(os.environ)
    clients = smoke._service_clients("ap-northeast-2", "admin")
    assert dict(os.environ) == before
    assert created["session"] == ("admin", "ap-northeast-2")
    assert clients == {
        "ec2": "ec2",
        "elbv2": "elbv2",
        "asg": "autoscaling",
        "sts": "sts",
    }
    assert len(created["configs"]) == 4
    for config in created["configs"]:  # 변경 호출을 SDK가 재전송하지 않는다
        assert config.retries == {"mode": "standard", "total_max_attempts": 1}
        assert config.connect_timeout == smoke.SERVICE_CONNECT_TIMEOUT
        assert config.read_timeout == smoke.SERVICE_READ_TIMEOUT
        assert config.ignore_configured_endpoint_urls is True


# ------------------------------------------------------------------ #446 status 귀속
EX, SRC = smoke.EXECUTION_TAG_KEY, smoke.SOURCE_TAG_KEY


def _t(execution=None, source=None):
    tags = []
    if execution:
        tags.append({"Key": EX, "Value": execution})
    if source:
        tags.append({"Key": SRC, "Value": source})
    return tags


def _image(image_id, snaps, execution=None, source=None):
    return {
        "ImageId": image_id,
        "Tags": _t(execution, source),
        "BlockDeviceMappings": [{"Ebs": {"SnapshotId": s}} for s in snaps],
    }


def _snap(snap_id, execution=None, source=None):
    return {"SnapshotId": snap_id, "Tags": _t(execution, source)}


class Pager:
    def __init__(self, key, pages):
        self.key, self.pages = key, pages

    def paginate(self, **kwargs):
        self.kwargs = kwargs
        return [{self.key: page} for page in self.pages]


class InvEc2:
    def __init__(self, images, snapshot_pages, instance_pages=()):
        # images: 페이지 목록(리스트의 리스트)
        self.image_pages, self.snapshot_pages, self.instance_pages = (
            images,
            snapshot_pages,
            instance_pages,
        )
        self.pagers = {}

    def get_paginator(self, name):
        pages = {
            "describe_images": self.image_pages,
            "describe_snapshots": self.snapshot_pages,
            "describe_instances": self.instance_pages,
        }[name]
        key = {
            "describe_images": "Images",
            "describe_snapshots": "Snapshots",
            "describe_instances": "Reservations",
        }[name]
        self.pagers[name] = Pager(key, pages)
        return self.pagers[name]


def test_inventory_classifies_owned_orphan_ambiguous_and_unattributed_across_pages():
    images = [
        [
            _image("ami-1", ["snap-m1", "snap-m2"], "ex-1", "i-src"),
            _image("ami-other", ["snap-shared"]),
        ],  # 446 태그 없는 AMI
        [
            _image(
                "ami-ex2", ["snap-conflict"], "ex-2", "i-src"
            ),  # 두 번째 페이지의 AMI도 매핑에 쓰인다
            _image("ami-foreign", ["snap-foreign"]),
            _image("ami-othersrc", ["snap-diffsrc"], "ex-1", "i-other"),
        ],
    ]
    snapshot_pages = [
        [
            _snap("snap-m1", "ex-1", "i-src"),
            _snap("snap-m2"),
        ],  # m2: 태그 없어도 매핑이 근거
        [
            _snap("snap-orphan", "ex-1", "i-src"),
            _snap("snap-shared", "ex-1", "i-src"),  # 태그 있는데 태그 없는 AMI가 매핑
            _snap("snap-conflict", "ex-1", "i-src"),  # 다른 실행의 AMI가 매핑
            _snap("snap-src-only", None, "i-src"),  # 원본 태그만
            _snap("snap-foreign"),  # 태그 없는 AMI 매핑 → 근거 없음
            _snap("snap-none"),
            _snap(
                "snap-diffsrc", "ex-1", "i-src"
            ),  # 같은 실행 ID, 다른 원본 → mapped 아님
        ],
    ]
    ec2 = InvEc2(images, snapshot_pages)
    inv = smoke.image_snapshot_inventory(ec2)
    assert ec2.pagers["describe_images"].kwargs == {"Owners": ["self"]}
    assert ec2.pagers["describe_snapshots"].kwargs == {"OwnerIds": ["self"]}
    assert [i["image_id"] for i in inv["images"]] == [
        "ami-1",
        "ami-ex2",
        "ami-othersrc",
    ]
    assert sorted(s["snapshot_id"] for s in inv["mapped"]) == ["snap-m1", "snap-m2"]
    assert [s["snapshot_id"] for s in inv["orphan"]] == ["snap-orphan"]
    assert sorted(s["snapshot_id"] for s in inv["ambiguous"]) == [
        "snap-conflict",
        "snap-diffsrc",
        "snap-shared",
        "snap-src-only",
    ]
    assert inv["unattributed_count"] == 2
    assert (
        next(s for s in inv["mapped"] if s["snapshot_id"] == "snap-m2")["execution_id"]
        == "ex-1"
    )


def _ids(rows, key="snapshot_id"):
    return sorted(r[key] for r in rows)


def test_inventory_requires_the_complete_execution_and_source_pair():
    images = [
        [
            _image(
                "ami-full",
                [
                    "snap-full-untagged",
                    "snap-full-tagged",
                    "snap-full-conflict",
                    "snap-full-partial-ok",
                    "snap-full-partial-bad",
                ],
                "ex-1",
                "i-src",
            ),
            _image(
                "ami-exonly", ["snap-exonly-mapped", "snap-exonly-untagged"], "ex-9"
            ),  # 원본 태그 없음
            _image(
                "ami-srconly", ["snap-srconly-mapped"], None, "i-9"
            ),  # 실행 태그 없음
            _image("ami-ok-a", ["snap-agree"], "ex-2", "i-a"),
            _image("ami-ok-b", ["snap-agree"], "ex-2", "i-a"),  # 같은 쌍 → 일치
            _image("ami-bad-a", ["snap-disagree"], "ex-3", "i-a"),
            _image("ami-bad-b", ["snap-disagree"], "ex-3", "i-b"),  # 원본이 다름 → 충돌
        ]
    ]
    snaps = [
        [
            _snap("snap-full-untagged"),  # 태그 없음 + 완전한 소유 AMI 매핑 → mapped
            _snap("snap-full-tagged", "ex-1", "i-src"),
            _snap(
                "snap-full-conflict", "ex-1", "i-other"
            ),  # 부분/완전 태그가 매핑과 충돌 → ambiguous
            _snap(
                "snap-full-partial-ok", "ex-1"
            ),  # 부분 태그, 매핑과 모순 없음 → mapped
            _snap(
                "snap-full-partial-bad", "ex-2"
            ),  # 부분 태그, 매핑과 모순 → ambiguous
            _snap(
                "snap-exonly-mapped", "ex-9", "i-src"
            ),  # 불완전 AMI가 매핑 → 귀속 안 함
            _snap("snap-exonly-untagged"),
            _snap("snap-srconly-mapped"),
            _snap("snap-exonly-orphan", "ex-9"),  # 실행 태그만, 매핑 없음 → orphan 아님
            _snap("snap-srconly-orphan", None, "i-9"),
            _snap("snap-full-orphan", "ex-1", "i-src"),  # 완전 태그, 매핑 없음 → orphan
            _snap("snap-agree"),
            _snap("snap-disagree"),
        ]
    ]
    inv = smoke.image_snapshot_inventory(InvEc2(images, snaps))
    assert _ids(inv["images"], "image_id") == [
        "ami-bad-a",
        "ami-bad-b",
        "ami-full",
        "ami-ok-a",
        "ami-ok-b",
    ]
    assert _ids(inv["ambiguous_images"], "image_id") == ["ami-exonly", "ami-srconly"]
    assert _ids(inv["mapped"]) == [
        "snap-agree",
        "snap-full-partial-ok",
        "snap-full-tagged",
        "snap-full-untagged",
    ]
    assert _ids(inv["orphan"]) == ["snap-full-orphan"]
    assert _ids(inv["ambiguous"]) == [
        "snap-disagree",
        "snap-exonly-mapped",
        "snap-exonly-orphan",
        "snap-exonly-untagged",
        "snap-full-conflict",
        "snap-full-partial-bad",
        "snap-srconly-mapped",
        "snap-srconly-orphan",
    ]
    untagged = next(
        s for s in inv["mapped"] if s["snapshot_id"] == "snap-full-untagged"
    )
    assert (untagged["execution_id"], untagged["source_instance"]) == ("ex-1", "i-src")


class InvAsg:
    def __init__(self, groups):
        self.groups = groups

    def get_paginator(self, name):
        assert name == "describe_auto_scaling_groups"
        return Pager("AutoScalingGroups", [self.groups[:1], self.groups[1:]])


def _reservation(instance_id, state):
    return {
        "Instances": [{"InstanceId": instance_id, "State": {"Name": state}, "Tags": []}]
    }


def test_cost_exposure_flags_original_and_asg_instances_without_prices():
    asg = InvAsg(
        [
            {
                "AutoScalingGroupName": "vigilantis-asg-a",
                "VPCZoneIdentifier": "subnet-1,subnet-2",
                "DesiredCapacity": 2,
                "Tags": _t("ex-1", "i-src"),
                "Instances": [{"InstanceId": "i-asg1"}, {"InstanceId": "i-asg2"}],
            },
            {
                "AutoScalingGroupName": "other",
                "VPCZoneIdentifier": "subnet-x",
                "Instances": [],
                "Tags": [],
            },
        ]
    )
    ec2 = InvEc2([], [], [[_reservation("i-src", "running")]])
    exposure = smoke.service_cost_exposure(ec2, asg, {"subnet-1"})
    assert [g["name"] for g in exposure["asgs"]] == ["vigilantis-asg-a"]
    assert exposure["running_originals"] == ["i-src"]
    assert exposure["double_running"] is True
    assert "price" not in json.dumps(exposure).lower() and "$" not in json.dumps(
        exposure
    )
    # 원본은 Name이 아니라 명시한 인스턴스 ID로만 조회한다
    filters = ec2.pagers["describe_instances"].kwargs["Filters"]
    assert {"Name": "instance-id", "Values": ["i-src"]} in filters
    assert not any(
        f["Name"].startswith("tag:") or f["Name"] == "vpc-id" for f in filters
    )


def test_cost_exposure_not_double_when_original_stopped():
    asg = InvAsg(
        [
            {
                "AutoScalingGroupName": "vigilantis-asg-a",
                "VPCZoneIdentifier": "subnet-1",
                "Tags": _t("ex-1", "i-src"),
                "Instances": [{"InstanceId": "i-asg1"}],
            }
        ]
    )
    ec2 = InvEc2([], [], [[]])
    exposure = smoke.service_cost_exposure(ec2, asg, {"subnet-1"})
    assert exposure["running_originals"] == [] and exposure["double_running"] is False


def test_double_running_is_per_same_asg_and_source_pair():
    """원본 A가 떠 있고 무관한 그룹 B에만 멤버가 있으면 이중 과금이 아니다(그룹별 판정)."""
    asg = InvAsg(
        [
            {
                "AutoScalingGroupName": "vigilantis-asg-a",
                "VPCZoneIdentifier": "subnet-1",
                "Tags": _t("ex-1", "i-srcA"),
                "Instances": [],
            },
            {
                "AutoScalingGroupName": "vigilantis-asg-b",
                "VPCZoneIdentifier": "subnet-1",
                "Tags": _t("ex-2", "i-srcB"),
                "Instances": [{"InstanceId": "i-b1"}],
            },
        ]
    )
    ec2 = InvEc2([], [], [[_reservation("i-srcA", "running")]])
    exposure = smoke.service_cost_exposure(ec2, asg, {"subnet-1"})
    by_name = {g["name"]: g for g in exposure["asgs"]}
    assert (
        by_name["vigilantis-asg-a"]["original_running"]
        and not by_name["vigilantis-asg-a"]["double_running"]
    )
    assert (
        not by_name["vigilantis-asg-b"]["original_running"]
        and not by_name["vigilantis-asg-b"]["double_running"]
    )
    assert exposure["double_running"] is False


def test_down_warning_says_down_is_not_full_image_cleanup():
    text = smoke._DOWN_IMAGE_WARNING
    assert "전체 정리가 아니다" in text and "cleanup-plan" in text and "cleanup" in text
