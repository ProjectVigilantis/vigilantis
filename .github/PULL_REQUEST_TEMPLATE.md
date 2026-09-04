<!--
제목 형식: <gitmoji> [TYPE] #이슈번호 - 한 줄 설명   (예: ✨ [FEAT] #7 - EC2/SG 자산 조회 API 구현)
  TYPE ∈ FEAT · FIX · REFACTOR · CHORE · DOCS / gitmoji 목록: https://gitmoji.dev
대상 브랜치는 항상 dev (main 직접 PR 금지). 전체 규칙은 CLAUDE.md §Pull Request(PR) 규칙.

체크박스 표기 규칙 (중요)
  [x] = 확인 완료  |  [x] + "— (해당 없음) 사유" = 이 PR과 무관해 확인 불필요
  [ ] = 아직 확인하지 못함 (리뷰어가 보류로 읽는다)
  → 해당 없는 항목을 [ ]로 두지 말 것. 반드시 체크하고 사유를 적는다.
-->

## 개요

<이 PR이 무엇을, 왜 바꾸는지 2–3줄>

**리뷰 요청**: @<핸들> — <담당 접점, 이 PR에서 봐줘야 할 지점>
<!-- 접점이 둘 이상이면 줄을 추가한다.
     담당 매핑의 원천은 docs/PROJECT_STATUS.md §팀 & 역할의 `담당 DOMAIN` 열과 `복수 담당 접점` 표다.
     고르는 절차는 CLAUDE.md §리뷰 요청 대상. -->

## 변경 사항

- <핵심 변경 1>
- <핵심 변경 2>

## 테스트

- [ ] `pytest` 통과
- [ ] `docker-compose up`으로 로컬 기동 확인
- [x] (`apps/web` 변경 시) `npm run lint` · `npm run build` · `npm test` 통과 — (해당 없음) <사유>
- [x] (API 변경 시) FE↔BE 계약/Mock 영향 확인 — (해당 없음) <사유>

<!-- docs/PROJECT_STATUS.md(SSOT)는 이 PR에서 갱신하지 않는다.
     SSOT는 주 2회(월요일 아침·목요일 회의 뒤) PM이 갱신하며, 그 밖의 주중 갱신은 범위·API 계약·역할·확정 결정이 걸린
     큰 변경에 한한 예외로 별도 PR로 올린다. 규칙은 CLAUDE.md §갱신 주체와 시점. -->

## 관련 이슈

Refs #<이슈번호>
<!-- Closes·Fixes·Resolves 등 자동 CLOSE 키워드 금지 (기본 브랜치가 dev라 머지 즉시 닫힌다).
     이슈는 머지 후 머지 책임자가 직접 판단해 수동으로 CLOSE한다 — 머지 승인 코멘트 끝의 Claude CLOSE 추천이 판단 근거다.
     전체 규칙은 CLAUDE.md §Git 작업 흐름·§Pull Request(PR) 규칙. -->
