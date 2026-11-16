// GuardDuty를 켜지 못했을 때의 안내(Post-MVP T-14 · #588 D). 관리자 화면(고객 상세의 연결 패널)과 고객사에
// 보내는 안내 페이지(/guides/guardduty)가 같이 쓴다.
//
// 원천은 연결 확인 결과의 `guardduty_failures`(packages/schemas/api/tenants.py `GuardDutyRegionFailure`)다.
// 연결 확인은 관제 리전마다 탐지기를 보고 없으면 만든다. 여기 오는 것은 그렇게 해도 켜지 못한 리전뿐이다.
// 사유 코드는 `DetectorDisabled`(정지된 탐지기 — 플랫폼이 다시 켜지 않는다, #588 Q1)이거나 AWS 오류 코드다.
// 코드별 문구는 연결 확인이 실제로 구분하는 것(apps/core-api/tests/test_tenant_verification.py)만 둔다.

import type { GuardDutyRegionFailure, TenantDetail, TenantVerification } from '../types/api.ts';

/** 요금 고지. 온보딩 안내문 · 연결 패널 · 안내 페이지가 같은 문장을 쓴다(#588 D). */
export const GUARDDUTY_FEE_NOTICE =
  'GuardDuty 요금은 고객사 AWS 계정에 부과됩니다. 계정과 리전마다 처음 30일은 무료 체험이고, 그 뒤에는 AWS가 분석한 로그 양에 따라 요금이 나옵니다.';

/**
 * 고객사가 직접 켤 때의 요금 주의(안내 페이지 §5). 플랫폼은 부가 보호 기능을 모두 끈 채 만들지만
 * (services/guardduty.py `DISABLED_FEATURES`), 고객사가 콘솔이나 명령줄로 켜면 그 기능이 함께 켜질 수 있다 —
 * CreateDetector는 기능 목록을 주지 않으면 부가 기능을 켠 채 만든다. 콘솔의 기본값은 확인하지 못해 「켜질 수 있고」로 적는다.
 */
export const GUARDDUTY_SELF_ENABLE_NOTICE =
  '직접 켤 때 S3 · EKS · 맬웨어 보호 같은 부가 보호 기능이 함께 켜질 수 있고, 켜진 기능마다 요금이 따로 붙습니다. 필요 없으면 GuardDuty 콘솔의 보호 플랜 설정에서 끕니다.';

export const GUARDDUTY_PRICING_URL = 'https://aws.amazon.com/guardduty/pricing/';

export interface GuardDutyReason {
  /** 무슨 일인지 한마디. */
  label: string;
  /** 고객사가 할 일. */
  action: string;
}

const FALLBACK: GuardDutyReason = {
  label: '켜지 못함',
  action: 'AWS 콘솔에서 이 리전의 GuardDuty를 직접 켭니다.',
};

const REASONS: Record<string, GuardDutyReason> = {
  DetectorDisabled: {
    label: '탐지기 정지됨',
    action: 'GuardDuty 콘솔의 설정에서 GuardDuty를 다시 활성화합니다. 정지해 둔 탐지기는 Vigilantis가 다시 켜지 않습니다.',
  },
  AccessDeniedException: {
    label: '권한 거절',
    action:
      '조직의 서비스 제어 정책(SCP)이 GuardDuty를 막는지 확인합니다. 연결 스택을 예전에 만들었다면 최신 템플릿으로 스택을 업데이트하거나, 콘솔에서 GuardDuty를 직접 켭니다.',
  },
  AccessDenied: {
    label: '서비스 역할 생성 거절',
    action: 'GuardDuty가 쓰는 서비스 연결 역할을 만들 권한이 없습니다. 콘솔에서 GuardDuty를 직접 켜면 역할이 함께 만들어집니다.',
  },
  BadRequestException: {
    label: 'AWS가 요청을 거절',
    action:
      '조직의 GuardDuty 관리 계정이 이 계정을 관리하는지, 이 리전이 GuardDuty를 지원하는지 확인합니다. 조직이 관리하면 관리 계정에서 켭니다.',
  },
};

/** 사유 코드의 안내. 모르는 코드는 「직접 켠다」로 접는다 — 코드는 화면이 그대로 함께 보인다. */
export function guardDutyReason(reasonCode: string): GuardDutyReason {
  // 사유 코드는 서버가 넘긴 문자열이다 — 객체의 기본 속성(`constructor` 등)에 걸리지 않게 자기 키만 본다
  return Object.hasOwn(REASONS, reasonCode) ? REASONS[reasonCode] : FALLBACK;
}

/**
 * 켜지 못한 리전 목록. GUARDDUTY_DISABLED가 아니면 비어 있다. 이 칸이 생기기 전에 남은 GUARDDUTY_DISABLED
 * 기록에는 목록이 없다(null) — 그때는 빈 배열이고, 화면은 「다시 확인하면 리전이 나온다」고 안내한다.
 */
export function guardDutyFailures(verification: TenantVerification | null): readonly GuardDutyRegionFailure[] {
  if (verification?.result !== 'GUARDDUTY_DISABLED') return [];
  return verification.guardduty_failures ?? [];
}

/**
 * 관리자가 고객사에 보낼 안내문. 켜지 못한 리전과 리전별 할 일, 안내 페이지 주소, 요금 고지를 싣는다.
 * 리전 목록이 없으면(예전 기록) 관제 리전 전부를 확인해 달라고 한다.
 *
 * 이 글을 받는 사람이 GuardDuty를 직접 켜는 사람이다. 그래서 요금 고지 뒤에 직접 켤 때의 주의
 * (`GUARDDUTY_SELF_ENABLE_NOTICE`)를 함께 싣는다 — 안내 페이지 링크를 열어야만 보이게 두지 않는다(#624 재리뷰).
 */
export function guardDutyMessage(
  tenant: Pick<TenantDetail, 'aws_account_id' | 'regions'>,
  failures: readonly GuardDutyRegionFailure[],
  guideUrl: string,
): string {
  const regions =
    failures.length > 0
      ? failures.map((f) => {
          const reason = guardDutyReason(f.reason_code);
          return `- ${f.region}: ${reason.label}(${f.reason_code}). ${reason.action}`;
        })
      : tenant.regions.map((region) => `- ${region}: GuardDuty가 켜져 있는지 확인합니다.`);
  return [
    `Vigilantis 관제를 시작하려면 AWS 계정 ${tenant.aws_account_id}의 아래 리전에서 Amazon GuardDuty를 켜 주세요.`,
    '',
    ...regions,
    '',
    `켜는 방법: ${guideUrl}`,
    GUARDDUTY_FEE_NOTICE,
    GUARDDUTY_SELF_ENABLE_NOTICE,
    '',
    '켠 뒤 회신 주시면 연결을 다시 확인하겠습니다.',
  ].join('\n');
}
