// 그래프 · 목록 카드 한 장: 제목, 범위와 단위를 적는 설명 한 줄, 오른쪽 요약값(DESIGN.md §데이터 시각화).
// 훅이 없어 서버 · 클라이언트 컴포넌트 어디서나 쓴다.
// 카드가 컨테이너(`@container/chart`)라 안쪽 배치는 화면 폭이 아니라 카드가 실제로 받은 폭으로 접는다(2026-11-03).
// 사이드바 · 고객사 상세 안 관제처럼 같은 화면 폭에서도 카드 폭이 달라지기 때문이다.
// 컨테이너는 쌓임 맥락을 새로 만들어 안쪽 말풍선이 z-index로 카드 밖에 못 나간다. 카드 밖으로 삐져나온 말풍선이
// 뒤에 오는 카드 글자에 덮이지 않도록, 마우스 · 포커스가 있는 카드를 통째로 형제 카드 위로 올린다.

import { cn } from '@/lib/utils';

export function ChartCard({
  title,
  description,
  aside,
  children,
  className,
  id,
}: {
  title: string;
  description?: string;
  aside?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <section
      id={id}
      className={cn('@container/chart bg-card ring-foreground/10 relative flex hover:z-10 focus-within:z-10 min-w-0 flex-col gap-4 rounded-xl p-4 ring-1 sm:p-6', className)}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex flex-col gap-0.5">
          <h2 className="text-base font-bold">{title}</h2>
          {description ? <p className="text-muted-foreground text-[13px]">{description}</p> : null}
        </div>
        {aside}
      </div>
      {children}
    </section>
  );
}
