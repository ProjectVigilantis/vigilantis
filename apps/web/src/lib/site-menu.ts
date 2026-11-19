// 로그인 전 화면의 상단 메뉴(components/landing/site-nav.tsx)와 바닥글 링크(components/legal/legal-document.tsx)가
// 함께 쓰는 목록. 상단 메뉴는 클라이언트 컴포넌트라, 서버에서 그리는 바닥글이 읽을 수 있게 데이터를 여기 따로 둔다.

import {
  GUARDDUTY_GUIDE_PATH,
  LANDING_PATH,
  ONBOARDING_GUIDE_PATH,
  PRIVACY_PATH,
  SUPPORT_CENTER_PATH,
  TERMS_PATH,
  USAGE_GUIDE_PATH,
} from '@/lib/return-to';

export type MenuGroup = { label: string; items: { label: string; href: string }[] };

export const SITE_MENU: readonly MenuGroup[] = [
  {
    label: '제품',
    items: [
      { label: '동작 방식', href: `${LANDING_PATH}#how` },
      { label: '기능', href: `${LANDING_PATH}#features` },
      { label: '보안', href: `${LANDING_PATH}#trust` },
    ],
  },
  {
    label: '도입',
    items: [
      { label: '도입 가이드', href: ONBOARDING_GUIDE_PATH },
      { label: 'GuardDuty 켜기 안내', href: GUARDDUTY_GUIDE_PATH },
    ],
  },
  {
    label: '지원',
    items: [
      { label: '고객센터', href: SUPPORT_CENTER_PATH },
      { label: '사용 가이드', href: USAGE_GUIDE_PATH },
    ],
  },
  {
    label: '정책',
    items: [
      { label: '이용약관', href: TERMS_PATH },
      { label: '개인정보처리방침', href: PRIVACY_PATH },
    ],
  },
];

/** 바닥글에 싣는 안내 문서. 좁은 화면에서는 상단 메뉴가 접히므로 이 링크가 유일한 길이다. 정책 문서는 `LegalLinks`가 싣는다. */
export const FOOTER_GUIDE_LINKS = SITE_MENU.filter((g) => g.label === '도입' || g.label === '지원').flatMap(
  (g) => g.items,
);
