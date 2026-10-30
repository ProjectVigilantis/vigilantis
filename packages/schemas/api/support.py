"""#552 접수·문의·운영자 알림 공통 어휘와 입력 예산.

짧은 표시 문자열은 TenantName(100), 연락 이메일은 Email(254),
자유 입력은 ResolutionNote(1000), 반려 사유는 정지 사유(500)의
기존 저장 규격을 따른다. 남용 방지 요청 횟수나 실측 적정값이 아니다.
전화번호는 자유 입력 연락처이며 E.164 정규화·검증을 약속하지 않는다.
"""

from enum import Enum

SHORT_TEXT_MAX_LENGTH = 100
SUPPORT_TEXT_MAX_LENGTH = 1000
REJECTION_REASON_MAX_LENGTH = 500


class SignupRequestStatus(str, Enum):
    NEW = "NEW"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class InquiryCategory(str, Enum):
    OUTAGE = "OUTAGE"
    BUG = "BUG"
    OTHER = "OTHER"


class InquiryStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"


class InquiryEventAction(str, Enum):
    INQUIRY_CREATED = "INQUIRY_CREATED"
    STATUS_CHANGED = "STATUS_CHANGED"
    NOTE_ADDED = "NOTE_ADDED"


class NotificationKind(str, Enum):
    SIGNUP_REQUEST = "SIGNUP_REQUEST"
    INQUIRY = "INQUIRY"
