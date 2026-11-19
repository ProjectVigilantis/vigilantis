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

const PERMISSIONS: [string, string][] = [
  ['자산 · 지표 조회', 'EC2 · Auto Scaling · 로드 밸런서의 구성 조회와 CloudWatch 지표 조회'],
  ['비용 조회', 'Cost Explorer의 사용 요금 조회'],
  ['위협 탐지', 'GuardDuty 탐지기 조회 · 생성과 탐지 결과 조회. 끄거나 지우는 권한은 없습니다.'],
  [
    '조치 실행',
    '허용된 런북에 필요한 작업만 엽니다. 인스턴스 중지 · 시작 · 유형 변경, 보안 그룹 · 네트워크 ACL 규칙 변경, EBS 스냅샷, 대상 그룹 등록 · 해제, 이미지 · 시작 템플릿 생성 등입니다.',
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
          조치가 새로 만드는 이미지 · 시작 템플릿 · Auto Scaling 그룹은 {SERVICE_NAME} 실행 태그가 붙어야만 만들어지도록
          권한이 묶여 있습니다. 역할을 다른 서비스에 넘기는 권한(iam:PassRole)은 받지 않습니다. 모든 조치는 입력 형식 ·
          허용 런북 · 대상 자원 일치 · AWS 사전 실행(Dry-Run)의 4단계 검증을 거친 뒤에만 실행됩니다.
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
          {SERVICE_NAME}는 Amazon GuardDuty가 찾아낸 위협을 받아 관제합니다. 연결을 확인할 때 관제 리전에 GuardDuty가
          꺼져 있으면 기본 위협 탐지만 켭니다. 직접 켜야 하는 경우는{' '}
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
