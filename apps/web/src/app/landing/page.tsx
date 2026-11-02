// 랜딩(Post-MVP T-15): 로그인 전 첫 화면. 셸 · 실시간 연결 없이 그린다(components/app-shell.tsx).
//
// 순서는 "무엇을 하는가(첫 화면) → 어떻게 하는가(흐름) → 무엇을 얻는가(기능) → 믿을 수 있는가(숫자 · 보안) →
// 로그인"이다. 숫자는 확정 문서(SSOT)에 있는 값만 쓴다. 고객사 로고 · 도입 사례처럼 아직 없는 것은 만들지 않는다.
// 화면 전체가 관제실이다: 격자와 회전 지구(대륙 · AWS 리전 · 서울 관제 지점)를 화면에 고정해 깔고,
// 섹션은 그 위로 보통 스크롤로 지나간다. 화면 가운데를 차지한 섹션만 또렷하고, 가운데에서 벗어난 섹션은
// 흐리고 옅게 둬서 지금 읽는 섹션을 강조한다(panel-focus.tsx).
// 반투명 막은 지구가 비치면서도 글이 읽히게 하려는 것이다. 배경 흐림(backdrop-filter)은 쓰지 않는다(panel-focus.tsx).
// 격자 · 첫 화면 등장 · 떠다니기는 antislop 감사(2026-10-29)에서 뺐다가 PM 지시로 되살렸다(R-07 · R-19 예외).
// 스크롤 등장은 박스가 스크롤보다 늦게 따라 올라와 밀리는 느낌을 줘서 다시 뺐다(globals.css).
// 반복 움직임은 [움직임 멈춤]으로 멈추고, 모든 움직임이 모션 줄이기 설정에서 멈춘다.

import { ArrowRight, Eye, KeyRound, Shield, ShieldCheck, Unplug, Wallet, type LucideIcon } from 'lucide-react';
import type { Metadata } from 'next';
import Link from 'next/link';
import { Suspense } from 'react';

import { CountUp } from '@/components/landing/count-up';
import { GlobeBackdrop } from '@/components/landing/globe-backdrop';
import { HeroPreview } from '@/components/landing/hero-preview';
import { LoginOverlay } from '@/components/landing/login-overlay';
import { PanelFocus } from '@/components/landing/panel-focus';
import { LegalLinks } from '@/components/legal/legal-document';
import { LOGIN_OVERLAY_HREF } from '@/lib/return-to';

export const metadata: Metadata = {
  title: 'Vigilantis · 24시간 AWS 클라우드 관제',
  description: 'AWS 자산과 보안 위협을 24시간 지켜보고, 검증된 조치만 실행하며, 실패하면 스스로 되돌립니다.',
};

// 수집 유형(assets.py AssetType 7종)과 지표 원천. 수집하지 않는 것은 넣지 않는다.
const RESOURCES = ['EC2 인스턴스', '보안 그룹', 'EBS 볼륨', '네트워크 ACL', 'Auto Scaling 그룹', '시작 템플릿', 'ALB 대상 그룹', 'CloudWatch 지표'];

// 흐름의 각 단계가 "언제" 일어나는지를 머리에 둔다. 번호 대신 시점이 단계를 가른다.
const STEPS: { when: string; title: string; body: string }[] = [
  {
    when: '수집 주기마다',
    title: '감지',
    body: '정해진 주기로 고객사 AWS 계정의 자산을 모으고, 판정 규칙이 위협과 낭비를 가려냅니다.',
  },
  {
    when: '위협이나 낭비가 잡히면',
    title: '판단',
    body: 'AI가 근거를 세 줄로 요약하고 조치 런북을 추천합니다. 추천은 4단계 검증을 통과해야 화면에 오릅니다.',
  },
  {
    when: '승인하면 바로, 급하면 그 전에',
    title: '조치와 원복',
    body: '승인 한 번으로 실행하고, 위험도가 높으면 먼저 격리합니다. 실행 뒤 상태 확인이 실패하면 직전 상태로 되돌립니다.',
  },
];

// 고객이 얻는 결과(보안 · 비용)가 주인공이고, 검증 · 회복은 모든 조치에 붙는 안전장치라 한 단계 낮춰 그린다.
const OUTCOMES: { icon: LucideIcon; title: string; body: string; points: string[] }[] = [
  {
    icon: Shield,
    title: '보안 위협 대응',
    body: '인터넷에 열린 포트와 무차별 대입 시도를 찾아 차단합니다.',
    points: ['위험도 높음은 먼저 격리', '위험도 중간은 1분 안에 응답이 없으면 자동 격리', '차단은 원클릭으로 해제'],
  },
  {
    icon: Wallet,
    title: '비용 절감',
    body: '쓰지 않는 볼륨과 놀고 있는 인스턴스를 찾아 월 절감액과 함께 제안합니다.',
    points: ['미사용 EBS 정리', '저활성 EC2 스펙 축소', '제안마다 예상 절감액 표시'],
  },
];

