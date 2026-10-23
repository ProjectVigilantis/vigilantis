// 플랫폼 운영자 초대 링크 등록(#492) — 화면(components/team/platform-enrollment.tsx)이 쓰는 링크 판독 · 상태 · 문구.
//
// 링크는 `<base>/team/enroll#token=<불투명 값>`이다. 토큰은 **URL 조각(fragment)에서만** 읽는다 — 조각은 서버 요청 ·
// Referer에 실리지 않는다. 읽는 즉시 주소창에서 조각을 지우고(`history.replaceState`, 이동 없음), 토큰은 이 모듈의
// 메모리 상태 하나에만 둔다. 저장소(localStorage 등) · 쿼리 · 로그 · 화면 텍스트 · 숨은 입력에 싣지 않는다.
//
// 토큰 검사는 형식만 본다(빈 값 · 허용 밖 글자 · 길이). 유효 · 만료 · 사용 여부의 판정은 서버 몫이고, 화면에서
// 링크를 막지 않는 것은 권한이 아니다 — 서버가 유효한 초대 없이는 거절한다(`ENROLLMENT_UNAVAILABLE`).

import type { ErrorCode } from '../types/api.ts';

/** URL에 그대로 실을 수 있는 글자(RFC 3986 unreserved)만, 16–512자. 퍼센트 인코딩 · 공백 · `&`는 형식 밖이다. */
const TOKEN_FRAGMENT = /^#token=([A-Za-z0-9._~-]{16,512})$/;

/** 조각 문자열(`location.hash`)에서 토큰을 꺼낸다. `#token=<값>` 하나만 받는다 — 그 밖은 null. */
export function parseEnrollmentFragment(hash: string): string | null {
  return TOKEN_FRAGMENT.exec(hash)?.[1] ?? null;
}

export type EnrollmentLink =
  /** 아직 읽지 않았다(서버 렌더 · 하이드레이션 직후). */
  | { kind: 'pending' }
  /** 조각이 없거나 형식이 맞지 않는다 — 요청하지 않는다. */
  | { kind: 'invalid' }
  | { kind: 'ready'; token: string }
  /** 이 화면에서 더 쓰지 않는다(성공 · 서버가 링크를 거절 · 결과 불명) — 토큰을 버렸다. */
  | { kind: 'spent' };

interface LocationLike {
  hash: string;
  pathname: string;
  search: string;
}

interface HistoryLike {
  replaceState(data: unknown, unused: string, url?: string): void;
}

/**
 * 조각을 읽고 **형식과 관계없이** 주소에서 지운다(형식이 틀린 조각에도 토큰 일부가 있을 수 있다).
 * 이동 없이 같은 경로 · 쿼리로 바꿔 쓴다 — Next 라우터가 `replaceState`를 받아 경로 상태를 맞춘다.
 */
export function takeEnrollmentLink(location: LocationLike, history: HistoryLike): EnrollmentLink {
  const { hash } = location;
  if (hash === '') return { kind: 'invalid' };
  history.replaceState(null, '', `${location.pathname}${location.search}`);
  const token = parseEnrollmentFragment(hash);
  return token === null ? { kind: 'invalid' } : { kind: 'ready', token };
}

/**
 * 링크 상태 저장소 — `useSyncExternalStore`가 구독한다.
 *
 * 조각은 한 번 지우면 다시 읽을 수 없다. 그래서 컴포넌트 상태가 아니라 이 저장소가 첫 구독 때 **한 번만** 읽고 들고
 * 있는다. dev StrictMode의 구독 → 해지 → 재구독이나 하이드레이션 재마운트가 와도 다시 읽지 않아 토큰을 잃지 않는다.
 * 구독자가 모두 떠난 채 한 틱이 지나면(화면을 실제로 떠났다) 토큰을 버리고 `pending`으로 되돌린다 — 같은 화면에
 * 다시 들어오면 그때의 주소를 새로 읽는다.
 */
