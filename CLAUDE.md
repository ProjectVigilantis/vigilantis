## 출력 스타일 — 상시 Caveman 모드 (항상 적용)

이 저장소에서 Claude의 모든 응답은 아래 규칙을 기본으로 따른다(`.claude/skills/caveman` 규칙 상시 적용). 사용자가 "자세히"·"풀어서" 등을 명시하면 그 응답만 예외로 완화한다.

1. **인사·서두 금지**: "좋은 질문", "물론이죠", "~해드리겠습니다" 생략. 답/코드부터 시작.
2. **간결체**: 짧은 구·불릿·키워드 중심. 군더더기 수식어 제거.
3. **불필요한 설명 금지**: 비자명한 로직만 설명. 코드 줄별 요약 금지.
4. **코드 우선**: 동작하는 코드 스니펫 제시. 변경 없는 부분은 `# ... existing code ...`로 생략.
5. **맺음말 금지**: "도움이 됐길", "필요하면 알려주세요" 생략.

단, **정확성·안전(파괴적 작업 확인 등)·핵심 경고**는 간결하더라도 반드시 유지한다.

---

## 프로젝트 현황 기준 문서 (SSOT)

- 프로젝트 범위·확정 결정·역할·미해결 이슈는 **`docs/PROJECT_STATUS.md`가 단일 기준**이다. 작업 전 이 문서를 먼저 확인하고, 다른 문서(README·기획서·MVP 범위 명세 등)와 충돌하면 `PROJECT_STATUS.md`를 따른다.
- 범위·API 계약·역할이 바뀌는 변경은 `docs/PROJECT_STATUS.md` 갱신을 함께 포함한다.

---

## Git 작업 흐름

- 저장소를 수정하거나 원격 최신성이 필요한 작업은 먼저 최신 `origin/main`과 `origin/dev`를 확인한다.
- `main`은 배포·안정 기준, `dev`는 기능 통합 및 작업 브랜치의 기준이다. `main` 확인이 `main`에서 기능 브랜치를 만든다는 의미는 아니다.
- 기능 작업은 최신 `origin/dev`에서 아래 **브랜치명 규칙**에 맞는 브랜치를 생성한다. README의 브랜치명은 예시이며 기존 브랜치의 존재나 재사용을 전제로 하지 않는다.
- 기능 PR은 `dev`를 대상으로 하며 `main`과 `dev`에 직접 커밋하거나 푸시하지 않는다.
- `main`과 `dev`의 관계에 이상이 있으면 원격을 임의로 갱신하지 않고 상태와 영향을 먼저 보고한다.
- 스테이징, 커밋, 푸시와 PR 생성은 변경 범위, 검증 결과와 실제 사용할 커밋·PR 제목을 사용자에게 확인한 뒤 진행한다.
- 커밋·PR·브랜치의 한글 설명은 한국어로 작성하며, 기술 용어·코드 식별자·파일명·영문 요약은 원문 표기를 유지한다.

---

## 공통 규격: TYPE / DOMAIN

브랜치·커밋·PR·이슈에서 공통으로 쓰는 두 축이다. TYPE은 "변경의 성격", DOMAIN은 "작업 영역"이며 서로 독립적이다(예: 문서 도메인이라도 신규 문서면 TYPE은 `FEAT`가 될 수 있다).

### TYPE (변경 성격)

| TYPE | 용도 |
| --- | --- |
| `FEAT` | 새로운 기능 추가 |
| `FIX` | 버그 수정 |
| `REFACTOR` | 동작 변경 없는 코드 개선 |
| `CHORE` | 빌드·패키지·CI/CD·설정 등 부수 작업 |
| `DOCS` | 문서 수정(README, ADR 등) |

- 브랜치명에서는 **소문자**(`feat`, `fix`, `refactor`, `chore`, `docs`)로 쓴다.
- 커밋·PR 제목에서는 **대문자 대괄호**(`[FEAT]` 등)로 쓴다.

### DOMAIN (작업 영역 코드)

모노레포 구조 기준. 하나의 작업이 여러 영역에 걸치면 **주된 영역** 하나를 고른다.

| DOMAIN | 범위 | 주요 경로 |
| --- | --- | --- |
| `FE` | 프론트엔드 대시보드 | `apps/web` |
| `BE` | 코어 백엔드 API | `apps/core-api` |
| `AI` | AI 엔진·4단계 가드레일 | `apps/ai-engine` |
| `DATA` | 자산/메트릭 수집·Rule Engine | `apps/scan-worker` |
| `SEC` | 보안 위협 대응(SOAR) | `apps/security-soar` |
| `SCHEMA` | 공통 Pydantic 스키마 | `packages/schemas` |
| `INFRA` | Docker·IaC·CI/CD·배포 | `packages/iac`, `docker-compose.yml`, `.github` |
| `DOCS` | 문서·ADR | `docs` |

---

## 브랜치명 규칙

