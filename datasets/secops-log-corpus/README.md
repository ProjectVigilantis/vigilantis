# SecOps SSH 모의 로그 테스트 자료

Issue #350의 로그 선택·집계 자료. C01–C07은 공식 예제 기반 합성 실패,
N01/N02는 NCP의 실제 인증 수락 구간을 익명화한 대조 자료다.
`check`는 현재 모의 위협 계약·정형화·초기 위험 규칙을 오프라인으로 대조한다.
`prepare`와 앱 소비자를 통한 저장·AI 전달 방식은 [MVP 연결 설계](MVP_CONNECTION.md)에 있다.
전체 9개 사례의 첫 실행 결과는 [서비스 실행 결과 요약](OBSERVATION_SUMMARY.md)에 있다.
실제 모델 입출력·DB/API 응답 등 상세 실행 기록은 로컬에만 보관한다.

여기서 테스트 자료는 생성기와 원천 표본으로 다시 만들 수 있는 로그·기대값 묶음이다.
`cases/`의 34개 파일은 테스트 입력으로 버전 관리하며 실제 서비스 실행 기록과 구분한다.

| 사례 | 실패 / 창 | 초기 위험 | 용도 | 연결 Golden 파일 |
| --- | --- | --- | --- | --- |
| C01 | 120 / 300초 | HIGH | 주평가 대표 | `evt_ssh_bruteforce_001.json` |
| C02 | 1000 / 60초 | HIGH | 합성 강도 확대 | `evt_ssh_bruteforce_004.json` |
| C03 | 120 / 300초 | HIGH | production 대상과 문맥 비교 | `evt_ssh_bruteforce_005.json` |
| C04 | 60 / 600초 | MEDIUM | 횟수 조건만 충족 | `evt_ssh_bruteforce_006.json` |
| C05 | 5 / 10초 | MEDIUM | 속도 조건만 충족 | `evt_ssh_bruteforce_007.json` |
| C06 | 5 / 3600초 | LOW | 초기 위험 경계, 정식 주평가 제외·서비스 관찰 포함 | `evt_ssh_bruteforce_002.json` |
| C07 | 1 / 1초 | LOW | 단발 경계, 정식 주평가 제외·서비스 관찰 포함 | `evt_ssh_bruteforce_003.json` |
| N01 | 실패 0, 공개키 수락 1 | 해당 없음 | 집계 대조, 위협 입력 없음 | — |
| N02 | 실패 0, 비밀번호 수락 1 | 해당 없음 | 집계 대조, 위협 입력 없음 | — |

C01–C05의 `ai_evaluation: true`는 평가 대상 후보라는 의미이며 모델 측정 완료나
AI 조치 정답 확정을 뜻하지 않는다. #350 서비스 관찰은 C01–C07 전부를 실행하며,
이 플래그로 관찰 대상을 제한하지 않는다. C03의 production 의미는 기존 Golden 자산에
있다. 이 테스트 자료에는 자산 스냅샷이나 NACL 관계를 새로 만들지 않는다.

## 파일과 재현

- `case-specs.json`: 생성할 조건과 선택 범위.
- `expectations.json`: 별도로 작성한 기대값·도출 근거·금지 단정. 코드 출력으로 갱신하지 않는다.
- `sources/registry.json`: 원천·버전·원본 사본 지문·변환·보장 범위.
- `sources/N01.jsonl`, `N02.jsonl`: 필요한 3행씩만 남긴 익명화 원천.
- `sources/failed-password.txt`: 공식 실패 예제에서 추출한 메시지 형식.
- `cases/<ID>/manifest.json`: 출처·변환·집계 창·수집 범위·Golden 연결.
- `cases/<ID>/logs.jsonl`: 로그 메시지와 원천/수신 시각, 호스트·프로세스 가명, 레코드 ID.
- `cases/<ID>/expected.json`: 독립 기대값의 사례별 사본.
- `cases/C*/observation.json`: 로그 집계에서 생성한 `SshBruteForceThreatInput` 모의 입력. 실제 서비스 실행 기록이 아니다.

