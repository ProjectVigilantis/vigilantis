// 도입 가이드. 도입을 검토하는 회사가 가입 전에 읽는 문서라 로그인 없이 열린다(lib/return-to.ts `PUBLIC_PATHS`).
//
// 사실은 코드에서 확인한 것만 적는다. 도입 순서는 components/auth/signup-guide.tsx `APPLY_STEPS`, 연결 역할과 권한은
// scripts/onboarding_template.py(packages/iac/onboarding/vigilantis-access.json을 만든다), 연결 끊기와 남는 자원은
// 이용약관 제6조 · 제7조의2, GuardDuty는 lib/guardduty.ts와 GuardDuty 켜기 안내(app/guides/guardduty)다.
// 그 동작이 바뀌면 이 문서도 함께 고친다.

import type { Metadata } from 'next';
import Link from 'next/link';

import { LegalDocument, LegalList, LegalTable, type LegalSection } from '@/components/legal/legal-document';
import { GUARDDUTY_FEE_NOTICE } from '@/lib/guardduty';
import { SERVICE_NAME } from '@/lib/legal';
import { GUARDDUTY_GUIDE_PATH, LOGIN_OVERLAY_HREF, TERMS_PATH } from '@/lib/return-to';

export const metadata: Metadata = {
  title: `도입 가이드 · ${SERVICE_NAME}`,
  description: `${SERVICE_NAME} 도입 신청부터 AWS 계정 연결, 관리자 계정 발급까지의 순서와 연결 역할이 받는 권한을 안내합니다.`,
};

const LINK = 'text-primary hover:underline';

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
          <>
            <strong>도입 신청</strong>: 회사와 담당자 정보를 보냅니다. 신청은{' '}
            <Link href={LOGIN_OVERLAY_HREF} className={LINK}>
              로그인 창
            </Link>
            의 회원가입에서 합니다.
          </>,
          <>
            <strong>운영팀 확인</strong>: 운영팀이 신청을 확인하고 회사를 등록한 뒤 담당자 이메일로 연락드립니다.
          </>,
          <>
            <strong>AWS 계정 연결</strong>: 안내받은 CloudFormation 템플릿으로 {SERVICE_NAME}가 맡을 역할을 만듭니다(3절).
          </>,
          <>
            <strong>관리자 초대</strong>: 연결이 확인되면 회사 관리자 계정이 초대 메일로 발급됩니다. 직원은 관리자가
            초대합니다.
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
          '관제를 맡길 AWS 계정과, 그 계정에서 CloudFormation 스택과 IAM 역할을 만들 수 있는 담당자',
          '회사 관리자 계정을 받을 담당자의 이메일',
          '로그인할 때 쓸 인증 앱. 관리자를 포함한 모든 계정이 2단계 인증을 거칩니다.',
        ]}
      />
    ),
  },
  {
    id: 'connect',
    title: '3. AWS 계정 연결',
    body: (
      <>
        <LegalList
          numbered
          items={[
            '운영팀이 보낸 빠른 생성 링크를 연결할 AWS 계정으로 로그인한 브라우저에서 엽니다.',
            '링크에는 역할 이름과 회사 전용 식별값(ExternalId)이 채워져 있습니다. 값을 바꾸지 말고 스택을 만듭니다.',
            '스택이 만든 역할을 운영팀이 연결 확인으로 맡아 봅니다. 확인이 끝나면 관리자 초대로 넘어갑니다.',
          ]}
        />
        <p>
          {SERVICE_NAME}는 장기 접근 키를 받지 않습니다. 이 역할을 회사 전용 ExternalId와 함께 맡아 들어가며, 역할은
          고객사가 소유합니다.
        </p>
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
          탐지기가 없으면 기본 위협 탐지만 켭니다. 탐지기를 일부러 멈춰 둔 경우는 그대로 두므로 직접 켜야 합니다. 방법은{' '}
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
          '연결 역할을 지우거나 고치면 언제든지 접근을 끊을 수 있습니다.',
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
      intro={
        <p>
          {SERVICE_NAME}는 회사 단위로 도입합니다. 도입 신청부터 AWS 계정 연결, 관리자 계정 발급까지의 순서와 연결 역할이
          받는 권한을 안내합니다.
        </p>
      }
      sections={SECTIONS}
    />
  );
}