const SAFEGUARDS: { title: string; body: string; points: string[] }[] = [
  {
    title: '4단계 실행 검증',
    body: 'AI가 추천해도 바로 실행하지 않습니다. 정해진 순서의 검증을 모두 통과해야 합니다.',
    points: ['입력 형식', '허용된 런북 목록', '대상 자원 일치', 'AWS 사전 실행(Dry-Run)'],
  },
  {
    title: '양방향 회복',
    body: '실행 전 상태를 기록해 두고, 실패하면 자동으로, 필요하면 원클릭으로 되돌립니다.',
    points: ['실행 전 스펙 백업', '상태 확인 실패 시 자동 원복', '복구 결과를 따로 기록'],
  },
];

const STATS: { value: number; suffix: string; label: string; note: string }[] = [
  { value: 24, suffix: '시간', label: '상시 관제', note: '로그인하지 않아도 수집 · 탐지 · 자동 대응은 계속됩니다' },
  { value: 10, suffix: '종', label: '조치 런북', note: '본편 7종과 되돌리기 3종' },
  { value: 4, suffix: '단계', label: '실행 검증', note: '모든 조치가 같은 순서로 통과합니다' },
  { value: 3, suffix: '초', label: '선차단 목표', note: '위험도 높음 위협을 격리하기까지' },
];

const TRUST: { icon: LucideIcon; title: string; body: string }[] = [
  {
    icon: KeyRound,
    title: '장기 키를 받지 않습니다',
    body: '고객사 AWS 계정에 만든 역할을 고객사 전용 ExternalId와 함께 맡아 들어갑니다.',
  },
  {
    icon: Unplug,
    title: '언제든 연결을 끊을 수 있습니다',
    body: '구독을 멈추면 플랫폼은 더 들어가지 않고, 역할은 고객사가 소유합니다.',
  },
  {
    icon: ShieldCheck,
    title: '모든 로그인에 2단계 인증',
    body: '관리자를 포함한 모든 계정이 인증 앱 코드를 입력해야 들어옵니다.',
  },
];

// 섹션: 내용 높이만큼만 차지해 섹션끼리 붙어 이어져 보이게 한다. 배경은 투명.
const PANEL = 'relative flex flex-col';
// 섹션 안의 박스.
const BOX = 'bg-card/60 ring-1 ring-white/10';