- 형식: `<type>/<DOMAIN>-<이슈번호>-<english-kebab-summary>`
  - `type`: TYPE 소문자.
  - `DOMAIN`: 도메인 코드 대문자.
  - `이슈번호`: GitHub 이슈 번호(숫자만, `#` 제외).
  - `english-kebab-summary`: 작업을 요약하는 **영문 소문자 kebab-case**. 2~5단어 권장, 축약보다 의미 명확성 우선.
- 연결된 이슈가 없으면 번호를 임의로 만들지 않고 이슈 번호를 생략한다: `<type>/<DOMAIN>-<english-kebab-summary>`.
- 하나의 브랜치는 하나의 이슈/작업 단위에 대응시킨다.

### 예시

| 이슈 | 브랜치명 |
| --- | --- |
| BE #7 · `[BE/FEAT] EC2/SG 자산 조회 API` | `feat/BE-7-assets-list-api` |
| 이슈 없음 · `[INFRA/CHORE] Docker Compose 구성` | `chore/INFRA-docker-compose-setup` |

---

## Gitmoji (커밋·PR 제목 접두)

- 커밋과 PR **제목 맨 앞**에 변경 성격을 나타내는 gitmoji **이모지 1개**를 붙인다. (전체 목록: https://gitmoji.dev)
- **이모지 문자**(예: ✨)를 그대로 사용한다 (`:sparkles:` 단축 코드 사용 지양).
- 브랜치명에는 이모지를 넣지 않는다(ASCII만).

### TYPE별 gitmoji 대표 예시

- `FEAT` ✨ (기능/DB `🗃️`/검증 `🦺`/타입 `🏷️`/로그 `🔊`)
- `FIX` 🐛 (핫픽스 `🚑️`/사소한수정 `🩹`/보안 `🔒️`/예외 `🥅`)
- `REFACTOR` ♻️ (구조 `🎨`/성능 `⚡️`/제거 `🔥`/이동 `🚚`)
- `CHORE` 🔧 (CI `👷`/인프라 `🧱`/의존성 `⬆️` `➕`/스크립트 `🔨`)
- `DOCS` 📝 (문서/주석 `💡`)

> 표에 없는 상황은 https://gitmoji.dev 에서 선택하며, TYPE(대문자 대괄호)은 항상 유지한다.

---

## 커밋 메시지 규칙

- **제목**: `<gitmoji> [TYPE] #이슈번호 - 한 줄 설명`
  - 한 줄 설명은 한국어, 명령형/요약형으로 50자 내외. 코드 식별자·파일명은 원문 유지.
  - 연결된 이슈가 없으면 `[TYPE] 한 줄 설명` 형식을 쓴다.
- **본문(선택, 권장)**: 제목과 한 줄 띄우고 작성. "무엇을·왜"를 불릿으로 정리한다. 어떻게(구현 상세)는 필요한 경우에만.
- **푸터(선택)**: 이슈 연결은 `Refs #이슈번호`(관련) 또는 `Closes #이슈번호`(해결)로 명시한다.
- **AI(Claude) 작성 커밋**: `Co-Authored-By` 등 AI 서명 트레일러를 **붙이지 않는다**.

### 예시

```
✨ [FEAT] #7 - EC2/SG 자산 조회 API 구현

- GET /api/v1/assets 엔드포인트 추가
- packages/schemas의 assets 모델로 응답 직렬화
- Skip 사유 코드(SKIP_LOW_UTIL) 필드 포함

Refs #7
```

> 예: 자동 원복 타임아웃 예외 처리 → `🥅 [FIX] #45 - EC2 Status Check 실패 시 롤백 타임아웃 예외 처리`, Docker 환경 구성 → `🧱 [CHORE] #4 - Docker Compose(FastAPI+PostgreSQL) 환경 구성`.

---

## Pull Request(PR) 규칙

- **대상 브랜치**: 항상 `dev`. (`main` 직접 PR 금지)
- **제목**: 커밋과 동일한 `<gitmoji> [TYPE] #이슈번호 - 한 줄 설명` 형식.
- **본문**: 아래 템플릿을 채운다.

```markdown
## 개요
<이 PR이 무엇을, 왜 바꾸는지 2~3줄>

## 변경 사항
- <핵심 변경 1>
- <핵심 변경 2>

## 테스트
- [ ] `pytest` 통과
- [ ] `docker-compose up`으로 로컬 기동 확인
- [ ] (API 변경 시) FE↔BE 계약/Mock 영향 확인
- [ ] (범위·계약·역할 변경 시) `docs/PROJECT_STATUS.md` 갱신

## 관련 이슈
Closes #<이슈번호>
```

- **리뷰/머지**: 최소 1명 이상(특히 백엔드↔AI↔프론트 API 접점 담당자)의 승인과 CI(GitHub Actions: pytest — Lint·Schema Validation은 도입 예정) 통과 후 머지한다.

---

## (선택) GitHub 이슈 제목 규칙

- 형식: `[DOMAIN/TYPE] 한글 설명` (예: `[BE/FEAT] EC2/SG 자산 조회 API`).
- 이 라벨의 `DOMAIN`·`TYPE`이 그대로 브랜치명과 커밋·PR 제목으로 이어진다.
