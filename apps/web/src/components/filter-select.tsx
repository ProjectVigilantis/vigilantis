'use client';

// 목록 화면 공용 필터 셀렉트 — AST-001(§4.2)·INC-001(§4.4)이 같은 컨트롤을 씁니다.
// `onChange`를 받는 컴포넌트라 클라이언트 경계를 **직접** 선언한다 — 호출부의 경계에 얹혀 돌면
// 서버 컴포넌트가 import하는 순간 깨진다(PR #171 리뷰).

export function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  return (
    <label className="flex items-center gap-1.5 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="border-input bg-background focus-visible:ring-ring/50 rounded-md border px-2 py-1 focus-visible:ring-2 focus-visible:outline-none"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}
