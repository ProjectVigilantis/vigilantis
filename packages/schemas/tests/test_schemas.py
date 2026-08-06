import pytest
from datetime import datetime, timezone
from pydantic import ValidationError

from packages.schemas import (
    AssetMetadata,
    AssetType,
    DriftStatus,
    TerraformDrift,
    EventSource,
    ThreatSeverity,
    GuardDutyFinding,
    ThreatEvent,
    GuardrailRequest,
    GuardrailResponse,
    GuardrailStep,
    GuardrailStatus,
    GuardrailEvaluation,
    RunbookDefinition,
    RunbookExecutionRequest,
    ExecutionMode,
    RunbookRiskLevel,
)


def test_asset_metadata_serialization():
    asset = AssetMetadata(
        asset_id="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123456789abcdef0",
        name="web-server-prod-01",
        asset_type=AssetType.EC2,
        account_id="123456789012",
        region="ap-northeast-2",
        tags={"Environment": "Production", "Owner": "SecOps"},
    )
    json_data = asset.model_dump_json()
    assert "web-server-prod-01" in json_data

    parsed = AssetMetadata.model_validate_json(json_data)
    assert parsed.asset_id == asset.asset_id
    assert parsed.asset_type == AssetType.EC2


def test_terraform_drift_model():
    drift = TerraformDrift(
        drift_id="drift-998877",
        asset_id="i-0123456789abcdef0",
        resource_address="aws_instance.web",
        drift_status=DriftStatus.DRIFTED,
        expected_state={"instance_type": "t3.micro"},
        actual_state={"instance_type": "t3.large"},
        diff_details={"instance_type": {"expected": "t3.micro", "actual": "t3.large"}},
    )
    assert drift.drift_status == DriftStatus.DRIFTED
    assert drift.diff_details["instance_type"]["actual"] == "t3.large"


def test_threat_event_validation():
    event = ThreatEvent(
        event_id="evt-1001",
        source=EventSource.GUARDDUTY,
        severity=ThreatSeverity.HIGH,
        title="UnauthorizedAccess:EC2/SSHBruteForce",
        description="SSH brute force attack detected against EC2 instance",
        affected_asset_id="i-0123456789abcdef0",
        account_id="123456789012",
        region="ap-northeast-2",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    )
    assert event.severity == ThreatSeverity.HIGH
    assert event.source == EventSource.GUARDDUTY


def test_guardrail_request_response():
    req = GuardrailRequest(
        request_id="greq-001",
        event_id="evt-1001",
        runbook_id="RB-EC2-ISOLATE-001",
        target_arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123456789abcdef0",
        parameters={"isolate_sg": "sg-0123456789abcdef0"},
    )
    assert req.runbook_id == "RB-EC2-ISOLATE-001"

    eval1 = GuardrailEvaluation(
        step=GuardrailStep.STEP1_INPUT_SANITIZATION,
        status=GuardrailStatus.PASSED,
        details="Input parameters sanitized cleanly",
    )
    eval2 = GuardrailEvaluation(
        step=GuardrailStep.STEP2_SCHEMA_VALIDATION,
        status=GuardrailStatus.PASSED,
        details="Schema matches RB-EC2-ISOLATE-001 definition",
    )
    eval3 = GuardrailEvaluation(
        step=GuardrailStep.STEP3_ACTION_WHITELIST,
        status=GuardrailStatus.PASSED,
        details="Action ec2:ModifyInstanceAttribute is whitelisted",
    )
    eval4 = GuardrailEvaluation(
        step=GuardrailStep.STEP4_ARN_DRYRUN_MATCH,
        status=GuardrailStatus.PASSED,
        details="Target ARN belongs to valid account/region and dry-run succeeded",
    )

    res = GuardrailResponse(
        request_id=req.request_id,
        is_allowed=True,
        overall_status=GuardrailStatus.PASSED,
        evaluations=[eval1, eval2, eval3, eval4],
    )
    assert res.is_allowed is True
    assert len(res.evaluations) == 4


def test_runbook_definition_and_execution():
    runbook = RunbookDefinition(
        runbook_id="RB-EC2-ISOLATE-001",
        title="Isolate EC2 Instance",
        description="Attach quarantine security group to EC2 instance",
        risk_level=RunbookRiskLevel.MEDIUM,
        allowed_target_types=["aws_instance", "EC2"],
        allowed_actions=["ec2:ModifyInstanceAttribute"],
    )
    assert runbook.risk_level == RunbookRiskLevel.MEDIUM

    exec_req = RunbookExecutionRequest(
        execution_id="exec-5555",
        runbook_id=runbook.runbook_id,
        target_arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123456789abcdef0",
        execution_mode=ExecutionMode.BOTO3_DIRECT,
        requester_id="ai-agent-01",
    )
    assert exec_req.execution_mode == ExecutionMode.BOTO3_DIRECT
