// 동시에 들어온 호출이 진행 중인 요청 하나를 나눠 받게 하는 래퍼(#492 PR 2).
//
// [계속 사용] 클릭은 창의 pointerdown 입력 연장과 버튼의 onClick 연장을 함께 일으킨다 — 둘이 겹치면
// `POST /auth/activity`가 두 번 나간다. 진행 중이면 같은 promise를 돌려주고, 끝나면(성공 · 실패 모두)
// 다음 호출이 새로 보낸다. 시간 간격 제한(1분 1회)은 하지 않는다 — 그것은 activity-throttle.ts의 일이다.

export function singleFlight<T>(run: () => Promise<T>): () => Promise<T> {
  let inFlight: Promise<T> | null = null;
  return () => {
    if (inFlight) return inFlight;
    const current = run().finally(() => {
      if (inFlight === current) inFlight = null;
    });
    inFlight = current;
    return current;
  };
}
