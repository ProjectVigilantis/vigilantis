// AST-002 자산 상세(§4.3)의 `spec` 값 한 칸을 **어떤 모양으로 그릴지**만 정한다.
//
// `spec`은 자산 유형마다 필드가 다른 자유 형식이라(계약의 `Ec2Spec`·`SgSpec`·`NaclSpec`…)
// 화면은 key를 미리 알지 못하고 **값의 모양**으로 렌더를 가른다. 그 분기를 컴포넌트에서
// 떼어 둔 이유는 하나다 — `Map`(태그처럼 Key→Value 하나로 온 값)이 분기에 없던 탓에
// `String({})`이 그대로 나가 화면에 `[object Object]`가 찍혔고, 그런 누락은 렌더 코드
// 안에서는 눈에 띄지 않기 때문이다. 여기 두면 `spec-value.test.ts`가 모양별로 지킨다.
//
// 색·배지·포트 규칙 표기처럼 **무엇으로 보이는가**는 여전히 컴포넌트 몫이다.

/** `spec` 값 한 칸의 렌더 모양. */
export type SpecValueView =
  /** 값이 없다 — `null`, 빈 배열, 빈 객체. 셋 다 화면에는 `—` 한 글자로 적는다. */
  | { kind: 'EMPTY' }
  | { kind: 'BOOLEAN'; value: boolean }
  /** 배열. 항목이 포트 규칙인지 아닌지는 컴포넌트가 가른다. */
  | { kind: 'LIST'; items: unknown[] }
  /** 태그처럼 Key→Value 로 온 객체. 키 순서는 응답 순서 그대로 둔다(정렬하지 않는다). */
  | { kind: 'MAP'; entries: [string, string][] }
  | { kind: 'SCALAR'; text: string; numeric: boolean };

/**
 * 값의 모양을 가른다. **빈 것 셋(`null`·`[]`·`{}`)을 같은 `EMPTY`로 접는 것이 핵심이다** —
 * "값이 없다"를 화면에서 다르게 말할 이유가 없다(§3.3의 `[]`≠`null` 구분은 계약 축이다).
 *
 * 중첩 객체(값이 다시 객체인 Map)는 지금 계약에 없어 `String()`으로 떨어진다. 생기면
 * 그때 이 함수에 모양을 하나 더한다 — 컴포넌트를 고칠 일이 아니다.
 */
export function specValueView(value: unknown): SpecValueView {
  if (value === null || value === undefined) return { kind: 'EMPTY' };
  if (typeof value === 'boolean') return { kind: 'BOOLEAN', value };
  if (Array.isArray(value)) {
    return value.length === 0 ? { kind: 'EMPTY' } : { kind: 'LIST', items: value };
  }
  if (typeof value === 'object') {
    const entries: [string, string][] = Object.entries(value as Record<string, unknown>).map(
      ([key, v]) => [key, String(v)],
    );
    return entries.length === 0 ? { kind: 'EMPTY' } : { kind: 'MAP', entries };
  }
  return { kind: 'SCALAR', text: String(value), numeric: typeof value === 'number' };
}
