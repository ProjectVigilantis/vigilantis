// 업무 화면(대시보드 · 자산 · 보안)을 열지 않는 계정에게 보이는 이용 제한 안내. 업무 데이터를 읽기 전에 그린다 —
// 담당 고객이 없는 고객 담당자에게는 배정 없음을, 그 밖(고객사 계정)에게는 기존 이용 제한 문구를 보인다.
// 누구에게 보일지는 호출부가 lib/home-path.ts `businessOpen` · lib/roles.ts `isUnassignedManager`로 정한다.

export function BusinessUnavailable({ unassigned }: { unassigned: boolean }) {
  return (
    <section className="flex flex-col gap-4" aria-labelledby="business-unavailable">
      <h1 id="business-unavailable" className="text-lg font-semibold">업무 화면 이용 제한</h1>
      <p role="status" className="rounded-lg border p-4 text-sm">
        {unassigned
          ? '담당 고객이 배정되지 않아 볼 수 있는 고객 데이터가 없습니다. 중앙 관리자에게 담당 고객 배정을 요청해 주세요.'
          : '고객사별 데이터 분리가 완료되기 전까지 업무 화면을 이용할 수 없습니다.'}
      </p>
    </section>
  );
}
