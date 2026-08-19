// AST-001 자산 관제 — 라우트 스텁입니다(실제 화면은 다음 단계, 화면설계서 v1.4 §4.2).

import { EmptyState } from '@/components/empty-state';

export default function AssetsPage() {
  return (
    <>
      <h1 className="mb-4 text-lg font-semibold">자산 관제</h1>
      {/* 임시 스캐폴딩 문구 — 4.9의 빈 상태 문구가 아니다. 실제 화면 구현 시 교체한다. */}
      <EmptyState
        message="화면 준비 중입니다."
        description="AST-001 자산 관제는 다음 단계에서 구현합니다."
      />
    </>
  );
}
