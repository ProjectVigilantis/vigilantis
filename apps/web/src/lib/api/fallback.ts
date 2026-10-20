// 서버 컴포넌트의 "실패해도 화면은 뜬다" 조회용 fallback(#492 PR 2). 오류를 null · 값으로 접되,
// **Next의 redirect(401 → 로그인)는 접지 않는다** — `rethrow`(서버에서는 `unstable_rethrow`)가 먼저 던진다.
//
// `rethrow`를 주입받는 이유: `next/navigation`을 여기서 import하면 `node --test`가 확장자 없는 하위 경로를
// 풀지 못한다. 묶는 자리는 lib/api/server.ts 하나다.

export type Rethrow = (error: unknown) => void;

export type Settled<T> = { value: T; error: null } | { value: null; error: unknown };

/**
 * 거부된 채 기다려지지 않을 수 있는 promise를 "처리됨"으로 표시해 돌려준다. 같은 promise를 나중에 `await`하면
 * 여전히 던진다. 화면이 다른 조회 실패로 일찍 끝나면(ErrorState · redirect) 나란히 시작한 조회의 redirect
 * 거부가 unhandled rejection으로 새지 않게 한다.
 */
function handled<T>(promise: Promise<T>): Promise<T> {
  promise.catch(() => {});
  return promise;
}

export function fallbacks(rethrow: Rethrow) {
  return {
    /** 실패하면 null. redirect 등 Next 내부 오류는 그대로 던진다. */
    orNull<T>(promise: Promise<T>): Promise<T | null> {
      return handled(
        promise.then(
          (value) => value,
          (error: unknown) => {
            rethrow(error);
            return null;
          },
        ),
      );
    },

    /** 실패해도 오류를 들고 간다(0건과 조회 실패를 구분해 그리는 자리). redirect는 그대로 던진다. */
    orError<T>(promise: Promise<T>): Promise<Settled<T>> {
      return handled(
        promise.then(
          (value): Settled<T> => ({ value, error: null }),
          (error: unknown): Settled<T> => {
            rethrow(error);
            return { value: null, error };
          },
        ),
      );
    },
  };
}
