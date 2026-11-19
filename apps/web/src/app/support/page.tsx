// 고객센터. 이용 질문(Q&A)과 도입 중 오류를 받는 창구를 안내한다. 가입 전 회사도 봐야 해서 로그인 없이 열린다
// (lib/return-to.ts `PUBLIC_PATHS`). 연락처는 lib/legal.ts `SUPPORT_CENTER` 하나에서 읽는다.
// 서비스 장애는 여기서 받지 않는다. 로그인 후 사이드바의 문의하기(components/support/inquiry-button.tsx)로 받는다.

import type { Metadata } from 'next';
import Link from 'next/link';

import { LegalDocument, LegalList, LegalTable, type LegalSection } from '@/components/legal/legal-document';
import { SERVICE_NAME, SUPPORT_CENTER } from '@/lib/legal';
import {
  GUARDDUTY_GUIDE_PATH,
  LOGIN_OVERLAY_HREF,
  ONBOARDING_GUIDE_PATH,
  USAGE_GUIDE_PATH,
} from '@/lib/return-to';
import { INQUIRY_CATEGORY_LABELS } from '@/lib/support';

export const metadata: Metadata = {
  title: `고객센터 · ${SERVICE_NAME}`,
  description: `${SERVICE_NAME} 이용 질문과 도입 중 오류를 받는 고객센터 연락처와 운영 시간을 안내합니다.`,
};

const LINK = 'text-primary hover:underline';
const OUTAGE_KINDS = [INQUIRY_CATEGORY_LABELS.OUTAGE.label, INQUIRY_CATEGORY_LABELS.BUG.label].join(' · ');

const SECTIONS: LegalSection[] = [
  {
    id: 'contact',
    title: '1. 연락처',
    body: (
      <LegalTable
        head={['구분', '내용']}
        rows={[
          [
            '이메일',
            <a key="email" href={`mailto:${SUPPORT_CENTER.email}`} className={LINK}>
              {SUPPORT_CENTER.email}
            </a>,
          ],
          ['운영 시간', SUPPORT_CENTER.hours],
        ]}
      />
    ),
  },
  {
    id: 'scope',
    title: '2. 고객센터에서 받는 문의',
    body: (
      <>
        <LegalList
          items={[
            `${SERVICE_NAME} 이용 방법에 대한 질문`,
            '도입 신청이나 AWS 계정 연결 중에 생긴 오류',
          ]}
        />
        <p>
          처음 쓰는 회사의 도입 신청은{' '}
          <Link href={LOGIN_OVERLAY_HREF} className={LINK}>
            로그인 창
          </Link>
          의 회원가입에서 합니다.
        </p>
      </>
    ),
  },
  {
    id: 'outage',
    title: '3. 서비스 장애',
    body: (
      <p>
        이미 이용 중인 고객사의 {OUTAGE_KINDS} 문의는 로그인한 뒤 왼쪽 메뉴 아래의 문의하기로 보냅니다. 운영팀이 문의를
        받아 처리합니다.
      </p>
    ),
  },
  {
    id: 'docs',
    title: '4. 함께 볼 문서',
    body: (
      <LegalList
        items={[
          <Link key="onboarding" href={ONBOARDING_GUIDE_PATH} className={LINK}>
            도입 가이드
          </Link>,
          <Link key="usage" href={USAGE_GUIDE_PATH} className={LINK}>
            사용 가이드
          </Link>,
          <Link key="guardduty" href={GUARDDUTY_GUIDE_PATH} className={LINK}>
            GuardDuty 켜기 안내
          </Link>,
        ]}
      />
    ),
  },
];

export default function SupportCenterPage() {
  return (
    <LegalDocument
      title="고객센터"
      dated={false}
      intro={<p>이용 질문과 도입 중 오류는 고객센터 이메일로 받습니다. 서비스 장애는 로그인 후 문의하기를 이용해 주세요.</p>}
      sections={SECTIONS}
    />
  );
}
