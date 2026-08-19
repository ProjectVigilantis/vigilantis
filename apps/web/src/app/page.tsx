// DSH-001 메인 대시보드 — 라우트 스텁입니다(실제 화면은 다음 단계, 화면설계서 v1.4 §4.1).

import { EmptyState } from '@/components/empty-state';

export default function DashboardPage() {
  return (
    <>
      <h1 className="mb-4 text-lg font-semibold">메인 대시보드</h1>
      {/* 임시 스캐폴딩 문구 — 4.9의 빈 상태 문구가 아니다. 실제 화면 구현 시 교체한다. */}
      <EmptyState
        message="화면 준비 중입니다."
        description="DSH-001 메인 대시보드는 다음 단계에서 구현합니다."
      />
    </>
  );
}