export default function LandingPage() {
  return (
    <div className="dark bg-shell text-foreground relative isolate flex flex-1 flex-col">
      {/* ── 고정 배경: 격자 · 지구 · 글이 읽히게 덮는 반투명 막. 스크롤해도 제자리 ── */}
      <div aria-hidden className="pointer-events-none fixed inset-0 -z-10">
        <div className="landing-grid absolute inset-0" />
        {/* 좁은 화면은 글이 지구 위를 지나가므로 지구를 한 단계 흐리게 둔다. */}
        <GlobeBackdrop className="absolute inset-0 size-full opacity-60 sm:opacity-100" />
        <div className="bg-shell/30 absolute inset-0" />
      </div>
      {/* 화면 가운데의 섹션은 또렷하게, 벗어난 섹션은 흐리게(data-panel · data-panel-body). */}
      <PanelFocus />
      {/* 로그인 버튼(`?login=1`)을 누르면 이 위로 로그인 창이 화면을 덮는다. 주소의 검색값을 읽어 Suspense로 감싼다. */}
      <Suspense fallback={null}>
        <LoginOverlay />
      </Suspense>
      {/* ── 첫 화면 ── */}
      <section data-panel className="relative flex min-h-svh flex-col">
        <div data-panel-body className="flex flex-1 flex-col">
          <header className="mx-auto flex h-16 w-full max-w-6xl items-center justify-between px-4 sm:px-6">
            <Link href="/landing" className="flex items-center gap-2.5">
              <span className="bg-primary flex size-8 items-center justify-center rounded-md">
                <Shield aria-hidden className="size-4.5 text-white" />
              </span>
              <span className="text-lg font-bold tracking-tight">Vigilantis</span>
            </Link>
            <nav aria-label="랜딩 목차" className="text-foreground-secondary hidden items-center gap-7 text-sm md:flex">
              <a href="#how" className="hover:text-foreground">동작 방식</a>
              <a href="#features" className="hover:text-foreground">기능</a>
              <a href="#trust" className="hover:text-foreground">보안</a>
            </nav>
            <Link
              href={LOGIN_OVERLAY_HREF}
              scroll={false}
              className="bg-primary inline-flex h-10 items-center rounded-md px-4 text-sm font-semibold text-white hover:bg-primary/90"
            >
              로그인
            </Link>
          </header>

          {/* 한 줄 배치에서도 칸 폭을 화면에 묶는다. 기본(auto)이면 미리보기의 줄바꿈 없는 이벤트 줄이 칸을 넓혀 390px에서 가로로 넘친다. */}
          <div className="mx-auto grid w-full max-w-6xl flex-1 grid-cols-[minmax(0,1fr)] items-center gap-14 px-4 py-14 sm:px-6 lg:grid-cols-[1.05fr_1fr]">
            <div className="flex flex-col gap-7">
              <h1 className="landing-rise text-4xl leading-[1.15] font-bold tracking-tight sm:text-5xl lg:text-[56px]">
                잠든 시간에도
                <br />
                클라우드를 지킵니다
              </h1>
              <p
                className="landing-rise text-foreground-secondary max-w-xl text-lg leading-relaxed"
                style={{ '--rise-delay': '80ms' } as React.CSSProperties}
              >
                Vigilantis는 AWS 자산과 보안 위협을 쉬지 않고 지켜봅니다. 검증을 통과한 조치만 실행하고, 실패하면
                스스로 직전 상태로 되돌립니다.
              </p>
              <div className="landing-rise flex flex-wrap gap-3" style={{ '--rise-delay': '160ms' } as React.CSSProperties}>
                <Link
                  href={LOGIN_OVERLAY_HREF}
                  scroll={false}
                  className="bg-primary inline-flex h-12 items-center gap-2 rounded-md px-6 text-base font-semibold text-white hover:bg-primary/90"
                >
                  로그인하고 시작하기
                  <ArrowRight aria-hidden className="size-4" />
                </Link>
                <a
                  href="#how"
                  className="inline-flex h-12 items-center rounded-md border border-white/20 px-6 text-base font-semibold hover:bg-white/8"
                >
                  동작 방식 보기
                </a>
              </div>
            </div>
            <div className="landing-rise" style={{ '--rise-delay': '240ms' } as React.CSSProperties}>
              <HeroPreview />
            </div>
          </div>

          {/* 관제 대상 자원 띠: 흐르게 하면 멈춤 장치가 필요해(WCAG 2.2.2) 고정으로 둔다. 첫 화면 바닥에 둔다. */}
          <ul
            aria-label="관제 대상 자원"
            className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-center gap-x-10 gap-y-3 border-t border-white/10 px-4 py-6 sm:px-6"
          >
            {RESOURCES.map((r) => (
              <li key={r} className="text-muted-foreground flex items-center gap-2.5 text-sm font-semibold whitespace-nowrap">
                <span aria-hidden className="bg-data size-1.5 rounded-full" />
                {r}
              </li>
            ))}
          </ul>
        </div>
      </section>

      {/* ── 동작 방식: 왼쪽 제목, 오른쪽 세로 흐름선. 단계는 번호가 아니라 일어나는 시점으로 가른다 ── */}
      <section data-panel id="how" className={PANEL}>
        <div data-panel-body className="mx-auto grid w-full max-w-6xl grid-cols-[minmax(0,1fr)] gap-12 px-4 py-10 sm:px-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
          <div className="flex flex-col gap-3">
            <p className="text-primary text-sm font-bold">동작 방식</p>
            <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">감지에서 원복까지 한 흐름으로</h2>
            <p className="text-muted-foreground text-lg">
              사람은 승인만 하면 되고, 급한 위협은 승인을 기다리지 않고 먼저 격리합니다.
            </p>
          </div>
          <ol className="ml-1.5 flex flex-col border-l-2">
            {STEPS.map((s) => (
              <li key={s.title} className="relative pb-4 pl-8 last:pb-0">
                <span
                  aria-hidden
                  className="bg-primary ring-shell absolute top-7 -left-[7px] size-3 rounded-[3px] ring-4"
                />
                <div className={`${BOX} rounded-2xl p-6`}>
                  <p className="text-primary text-sm font-semibold">{s.when}</p>
                  <h3 className="mt-1 text-xl font-bold">{s.title}</h3>
                  <p className="text-foreground-secondary mt-2 max-w-xl leading-relaxed">{s.body}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      {/* ── 기능: 결과 둘은 큰 박스, 안전장치 둘은 한 판을 나눠 쓴다 ── */}
      <section data-panel id="features" className={PANEL}>
        <div data-panel-body className="mx-auto w-full max-w-6xl px-4 py-10 sm:px-6">
          <div className="flex max-w-2xl flex-col gap-3">
            <p className="text-primary text-sm font-bold">기능</p>
            <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">위협은 막고, 낭비는 줄입니다</h2>
          </div>
          <div className="mt-10 grid gap-5 md:grid-cols-2">
            {OUTCOMES.map((f) => (
              <article key={f.title} className={`${BOX} flex flex-col gap-4 rounded-2xl p-7`}>
                <span className="bg-primary flex size-11 items-center justify-center rounded-xl text-white">
                  <f.icon aria-hidden className="size-5.5" />
                </span>
                <h3 className="text-2xl font-bold">{f.title}</h3>
                <p className="text-foreground-secondary text-lg leading-relaxed">{f.body}</p>
                <ul className="mt-1 flex flex-col gap-2">
                  {f.points.map((p) => (
                    <li key={p} className="flex items-center gap-2.5 text-[15px]">
                      <span aria-hidden className="bg-data size-1.5 shrink-0 rounded-full" />
                      {p}
                    </li>
                  ))}
                </ul>
              </article>
            ))}
          </div>

          <div className={`${BOX} mt-5 rounded-2xl`}>
            <p className="text-muted-foreground border-b px-7 py-4 text-sm font-semibold">모든 조치에 붙는 안전장치</p>
            <div className="grid divide-y md:grid-cols-2 md:divide-x md:divide-y-0">
              {SAFEGUARDS.map((g) => (
                <div key={g.title} className="flex flex-col gap-2 px-7 py-6">
                  <h3 className="text-lg font-bold">{g.title}</h3>
                  <p className="text-foreground-secondary leading-relaxed">{g.body}</p>
                  <p className="text-muted-foreground text-sm">{g.points.join(' · ')}</p>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* ── 숫자 ── */}
      <section data-panel aria-label="Vigilantis 숫자" className={PANEL}>
        <dl data-panel-body className="mx-auto grid w-full max-w-6xl gap-5 px-4 py-10 sm:grid-cols-2 sm:px-6 lg:grid-cols-4">
          {STATS.map((s) => (
            <div key={s.label} className={`${BOX} flex flex-col gap-2 rounded-2xl p-7`}>
              <dt className="text-foreground-secondary order-2 text-base font-semibold">{s.label}</dt>
              <dd className="order-1 text-5xl font-bold tracking-tight">
                <CountUp value={s.value} suffix={s.suffix} />
              </dd>
              <dd className="text-muted-foreground order-3 text-sm">{s.note}</dd>
            </div>
          ))}
        </dl>
      </section>

      {/* ── 보안 ── */}
      <section data-panel id="trust" className={PANEL}>
        <div data-panel-body className="mx-auto w-full max-w-6xl px-4 py-10 sm:px-6">
          <div className="flex max-w-2xl flex-col gap-3">
            <p className="text-primary text-sm font-bold">보안</p>
            <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">맡긴 계정을 함부로 다루지 않습니다</h2>
          </div>
          <div className="mt-10 grid gap-5 md:grid-cols-3">
            {TRUST.map((t) => (
              <div key={t.title} className={`${BOX} flex flex-col gap-3 rounded-2xl border-t-2 border-primary p-6`}>
                <t.icon aria-hidden className="text-primary size-7" />
                <h3 className="text-lg font-bold">{t.title}</h3>
                <p className="text-foreground-secondary leading-relaxed">{t.body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── 마무리 · 바닥글: 마지막 판 ── */}
      <section data-panel className={PANEL}>
        <div data-panel-body className="flex w-full flex-1 flex-col">
          <div className="flex w-full flex-1 items-center px-4 pt-10 pb-16 sm:px-6">
            <div
              className={`${BOX} mx-auto flex w-full max-w-6xl flex-col items-start gap-6 rounded-3xl p-10 sm:p-14 md:flex-row md:items-center md:justify-between`}
            >
              <div className="flex flex-col gap-2">
                <h2 className="text-3xl font-bold tracking-tight">오늘 밤부터 맡겨 보세요</h2>
                <p className="text-foreground-secondary text-lg">로그인하면 내 계정의 자산과 위협이 바로 보입니다.</p>
              </div>
              <Link
                href={LOGIN_OVERLAY_HREF}
                scroll={false}
                className="bg-primary inline-flex h-12 shrink-0 items-center gap-2 rounded-md px-6 text-base font-semibold text-white hover:bg-primary/90"
              >
                <Eye aria-hidden className="size-4.5" />
                로그인하고 대시보드 보기
              </Link>
            </div>
          </div>
          <footer className="border-t border-white/10">
            <div className="text-muted-foreground mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-4 px-4 py-8 text-sm sm:px-6">
              <span>© Vigilantis · 딸깍 인프라</span>
              <LegalLinks />
            </div>
          </footer>
        </div>
      </section>
    </div>
  );
}
