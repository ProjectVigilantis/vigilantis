from enum import Enum
from typing import Any, Dict, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field


class AssetType(str, Enum):
    EC2 = "EC2"
    S3 = "S3"
    RDS = "RDS"
    IAM = "IAM"
    SECURITY_GROUP = "SECURITY_GROUP"
    LAMBDA = "LAMBDA"
    EKS = "EKS"
    OTHER = "OTHER"


class DriftStatus(str, Enum):
    IN_SYNC = "IN_SYNC"
    DRIFTED = "DRIFTED"
    UNTRACKED = "UNTRACKED"
    UNKNOWN = "UNKNOWN"


class AssetMetadata(BaseModel):
    asset_id: str = Field(..., description="Unique AWS resource ID or ARN (e.g. i-0123456789abcdef0)")
    name: str = Field(..., description="Human-readable asset name or tag:Name")
    asset_type: AssetType = Field(..., description="Type of AWS infrastructure asset")
    account_id: str = Field(..., description="AWS Account ID")
    region: str = Field(..., description="AWS Region")
    tags: Dict[str, str] = Field(default_factory=dict, description="Asset resource tags")
    attributes: Dict[str, Any] = Field(default_factory=dict, description="Resource specific attributes")
    created_at: Optional[datetime] = Field(default=None, description="Asset creation timestamp")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Last metadata sync timestamp")


class TerraformResource(BaseModel):
    address: str = Field(..., description="Terraform resource address (e.g., aws_instance.web)")
    type: str = Field(..., description="Terraform resource type (e.g., aws_instance)")
    name: str = Field(..., description="Terraform resource local name")
    provider_name: str = Field(default="aws", description="Terraform provider name")
    values: Dict[str, Any] = Field(default_factory=dict, description="Parsed resource values from state")


class TerraformDrift(BaseModel):
    drift_id: str = Field(..., description="Unique ID of the detected drift event")
    asset_id: str = Field(..., description="Target AWS asset ID / ARN")
    resource_address: str = Field(..., description="Terraform resource address in code")
    drift_status: DriftStatus = Field(..., description="Drift detection result status")
    expected_state: Dict[str, Any] = Field(default_factory=dict, description="Expected state from IaC (tfstate)")
    actual_state: Dict[str, Any] = Field(default_factory=dict, description="Actual state measured from AWS API")
    diff_details: Dict[str, Any] = Field(default_factory=dict, description="Detailed field-level diffs")
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Drift detection timestamp")
