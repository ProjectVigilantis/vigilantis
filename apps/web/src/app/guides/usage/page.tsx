// 사용 가이드. 고객사 계정이 쓰는 메뉴를 메뉴 순서대로 안내한다. 로그인 없이 열린다(lib/return-to.ts `PUBLIC_PATHS`).
//
// 공개 문서라 고객사 계정 메뉴만 싣는다(lib/home-path.ts `navPathsFor`의 고객사 분기). 플랫폼 계정 메뉴(고객사 ·
// 도입 신청 · 고객사 문의 · 직원 계정)는 내부 운영 화면이라 넣지 않는다.
// 지금은 절 구조와 한두 문장만 있다(2026-11-19). 화면 구성이 확정되면 절마다 화면 캡처와 순서를 채운다.
// 사실은 각 화면 파일의 머리 주석과 lib/roles.ts · lib/user-admin.ts에서 확인한 것만 적는다.

import type { Metadata } from 'next';

import { LegalDocument, LegalList, type LegalSection } from '@/components/legal/legal-document';
import { SERVICE_NAME } from '@/lib/legal';
import { ACCOUNT_KIND_LABELS } from '@/lib/roles';
import { ROLE_LABELS } from '@/lib/user-admin';

export const metadata: Metadata = {
  title: `사용 가이드 · ${SERVICE_NAME}`,
  description: `${SERVICE_NAME} 고객사 계정의 메뉴별 기능을 안내합니다.`,
};

const ADMIN = ACCOUNT_KIND_LABELS.TENANT_ADMIN;
const STAFF = ACCOUNT_KIND_LABELS.TENANT_STAFF;

const SECTIONS: LegalSection[] = [
  {
    id: 'start',
    title: '1. 시작하기',
    body: (
      <LegalList
        items={[
          '로그인은 이메일 · 비밀번호와 인증 앱의 2단계 인증으로 합니다.',
          `계정은 ${ADMIN}과, ${ADMIN}이 초대하는 ${STAFF}으로 나뉩니다.`,
          `${STAFF}의 역할은 ${ROLE_LABELS.OPERATOR} · ${ROLE_LABELS.VIEWER} 중 하나이며, 역할에 따라 누를 수 있는 버튼이 다릅니다.`,
        ]}
      />
    ),
  },
  {
    id: 'dashboard',
    title: '2. 대시보드',
    body: <p>로그인하면 처음 보이는 화면입니다. 우리 회사 계정의 자산과 인시던트 현황을 모아 보여 줍니다.</p>,
  },
  {
    id: 'monitoring',
    title: '3. 관제',
    body: (
      <LegalList
        items={[
          '자산과 보안 두 축으로 나뉩니다. 자산은 자원 목록과 비용 최적화를, 보안은 그 자원의 노출과 위협을 봅니다.',
          '위쪽 지표 띠에서 자원 유형을 고르고, 아래 목록 · 요약 · 토폴로지 · 인시던트 탭으로 내용을 봅니다.',
        ]}
      />
    ),
  },
  {
    id: 'incidents',
    title: '4. 인시던트와 조치',
    body: (
      <LegalList
        items={[
          '인시던트 상세에서 원인 요약과 추천 조치를 확인합니다.',
          `조치 실행은 ${ADMIN}과 ${ROLE_LABELS.OPERATOR} 역할 직원이 승인합니다. ${ROLE_LABELS.VIEWER} 역할은 볼 수만 있습니다.`,
          '실행 뒤 상태 확인에서 문제가 나오면 실행 전 상태로 자동 원복합니다. AWS 상태를 조회하지 못하면 원복하지 않고 "결과 확인 불가"로 끝나므로, 그때는 자원 상태를 직접 확인합니다.',
        ]}
      />
    ),
  },
  {
    id: 'logs',
    title: '5. 로그',
    body: <p>하루 · 일주일 · 한 달 기간으로 감사 기록, 위협 이벤트, 인시던트 발생을 한 목록에서 봅니다.</p>,
  },
  {
    id: 'users',
    title: '6. 계정',
    body: <p>{ADMIN}에만 보이는 메뉴입니다. 직원을 초대하고 역할을 정합니다.</p>,
  },
  {
    id: 'settings',
    title: '7. 고객사 설정',
    body: <p>모든 역할이 볼 수 있고, 바꾸는 것은 {ADMIN}만 할 수 있습니다.</p>,
  },
  {
    id: 'subscription',
    title: '8. 구독',
    body: <p>모든 역할이 구독 상태를 볼 수 있고, 구독을 바꾸는 것은 {ADMIN}만 할 수 있습니다.</p>,
  },
  {
    id: 'support',
    title: '9. 문의하기와 화면 모드',
    body: <p>왼쪽 메뉴 아래의 문의하기로 운영팀에 문의를 보냅니다. 설정에서 주간 · 야간 화면 모드를 바꿉니다.</p>,
  },
];

export default function UsageGuidePage() {
  return (
    <LegalDocument
      title="사용 가이드"
      dated={false}
      intro={<p>{SERVICE_NAME} 고객사 계정의 메뉴별 기능을 메뉴 순서대로 안내합니다.</p>}
      sections={SECTIONS}
    />
  );
}
