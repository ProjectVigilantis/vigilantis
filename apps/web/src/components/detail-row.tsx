// 상세 화면 공용 행 — 라벨(좌) · 값(우). AST-002 상세와 INC-002가 같은 형태를 쓴다.

export function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 text-sm">
      <span className="text-muted-foreground shrink-0">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  );
}