export function createEnrollmentLinkStore(read: () => EnrollmentLink) {
  const PENDING: EnrollmentLink = { kind: 'pending' };
  let link: EnrollmentLink = PENDING;
  const listeners = new Set<() => void>();
  const emit = () => {
    for (const listener of listeners) listener();
  };

  return {
    subscribe(listener: () => void): () => void {
      listeners.add(listener);
      if (link.kind === 'pending') {
        link = read();
        emit();
      }
      return () => {
        listeners.delete(listener);
        setTimeout(() => {
          if (listeners.size === 0) link = PENDING;
        }, 0);
      };
    },
    getSnapshot: (): EnrollmentLink => link,
    getServerSnapshot: (): EnrollmentLink => PENDING,
    /** 지금 들고 있는 링크가 이 토큰의 초대인가(요청을 보낼 때 잡은 토큰과 비교한다). */
    isCurrent(token: string): boolean {
      return link.kind === 'ready' && link.token === token;
    },
    /**
     * 이 토큰의 초대를 버린다 — 성공했거나, 서버가 링크를 거절했거나, 결과를 알 수 없다. 이 화면에서 다시 보내지 않는다.
     * 지금 링크가 다른 초대면 건드리지 않는다(떠난 화면의 늦은 응답이 새 초대를 지우지 않게).
     */
    spend(token: string): void {
      if (link.kind !== 'ready' || link.token !== token) return;
      link = { kind: 'spent' };
      emit();
    },
  };
}

export type EnrollmentLinkStore = ReturnType<typeof createEnrollmentLinkStore>;

/**
 * 요청이 끝났을 때(성공은 `view` null) 결과를 화면에 반영할지 정한다. 보낼 때 잡은 토큰이 지금 링크가 아니면 —
 * 화면을 떠났다가 다른 초대로 다시 들어온 뒤 옛 요청이 끝났다 — false를 돌려주고 지금 링크도 버리지 않는다.
 * 지금 링크면 성공 · 최종 실패에서 토큰을 버리고 true다.
 */
export function settleEnrollment(
  store: Pick<EnrollmentLinkStore, 'isCurrent' | 'spend'>,
  token: string,
  view: EnrollmentErrorView | null,
): boolean {
  if (!store.isCurrent(token)) return false;
  if (view === null || view.final) store.spend(token);
  return true;
}

/* ─────────────────────────── 문구 ─────────────────────────── */
/* 어떤 문구에도 토큰 · 서버 message를 싣지 않는다. 메일 도착 · 이메일 확인 · 로그인 완료를 말하지 않는다. */

export const ENROLLMENT_LINK_INVALID =
  '초대 링크가 올바르지 않습니다. 받은 링크 전체를 그대로 열었는지 확인하고, 그래도 안 되면 링크를 보낸 담당자에게 새 링크를 요청해 주세요.';

/** 204 — 계정 생성과 초대 메일 발송을 서버가 받아 처리했다. 메일 도착 · 이메일 소유 확인은 아직이다. */
export const ENROLLMENT_DONE = [
  '입력한 이메일로 플랫폼 운영자 계정을 만들고 초대 메일 발송을 요청했습니다.',
  '메일 도착과 이메일 확인은 아직 끝나지 않았습니다. 초대 메일을 확인한 뒤 로그인 화면에서 로그인하고, 안내에 따라 새 비밀번호 설정 · 인증 앱(TOTP) 등록 · 이메일 인증을 마쳐야 사용할 수 있습니다.',
  '이 링크는 다시 쓸 수 없습니다. 메일이 오지 않으면 링크를 보낸 담당자에게 문의해 주세요.',
] as const;

export interface EnrollmentErrorView {
  message: string;
  /** true면 이 링크로 더 요청하지 않는다(토큰을 버리고 폼을 다시 열지 않는다). */
  final: boolean;
  /**
   * true면 서버가 계정을 만들었거나 링크를 예약 · 사용 처리했을 수 있다(결과 불명 · 부분 실패). 서버 계약은 이 경우
   * 예약을 유지하고 공개 복구 · 재생성 경로를 두지 않는다 — 메일이 오지 않은 것도 생성되지 않았다는 증거가 아니다.
   * 그래서 언제나 `final`이고, 문구는 재요청이 아니라 메일함 확인 · 담당자 문의를 안내한다.
   */
  uncertain: boolean;
}

