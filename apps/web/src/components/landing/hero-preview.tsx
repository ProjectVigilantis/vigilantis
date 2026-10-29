// 랜딩 첫 화면의 제품 미리보기: 고객사 대시보드(components/customer-dashboard)를 줄여 그린 그림이다.
// 값은 예시이고 카드에 그렇게 적는다. 구성은 실제 화면을 따른다: 전반 지표 3개(숫자 카드 이름 그대로) →
// 실시간 지표(최근 24시간 위협 이벤트, 1시간 단위 막대) → 분류(보안 · 최적화)를 붙인 이벤트 줄.
// 움직임은 CSS만 쓴다(globals.css의 landing-*): 실시간 점의 맥박, 막대 올라오기, 이벤트 줄 흐름.
// 반복 움직임은 아래 [움직임 멈춤]으로 멈출 수 있다.

import { MotionToggle } from '@/components/landing/motion-toggle';
import { EnumBadge } from '@/components/status-badge';
import { CATEGORY_LABELS } from '@/lib/enum-labels';
import type { IncidentCategory } from '@/types/api';

const KPIS = [
  { label: '승인 대기 조치', value: '2', tone: 'text-badge-critical-fg' },
  { label: '처리 중인 보안 위협', value: '3', tone: 'text-foreground' },
  { label: '월 절감 가능액', value: '$101', tone: 'text-foreground' },
];

// 1시간 단위 발생 건수 24개. 마지막 칸이 지금 시각이다.
const HOURS = [1, 0, 2, 1, 0, 0, 1, 3, 2, 1, 4, 2, 1, 0, 2, 6, 3, 2, 1, 2, 5, 3, 2, 4];
const HOUR_MAX = Math.max(...HOURS);
const HOUR_TOTAL = HOURS.reduce((s, n) => s + n, 0);

const EVENTS: { time: string; category: IncidentCategory; text: string }[] = [
  { time: '09:12', category: 'SECOPS', text: 'SSH 무차별 대입 감지 · 인스턴스 자동 격리' },
  { time: '09:05', category: 'FINOPS', text: '미사용 EBS 발견 · 월 $8 절감 후보' },
  { time: '08:58', category: 'FINOPS', text: '스펙 축소 뒤 상태 확인 실패 · 자동 원복 완료' },
  { time: '08:41', category: 'SECOPS', text: '인터넷에 열린 22번 포트 · 차단 승인 대기' },
  { time: '08:30', category: 'FINOPS', text: '저활성 EC2 발견 · 스펙 축소 제안' },
  { time: '08:15', category: 'SECOPS', text: '격리 해제 승인 · 원클릭 원복 완료' },
];

export function HeroPreview() {
  return (
    // 카드는 그림 한 장으로 읽힌다. 이벤트 줄은 끊김 없이 흐르려고 두 번 그리므로 낭독기에는 감춘다.
    // 멈춤 버튼은 그림 밖에 둔다(role="img" 안의 버튼은 낭독기가 찾지 못한다).
    <div className="relative">
      <div
        role="img"
        aria-label="Vigilantis 고객사 대시보드 화면 예시: 전반 지표 3개, 최근 24시간 위협 이벤트 막대, 분류별 이벤트 목록"
        className="landing-float bg-card/75 overflow-hidden rounded-2xl shadow-2xl ring-1 shadow-black/40 ring-white/10"
      >
        <div aria-hidden>
          <div className="flex items-center justify-between border-b px-5 py-3">
            <span className="text-sm font-bold">고객사 대시보드</span>
            <span className="text-foreground-secondary inline-flex items-center gap-2 text-xs">
              <span className="landing-pulse size-2 rounded-full bg-[#22c55e]" />
              실시간 관제 중
            </span>
          </div>

          <div className="grid grid-cols-3 divide-x">
            {KPIS.map((k) => (
              <div key={k.label} className="flex flex-col justify-between gap-1 px-4 py-4 sm:px-5">
                <span className="text-muted-foreground text-[11px] leading-tight font-semibold">{k.label}</span>
                <span className={`text-2xl font-bold tabular-nums ${k.tone}`}>{k.value}</span>
              </div>
            ))}
          </div>

          <div className="border-t px-5 pt-4 pb-3">
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-muted-foreground text-[11px] font-semibold">최근 24시간 위협 이벤트 · 1시간 단위</span>
              <span className="text-foreground-secondary text-[11px]">
                합계 <strong className="text-foreground text-sm tabular-nums">{HOUR_TOTAL}</strong>건
              </span>
            </div>
            <div className="border-axis mt-3 flex h-16 items-end gap-0.5 border-b">
              {HOURS.map((n, i) => (
                <div
                  key={i}
                  className={`landing-grow w-full rounded-t-[2px] ${i === HOURS.length - 1 ? 'bg-primary' : 'bg-data'}`}
                  style={
                    {
                      height: n === 0 ? 0 : `${(n / HOUR_MAX) * 100}%`,
                      '--grow-delay': `${400 + i * 30}ms`,
                    } as React.CSSProperties
                  }
                />
              ))}
            </div>
            <div className="text-muted-foreground mt-1 flex justify-between text-[10px]">
              <span>24시간 전</span>
              <span>지금</span>
            </div>
          </div>

          {/* 위아래 가장자리는 마스크로 완전히 투명하게 지운다. 반투명 덮개만 두면 흐르는 줄이 구분선에 반쯤 걸린 채 비친다.
              마스크는 테두리까지 지우므로 구분선은 바깥 상자에 둔다. */}
          <div className="border-t">
            <div className="h-40 overflow-hidden [mask-image:linear-gradient(to_bottom,transparent_0,transparent_24px,black_46px,black_calc(100%-40px),transparent_calc(100%-14px))]">
              <ul className="landing-ticker flex flex-col">
                {[...EVENTS, ...EVENTS].map((e, i) => (
                  <li key={i} className="flex h-9 shrink-0 items-center gap-3 px-5 text-[13px]">
                    <span className="text-muted-foreground w-10 shrink-0 font-mono text-xs">{e.time}</span>
                    <EnumBadge entry={CATEGORY_LABELS[e.category]} className="w-12 shrink-0 justify-center" />
                    <span className="truncate">{e.text}</span>
                  </li>
                ))}
              </ul>
            </div>
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
