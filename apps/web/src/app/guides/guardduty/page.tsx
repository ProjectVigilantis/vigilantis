// GuardDuty 켜기 안내(Post-MVP T-14 · #588 D). 연결 확인이 GuardDuty를 켜지 못했을 때 관리자가 고객사에 보내는
// 주소다. 받는 사람은 고객사의 AWS 담당자라 로그인 없이 열린다(lib/return-to.ts `PUBLIC_PATHS`).
//
// 고객사마다 다른 값(어느 리전 · 어떤 사유)은 여기 없다 — 그건 관리자가 복사해 보내는 안내문에 실린다
// (lib/guardduty.ts `guardDutyMessage`). 사실은 코드에서 확인한 것만 적는다: 플랫폼이 하는 일은
// apps/core-api/services/guardduty.py(없으면 만든다 · 부가 보호 기능은 끈 채 · 정지된 탐지기는 다시 켜지 않는다 ·
// 끄는 호출 없음), 사유별 안내는 lib/guardduty.ts다. 그 동작이 바뀌면 이 문서도 함께 고친다.
//
// **명령줄 예시는 두지 않는다.** `aws guardduty create-detector --enable`은 기능 목록을 주지 않으면 부가 보호 기능이
// 켜진 채 만들어진다(CreateDetector API — 플랫폼이 만들 때 기능을 모두 끈 채 넘기는 이유와 같다). 문서가 말하는
// 요금(기본 탐지)과 고객사 청구액이 어긋나게 된다. 기능을 끄는 긴 명령을 싣는 대신, 직접 켤 때 부가 기능이 함께
// 켜질 수 있다는 것과 끄는 자리를 §5에 적는다(#624 리뷰).

import type { Metadata } from 'next';

import { LegalDocument, LegalList, LegalTable, type LegalSection } from '@/components/legal/legal-document';
import {
  GUARDDUTY_FEE_NOTICE,
  GUARDDUTY_PRICING_URL,
  GUARDDUTY_SELF_ENABLE_NOTICE,
  guardDutyReason,
} from '@/lib/guardduty';
import { SERVICE_NAME } from '@/lib/legal';

export const metadata: Metadata = {
  title: `GuardDuty 켜기 안내 · ${SERVICE_NAME}`,
  description: `${SERVICE_NAME} 관제를 시작하기 전에 고객사 AWS 계정에서 Amazon GuardDuty를 켜는 방법과 요금을 안내합니다.`,
};

const SITUATIONS: [string, string][] = [
  ['GuardDuty를 정지해 둔 리전', 'DetectorDisabled'],
  ['조직 정책이 막거나 연결 스택이 예전 것인 경우', 'AccessDeniedException'],
  ['서비스 연결 역할을 만들 권한이 없는 경우', 'AccessDenied'],
  ['조직의 GuardDuty 관리 계정이 관리하거나 리전이 지원하지 않는 경우', 'BadRequestException'],
];

const SECTIONS: LegalSection[] = [
  {
    id: 'why',
    title: '1. GuardDuty가 왜 필요한가요',
    body: (
      <>
        <p>
          {SERVICE_NAME}는 고객사 AWS 계정의 Amazon GuardDuty가 찾아낸 위협을 받아 관제합니다. 그래서 관제받는 리전마다
          GuardDuty가 켜져 있어야 연결이 끝납니다. 한 리전이라도 꺼져 있으면 관제를 시작하지 않습니다.
        </p>
      </>
    ),
  },
  {
    id: 'platform',
    title: `2. ${SERVICE_NAME}가 하는 일과 하지 않는 일`,
    body: (
      <LegalList
        items={[
          '연결을 확인할 때 관제 리전에 GuardDuty 탐지기가 없으면 새로 만들어 켭니다.',
          '기본 위협 탐지만 켭니다. S3 · EKS · 맬웨어 보호 같은 부가 보호 기능은 켜지 않습니다.',
          '고객사가 정지해 둔 탐지기는 다시 켜지 않습니다. 일부러 정지했을 수 있기 때문입니다.',
          'GuardDuty를 끄거나 지우지 않습니다. 그 권한을 받지 않습니다.',
        ]}
      />
    ),
  },
  {
    id: 'cases',
    title: '3. 직접 켜야 하는 경우',
    body: (
      <>
        <p>아래 경우에는 {SERVICE_NAME}가 켜지 못합니다. 담당자가 보낸 안내문에 어느 리전이 어떤 경우인지 적혀 있습니다.</p>
        <LegalTable
          head={['상황', '할 일']}
          rows={SITUATIONS.map(([situation, code]) => [situation, guardDutyReason(code).action])}
        />
      </>
    ),
  },
  {
    id: 'how',
    title: '4. 켜는 방법',
    body: (
      <LegalList
        numbered
        items={[
          '안내받은 AWS 계정으로 AWS 콘솔에 로그인합니다.',
          '오른쪽 위 리전 목록에서 안내받은 리전을 고릅니다. GuardDuty는 리전마다 따로 켭니다.',
          'GuardDuty 서비스를 엽니다.',
          '처음 쓰는 리전이면 시작 화면에서 GuardDuty를 활성화합니다. 정지해 둔 리전이면 설정에서 다시 활성화합니다.',
          '안내받은 리전이 여럿이면 리전마다 되풀이합니다.',
          '다 켰으면 담당자에게 알려 주세요. 담당자가 연결을 다시 확인합니다.',
        ]}
      />
    ),
  },
  {
    id: 'fee',
    title: '5. 요금',
    body: (
      <>
        <p>{GUARDDUTY_FEE_NOTICE}</p>
        <p>{GUARDDUTY_SELF_ENABLE_NOTICE}</p>
        <p>
          {SERVICE_NAME} 이용 요금과는 별개이며 AWS 청구서에 나옵니다. 무료 체험이 며칠 남았는지와 예상 비용은 GuardDuty
          콘솔의 사용량 화면에서 볼 수 있습니다. 요금 기준은{' '}
          <a href={GUARDDUTY_PRICING_URL} target="_blank" rel="noreferrer" className="text-primary hover:underline">
            AWS의 GuardDuty 요금 안내
          </a>
          를 따릅니다.
        </p>
      </>
    ),
  },
  {
    id: 'after',
    title: '6. 켠 뒤에는',
    body: (
      <p>
        GuardDuty를 끄거나 정지하면 {SERVICE_NAME}가 그 리전의 위협을 받지 못합니다. 관제를 받는 동안에는 켜 둔 채로 두세요.
      </p>
    ),
  },
];

export default function GuardDutyGuidePage() {
  return (
    <LegalDocument
      title="GuardDuty 켜기 안내"
      dated={false}
      intro={
        <p>
          {SERVICE_NAME} 관제를 시작하려면 고객사 AWS 계정의 관제 리전마다 Amazon GuardDuty가 켜져 있어야 합니다. 이
          문서는 {SERVICE_NAME}가 대신 켜지 못한 리전을 고객사가 직접 켜는 방법을 안내합니다.
        </p>
      }
      sections={SECTIONS}
    />
  );
}
