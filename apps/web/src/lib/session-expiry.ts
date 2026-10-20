// 세션 만료 안내의 시각 계산(#492 PR 2). 화면(components/auth/session-expiry-notice.tsx)은 이 결과를 그리기만 한다.
//
// 세션 한도는 두 개다 — 유휴(입력이 있으면 `POST /auth/activity`로 늘어난다)와 절대(로그인 후 8시간, 늘지 않는다).
// 서버는 유휴를 절대 너머로 늘리지 않으므로, 절대가 먼저(또는 같이) 오면 [계속 사용]은 아무것도 살리지 못한다.

import type { SessionInfo } from '../types/api.ts';

/** 유휴 만료 몇 분 전부터 `N분 후 자동 로그아웃`과 [계속 사용]을 띄우는가. */
export const IDLE_WARNING_MS = 5 * 60_000;
/** 절대 만료 몇 분 전부터 `최대 사용 시간이 곧 끝납니다`를 띄우는가. */
export const ABSOLUTE_WARNING_MS = 10 * 60_000;
const MINUTE_MS = 60_000;

export interface ExpirySchedule {
  /** 유효 만료(둘 중 이른 쪽)에 닿았다 — 로그인 화면으로 보낸다. */
  expired: boolean;
  /** null이 아니면 유휴 경고를 [계속 사용]과 함께 띄운다. 값은 올림한 남은 분. */
  idleMinutesLeft: number | null;
  /** true면 절대 만료 안내를 띄운다(연장 버튼 없음). */
  absoluteNotice: boolean;
  /**
   * 위 세 값이 다음으로 바뀌는 시각(epoch ms). 그 전까지는 결과가 같으므로 화면은 이 시각에만 다시 계산한다.
   * 만료됐거나 시각을 해석할 수 없으면 null.
   */
  nextChangeAt: number | null;
}

const NONE: ExpirySchedule = { expired: false, idleMinutesLeft: null, absoluteNotice: false, nextChangeAt: null };

/** 남은 시간을 분으로 올림한다 — 4분 30초 남았으면 `5분 후`. */
function minutesLeft(remainingMs: number): number {
  return Math.ceil(remainingMs / MINUTE_MS);
}

export function expirySchedule(
  session: Pick<SessionInfo, 'idle_expires_at' | 'absolute_expires_at'>,
  now: number,
): ExpirySchedule {
  const idle = Date.parse(session.idle_expires_at);
  const absolute = Date.parse(session.absolute_expires_at);
  if (!Number.isFinite(idle) || !Number.isFinite(absolute)) return NONE;

  const end = Math.min(idle, absolute);
  if (now >= end) return { expired: true, idleMinutesLeft: null, absoluteNotice: false, nextChangeAt: null };

  const changes = [end];
  let idleMinutesLeft: number | null = null;
  // 연장은 유휴가 절대보다 **먼저** 끝날 때만 의미가 있다.
  if (idle < absolute) {
    const warnAt = idle - IDLE_WARNING_MS;
    if (now < warnAt) {
      changes.push(warnAt);
    } else {
      idleMinutesLeft = minutesLeft(idle - now);
      // 표시한 분이 하나 줄어드는 순간.
      changes.push(idle - (idleMinutesLeft - 1) * MINUTE_MS);
    }
  }

  const absoluteWarnAt = absolute - ABSOLUTE_WARNING_MS;
  const absoluteNotice = now >= absoluteWarnAt;
  if (!absoluteNotice) changes.push(absoluteWarnAt);

  return { expired: false, idleMinutesLeft, absoluteNotice, nextChangeAt: Math.min(...changes) };
}