저장소 루트에서 실행한다. `build`는 선언된 사례 파일만 쓰고, `check`는 읽기만 한다.
두 명령은 앱 inbox에 전달하거나 DB·AWS·모델을 호출하지 않는다.

```powershell
uv sync --locked --all-packages
uv run --no-sync python scripts/secops_log_corpus.py build
uv run --no-sync python scripts/secops_log_corpus.py check
# 재생성 결과를 별도 디렉터리에서 대조할 수도 있다.
uv run --no-sync python scripts/secops_log_corpus.py build --output <새-출력-디렉터리>
uv run --no-sync python scripts/secops_log_corpus.py check --output <같은-출력-디렉터리>
```

생성물은 34개 파일, 로그는 2,201행이다. 실패 결과 1,311행과 합성 보조 행
884행, NCP 대조 자료 6행으로 구성된다. 원천 표본 파일은 생성 로그와 중복 집계하지 않는다.
`check`는 별도 기대값, 기존 Golden 입력, 현재 Pydantic·정형화·위험 판정,
재생성 바이트 일치와 파일 누락/추가를 확인한다. 런타임 UUID·접수 시각은 정답에 고정하지 않는다.
기존 Golden 의미가 바뀌면 불일치를 검토하고 생성 목표와 기대값을 사람이 각각 갱신한다.

## 원천과 해석 범위

