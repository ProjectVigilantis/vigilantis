---
version: alpha
name: Vigilantis 관제 블루
description: "24시간 클라우드 관제 화면. 차가운 회백색 작업 영역을 남색 셸이 감싸고, 조작과 데이터는 파랑 한 계열, 상태 색은 상태 표시에만 쓴다."
colors:
  background: "#f5f7fa"
  surface: "#eef1f5"
  surface-elevated: "#ffffff"
  on-background: "#0f1b2d"
  on-surface: "#0f1b2d"
  on-surface-secondary: "#3d4a5c"
  on-surface-muted: "#5b6878"
  outline: "#dde3ea"
  gridline: "#e8ecf1"
  axis: "#c5cdd8"
  primary: "#1d5bd8"
  primary-soft: "#e7effd"
  on-primary: "#ffffff"
  on-primary-soft: "#1747a6"
  focus: "#3b82f6"
  shell-base: "#0b1526"
  shell-nav: "#0f1d33"
  on-shell: "#ffffff"
  live: "#22c55e"
  data: "#2a78d6"
  data-track: "#eef1f5"
  data-ramp-1: "#9ec5f4"
  data-ramp-2: "#6da7ec"
  data-ramp-3: "#3987e5"
  data-ramp-4: "#2a78d6"
  data-ramp-5: "#256abf"
  data-ramp-6: "#1c5cab"
  data-ramp-7: "#104281"
  heat-ok: "#9fdcaa"
  heat-none: "#e8ecf1"
  status-good: "#0ca30c"
  status-warning: "#fab219"
  status-serious: "#ec835a"
  status-critical: "#d03b3b"
  status-idle: "#94a3b8"
  badge-good-bg: "#e3f6e5"
  badge-good-fg: "#0b6b1f"
  badge-warning-bg: "#fff4d6"
  badge-warning-fg: "#7a4d00"
  badge-serious-bg: "#ffe8dc"
  badge-serious-fg: "#9a3a12"
  badge-critical-bg: "#fde4e4"
  badge-critical-fg: "#b42323"
  badge-idle-bg: "#eef1f5"
  badge-idle-fg: "#3d4a5c"
  danger-action: "#b42323"
  dark-background: "#0a1220"
  dark-surface: "#17233a"
  dark-surface-elevated: "#111b2c"
  dark-on-surface: "#e6ebf2"
  dark-on-surface-secondary: "#c3cddb"
  dark-on-surface-muted: "#9aa8bb"
  dark-outline: "#22304a"
  dark-gridline: "#1d2a40"
  dark-axis: "#33415a"
  dark-primary: "#2563eb"
  dark-primary-soft: "#15274a"
  dark-on-primary-soft: "#7cb0ff"
  dark-shell-base: "#060c17"
  dark-shell-nav: "#0b1424"
  dark-data: "#3987e5"
  dark-data-track: "#17233a"
  dark-heat-ok: "#1f6b3a"
  dark-heat-none: "#1a263b"
  dark-status-idle: "#5b6b85"
  dark-badge-good-bg: "#0f2e1a"
  dark-badge-good-fg: "#6fdc8c"
  dark-badge-warning-bg: "#33270a"
  dark-badge-warning-fg: "#f5c451"
  dark-badge-serious-bg: "#3a1f12"
  dark-badge-serious-fg: "#f4a37f"
  dark-badge-critical-bg: "#3a1414"
  dark-badge-critical-fg: "#f48a8a"
  dark-badge-idle-bg: "#1d2a40"
  dark-badge-idle-fg: "#c3cddb"
typography:
  headline-lg:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif"
    fontSize: "1.5rem"
    fontWeight: 700
    lineHeight: "2rem"
    letterSpacing: "-0.01em"
  stat-value:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif"
    fontSize: "1.875rem"
    fontWeight: 700
    lineHeight: "2.375rem"
  title-md:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif"
    fontSize: "1rem"
    fontWeight: 700
    lineHeight: "1.5rem"
  body-md:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: "1.25rem"
  body-sm:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: "1.125rem"
  nav:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 700
    lineHeight: "1rem"
  label-sm:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif"
    fontSize: "0.75rem"
    fontWeight: 600
    lineHeight: "1rem"
    letterSpacing: "0.04em"
  axis:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif"
    fontSize: "0.6875rem"
    fontWeight: 400
    lineHeight: "1rem"
  code-sm:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: "1.25rem"
