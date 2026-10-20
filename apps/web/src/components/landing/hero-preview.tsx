// 랜딩 첫 화면의 제품 미리보기: 관제 화면을 줄여 그린 그림이다. 값은 예시이고 카드에 그렇게 적는다.
// 움직임은 CSS만 쓴다(globals.css의 landing-*): 실시간 점의 맥박, 추이 선 그리기, 이벤트 줄 흐름.
// 반복 움직임은 아래 [움직임 멈춤]으로 멈출 수 있다.

import { MotionToggle } from '@/components/landing/motion-toggle';

const EVENTS = [
  { time: '09:12', tone: 'bg-status-critical', text: 'SSH 무차별 대입 감지 · 인스턴스 자동 격리' },
  { time: '09:05', tone: 'bg-status-warning', text: '미사용 EBS 발견 · 월 $8 절감 후보' },
  { time: '08:58', tone: 'bg-status-serious', text: '스펙 축소 뒤 상태 확인 실패 · 자동 원복 완료' },
  { time: '08:41', tone: 'bg-status-critical', text: '인터넷에 열린 22번 포트 · 차단 승인 대기' },
  { time: '08:30', tone: 'bg-status-good', text: '수집 완료 · 자산 22개 판정' },
  { time: '08:15', tone: 'bg-status-good', text: '연결 확인 성공 · 서울 리전' },
];

const KPIS = [
  { label: '승인 대기', value: '2', tone: 'text-badge-critical-fg' },
  { label: '위협 자산', value: '3', tone: 'text-foreground' },
  { label: '월 절감 후보', value: '$101', tone: 'text-foreground' },
];

export function HeroPreview() {
  return (
    // 카드는 그림 한 장으로 읽힌다. 이벤트 줄은 끊김 없이 흐르려고 두 번 그리므로 낭독기에는 감춘다.
    // 멈춤 버튼은 그림 밖에 둔다(role="img" 안의 버튼은 낭독기가 찾지 못한다).
    <div className="relative">
      <div
        role="img"
        aria-label="Vigilantis 고객사 대시보드 화면 예시"
        className="landing-float bg-card ring-foreground/10 overflow-hidden rounded-2xl shadow-2xl ring-1 shadow-black/40"
      >
        <div aria-hidden>
        <div className="flex items-center justify-between border-b px-5 py-3">
          <span className="text-sm font-bold">고객사 대시보드</span>
          <span className="text-foreground-secondary inline-flex items-center gap-2 text-xs">
            <span className="landing-pulse size-2 rounded-full bg-[#22c55e]" />
            실시간 관제 중
          </span>
        </div>

        <div className="grid grid-cols-3 gap-px bg-[var(--border)]">
          {KPIS.map((k) => (
            <div key={k.label} className="bg-card flex flex-col gap-1 px-5 py-4">
              <span className="text-muted-foreground text-[11px] font-semibold">{k.label}</span>
              <span className={`text-2xl font-bold ${k.tone}`}>{k.value}</span>
            </div>
          ))}
        </div>

        <div className="border-t px-5 pt-4 pb-2">
          <span className="text-muted-foreground text-[11px] font-semibold">최근 24시간 위협 이벤트</span>
          <svg viewBox="0 0 320 80" className="mt-2 h-20 w-full" aria-hidden preserveAspectRatio="none">
            <line x1="0" y1="78" x2="320" y2="78" stroke="var(--axis)" strokeWidth="1" />
            <path
              className="landing-draw"
              d="M0,70 L26,68 L53,72 L80,60 L106,64 L133,40 L160,52 L186,58 L213,30 L240,44 L266,56 L293,48 L320,52"
              fill="none"
              stroke="var(--data)"
              strokeWidth="2.5"
              strokeLinejoin="round"
              vectorEffect="non-scaling-stroke"
            />
          </svg>
        </div>

        <div className="relative h-36 overflow-hidden border-t">
          <ul className="landing-ticker flex flex-col">
            {[...EVENTS, ...EVENTS].map((e, i) => (
              <li key={i} className="flex h-9 shrink-0 items-center gap-3 px-5 text-[13px]">
                <span className="text-muted-foreground w-10 shrink-0 font-mono text-xs">{e.time}</span>
                <span aria-hidden className={`size-2 shrink-0 rounded-full ${e.tone}`} />
                <span className="truncate">{e.text}</span>
              </li>
            ))}
          </ul>
          <div className="from-card pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b to-transparent" />
          <div className="from-card pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-gradient-to-t to-transparent" />
        </div>
        </div>
      </div>
      <div className="mt-2 flex items-center justify-center gap-3">
        <p className="text-muted-foreground text-xs">화면 예시 · 값은 시연용입니다</p>
        <MotionToggle />
      </div>
    </div>
  );
}