실패 형식은 [AWS Directory Service 예제](https://docs.aws.amazon.com/directoryservice/latest/admin-guide/ms_ad_troubleshooting_join_linux.html)에
근거한다. 도메인 인증 문제 문서이므로 실제 공격 사례라고 부르지 않는다.
실패 횟수·시간·식별자와 보조 행은 합성이다. 일반 syslog 파일 전체나
실제 sshd 실행 결과를 그대로 재현했다고 주장하지 않는다.

NCP 제공 journal은 888행이며 `Accepted publickey` 199건, `Accepted password`
2건, `Failed` 0건을 확인했다. 공개하는 것은 수락·PAM 세션 열림·닫힘의 두 구간
뿐이다. 원문 전체와 가명 역매핑은 저장소에 포함하지 않는다. 원문을 받을 수 없는
팀원도 공개한 익명화 표본만으로 같은 테스트 자료를 재생성할 수 있다.
PAM 세션의 UID도 `REDACTED`로 마스킹한다. 인증 방식·수락 횟수·세션 열림/닫힘은
대조에 사용하지만, 원래 계정의 권한 수준은 이 표본의 검증 대상이 아니다.

NCP 출처를 AWS 관측으로 바꾸지 않는다. 사례에 연결된 AWS 계정·ARN은 기존
Golden의 가상 값이다. #324 평가 입력의 NACL 관계 역시 별도 합성 조건이며
이 자료가 NCP에서 관측한 관계가 아니다.

실패 0건은 제공 사본에서 그 기록을 찾지 못했다는 뜻이다. 실제 실패 부재나
접속의 정당성을 보장하지 않는다. OpenSSH 9.6의 기본 INFO 수준에서는 일부
공개키 실패가 보이지 않을 수 있다. [버전 고정 소스](https://github.com/openssh/openssh-portable/blob/V_9_6_P1/auth.c)

`Accepted` 자체는 셸·작업 성공이나 정당성을 보장하지 않는다. 선택한 NCP 자료에는
동일 boot·프로세스의 PAM 세션 열림/닫힘 기록도 있어 함께 보존했다.
Ubuntu 추가 패치와 전체 실효 LogLevel까지 검증한 자료는 아니다.

## 생성·집계 규칙

합성 실패는 목표 N건, 창 W초에 대해 `start + (i + 0.5) × W/N`에 배치한다.
한 연결에 최대 3회, 실패 사이의 전체 간격은 60초 이내로 두고 PID·원격 포트를
다음 연결에서 바꾼다. C06처럼 간격이 긴 실패는 각각 별도 연결이다.
연결마다 `Invalid user`와 종료 행을 1개씩 추가하되 인증 실패 수에는 더하지 않는다.
보조 행은 실패 앞뒤 최대 1ms에 배치해 단발 실패의 연결을 길게 늘리지 않는다.
균등 간격과 50ms 수신 시각 차이는 재현을 위한 합성 조건이다.
Golden의 `occurred_at`을 **새 fixture의 창 끝점**으로 선택했으며, 기존 사건이
실제로 그 창을 관측했다는 의미는 아니다. 원문 발생 시각의 형식을 변경한 부분도 합성이다.

선택은 호스트·인증 결과의 출발지 IP·원천 시각 `[start,end)`를 사용한다.
보조 행 수는 선택한 호스트/창에서 읽은 보조 메시지 수이며 인증 횟수가 아니다.
`Failed password`만 실패로, `Accepted password/publickey`만 수락으로 센다.
`Partial`·`Postponed`·`Failed none/publickey` 등 범위 밖 결과는 unsupported로
남기고 위협 요약 생성을 거부한다. 다양한 배포판을 지원하는 운영 파서가 아니다.

전달 중복은 `record_id`로 접되 같은 ID의 내용 충돌은 거부한다. 내용이 같아도
ID가 다른 반복 실패는 보존한다. `record_count`는 선택 전 고유 레코드 수이며
필터에서 제외된 행은 `excluded` 사유로 구별한다.

`complete`는 생성한 fixture 범위, `sampled`는 선택한 NCP 프로세스 구간이다.
부분 수집은 관측 수를 유지하되 현재 위협 계약으로 투영하지 않는다.
미수집·조회 실패의 결과는 null이며, 읽어 본 자료에서의 0건과 구분한다.

NCP `source_time`은 `_SOURCE_REALTIME_TIMESTAMP`, `received_at`은
`__REALTIME_TIMESTAMP`에서 가져왔다. 전자는 journal이 아는 가장 이른 trusted
메시지 시각, 후자는 수신 시각이며 정확한 인증 발생 시각·시계 정확도를 보장하지 않는다.
사본에서 둘의 차이가 최대 약 15초였으므로 수신 시각으로 집계 창을 대체하지 않는다.
[systemd 필드 문서](https://github.com/systemd/systemd/blob/main/man/systemd.journal-fields.xml)

정규화 원천 지문은 정렬된 JSON의 SHA-256이므로 OS 줄바꿈에 의존하지 않는다.
registry의 원래 NCP 사본 SHA-256만 원본 바이트 지문이다. 지문은 전달 사본의
동일성을 확인하며 원래 journal의 진위를 인증하지 않는다.

## 서비스 연결 경계

자료 생성 도구는 실제 로그 감시·탐지기를 구현하지 않는다. `prepare`는 검증한 집계와
최대 12행 발췌를 기존 파일 inbox에 준비한다. 앱 소비자가 접수할 때 Inventory 사본과
함께 THREAT 근거에 보존하고 Dispatcher가 그 저장분을 읽는다. 준비 완료는 접수 성공이
아니며, 실제 전달·저장은 Incident 조회와 별도 서비스 테스트로 확인한다.
`check` 결과만으로 DB·AI·AWS 실행 성공이나 #324 평가 재개를 선언하지 않는다.

회귀 검증은 `tests/test_secops_log_corpus.py`에 있으며 기존 CI의 `tests` 수집에 포함된다.
검증 항목은 시간 창·시각 필드·대상 선택, 전달 중복, 보조 행, 범위 밖 인증 방식,
수집 상태, 익명화, 독립 기대값과 재생성이다. 모든 운영 탐지 경우를 포함하는 목록은 아니다.
