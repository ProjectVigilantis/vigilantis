# FE 관찰용 테스트 서버와 단계 진행 CLI

타이머를 끈 테스트 서버를 띄우고, 명령 한 번에 한 주기씩 진행하며 FE 화면이 어떻게 바뀌는지 본다. 수집·판정·AI 분석·실행이 앱 타이머로 이어지는 시연 경로(`docs/E2E_DEMO_SCENARIOS.md`)와는 별개다. 초기화로 같은 흐름을 처음부터 다시 돌릴 수 있다.

- 테스트 서버: `docker-compose.yml`에 `compose.override.yml`을 얹어 `-p vigilantis-test`로 띄운다. 스캔·실행·AI 분석 타이머와 모의 위협 파일 소비 루프를 끄고, 포트는 `127.0.0.1`에만 연다.
- 단계 진행 라우터(`app.py`): 제품 앱에 `/_stepper/*`를 붙인다. 명령 하나가 앱의 주기 함수를 1회 부르고, 그 이벤트가 같은 프로세스의 WebSocket으로 FE까지 간다.
- CLI(`cli.py`·`server.py`): 테스트 서버를 띄우고 내리며, 명령마다 한 일 → FE에서 볼 곳 → 다음에 할 수 있는 것을 출력한다.

## 준비

1. Docker Desktop을 켠다. 개발 스택(`docker compose up`)과 시연 스택은 내려 둔다 — 같은 포트(5432·4566·8000)를 쓴다.
2. 저장소 루트에서 `uv sync --locked --all-packages`. 루트 `.env`가 있어야 테스트 서버가 뜬다(없으면 `.env.example`로 만든다). `analyze`는 그 `.env`의 `OPENAI_API_KEY`로 돈다(워크트리에서 쓰면 메인 체크아웃의 `.env`를 복사한다).
3. FE는 `apps/web`에서 `npm ci`(처음 한 번) 뒤 `npm run dev -- -H localhost`로 띄우고 `http://localhost:3000`으로 연다.
   - `-H localhost`는 FE를 이 노트북에서만 연다. 기본값(`next dev`)은 모든 네트워크 인터페이스에 열려, 같은 망의 다른 기기에서 테스트 데이터가 그려진 화면을 볼 수 있다.
   - 주소는 `127.0.0.1`이 아니라 `localhost`여야 한다. API의 CORS·WebSocket 허용 출처 기본값이 `http://localhost:3000`이다.

## 명령

저장소 루트에서 `uv run python scripts/stepper/cli.py <명령>`.

| 명령 | 하는 일 | 모델 호출 |
| --- | --- | --- |
| `up` | 테스트 서버를 띄운다. 떠 있으면 그대로 쓰고, 아니면 `reset`처럼 처음부터 준비한다 | 없음 |
| `reset` | 이 프로젝트만 볼륨째 내리고 LocalStack 시드·migration·준비 확인까지 한다. 새 회차가 시작된다 | 없음(키 확인은 모델 조회만) |
| `down` | 테스트 서버를 볼륨째 내린다. 회차 기록은 남는다 | 없음 |
| `status` | 사건·실행 상태와 FE 상세 주소, 다음에 할 수 있는 것 | 없음 |
| `collect` | 자산·메트릭 수집·적재만 한다(판정·카드 없음) | 없음 |
| `scan` | 수집 → 판정 → FinOps 사건 생성 | 없음 |
| `inject ssh --case C01 --target <Name>` | 로그 근거가 붙은 SSH 관측 1건(`datasets/secops-log-corpus`, C01–C07)을 넣고 1회 소비한다 | 없음 |
| `inject golden <파일> --target <Name>` | 골든 위협 입력(`datasets/golden/secops/input`) 1건을 넣고 1회 소비한다 | 없음 |
| `analyze` | 분석 대기 전부를 실제 모델로 분석한다. 누르기 전에 예상 호출 수를 보여 주고 확인받는다(`-y`로 생략) | FinOps EC2 3회, 그 밖의 FinOps 2회, SecOps 3회(재시도 제외) |
| `dispatch` | 진행 중인 실행을 한 칸 진행하고, 끝난 차단에 해제 후보를 낸다 | 없음 |
| `dispatch --fail-status-check <Name>` | 실패 주입기를 띄운 뒤 `dispatch`한다. 다운사이징의 다음 칸 2/2 Status Check가 실패한다 | 없음 |

- `--target`은 수집된 자산의 Name이며 정확히 1건이어야 한다. 없으면 `collect`나 `scan`을 먼저 누른다.
- 관측 시각은 누른 시각(UTC, 초 단위)이라 같은 입력을 다시 넣어도 새 Incident가 된다. 같은 관측을 재전달하려면 `--occurred-at`에 같은 시각을 준다.
- 순서는 강제하지 않는다. 서버가 받지 않는 조합은 서버의 거부·실패가 그대로 보인다.
- 승인·해제·종료 판단은 FE에서 누른다.

## 흐름 예시

시드 자산 이름 기준이다. 모델 호출 수는 재시도가 없을 때의 값이다.

**T1 — 다운사이징 실패와 자동 원복**