rounded:
  xs: "3px"
  data: "4px"
  sm: "6px"
  md: "8px"
  lg: "12px"
  pill: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  "2xl": "32px"
  "3xl": "40px"
  "4xl": "64px"
  container-padding: "32px"
layout:
  container-max: "1400px"
  shell-header-height: "64px"
  shell-sidebar-width: "232px"
motion:
  fast: "150ms"
  normal: "200ms"
  slow: "300ms"
  easing-standard: "cubic-bezier(0.4, 0, 0.2, 1)"
shadows:
  dialog: "0 20px 25px rgba(15, 27, 45, 0.12), 0 8px 10px rgba(15, 27, 45, 0.06)"
  popover: "0 4px 12px rgba(15, 27, 45, 0.12)"
components:
  shell-header:
    backgroundColor: "{colors.shell-base}"
    textColor: "{colors.on-shell}"
    height: "{layout.shell-header-height}"
    padding: "0 16px"
  shell-sidebar:
    backgroundColor: "{colors.shell-nav}"
    textColor: "{colors.on-shell}"
    width: "{layout.shell-sidebar-width}"
    padding: "{spacing.lg}"
  nav-item:
    backgroundColor: "transparent"
    textColor: "rgba(255, 255, 255, 0.78)"
    typography: "{typography.nav}"
    rounded: "{rounded.md}"
    height: "40px"
    padding: "0 12px"
  nav-item-active:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.nav}"
    rounded: "{rounded.md}"
    height: "40px"
    padding: "0 12px"
  live-indicator:
    backgroundColor: "rgba(255, 255, 255, 0.08)"
    textColor: "rgba(255, 255, 255, 0.88)"
    typography: "{typography.body-sm}"
    rounded: "{rounded.pill}"
    height: "32px"
    padding: "0 12px"
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body-md}"
    rounded: "{rounded.sm}"
    height: "40px"
    padding: "0 16px"
  button-outline:
    backgroundColor: "transparent"
    textColor: "{colors.on-surface}"
    borderColor: "{colors.outline}"
    typography: "{typography.body-md}"
    rounded: "{rounded.sm}"
    height: "36px"
    padding: "0 14px"
  button-danger:
    backgroundColor: "{colors.danger-action}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body-md}"
    rounded: "{rounded.sm}"
    height: "36px"
    padding: "0 14px"
  filter-chip-active:
    backgroundColor: "{colors.primary-soft}"
    textColor: "{colors.on-primary-soft}"
    borderColor: "{colors.primary}"
    rounded: "{rounded.pill}"
    height: "32px"
    padding: "0 12px"
  card-panel:
    backgroundColor: "{colors.surface-elevated}"
    textColor: "{colors.on-surface}"
    borderColor: "{colors.outline}"
    rounded: "{rounded.lg}"
    padding: "{spacing.xl}"
  stat-tile:
    backgroundColor: "{colors.surface-elevated}"
    textColor: "{colors.on-surface}"
    borderColor: "{colors.outline}"
    typography: "{typography.stat-value}"
    rounded: "{rounded.lg}"
    padding: "18px 20px"
  table-header:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.on-surface-secondary}"
    typography: "{typography.label-sm}"
    height: "44px"
    padding: "0 16px"
  table-cell:
    backgroundColor: "transparent"
    textColor: "{colors.on-surface}"
    typography: "{typography.body-md}"
    height: "52px"
    padding: "0 16px"
  status-badge:
    typography: "{typography.label-sm}"
    rounded: "{rounded.pill}"
    padding: "3px 10px"
  chart-bar:
    backgroundColor: "{colors.data}"
    trackColor: "{colors.data-track}"
    rounded: "{rounded.data}"
  chart-line:
    strokeColor: "{colors.data}"
    strokeWidth: "2px"
    areaFillOpacity: "0.14"
  heat-cell:
    height: "22px"
    rounded: "{rounded.xs}"
    gap: "2px"
---

## 개요

Vigilantis는 고객 AWS 계정을 하루 24시간 지켜보는 관제 화면이다. 운영자가 화면에서 얻어야 하는 것은 두 가지다. 지금 문제가 있는가, 그리고 그 판단의 근거가 얼마나 새것인가.

