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
//
// 콘솔의 단추 이름은 AWS 공식 문서의 영문 표기를 그대로 적는다(콘솔 언어마다 번역이 달라 확인할 수 없다).
// 처음 켜기 · 다시 활성화 · 보호 플랜 설정의 순서는 §4 · §5 끝에 연결한 AWS 사용 설명서를 따른다.

import type { Metadata } from 'next';

import { GuideBranches, GuideFlow, GuidePreconditions, GuideSources } from '@/components/guides/guide-steps';
import { LegalDocument, LegalList, LegalTable, type LegalSection } from '@/components/legal/legal-document';
import {
  GUARDDUTY_FEE_NOTICE,
  GUARDDUTY_PRICING_URL,
  GUARDDUTY_SELF_ENABLE_NOTICE,
  guardDutyReason,
} from '@/lib/guardduty';
import { SERVICE_NAME } from '@/lib/legal';
import { VERIFICATION_LABELS } from '@/lib/tenants';

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
        <p>아래 경우에는 {SERVICE_NAME}가 켜지 못합니다. 운영팀이 보낸 안내문에 어느 리전이 어떤 경우인지 적혀 있습니다.</p>
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
      <>
        <GuidePreconditions
          items={[
            '운영팀이 보낸 안내문. AWS 계정 ID와 켤 리전, 리전마다의 사유가 적혀 있습니다.',
            '그 계정에서 GuardDuty를 켤 수 있는 로그인. AWS는 관리자 권한이 있는 사용자면 켤 수 있다고 안내합니다.',
          ]}
        />

        <GuideFlow
          title="리전 하나를 켜는 순서"
          steps={[
            {
              actor: 'customer',
              where: 'AWS 콘솔 · 계정 메뉴',
              title: '안내받은 AWS 계정으로 로그인합니다.',
              body: `다른 계정에서 켜면 ${SERVICE_NAME}가 관제하는 계정에는 반영되지 않습니다.`,
              signal: '계정 메뉴의 계정 ID가 안내문과 같습니다.',
            },
            {
              actor: 'customer',
              where: '오른쪽 위 리전 목록',
              title: '안내받은 리전 하나를 고르고 GuardDuty 서비스를 엽니다.',
              body: 'GuardDuty는 리전마다 따로 켭니다. 화면은 지금 고른 리전의 상태만 보입니다.',
              signal: '리전 목록에 그 리전이 보입니다.',
            },
            {
              actor: 'customer',
              where: 'GuardDuty',
              title: '아래 세 경우 중 이 리전에 맞는 것을 따라 켭니다.',
              signal: '시작 화면이나 「Re-enable GuardDuty」 단추 없이 GuardDuty의 요약 · 탐지 결과 화면이 열립니다.',
            },
            {
              actor: 'customer',
              where: 'GuardDuty · Protection Plans',
              title: '켜진 보호 플랜과 요금을 5절대로 확인합니다.',
            },
          ]}
          loop="안내받은 리전이 남았으면 2단계로 돌아가 다음 리전을 고릅니다. 다 켰으면 6절로 넘어갑니다."
        />

        <GuideBranches
          title="3단계: 이 리전에 맞는 켜기"
          question="GuardDuty를 연 화면과 안내문의 사유로 이 리전이 어느 경우인지 고릅니다"
          branches={[
            {
              actor: 'customer',
              label: '처음 쓰는 리전',
              when: '시작 화면이 보입니다. 이 리전에서 GuardDuty를 쓴 적이 없습니다.',
              steps: [
                '「Amazon GuardDuty - All features」를 고릅니다.',
                '「Get started」를 누릅니다.',
                <span key="enable">「Welcome to GuardDuty」 화면에서 서비스 약관을 보고 <strong>Enable GuardDuty</strong>를 누릅니다.</span>,
              ],
              result:
                '그 리전에서 바로 탐지를 시작하고, 그 리전의 30일 무료 체험이 시작됩니다. AWS는 처음 켤 때 일부 보호 플랜을 기본으로 함께 켭니다.',
            },
            {
              actor: 'customer',
              label: '정지해 둔 리전',
              when: `안내문의 사유가 「${guardDutyReason('DetectorDisabled').label}」(DetectorDisabled)입니다.`,
              steps: [
                '왼쪽 탐색 창에서 「Settings」를 엽니다.',
                <span key="re-enable"><strong>Re-enable GuardDuty</strong>를 누릅니다.</span>,
              ],
              result: `정지 전의 탐지 결과는 그대로 남고, 그 리전의 탐지가 다시 시작됩니다. 정지해 둔 탐지기는 ${SERVICE_NAME}가 다시 켜지 않습니다.`,
            },
            {
              actor: 'customer',
              label: '조직 관리 계정 · 미지원 리전',
              when: `안내문의 사유가 「${guardDutyReason('BadRequestException').label}」(BadRequestException)입니다.`,
              steps: [
                '조직의 GuardDuty 관리 계정이 이 계정을 관리하면, 그 관리 계정 담당자에게 이 계정과 리전을 켜 달라고 요청합니다.',
                '조직이 관리하지 않으면 그 리전이 GuardDuty를 지원하는지 확인합니다.',
              ],
              result: '관리 계정에서 켰거나 지원하지 않는 리전임을 확인했으면 운영팀에 알려 주세요.',
            },
          ]}
          note="단추 이름은 AWS 공식 문서의 영문 표기입니다. 콘솔 언어에 따라 다르게 보일 수 있습니다."
        />

        <GuideSources
          links={[
            {
              label: 'GuardDuty 시작하기(처음 켜기)',
              href: 'https://docs.aws.amazon.com/guardduty/latest/ug/guardduty_settingup.html',
            },
            {
              label: 'GuardDuty 정지 · 다시 활성화',
              href: 'https://docs.aws.amazon.com/guardduty/latest/ug/guardduty_suspend-disable.html',
            },
            {
              label: 'AWS 콘솔에서 리전 고르기',
              href: 'https://docs.aws.amazon.com/awsconsolehelpdocs/latest/gsg/select-region.html',
            },
          ]}
        />
      </>
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
          {SERVICE_NAME} 이용 요금과는 별개이며 AWS 청구서에 나옵니다. 요금 기준은{' '}
          <a href={GUARDDUTY_PRICING_URL} target="_blank" rel="noreferrer" className="text-link hover:underline">
            AWS의 GuardDuty 요금 안내
          </a>
          를 따릅니다.
        </p>

        <GuideFlow
          title="보호 플랜과 요금 확인"
          steps={[
            {
              actor: 'customer',
              where: 'GuardDuty · 탐색 창 「Protection Plans」',
              title: '켜진 보호 플랜을 봅니다.',
              body: 'S3 보호 · EKS 보호 · 맬웨어 보호처럼 기본 위협 탐지 밖의 플랜이 켜져 있는지 봅니다.',
            },
            {
              actor: 'customer',
              where: '「Configure all enablements」',
              title: '필요 없는 플랜을 끕니다.',
              body: `${SERVICE_NAME} 관제에는 기본 위협 탐지면 됩니다. 필요한 플랜은 켜 두어도 됩니다.`,
              signal: '「Save all」과 「Confirm and save」를 누른 뒤, 끈 플랜이 꺼짐으로 보입니다.',
            },
            {
              actor: 'customer',
              where: 'GuardDuty · 사용량 화면',
              title: '무료 체험이 며칠 남았는지와 예상 비용을 봅니다.',
              body: 'AWS 요금 안내에 따르면 S3 맬웨어 보호와 EC2 맬웨어 보호의 온디맨드 검사는 30일 무료 체험에 들지 않습니다.',
            },
          ]}
          loop="보호 플랜과 무료 체험은 리전마다 따로입니다. 켠 리전마다 되풀이합니다."
        />

        <GuideSources
          links={[
            {
              label: 'GuardDuty 요금과 30일 무료 체험',
              href: 'https://docs.aws.amazon.com/guardduty/latest/ug/guardduty-pricing.html',
            },
            {
              label: 'GuardDuty 보호 플랜 설정',
              href: 'https://docs.aws.amazon.com/guardduty/latest/ug/protection-plans-configure.html',
            },
            {
              label: 'GuardDuty 사용량과 비용 추정',
              href: 'https://docs.aws.amazon.com/guardduty/latest/ug/monitoring_costs.html',
            },
          ]}
        />
      </>
    ),
  },
  {
    id: 'after',
    title: '6. 켠 뒤에는',
    body: (
      <>
        <p>
          GuardDuty를 끄거나 정지하면 {SERVICE_NAME}가 그 리전의 위협을 받지 못합니다. 관제를 받는 동안에는 켜 둔 채로
          두세요.
        </p>

        <GuideFlow
          title="운영팀의 연결 재확인"
          steps={[
            {
              actor: 'customer',
              title: '켠 리전을 운영팀에 알립니다.',
            },
            {
              actor: 'operator',
              title: '연결을 다시 확인합니다.',
              body: '관제 리전을 모두 다시 봅니다. 켜져 있는 탐지기는 그대로 두고, 탐지기가 없는 리전은 기본 위협 탐지만 켠 채 만듭니다.',
              signal: `결과가 「${VERIFICATION_LABELS.OK.label}」이면 ${SERVICE_NAME}가 그 리전들의 GuardDuty 탐지 결과를 받아 관제합니다.`,
            },
            {
              actor: 'operator',
              title: `한 리전이라도 켜지지 않았으면 결과가 「${VERIFICATION_LABELS.GUARDDUTY_DISABLED.label}」입니다.`,
              body: '남은 리전과 리전별 사유를 담은 안내문을 다시 보내 드립니다. 그 리전만 4절대로 켭니다.',
            },
          ]}
        />
      </>
    ),
  },
];

export default function GuardDutyGuidePage() {
  return (
    <LegalDocument
      title="GuardDuty 켜기 안내"
      dated={false}
      layout="guide"
      intro={
        <p>
          {SERVICE_NAME} 관제를 시작하려면 고객사 AWS 계정의 관제 리전마다 Amazon GuardDuty가 켜져 있어야 합니다. 이
          문서는 {SERVICE_NAME}가 대신 켜지 못한 리전을 고객사 AWS 담당자가 직접 켜는 방법을 안내합니다. 켤 리전과
          리전마다의 사유는 연결을 확인한 {SERVICE_NAME} 운영팀이 안내문으로 보내 드립니다.
        </p>
      }
      sections={SECTIONS}
    />
  );
}
