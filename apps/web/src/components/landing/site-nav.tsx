// 로그인 전 화면(랜딩 · 도입 가이드 · 약관 같은 공개 문서)의 상단 메뉴. 메뉴 이름에 마우스를 올리거나 키보드로
// 포커스하면 하위 메뉴가 열리고, 항목을 누르면 그 페이지로 간다. 여닫기는 CSS(group-hover · group-has-[:focus-visible])만
// 쓴다. 서버 컴포넌트로 그려 랜딩의 첫 화면에 스크립트를 더하지 않는다.
// 좁은 화면(< md)에서는 접는다. 같은 문서는 바닥글 링크로도 갈 수 있다.

import { ChevronDown } from 'lucide-react';
import Link from 'next/link';

import {
  GUARDDUTY_GUIDE_PATH,
  LANDING_PATH,
  ONBOARDING_GUIDE_PATH,
  PRIVACY_PATH,
  SUPPORT_CENTER_PATH,
  TERMS_PATH,
  USAGE_GUIDE_PATH,
} from '@/lib/return-to';

type MenuGroup = { label: string; items: { label: string; href: string }[] };

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

export function SiteNav({ className }: { className?: string }) {
  return (
    <nav aria-label="사이트 메뉴" className={className ?? 'hidden items-center gap-2 md:flex'}>
      {SITE_MENU.map((group) => (
        <div key={group.label} className="group relative">
          <button
            type="button"
            aria-haspopup="true"
            className="text-foreground-secondary group-hover:text-foreground group-has-[:focus-visible]:text-foreground focus-visible:ring-ring inline-flex h-10 items-center gap-1 rounded-md px-3 text-sm font-medium transition-colors focus-visible:ring-2 focus-visible:outline-none"
          >
            {group.label}
            <ChevronDown
              aria-hidden
              className="size-3.5 transition-transform group-has-[:focus-visible]:rotate-180 group-hover:rotate-180"
            />
          </button>
          {/* pt-2는 버튼과 메뉴 사이의 빈틈이다. 이 틈도 hover 영역에 들어가야 마우스를 내리는 동안 메뉴가 닫히지 않는다. */}
          <div className="invisible absolute top-full left-1/2 z-50 -translate-x-1/2 pt-2 opacity-0 transition-opacity group-has-[:focus-visible]:visible group-has-[:focus-visible]:opacity-100 group-hover:visible group-hover:opacity-100">
            {/* 테두리 · 그림자 없이 뒤 배경만 흐리게 해 글자를 띄운다. 옅은 바탕은 밝은 배경 위에서 글자가 묻히지 않게 하는 몫이다. */}
            <ul className="bg-background/25 flex min-w-44 flex-col rounded-xl p-1.5 backdrop-blur-xl">
              {group.items.map((item) => (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    className="text-foreground-secondary hover:text-foreground focus-visible:text-foreground flex h-9 items-center rounded-md px-3 text-sm whitespace-nowrap transition-colors focus-visible:outline-none"
                  >
                    {item.label}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ))}
    </nav>
  );
}
