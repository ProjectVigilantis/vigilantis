// 유휴 연장(`POST /auth/activity`) 간격 제한 — 실제 사용자 입력이 와도 최대 1분에 1번만 보낸다(#492 PR 1).

/** 서버 유휴 한도(기본 30분)보다 충분히 짧고, 입력마다 요청이 나가지 않을 만큼 길다. */
export const ACTIVITY_INTERVAL_MS = 60_000;

/**
 * 입력 이벤트마다 부르는 판정기. 첫 입력은 바로 보내고, 그 뒤로는 마지막 전송에서
 * `intervalMs`가 지나야 다시 true다. 시계는 테스트를 위해 주입한다.
 */
export function createActivityThrottle(
  intervalMs: number = ACTIVITY_INTERVAL_MS,
  now: () => number = Date.now,
): () => boolean {
  let last: number | null = null;
  return () => {
    const at = now();
    if (last !== null && at - last < intervalMs) return false;
    last = at;
    return true;
  };
}
