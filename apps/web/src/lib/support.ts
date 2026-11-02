// 도입 신청 · 고객 문의의 화면 보조(2026-10-29). 도입 신청은 로그인 창 회원가입에서 처음 쓰는 회사가 보낸다.

/** 서버가 아직 이 기능을 열지 않았나(경로 없음 404 · 메서드 없음 405). 화면은 오류 대신 준비 중으로 읽는다. */
export function notYetServed(httpStatus: number | null): boolean {
  return httpStatus === 404 || httpStatus === 405;
}
