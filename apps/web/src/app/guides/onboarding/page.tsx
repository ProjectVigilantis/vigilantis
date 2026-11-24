// 도입 가이드. 도입을 검토하는 회사가 가입 전에 읽는 문서라 로그인 없이 열린다(lib/return-to.ts `PUBLIC_PATHS`).
//
// 사실은 코드에서 확인한 것만 적는다. 도입 순서는 components/auth/signup-guide.tsx `APPLY_STEPS`, 연결 역할과 권한은
// scripts/onboarding_template.py(packages/iac/onboarding/vigilantis-access.json을 만든다), 연결 끊기와 남는 자원은
// 이용약관 제6조 · 제7조의2, GuardDuty는 lib/guardduty.ts와 GuardDuty 켜기 안내(app/guides/guardduty)다.
// 3절의 빠른 생성 링크(리전 · 스택 이름 · 파라미터)는 apps/core-api/tenant_admin.py `onboarding_url`, 연결 확인의
// 단계와 결과 이름은 apps/core-api/tenant_verification.py와 lib/tenants.ts `VERIFICATION_LABELS`다.
// 그 동작이 바뀌면 이 문서도 함께 고친다.

import type { Metadata } from 'next';
import Link from 'next/link';

import {
  GuideBranches,
  GuideHeading,
  GuidePreconditions,
  GuideSketch,
  GuideSources,
} from '@/components/guides/guide-steps';
import { LegalDocument, LegalList, LegalTable, type LegalSection } from '@/components/legal/legal-document';
import { GUARDDUTY_FEE_NOTICE } from '@/lib/guardduty';
import { SERVICE_NAME } from '@/lib/legal';
import { GUARDDUTY_GUIDE_PATH, LOGIN_OVERLAY_HREF, TERMS_PATH } from '@/lib/return-to';
import { ACCOUNT_KIND_LABELS } from '@/lib/roles';
import { ONBOARDING_STACK_NAME, VERIFICATION_LABELS } from '@/lib/tenants';

// 계정 이름은 화면의 계정 종류 표기(lib/roles.ts)를 그대로 쓴다. AWS의 관리자 권한과 섞이지 않게 "관리자 계정"이라고 쓰지 않는다.
const ADMIN = ACCOUNT_KIND_LABELS.TENANT_ADMIN;
const STAFF = ACCOUNT_KIND_LABELS.TENANT_STAFF;

export const metadata: Metadata = {
  title: `도입 가이드 · ${SERVICE_NAME}`,
  description: `${SERVICE_NAME} 도입 신청부터 AWS 계정 연결, ${ADMIN} 발급까지의 순서와 연결 역할이 받는 권한을 안내합니다.`,
};

const LINK = 'text-link hover:underline';

// 템플릿이 주는 동작을 빠짐없이 적는다. 고객사는 이 표를 보고 역할 생성을 정하므로, 삭제 · 생성 권한을 줄여 적으면 안 된다.
const PERMISSIONS: [string, string][] = [
  ['자산 · 지표 조회', 'EC2 · Auto Scaling · 로드 밸런서의 구성 조회와 CloudWatch 지표 조회'],
  ['비용 조회', 'Cost Explorer의 사용 요금 조회'],
  [
    '위협 탐지',
    'GuardDuty 탐지기 조회 · 생성과 탐지 결과 조회, GuardDuty를 처음 켤 때 생기는 서비스 연결 역할 생성(iam:CreateServiceLinkedRole, GuardDuty 역할 하나로 한정). 탐지기를 끄거나 지우는 권한은 없습니다.',
  ],
  ['조치: 인스턴스', '인스턴스 중지 · 시작 · 유형 변경, 인스턴스에서 이미지 생성'],
  [
    '조치: 네트워크',
    '보안 그룹 생성 · 삭제와 규칙 추가 · 아웃바운드 규칙 회수, 네트워크 인터페이스의 보안 그룹 교체, 네트워크 ACL 규칙 추가 · 삭제, 로드 밸런서 대상 그룹 등록 · 해제',
  ],
  ['조치: 저장소', 'EBS 스냅샷 생성, 연결되지 않은 볼륨 삭제(삭제 전에 스냅샷을 남깁니다)'],
  [
    '조치: Auto Scaling 전환',
    '시작 템플릿 · 이미지 생성, Auto Scaling 그룹 생성과 그 그룹의 조정 정책 · 인스턴스 보호 변경, 그룹이 띄우는 새 인스턴스 실행, 이 자원들의 태그 쓰기',
  ],
];