화면은 두 층으로 나뉜다. 바깥은 남색 셸(상단바와 사이드바)이고, 안쪽은 차가운 회백색 작업 영역이다. 셸은 화면의 틀을 잡고, 작업 영역은 숫자와 표와 그래프를 읽는 자리다. 기본은 주간(밝은) 모드이고, 야간 근무와 관제실 벽면 화면을 위한 야간 모드를 함께 둔다.

이 체계는 Evreghen Command Center(아래 출처)에서 출발했다. 어두운 셸과 밝은 작업 영역의 두 층 구조, 시스템 글꼴, 4px 간격 체계는 원작에서 가져왔다. 주황 강조색, 크림색 바탕, 반투명 셸은 관제 목적에 맞지 않아 바꿨다.

## 색

색은 세 가족이며, 가족끼리 섞지 않는다.

| 가족 | 쓰는 곳 | 쓰지 않는 곳 |
| --- | --- | --- |
| 바탕(차가운 회색과 남색 글자) | 페이지, 카드, 표, 글자, 선 | 상태 표시 |
| 파랑(`primary`, `data`) | 버튼, 선택된 메뉴와 필터, 링크, 포커스, 그래프의 선과 막대 | 상태 표시 |
| 상태 색(`status-*`) | 상태 배지, 상태 점, 히트맵 칸, 경고 배너 | 버튼, 메뉴, 장식 |

주황과 빨강을 강조색으로 쓰지 않는 이유는 관제 화면에서 그 색이 이미 경고를 뜻하기 때문이다. 버튼과 메뉴가 주황이면 진짜 경고가 화면에 묻힌다. 상태 색이 상태 표시에만 남아 있어야, 화면에 빨강이 보일 때 그것이 실제 문제라는 뜻이 된다.

### 상태 색 다섯 단계

| 단계 | 토큰 | 뜻 | Vigilantis 예 |
| --- | --- | --- | --- |
| 정상 | `status-good` | 기대한 대로 돌고 있다 | 관제 중, 연결 확인됨, 수집 성공 |
| 대기 · 확인 불가 | `status-warning` | 아직 판단할 수 없다 | 연결 대기, 확인 불가(AWS 응답 없음) |
| 경고 | `status-serious` | 사람이 손봐야 하지만 멈추지는 않았다 | 읽기 권한 없음 |
| 위험 · 실패 | `status-critical` | 실패했거나 접근이 막혔다 | 역할 접근 거절, 계정 불일치, 수집 실패 |
| 중립 · 정지 | `status-idle` | 의도적으로 꺼 두었다 | 정지(원격 회수), 수집 안 함 |

정지는 관리자가 일부러 내린 결정이라 빨강이 아니라 중립 회색이다. 빨강은 실패에만 쓴다. 새 상태값이 생기면 이 다섯 단계 중 하나에 대응시키고, 여섯 번째 색을 만들지 않는다.

상태 색은 항상 글자와 함께 쓴다. 배지에는 상태 이름을, 점과 히트맵 칸에는 범례와 마우스를 올렸을 때 보이는 설명을 붙인다. 색만으로 뜻을 전하지 않는다.

배지는 옅은 바탕(`badge-*-bg`)에 진한 글자(`badge-*-fg`)를 쓴다. 상태 원색(`status-*`)은 글자 대비가 낮은 것이 있어(노랑 등) 넓은 칠이나 글자 바탕에 쓰지 않는다.

### 대비

글자 조합은 주간과 야간 모두 WCAG AA(본문 4.5:1)를 넘긴 값만 쓴다. 측정한 대표 값은 아래와 같다.

| 조합 | 대비 |
| --- | --- |
| 흰 글자 / `primary` `#1d5bd8` | 5.93:1 |
| `on-surface-muted` `#5b6878` / `background` | 5.29:1 |
| `on-primary-soft` / `primary-soft` | 7.30:1 |
| 배지 글자 / 배지 바탕(주간 다섯 쌍) | 5.43:1 이상 |
| `dark-on-surface-muted` / `dark-surface-elevated` | 7.14:1 |
| 배지 글자 / 배지 바탕(야간 다섯 쌍) | 6.86:1 이상 |

색을 새로 고를 때도 대비를 측정한 뒤 넣는다. 눈으로 판단하지 않는다.

## 야간 모드

야간 모드는 주간 색을 뒤집은 것이 아니라, 같은 역할에 어두운 바탕용 값을 따로 고른 것이다(`dark-*` 토큰). 상태 원색 네 가지(`status-good`, `status-warning`, `status-serious`, `status-critical`)는 두 모드에서 같고, 배지 바탕과 글자, 중립 회색, 히트맵 칸, 데이터 파랑은 모드마다 다르다.

