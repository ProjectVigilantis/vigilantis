'use client';

// CMN-001 연결 인디케이터 — 화면설계서 v1.5 §4.8. GNB 우측에 붙습니다.
// 옆의 `수집 대상` 인디케이터는 CMN-001이 아니다 — `GET /assets` 봉투 소관이다(§3.1).
//
// 표기는 옆 칩과 **같은 구조**다: `속성 │ 상태값 · 대상명`. 종전에는 값만 있었고 그 값에
// 속성이 붙었다 말았다 해서(`실시간 연결됨` vs `연결 중…`) 무엇에 대한 상태인지가 상태마다
// 다르게 읽혔다. 속성을 항상 앞에 고정하면 훑어보는 사람이 자리로 뜻을 알 수 있다.

import { useRealtime, type ConnectionState } from '@/components/realtime-provider';
import { StatusChip, type ChipTone } from '@/components/status-chip';
import { Button } from '@/components/ui/button';
import { apiBaseUrl } from '@/lib/api/client';
import { websocketUrl } from '@/lib/realtime-events';

/** 이 칩이 말하는 속성. 상태값이 무엇이든 바뀌지 않는다. */
const ATTRIBUTE = '실시간';

/**
 * §4.8의 3상태에 `disabled` 하나를 더 둔다 — `NEXT_PUBLIC_API_BASE_URL`이 소켓 주소로 성립하지
 * 않으면(스킴 없는 `localhost:8000` 등) 붙을 곳 자체가 없다. 이걸 "연결 끊김"으로 그리면
 * **서버가 죽은 것처럼** 읽히는데 실제 원인은 설정이다. 재시도로 풀리지 않으므로 문구로 가른다.
 *
 * 연결·구독 오류 이벤트는 계약에 없다(§4.8) — 상태는 소켓 수명주기로만 표시하고,
 * **실행 상태로 오해되지 않게** 문구를 실행 어휘와 겹치지 않게 골랐다.
 *
 * `value`는 **상태값만** 담는다. 속성(`실시간`)은 칩이 늘 앞에 붙인다.
 */
const PRESENTATION: Record<ConnectionState, { value: string; tone: ChipTone; pulse?: boolean }> = {
  open: { value: '연결됨', tone: 'ok' },
  connecting: { value: '연결 중…', tone: 'warn', pulse: true },
  reconnecting: { value: '재연결 중…', tone: 'warn', pulse: true },
  closed: { value: '연결 끊김', tone: 'idle' },
  // 붙을 주소가 없는 상태다. 재시도로 풀리지 않으므로 사유는 툴팁이 말한다.
  disabled: { value: '미연동', tone: 'idle' },
};

/**
 * 소켓 주소. **화면에는 적지 않고 툴팁으로만 둔다** — 모든 상태에서 같은 값이라 자리만 먹고,
 * 관제자가 GNB에서 판단할 것은 "붙었나"이지 "어디에 붙었나"가 아니다. 어느 백엔드를 보고
 * 있는지 확인할 길은 남긴다(옆 칩이 계정 ID를 툴팁으로 돌린 것과 같은 규칙).
 *
 * 주소가 소켓으로 성립하지 않으면(= `disabled`) null이고, 그 사유는 상태값이 말한다.
 */
function socketUrl(): string | undefined {
  return websocketUrl(apiBaseUrl()) ?? undefined;
}

export function ConnectionIndicator() {
  const { connection, reconnect } = useRealtime();
  const view = PRESENTATION[connection];
  const url = socketUrl();

  return (
    <StatusChip
      tone={view.tone}
      label={ATTRIBUTE}
      value={view.value}
      pulse={view.pulse}
      title={url ?? 'NEXT_PUBLIC_API_BASE_URL이 소켓 주소로 성립하지 않습니다'}
    >
      {/* 재연결 실패가 지속되면 수동 버튼을 노출한다(§4.8 4). 자동 재시도는 계속 돈다. */}
      {connection === 'closed' ? (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="-mr-1 ml-0.5 h-5 px-1.5 text-[11px]"
          onClick={reconnect}
        >
          재연결
        </Button>
      ) : null}
    </StatusChip>
  );
}
