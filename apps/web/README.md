# apps/web — Vigilantis 관제 대시보드

Next.js 16(App Router) + React 19 + TypeScript + Tailwind CSS v4 + Shadcn UI(radix-ui)로 만든 프런트엔드다. 스택 상향 배경은 [ADR-0003](../../docs/adr/0003-fe-stack-nextjs-16.md)에 있다.

> **실행 절차는 저장소 루트 [`README.md`](../../README.md) §로컬 실행 하나에만 둔다.** 여기에 복제하지 않는다 — 두 곳에 적히면 한쪽만 고쳐져 갈린다(이슈 #352).

## 먼저 알아야 할 것 — 백엔드가 떠 있어야 화면이 선다

**mock 계층은 없다**(2026-09-16 폐기 · PR #351). `apps/web/src/app/api/v1/**`의 mock Route Handler 6파일이 통째로 지워졌고, 화면은 실 `core-api`에만 붙는다. **`npm run dev`만 띄우면 모든 화면이 조회 오류다.**

* 조회 대상 오리진은 `NEXT_PUBLIC_API_BASE_URL`이며, 미설정이면 `http://localhost:8000`(compose의 `core-api`)으로 간다. 설정 예시는 [`.env.example`](.env.example) → `.env.local`로 복사해 쓴다.
* 백엔드의 `CORS_ALLOW_ORIGINS`(기본 `http://localhost:3000`)에 이 웹의 오리진이 들어 있어야 브라우저 조회와 WebSocket handshake가 통과한다(`routers/ws.py`의 Origin 검증).

## 디렉터리

```text
src/
├── app/                        # App Router
│   ├── page.tsx                #   메인 대시보드 (DSH-001)
│   ├── assets/                 #   자산 목록 — 상세는 별도 라우트가 아니라 목록 화면의 Sheet다
│   ├── incidents/              #   보안 인시던트 목록 (INC-001) · [id] 상세
│   ├── asset-incidents/        #   자산 인시던트 목록 (INC-004)
│   ├── layout.tsx              #   GNB · 실시간 연결 provider · 토스트
│   └── global-error.tsx
├── components/
│   ├── assets/                 #   자산 카드·상세·토폴로지 그래프
│   ├── dashboard/              #   대시보드 뷰·토폴로지·AI 조치 제안 카드
│   ├── incidents/              #   인시던트 카드·상세·실행 모달·종료 처리 모달
│   └── ui/                     #   shadcn 프리미티브 (badge·button·card·dialog 등)
├── lib/
│   ├── api/client.ts           #   계약 조회 클라이언트 — apiBaseUrl() · 오류 구분
│   ├── realtime-events.ts      #   WebSocket 이벤트 해석
│   ├── dashboard.ts            #   대시보드 집계
│   ├── asset-graph.ts          #   자산 연결관계 6종 → 그래프 배치
│   └── *.test.ts               #   `node --test` 유닛 (CI web 잡이 돌린다)
└── types/api.ts                # packages/schemas api/ DTO의 TypeScript 대응 — 계약이 바뀌면 함께 고친다
```

## 스크립트

| 명령 | 하는 일 |
| :--- | :--- |
| `npm run dev` | 개발 서버 (:3000) — **core-api가 떠 있어야 한다** |
| `npm run lint` | ESLint — CI `web` 잡과 같은 명령 |
| `npm run build` | `next build`. **tsc 타입 체크를 포함**하므로 타입 오류는 여기서 잡힌다 |
| `npm run test` | `node --test` — 인자 없이 재귀 탐색이라 `src/lib/**/*.test.ts` 전부 돈다(`lib/api/client.test.ts` 포함) |

푸시 전에 `lint` · `build` · `test` 셋을 로컬에서 돌린다. CI `web` 잡이 같은 순서로 검사하며, 세 잡(`test` · `web` · `ai-signature`) 전부 통과해야 머지된다.

## 계약이 바뀔 때

`packages/schemas/api/`가 FE↔BE 공개 계약의 원천이고 [`src/types/api.ts`](src/types/api.ts)가 그 TypeScript 대응이다. **한쪽만 고치면 빌드는 통과하고 런타임에서 깨진다** — 계약 변경 PR은 양쪽을 함께 고치고, 리뷰어도 양쪽 담당자를 지목한다(루트 [`CLAUDE.md`](../../CLAUDE.md) §리뷰 요청 대상).

## 참고

* [`README.md`](../../README.md) — 프로젝트 소개 · 로컬 실행 · API 계약 요약
* [`docs/PROJECT_STATUS.md`](../../docs/PROJECT_STATUS.md) — **SSOT.** 범위·확정 결정·역할
* [`docs/E2E_DEMO_SCENARIOS.md`](../../docs/E2E_DEMO_SCENARIOS.md) — 시연 대본의 원천(화면 선행 조건 포함)

> `AGENTS.md`는 `next dev`가 자동으로 쓰고 다시 붙이는 블록이다 — 손으로 지워도 재생성된다.
