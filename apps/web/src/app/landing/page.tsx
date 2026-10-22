// 랜딩(Post-MVP T-15): 로그인 전 첫 화면. 셸 · 실시간 연결 없이 그린다(components/app-shell.tsx).
//
// 순서는 "무엇을 하는가(첫 화면) → 어떻게 하는가(3단계) → 무엇을 얻는가(기능) → 믿을 수 있는가(숫자 · 보안) →
// 로그인"이다. 숫자는 확정 문서(SSOT)에 있는 값만 쓴다. 고객사 로고 · 도입 사례처럼 아직 없는 것은 만들지 않는다.
// 움직임은 첫 화면 등장, 스크롤 등장, 숫자 세기, 지원 자원 띠 흐름 네 가지이며 모두 모션 줄이기에서 멈춘다.

import {
  ArrowRight,
  Eye,
  KeyRound,
  ListChecks,
  RotateCcw,
  ScanSearch,
  Shield,
  ShieldCheck,
  Sparkles,
  Unplug,
  Wallet,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import type { Metadata } from 'next';
import Link from 'next/link';

import { CountUp } from '@/components/landing/count-up';
import { HeroPreview } from '@/components/landing/hero-preview';
import { RevealRoot } from '@/components/landing/reveal-root';

export const metadata: Metadata = {
  title: 'Vigilantis · 24시간 AWS 클라우드 관제',
  description: 'AWS 자산과 보안 위협을 24시간 지켜보고, 검증된 조치만 실행하며, 실패하면 스스로 되돌립니다.',
};

// 수집 유형(assets.py AssetType 7종)과 지표 원천. 수집하지 않는 것은 넣지 않는다.
const RESOURCES = ['EC2 인스턴스', '보안 그룹', 'EBS 볼륨', '네트워크 ACL', 'Auto Scaling 그룹', '시작 템플릿', 'ALB 대상 그룹', 'CloudWatch 지표'];

const STEPS: { icon: LucideIcon; title: string; body: string }[] = [
  {
    icon: ScanSearch,
    title: '감지',
    body: '정해진 주기로 고객 계정의 자산을 모으고, 판정 규칙이 위협과 낭비를 가려냅니다.',
  },
  {
    icon: Sparkles,
    title: '판단',
    body: 'AI가 근거를 세 줄로 요약하고 조치 런북을 추천합니다. 추천은 4단계 검증을 통과해야 화면에 오릅니다.',
  },
  {
    icon: RotateCcw,
    title: '조치와 원복',
    body: '승인 한 번으로 실행하고, 위험도가 높으면 먼저 격리합니다. 실행 뒤 상태 확인이 실패하면 직전 상태로 되돌립니다.',
  },
];

const FEATURES: { icon: LucideIcon; title: string; body: string; points: string[] }[] = [
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
  {
    icon: ListChecks,
    title: '4단계 실행 검증',
    body: 'AI가 추천해도 바로 실행하지 않습니다. 정해진 순서의 검증을 모두 통과해야 합니다.',
    points: ['입력 형식', '허용된 런북 목록', '대상 자원 일치', 'AWS 사전 실행(Dry-Run)'],
  },
  {
    icon: RotateCcw,
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
    body: '고객 계정에 만든 역할을 고객 전용 ExternalId와 함께 맡아 들어갑니다.',
  },
  {
    icon: Unplug,
    title: '언제든 연결을 끊을 수 있습니다',
    body: '구독을 멈추면 플랫폼은 더 들어가지 않고, 역할은 고객이 소유합니다.',
  },
  {
    icon: ShieldCheck,
    title: '모든 로그인에 2단계 인증',
    body: '관리자를 포함한 모든 계정이 인증 앱 코드를 입력해야 들어옵니다.',
  },
];

export default function LandingPage() {
  return (
    <RevealRoot className="bg-background flex flex-1 flex-col">
      {/* ── 머리 · 첫 화면 ── */}
      <section className="dark bg-shell text-foreground relative overflow-hidden">
        <div aria-hidden className="landing-grid pointer-events-none absolute inset-0" />
        <div aria-hidden className="landing-glow pointer-events-none absolute -top-40 right-[-10%] size-[640px]" />

        <header className="relative mx-auto flex h-16 w-full max-w-6xl items-center justify-between px-4 sm:px-6">
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
            href="/login"
            className="bg-primary inline-flex h-10 items-center rounded-md px-4 text-sm font-semibold text-white hover:bg-primary/90"
          >
            로그인
          </Link>
        </header>

        <div className="relative mx-auto grid w-full max-w-6xl items-center gap-14 px-4 pt-14 pb-24 sm:px-6 lg:grid-cols-[1.05fr_1fr] lg:pt-20 lg:pb-32">
          <div className="flex flex-col gap-7">
            <p className="landing-rise text-accent-foreground inline-flex w-fit items-center gap-2 rounded-full border border-white/15 px-3 py-1 text-[13px]">
              <span className="landing-pulse size-2 rounded-full bg-[#22c55e]" />
              AWS 클라우드 24시간 관제
            </p>
            <h1
              className="landing-rise text-4xl leading-[1.15] font-bold tracking-tight sm:text-5xl lg:text-[56px]"
              style={{ '--rise-delay': '80ms' } as React.CSSProperties}
            >
              잠든 시간에도
              <br />
              클라우드를 지킵니다
            </h1>
            <p
              className="landing-rise text-foreground-secondary max-w-xl text-lg leading-relaxed"
              style={{ '--rise-delay': '160ms' } as React.CSSProperties}
            >
              Vigilantis는 AWS 자산과 보안 위협을 쉬지 않고 지켜봅니다. 검증을 통과한 조치만 실행하고, 실패하면
              스스로 직전 상태로 되돌립니다.
            </p>
            <div className="landing-rise flex flex-wrap gap-3" style={{ '--rise-delay': '240ms' } as React.CSSProperties}>
              <Link
                href="/login"
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
          <div className="landing-rise" style={{ '--rise-delay': '320ms' } as React.CSSProperties}>
            <HeroPreview />
          </div>
        </div>
      </section>

      {/* ── 관제 대상 자원 띠: 흐르게 하면 멈춤 장치가 필요해(WCAG 2.2.2) 고정으로 둔다 ── */}
      <section aria-label="관제 대상 자원" className="bg-card border-b py-5">
        <ul className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-center gap-x-10 gap-y-3 px-4 sm:px-6">
          {RESOURCES.map((r) => (
            <li key={r} className="text-muted-foreground flex items-center gap-2.5 text-sm font-semibold whitespace-nowrap">
              <span aria-hidden className="bg-data size-1.5 rounded-full" />
              {r}
            </li>
          ))}
        </ul>
      </section>

      {/* ── 동작 방식 ── */}
      <section id="how" className="mx-auto w-full max-w-6xl scroll-mt-8 px-4 py-24 sm:px-6">
        <div data-reveal className="mx-auto flex max-w-2xl flex-col gap-3 text-center">
          <p className="text-primary text-sm font-bold">동작 방식</p>
          <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">감지에서 원복까지 한 흐름으로</h2>
          <p className="text-muted-foreground text-lg">사람은 승인만 하면 됩니다. 급한 위협은 기다리지 않습니다.</p>
        </div>
        <ol className="relative mt-14 grid gap-6 md:grid-cols-3">
          <div aria-hidden className="bg-border absolute top-9 right-[16%] left-[16%] hidden h-px md:block" />
          {STEPS.map((s, i) => (
            <li
              key={s.title}
              data-reveal
              style={{ '--reveal-delay': `${i * 120}ms` } as React.CSSProperties}
              className="bg-card ring-foreground/10 relative flex flex-col gap-4 rounded-2xl p-7 ring-1"
            >
              <span className="bg-accent text-accent-foreground relative flex size-12 items-center justify-center rounded-xl">
                <s.icon aria-hidden className="size-6" />
              </span>
              <div className="flex items-baseline gap-2">
                <span className="text-muted-foreground font-mono text-sm">0{i + 1}</span>
                <h3 className="text-xl font-bold">{s.title}</h3>
              </div>
              <p className="text-foreground-secondary leading-relaxed">{s.body}</p>
            </li>
          ))}
        </ol>
      </section>

      {/* ── 기능 ── */}
      <section id="features" className="bg-secondary scroll-mt-8 py-24">
        <div className="mx-auto w-full max-w-6xl px-4 sm:px-6">
          <div data-reveal className="flex max-w-2xl flex-col gap-3">
            <p className="text-primary text-sm font-bold">기능</p>
            <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">위협은 막고, 낭비는 줄입니다</h2>
          </div>
          <div className="mt-12 grid gap-5 md:grid-cols-2">
            {FEATURES.map((f, i) => (
              <article
                key={f.title}
                data-reveal
                style={{ '--reveal-delay': `${(i % 2) * 120}ms` } as React.CSSProperties}
                className="bg-card ring-foreground/10 group flex flex-col gap-4 rounded-2xl p-7 ring-1 transition-shadow hover:shadow-lg"
              >
                <span className="bg-primary flex size-11 items-center justify-center rounded-xl text-white transition-transform group-hover:-translate-y-0.5">
                  <f.icon aria-hidden className="size-5.5" />
                </span>
                <h3 className="text-xl font-bold">{f.title}</h3>
                <p className="text-foreground-secondary leading-relaxed">{f.body}</p>
                <ul className="mt-1 flex flex-col gap-2">
                  {f.points.map((p) => (
                    <li key={p} className="flex items-center gap-2.5 text-[15px]">
                      <Zap aria-hidden className="text-primary size-4 shrink-0" />
                      {p}
                    </li>
                  ))}
                </ul>
              </article>
            ))}
          </div>
        </div>
      </section>

      {/* ── 숫자 ── */}
      <section aria-label="Vigilantis 숫자" className="dark bg-shell text-foreground relative overflow-hidden py-20">
        <div aria-hidden className="landing-grid pointer-events-none absolute inset-0 opacity-70" />
        <dl className="relative mx-auto grid w-full max-w-6xl gap-10 px-4 sm:grid-cols-2 sm:px-6 lg:grid-cols-4">
          {STATS.map((s, i) => (
            <div
              key={s.label}
              data-reveal
              style={{ '--reveal-delay': `${i * 100}ms` } as React.CSSProperties}
              className="flex flex-col gap-2"
            >
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
      <section id="trust" className="mx-auto w-full max-w-6xl scroll-mt-8 px-4 py-24 sm:px-6">
        <div data-reveal className="flex max-w-2xl flex-col gap-3">
          <p className="text-primary text-sm font-bold">보안</p>
          <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">맡긴 계정을 함부로 다루지 않습니다</h2>
        </div>
        <div className="mt-12 grid gap-5 md:grid-cols-3">
          {TRUST.map((t, i) => (
            <div
              key={t.title}
              data-reveal
              style={{ '--reveal-delay': `${i * 120}ms` } as React.CSSProperties}
              className="flex flex-col gap-3 border-t-2 border-primary pt-6"
            >
              <t.icon aria-hidden className="text-primary size-7" />
              <h3 className="text-lg font-bold">{t.title}</h3>
              <p className="text-foreground-secondary leading-relaxed">{t.body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* ── 마무리 ── */}
      <section className="px-4 pb-24 sm:px-6">
        <div
          data-reveal
          className="dark bg-shell text-foreground relative mx-auto flex w-full max-w-6xl flex-col items-start gap-6 overflow-hidden rounded-3xl p-10 sm:p-14 md:flex-row md:items-center md:justify-between"
        >
          <div aria-hidden className="landing-glow pointer-events-none absolute -right-20 -bottom-40 size-[420px]" />
          <div className="relative flex flex-col gap-2">
            <h2 className="text-3xl font-bold tracking-tight">오늘 밤부터 맡겨 보세요</h2>
            <p className="text-foreground-secondary text-lg">로그인하면 내 계정의 자산과 위협이 바로 보입니다.</p>
          </div>
          <Link
            href="/login"
            className="bg-primary relative inline-flex h-12 shrink-0 items-center gap-2 rounded-md px-6 text-base font-semibold text-white hover:bg-primary/90"
          >
            <Eye aria-hidden className="size-4.5" />
            대시보드 보기
          </Link>
        </div>
      </section>

      <footer className="border-t">
        <div className="text-muted-foreground mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-4 px-4 py-8 text-sm sm:px-6">
          <span>© Vigilantis · 딸깍 인프라</span>
          {/* 처리방침 · 약관은 T-15 몫으로 아직 문서가 없다. 링크처럼 보이지 않게 둔다. */}
          <span className="flex gap-5">
            <span>개인정보처리방침 (준비 중)</span>
            <span>이용약관 (준비 중)</span>
          </span>
        </div>
      </footer>
    </RevealRoot>
  );
}