모드 전환은 상단바의 버튼으로 한다. 관제실 벽면 화면처럼 오래 켜 두는 화면은 야간 모드를 기본으로 띄울 수 있다.

## 글꼴

시스템 산세리프를 쓴다. 웹폰트를 받지 않으므로 빨리 뜨고, 한글은 운영체제 글꼴(Apple SD Gothic Neo, 맑은 고딕)로 나온다. 계정 ID, ARN, 리전, 오류 코드처럼 철자가 중요한 값은 고정폭(`code-sm`)으로 쓴다.

| 자리 | 토큰 |
| --- | --- |
| 화면 제목 | `headline-lg` 24px 굵게 |
| 숫자 카드 값 | `stat-value` 30px 굵게 |
| 카드 제목 | `title-md` 16px 굵게 |
| 본문, 표 | `body-md` 14px |
| 카드 설명, 보조 글 | `body-sm` 13px |
| 숫자 카드 이름, 표 머리 | `label-sm` 12px |
| 그래프 축 | `axis` 11px |

표에서 위아래로 줄을 맞춰야 하는 숫자 열에는 `tabular-nums`를 준다.

## 레이아웃과 간격

4px 단위를 쓴다. 상단바는 64px, 사이드바는 232px, 작업 영역은 최대 1400px에 좌우 32px 여백이다.

화면 안의 구역 사이는 24px, 카드끼리는 16px, 카드 안쪽 여백은 24px이다. 숫자 카드 줄은 `repeat(auto-fit, minmax(170px, 1fr))` 격자로 두어 화면 폭에 따라 줄이 바뀌게 한다.

좁은 화면에서는 사이드바가 위로 올라가고, 넓은 표와 히트맵은 가로 스크롤 상자 안에 둔다. 페이지 전체가 가로로 넘치지 않게 한다.

화면 순서는 위에서부터 "숫자 카드 → 추이 그래프 → 상태 그래프 → 목록 표"다. 운영자는 위에서 문제가 있는지 보고, 아래로 내려가며 어디인지 찾는다.

## 깊이

카드는 그림자 없이 1px 테두리(`outline`)로 구분한다. 그림자는 대화 상자와 떠 있는 메뉴에만 쓴다. 셸은 원작의 반투명 유리 효과 대신 불투명한 남색이다. 반투명 셸은 뒤가 밝으면 탁한 회색으로 보이고, 글자 대비가 배경에 따라 흔들린다.

## 모양

버튼과 입력은 6px, 메뉴 항목은 8px, 카드는 12px, 배지와 필터 칩은 완전히 둥글게 한다. 그래프 막대는 값 쪽 끝만 4px 둥글게 하고 기준선 쪽은 각지게 둔다. 히트맵 칸은 3px이다.

## 컴포넌트

### 셸

상단바(`shell-header`)에는 왼쪽에 제품 이름과 "플랫폼 운영" 같은 영역 이름을, 오른쪽에 실시간 연결 표시, 주간·야간 전환, 계정 메뉴를 둔다. 실시간 표시(`live-indicator`)는 초록 점과 함께 마지막 갱신 시각을 초 단위까지 보여 준다. 운영자가 화면의 숫자가 얼마나 새것인지 늘 알 수 있어야 한다.

사이드바(`shell-sidebar`)의 선택된 메뉴만 파랑으로 칠한다.

### 버튼

주 동작은 파랑 칠 버튼(`button-primary`) 하나만 둔다. 보조 동작은 테두리 버튼(`button-outline`)이다. 되돌릴 수 없는 동작(고객 정지, 삭제)은 빨강 버튼(`button-danger`)이며, 화면 아래 "위험 작업" 카드 안에 따로 모으고 확인 대화 상자를 거친다.

### 숫자 카드

이름(`label-sm`), 값(`stat-value`), 한 줄 설명(`body-sm`)의 세 줄이다. 설명에는 값이 무엇의 합계인지 또는 어떻게 나뉘는지를 적는다(예: "관제 중 3 · 대기 1 · 정지 1"). 값은 기본 글자색이고, 문제가 있는 값만 해당 상태 배지 글자색으로 칠한다.

### 표

