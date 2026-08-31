# ADR-0003: FE 스택을 Next.js 16(React 19·Tailwind v4·shadcn 4)으로 상향한다

- **Status**: Accepted
- **Date**: 2026-08-13
- **Deciders**: 유건희(FE) 제안, 김세혁(PM/Infra) 승인

## Context (배경)

SSOT(`docs/PROJECT_STATUS.md`)는 FE 스택을 **Next.js 14(App Router)** 로 표기해 왔다. `apps/web` 실제 스캐폴딩(PR #30) 시점에 아래 제약을 확인했다(npm 레지스트리 검증: 2026-08-13):

- **next 14 라인은 `14.2.35`에서 동결**(dist-tag `next-14`). 현재 latest는 `16.3.0`.
- **shadcn CLI 4.x**(latest `4.17.0`)는 **Tailwind v4 / Next 15+ 기준** — 14 유지 시 컴포넌트 추가마다 수동 구성 부담 발생.
- 팀 로컬 Node 런타임(24–26)과 동결된 14 라인의 지원 범위 불일치(유건희 로컬 Node 26 기준 문제 확인).
- `apps/web`은 신규 생성이라 마이그레이션 비용이 없음 — "시작 시점에 어떤 안정판을 고르나"의 문제.

## Decision (결정)

**`apps/web` FE 스택을 Next.js `16.3.0`(App Router, Turbopack) + React 19 + TypeScript + Tailwind CSS v4 + shadcn UI(radix-nova, Lucide)로 확정한다.**

- SSOT·README의 "Next.js 14" 표기는 16으로 갱신한다(PR #30 동봉).
- 부수 정리: `shadcn`(CLI 도구)은 `devDependencies`로 이동, `@types/node`는 팀 최소 런타임(Node 24) 기준 `^24`로 정렬.

## Consequences (결과·트레이드오프)

**장점**

- 시작 시점 최신 안정판 채택으로 MVP 기간(10/15까지) 내 프레임워크 EOL·보안 패치 공백 리스크 제거
- shadcn CLI 표준 경로를 그대로 사용 — 컴포넌트 추가 비용 최소화
- Turbopack 기본화로 dev/build 속도 이점

**비용/유의**

- 팀 학습 자료·예제 코드가 Next 14/15 기준일 수 있음. App Router 개념은 동일하나 breaking change 존재 — `apps/web/AGENTS.md` 규칙대로 `node_modules/next/dist/docs/` 문서를 우선 참조
- React 19·Tailwind v4(CSS-first config) 신규 문법 — 컴포넌트는 shadcn 생성물 기준이라 영향 제한적
- **Recharts/Tremor 등 후속 차트 라이브러리 도입 시 React 19 호환성 확인 필요**(도입 PR에서 검증)

## Related

- 결정 반영: PR #30 (`feat/FE-nextjs-shadcn-scaffold`)
- 현황 기준: `docs/PROJECT_STATUS.md` 확정 결정 로그(2026-08-13)
- 선행 결정: [ADR-0001](0001-mvp-monorepo-structure.md)