const SECTIONS: LegalSection[] = [
  {
    id: 'steps',
    title: '1. 도입 순서',
    body: (
      <LegalList
        numbered
        items={[
          '도입 신청: 회사와 담당자 정보를 보냅니다.',
          <>
            운영팀 확인: {SERVICE_NAME} 운영팀이 신청을 확인하고 회사를 등록한 뒤 신청서의 업무 이메일로 연락드립니다.
          </>,
          <>
            AWS 계정 연결: 고객사 AWS 담당자가 안내받은 CloudFormation 템플릿으로 {SERVICE_NAME}가 맡을 역할을
            만듭니다(3절).
          </>,
          <>
            관리자 초대: 운영팀이 연결을 확인하면 신청서의 업무 이메일로 {ADMIN} 초대 메일을 보냅니다. {ADMIN}은{' '}
            {STAFF}을 초대하고 권한을 관리하는 {SERVICE_NAME} 계정입니다. AWS 관리자 권한과는 별개입니다.
          </>,
        ]}
      />
    ),
  },
  {
    id: 'prepare',
    title: '2. 준비할 것',
    body: (
      <LegalList
        items={[
          '관제를 맡길 AWS 계정과, 그 계정에서 CloudFormation 스택과 IAM 역할을 만들 수 있는 고객사 AWS 담당자',
          `${ADMIN}으로 쓸 업무 이메일. 도입 신청서에 적은 이 주소로 초대 메일이 갑니다.`,
          `로그인할 때 쓸 인증 앱. ${ADMIN}을 포함한 모든 계정이 2단계 인증을 거칩니다.`,
        ]}
      />
    ),
  },
  {
    id: 'connect',
    title: '3. AWS 계정 연결',
    body: (
      <>
        <p>
          {SERVICE_NAME}는 장기 접근 키를 받지 않습니다. 이 역할을 회사 전용 ExternalId와 함께 맡아 들어가며, 역할은
          고객사가 소유합니다.
        </p>

        <GuidePreconditions
          items={[
            '운영팀이 보낸 안내문과 빠른 생성 링크. 안내문에 연결할 AWS 계정 ID(12자리)와 스택을 만들 리전이 적혀 있습니다.',
            '그 계정에서 CloudFormation 스택과 이름이 정해진 IAM 역할을 만들 수 있는 로그인',
            '4절의 권한 표를 읽고 역할 생성을 정한 상태',
          ]}
        />

        <GuideHeading>1. 연결할 AWS 계정으로 로그인한 브라우저에서 링크를 엽니다</GuideHeading>
        <GuideSketch
          title="링크를 연 뒤 상단 메뉴 막대에서 확인할 두 자리"
          summary="AWS 콘솔 상단 메뉴 막대의 도식. 오른쪽에 리전 메뉴(1)와 계정 메뉴(2)가 있습니다."
          screen="CloudFormation · 빠른 스택 생성"
          topbar={[
            { label: '리전', value: '안내문의 리전', mark: 1 },
            { label: '계정', value: '안내문의 계정 ID', mark: 2 },
          ]}
          items={[{ label: '본문', value: '링크가 채운 빠른 스택 생성 화면' }]}
          callouts={[
            <>
              리전: 링크가 연 리전을 바꾸지 않습니다. 링크는 고객사가 관제받는 첫 리전을 엽니다. 역할은
              계정 전체에 하나 만들어져 리전과 상관없이 쓰이지만, 운영팀은 안내문에 적힌 리전의 스택을 기준으로 확인합니다.
            </>,
            <>
              계정: 계정 메뉴의 계정 ID가 안내문의 12자리와 같은지 봅니다. 다르면 스택을 만들지 말고 맞는
              계정으로 다시 로그인한 뒤 링크를 새로 엽니다. 다른 계정에 만든 역할은 연결 확인에서 「
              {VERIFICATION_LABELS.ASSUME_DENIED.label}」로 나옵니다.
            </>,
          ]}
        />

        <GuideHeading>2. 링크가 채운 값을 바꾸지 않고 스택을 만듭니다</GuideHeading>
        <GuideSketch
          title="빠른 스택 생성 화면에서 볼 자리"
          summary="CloudFormation 빠른 스택 생성 화면의 도식. 스택 이름과 파라미터(1), IAM 리소스 확인란(2), 스택 생성 버튼(3)이 위에서 아래로 놓여 있습니다."
          screen="CloudFormation · 빠른 스택 생성"
          items={[
            { label: '템플릿 URL', value: '링크가 채움' },
            { label: '스택 이름', value: ONBOARDING_STACK_NAME, mark: 1 },
            { label: '파라미터', value: 'ExternalId · PlatformPrincipalArn · RoleName (링크가 채움)' },
            {
              label: '기능(Capabilities): IAM 리소스를 만들 수 있다는 데 동의하는 확인란',
              kind: 'check',
              mark: 2,
            },
            { label: '스택 생성', kind: 'button', mark: 3 },
          ]}
          callouts={[
            <>
              스택 이름 <code className="font-mono text-[13px]">{ONBOARDING_STACK_NAME}</code>와 파라미터 세 개가 채워져
              있는지만 봅니다. ExternalId는 회사 전용 식별값, PlatformPrincipalArn은 역할을 맡을{' '}
              {SERVICE_NAME} 쪽 주체, RoleName은 안내문의 「만들어지는 역할」과 같은 이름입니다.
            </>,
            <>
              IAM 리소스 확인란에 체크합니다. 이 템플릿은 이름이 정해진 IAM 역할을 만들어, 체크하지 않으면 AWS가 스택
              생성을 받지 않습니다.
            </>,
            <>
              스택 생성을 누릅니다. 태그 · 권한 · 실패 옵션 같은 선택 항목은 바꾸지 않아도 됩니다. 화면 문구는 콘솔
              언어에 따라 다르게 보일 수 있습니다.
            </>,
          ]}
        />

        <GuideHeading>3. 스택 완료를 회신하면 운영팀이 연결을 확인합니다</GuideHeading>
        <GuideBranches
          title="두 확인이 각각 알려 주는 것"
          question="스택이 CREATE_COMPLETE가 됐다고 연결이 끝난 것은 아닙니다"
          branches={[
            {
              actor: 'customer',
              label: '스택 CREATE_COMPLETE',
              when: '고객사가 CloudFormation 스택 화면에서 봅니다.',
              steps: [
                '스택 목록에서 상태가 CREATE_IN_PROGRESS를 거쳐 CREATE_COMPLETE가 되는지 봅니다. 진행은 이벤트(Events) 탭에 나옵니다.',
                '출력(Outputs) 탭의 RoleArn에서 만들어진 역할을 볼 수 있습니다.',
                '운영팀에 스택이 CREATE_COMPLETE가 됐다고 회신합니다.',
              ],
              result: '역할이 고객사 계정에 만들어졌습니다. 고객사가 할 일은 여기까지이며, 아직 연결된 것은 아닙니다.',
            },
            {
              actor: 'operator',
              label: '연결 확인',
              when: '회신을 받은 운영팀이 실제로 역할을 맡아 봅니다.',
              steps: [
                '회사 전용 ExternalId로 역할을 맡습니다.',
                '받은 자격의 계정이 등록한 계정 ID와 같은지 대조합니다.',
                '최소 읽기 한 건으로 권한 정책이 붙었는지 봅니다.',
                '관제 리전마다 GuardDuty를 보고, 없으면 켭니다(5절).',
              ],
              result: `「${VERIFICATION_LABELS.OK.label}」이면 연결이 끝나고, ${ADMIN}이 초대 메일로 발급됩니다(1절 4번).`,
            },
          ]}
          note="스택이 CREATE_COMPLETE가 되지 않고 CREATE_FAILED · ROLLBACK_COMPLETE 같은 실패 상태로 끝나면, 스택을 지우거나 다시 만들기 전에 이벤트 탭의 상태 사유를 운영팀에 알려 주세요. 무엇이 남았는지는 스택의 리소스 탭에서 볼 수 있고, 다음 조치는 사유에 따라 운영팀이 안내합니다."
        />

        <p>연결 확인 결과는 운영팀이 알려 드립니다. 결과마다 고객사가 할 일은 아래와 같습니다.</p>
        <LegalTable
          head={['연결 확인 결과', '뜻', '고객사가 할 일']}
          rows={[
            [VERIFICATION_LABELS.OK.label, '역할을 맡아 읽기 권한과 관제 리전의 GuardDuty까지 확인했습니다.', `${ADMIN} 초대 메일을 기다립니다.`],
            [
              VERIFICATION_LABELS.ASSUME_DENIED.label,
              `역할이 없거나, 신뢰 정책이 ${SERVICE_NAME} 또는 ExternalId를 허용하지 않습니다.`,
              '안내문의 계정에 스택이 CREATE_COMPLETE로 있는지, 링크의 값을 바꾸지 않았는지 확인합니다. 운영팀 안내에 따라 링크로 스택을 다시 만듭니다.',
            ],
            [
              VERIFICATION_LABELS.READ_DENIED.label,
              '역할은 맡았지만 권한 정책이 붙어 있지 않습니다.',
              '스택이 만든 역할의 권한 정책을 지우거나 고치지 않았는지 확인합니다.',
            ],
            [
              VERIFICATION_LABELS.GUARDDUTY_DISABLED.label,
              '관제 리전 중 GuardDuty를 켜지 못한 곳이 있습니다.',
              <>
                운영팀이 보낸 리전별 안내대로{' '}
                <Link href={GUARDDUTY_GUIDE_PATH} className={LINK}>
                  GuardDuty 켜기 안내
                </Link>
                를 따라 켭니다.
              </>,
            ],
            [
              VERIFICATION_LABELS.UNREACHABLE.label,
              'AWS에 묻지 못했습니다(네트워크 · 요청 제한). 연결 여부와는 무관합니다.',
              '할 일이 없습니다. 운영팀이 잠시 뒤 다시 확인합니다.',
            ],
          ]}
        />

        <GuideSources
          links={[
            {
              label: 'CloudFormation 빠른 생성 링크로 스택 만들기',
              href: 'https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/cfn-console-create-stacks-quick-create-links.html',
            },
            {
              label: 'CloudFormation 템플릿의 IAM 리소스 확인',
              href: 'https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/control-access-with-iam.html#using-iam-capabilities',
            },
            {
              label: 'CloudFormation 스택 이벤트와 상태 코드',
              href: 'https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/view-stack-events.html',
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
    id: 'permissions',
    title: '4. 연결 역할이 받는 권한',
    body: (
      <>
        <LegalTable head={['구분', '내용']} rows={PERMISSIONS} />
        <p>
          조치 권한은 리전을 가리지 않고 계정 안의 같은 종류 자원 전부에 걸립니다. 런북마다 대상을 좁히는 것은
          {SERVICE_NAME}의 실행 전 검증이며, IAM 정책이 그 범위를 막지는 않습니다.
        </p>
        <p>
          조치가 새로 만드는 이미지 · 시작 템플릿 · Auto Scaling 그룹은 {SERVICE_NAME} 실행 태그가 붙어야만 만들어지도록
          권한이 묶여 있습니다. 조정 정책 · 인스턴스 보호 변경과 새 인스턴스 실행도 {SERVICE_NAME}가 만든 그룹 · 시작
          템플릿 · 이미지에만 됩니다. IAM 권한은 위의 GuardDuty 서비스 연결 역할 생성 하나뿐이며, 역할을 다른 서비스에 넘기는
          권한(iam:PassRole)은 받지 않습니다.
        </p>
        <p>
          모든 조치는 입력 형식 · 허용 런북 · 대상 자원 일치 · AWS 사전 확인의 4단계 검증을 거친 뒤에만 실행됩니다. 사전
          확인은 AWS 사전 실행(Dry-Run)으로 하고, 사전 실행을 지원하지 않는 작업은 대상 자원 조회로 대신합니다.
        </p>
      </>
    ),
  },
  {
    id: 'guardduty',
    title: '5. GuardDuty',
    body: (
      <>
        <p>
          {SERVICE_NAME}는 Amazon GuardDuty가 찾아낸 위협을 받아 관제합니다. 연결을 확인할 때 관제 리전에 GuardDuty
          탐지기가 없으면 기본 위협 탐지만 켭니다. 탐지기를 일부러 멈춰 둔 경우는 그대로 두므로 고객사 AWS 담당자가 직접
          켜야 합니다. 방법은{' '}
          <Link href={GUARDDUTY_GUIDE_PATH} className={LINK}>
            GuardDuty 켜기 안내
          </Link>
          에 있습니다.
        </p>
        <p>{GUARDDUTY_FEE_NOTICE}</p>
      </>
    ),
  },
  {
    id: 'disconnect',
    title: '6. 연결 끊기',
    body: (
      <LegalList
        items={[
          '고객사는 연결 역할을 지우거나 고쳐 언제든지 접근을 끊을 수 있습니다.',
          '연결 과정에서 켠 GuardDuty는 이용계약이 끝나도 자동으로 꺼지지 않습니다. 따로 끄지 않으면 AWS 사용 요금이 계속 생길 수 있습니다.',
          <>
            조치로 만든 스냅샷 · 이미지 같은 자원은 이용계약이 끝난 뒤 고객사가 관리합니다. 자세한 내용은{' '}
            <Link href={`${TERMS_PATH}#resource-addition`} className={LINK}>
              이용약관 제7조의2
            </Link>
            를 따릅니다.
          </>,
        ]}
      />
    ),
  },
];

export default function OnboardingGuidePage() {
  return (
    <LegalDocument
      title="도입 가이드"
      dated={false}
      layout="guide"
      intro={
        <p>
          도입을 신청하려면 <Link href={LOGIN_OVERLAY_HREF} className={LINK}>로그인 창</Link>에서 ‘회원가입’을 선택하세요.
        </p>
      }
      sections={SECTIONS}
    />
  );
}