1. `reset` → `scan` → `analyze`(카드 3장, 7회)
2. FE 자산 인시던트의 「승인 대기」에서 `vigilantis-seed-idle-dev` 카드 [조치 실행] → [실행]
3. `dispatch --fail-status-check vigilantis-seed-idle-dev` — 다운사이징 실행, 주입기가 기동 직후 인스턴스를 멈춘다
4. `dispatch` 세 번 — Status Check 실패 → 자동 원복 접수 → 원복 실행. 원본이 `ROLLED_BACK`, Incident가 종료 판단 대기가 된다
5. FE 상세 「수행된 조치」에서 '복구 완료' 배지를 본다. 다운사이징 실행 칸과 자동 원복 접수 칸은 WebSocket 이벤트가 없어 화면이 다음 칸에서 바뀐다

**T2 — SSH 차단과 해제**

1. `inject ssh --case C01 --target vigilantis-seed-idle` → `analyze`(3회)
2. FE 상세 [승인하고 차단] → [실행] → `dispatch` — 차단이 끝난 칸에 해제 후보가 선다
3. FE [승인하고 해제] → [실행] → `dispatch` → FE [종료 판단]

**OPEN_IP** — `inject golden evt_open_ip_001 --target vigilantis-seed-open-ssh` → `analyze`. 제공할 조치·조회가 없는 위협이면 모델을 부르기 전에 진행 불가로 끝난다(서버 로그 `agent_graph_input_unavailable`).

**FE에서 볼 곳** — 카드와 배지, 「수행된 조치」의 실행 배지는 새로고침 없이 바뀐다. 보안 인시던트 목록의 「승인 대기 N」 칩과 T1 원복 안내 패널(상세를 떠나면 사라진다)은 그렇지 않으므로 카드 배지와 「수행된 조치」를 본다. `collect`는 WebSocket 이벤트가 없어 새로고침해야 보인다. 알림은 명령을 누를 때 열려 있던 화면에만 뜨므로 볼 화면을 먼저 열어 두고 누른다.

## 기록

- 회차는 `reset`부터 다음 `reset`까지다. 기록은 `~/.vigilantis/stepper/rounds/<회차 ID>/`에 남는다(`--data-root`로 위치를 바꾼다).
  - `steps.jsonl`: 명령마다 한 줄 — 서버 보고, 누르는 동안 받은 WebSocket 이벤트, 누른 뒤의 REST 상태
  - `inputs/`(주입한 입력 파일), `seed.log`, `inbox/`(다음 초기화 때 옮겨 온 소비 결과)
- 토큰은 데이터 루트의 `token` 파일에만 있고 출력·기록에 남지 않는다.

## 안전장치

- 테스트 서버 포트를 다른 compose 프로젝트나 compose 밖 프로세스가 잡고 있으면, 모든 명령이 아무것도 하지 않고 거부한다. 시드는 LocalStack의 NACL 규칙을 비우고 실패 주입기는 인스턴스를 멈추므로, 같은 포트의 개발·시연 스택에 닿으면 그 스택을 망가뜨린다. 그 스택은 직접 내린다.
- 띄우기 전에 `docker compose config` 결과에서 포트가 전부 `127.0.0.1`인지, 볼륨이 이 프로젝트 것인지 본다. `!override`를 모르는 Compose에서는 기동하지 않는다.
- 테스트 서버 진입점은 타이머·파일 소비 루프가 켜졌거나, 토큰이 없거나, AWS 엔드포인트가 LocalStack이 아니면 기동하지 않는다.
- 명령은 한 번에 하나만 돈다. 처리 중에 누르면 처리 중인 명령 이름과 함께 거부된다.
- 초기화는 볼륨째 한다. LocalStack은 비영속이라 DB만 남기면 새 인스턴스 ID로 카드가 이중으로 생긴다.

## 주의

- 테스트 서버를 띄운 채 pytest를 돌리지 않는다. 같은 포트라 테스트가 테스트 서버의 DB·LocalStack에 붙는다. 포트가 IPv4에만 열려 있어, `localhost`로 붙는 클라이언트는 IPv6(`::1`)부터 시도하느라 연결마다 늦어질 수 있다(Windows에서 약 3초).
- 모델 호출은 과금된다. 한도는 키가 아니라 조직·프로젝트 단위라 같은 조직의 키를 쓰는 다른 작업과 한도를 나눠 쓴다.
- CLI의 WebSocket 클라이언트(`websockets`)는 직접 선언한 의존성이 아니라 `uvicorn[standard]`를 통해 설치된다.

## 유지

서비스 흐름 밖의 도구라 제품 동작이 바뀔 때마다 함께 고치지 않는다.

- CI는 이 도구 자신의 로직과 안전장치(토큰·포트 점유 거부·타이머 끔·제품 앱에 라우터 없음)만 본다. 제품 흐름과 붙는 부분은 쓸 때 확인한다 — 쓰기 전에 `reset` → `scan`을 한 번 돌려 본다.
- 흐름 예시·FE에서 볼 곳·예상 모델 호출 수는 이 도구를 추가한 시점의 동작 기준이다. 쓰다가 실제와 어긋나면 그때 고친다.

## 보장 범위

명령의 서버 보고, 그 뒤의 REST 상태, 누르는 동안의 WebSocket 수신까지 기록한다. 아래는 이 도구로 주장하지 않는다.

- FE 표시 — 사람이 화면에서 본다
- 실 AWS 권한·동작 — LocalStack 변화다(ADR-0006)
- 모델 판단 근거의 품질
- 타이머끼리 섞이는 실제 동작(예: 원복 도중 스캔) — 앱 타이머로 도는 시연·리허설에서 본다