머리는 옅은 면(`surface`)에 보조 글자색이다. 이름 칸은 상세 화면으로 가는 링크다. 상태는 배지로, 연결 확인 결과는 상태 글자색과 시각으로 쓴다. 표 위에 상태 필터 칩을 두고, 칩에는 건수를 함께 적는다.

### 경고 배너

상세 화면 맨 위에서, 그 고객에게 지금 손봐야 할 것이 있으면 한 줄 결론과 다음 할 일을 보여 준다. 배지와 같은 상태 바탕과 글자색, 같은 계열 테두리를 쓴다.

## 데이터 시각화

그래프는 색보다 형태를 먼저 정한다.

| 보여 줄 것 | 형태 |
| --- | --- |
| 지금 값 하나 | 숫자 카드(그래프 아님) |
| 시간에 따른 변화 | 선 그래프, 한 계열이면 아래를 옅게 채운다 |
| 항목끼리 크기 비교 | 가로 막대(긴 이름) 또는 세로 막대(날짜) |
| 전체 중 비율 | 한 줄 누적 막대와 건수 범례 |
| 시간대별 상태 | 히트맵(1칸 = 1시간), 상태 색 사용 |
| 마지막 확인 후 지난 시간 | 점 그림과 기준선 |

지킬 것:

- 크기를 나타내는 그래프는 파랑 한 계열(`data`)만 쓴다. 값이 클수록 진하게 하려면 `data-ramp-1`부터 `data-ramp-7`까지를 쓴다. 무지개 색을 쓰지 않는다.
- 상태 색은 히트맵 칸이나 상태 점처럼 값이 상태일 때만 쓴다. 크기 그래프에 상태 색을 쓰지 않는다.
- 세로축은 하나만 둔다. 단위가 다른 두 값은 그래프를 나눈다.
- 계열이 둘 이상이면 범례를 둔다. 한 계열이면 제목이 계열 이름을 대신한다.
- 모든 그래프 제목 아래에 범위와 단위를 적는다(예: "전체 고객 합계 · 시간당 건수"). 합계가 있으면 제목 오른쪽에 쓴다.
- 막대와 칸, 점에는 마우스를 올리면 시각과 값이 보이게 한다. 선 그래프는 시간 칸마다 같은 설명을 붙인다.
- 가장 큰 값 같은 핵심 한 곳에만 직접 값을 적는다. 모든 점에 숫자를 달지 않는다.
- 값이 없는 칸(수집 안 함, 연결 전)은 바탕보다 한 단계 진한 회색(`heat-none`)으로, 0과 구별한다.
- 격자선은 `gridline`, 기준선은 `axis`로 흐리게 그린다. 축 글자는 `axis` 글꼴과 흐린 글자색이다.
- 시각은 모두 KST이며, 화면 머리에 한 번 밝힌다.

## 지킬 것과 피할 것

지킬 것

- 파랑은 조작과 데이터에, 상태 색은 상태에만 쓴다.
- 상태는 색과 글자를 함께 보여 준다.
- 화면의 숫자가 언제 것인지(갱신 시각, 마지막 확인 시각)를 늘 함께 보여 준다.
- 새 색은 대비를 측정한 뒤 넣고, 주간과 야간 값을 둘 다 정한다.
- 카드는 조용하게 두어 그래프와 숫자가 먼저 보이게 한다.

피할 것

- 주황이나 빨강을 버튼, 메뉴, 장식에 쓰기.
- 정지처럼 의도된 상태를 빨강으로 칠하기.
- 상태 단계를 여섯 개 이상으로 늘리기.
- 카드마다 그림자를 넣거나 배경을 색으로 칠하기.
- 두 개의 세로축, 무지개 색 계열, 모든 점에 숫자 달기.
- 장식용 웹폰트로 바꾸기.

## 출처

- 원본: [Evreghen Command Center](https://designmd.ai/frknaykc/command-center) · 작성자 @frknaykc · v1 · MIT License
- 2026-10-20 designmd.ai의 Raw 원문을 옮겨 온 뒤, 같은 날 관제 목적에 맞춰 색(주황 강조 → 파랑, 크림 바탕 → 차가운 회백색, 반투명 셸 → 불투명 남색), 상태 색 체계, 야간 모드, 데이터 시각화 규칙을 고쳐 썼다. 두 층 구조, 시스템 글꼴, 간격 체계는 원작을 따른다.
- 이 파일이 웹 FE의 유일한 디자인 원칙이다. 바꿀 값은 이 파일을 직접 고친다. 원문은 위 링크에 있다.