const CONTACT = '링크를 보낸 담당자에게 문의해 주세요.';

export const ENROLLMENT_UNAVAILABLE_MESSAGE =
  '이 초대 링크로는 계정을 만들 수 없습니다. 링크가 만료되었거나 이미 사용되었을 수 있습니다. ' +
  '앞서 요청한 적이 있다면 초대 메일함을 먼저 확인하고, 아니면 링크를 보낸 담당자에게 새 링크를 요청해 주세요.';

const UNCERTAIN_NEXT = `이 링크로는 다시 요청할 수 없습니다. 초대 메일함을 확인하고, 메일이 없으면 ${CONTACT}`;

export const ENROLLMENT_UNCERTAIN =
  '처리 결과를 확인하지 못했습니다. 계정이 이미 만들어졌거나 초대 링크가 사용 처리되었을 수 있습니다. ' + UNCERTAIN_NEXT;

export const ENROLLMENT_DELIVERY_UNCERTAIN =
  '계정은 만들어졌을 수 있지만 초대 메일 발송을 확인하지 못했습니다. ' + UNCERTAIN_NEXT;

/** 429 — 서버가 링크를 예약한 뒤 인증 서비스가 요청 한도로 거절했다. 인증 서비스 쪽 결과는 약속되지 않는다. */
export const ENROLLMENT_RATE_LIMITED =
  '인증 서비스 요청이 너무 많아 처리 결과를 확인하지 못했습니다. 계정이 만들어졌을 수 있습니다. ' + UNCERTAIN_NEXT;

/** 409 — 서버가 링크를 예약한 뒤 인증 서비스에 같은 이메일이 이미 있었다. 이번 요청으로 새 계정은 만들지 않았다. */
export const ENROLLMENT_EMAIL_EXISTS =
  '이미 계정이 있는 이메일이라 이번 요청으로 새 계정을 만들지 않았습니다. 이 초대 링크는 다시 쓸 수 없습니다. ' +
  '그 이메일의 기존 계정이 있다면 로그인하고, 플랫폼 운영자 계정이 필요하면 링크를 보낸 담당자에게 새 링크를 요청해 주세요.';

// 409 · 429는 예약 뒤 거절이다. 404 · 결과 불명은 예약 여부를 단정하지 않지만 모두 토큰을 버리고 끝낸다.
// 422 · 403(출처)만 예약 전 거절임이 확실해 다시 낼 수 있다.
const VIEWS: Partial<Record<ErrorCode, EnrollmentErrorView>> = {
  ENROLLMENT_UNAVAILABLE: { message: ENROLLMENT_UNAVAILABLE_MESSAGE, final: true, uncertain: false },
  AUTH_RATE_LIMITED: { message: ENROLLMENT_RATE_LIMITED, final: true, uncertain: true },
  USER_STATE_CONFLICT: { message: ENROLLMENT_EMAIL_EXISTS, final: true, uncertain: false },
  REQUEST_VALIDATION_FAILED: {
    message: `이메일 주소를 다시 확인해 주세요. 이메일이 맞는데도 거절되면 ${CONTACT}`,
    final: false,
    uncertain: false,
  },
  AUTH_ORIGIN_REJECTED: { message: `허용되지 않은 주소에서 보낸 요청입니다. ${CONTACT}`, final: false, uncertain: false },
  AUTH_EMAIL_DELIVERY_FAILED: { message: ENROLLMENT_DELIVERY_UNCERTAIN, final: true, uncertain: true },
};

/**
 * 등록 요청 실패 문구. `null`은 응답을 받지 못한 실패(네트워크)다. 위 목록 밖(인증 서비스 장애 · 500 · 계약 밖 코드 ·
 * 네트워크)은 서버가 일부를 처리했을 수 있어 결과 불명이고, 이 링크로 더 요청하지 않는다.
 */
export function enrollmentErrorView(code: ErrorCode | null): EnrollmentErrorView {
  return (code === null ? undefined : VIEWS[code]) ?? { message: ENROLLMENT_UNCERTAIN, final: true, uncertain: true };
}
