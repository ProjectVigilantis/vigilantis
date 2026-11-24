// 사용 가이드. 고객사 계정이 할 일을 메뉴 순서대로, 준비 → 순서 → 결과 → 다음 단계로 안내한다.
// 로그인 없이 열린다(lib/return-to.ts `PUBLIC_PATHS`).
//
// 공개 문서라 고객사 계정 메뉴만 싣는다(lib/home-path.ts `navPathsFor`의 고객사 분기, components/gnb.tsx `NAV`).
// 플랫폼 계정 메뉴는 내부 운영 화면이라 넣지 않는다.
// 화면 문구는 각 화면 파일에서 확인한 것만 적는다: 로그인 components/auth/login-flow.tsx, 인시던트
// components/incidents/incident-detail.tsx · action-execute-dialog.tsx · close-incident-dialog.tsx ·
// execution-status-panel.tsx와 lib/incident-closure.ts · lib/proposal-buttons.ts, 계정 components/users/users-view.tsx,
// 설정 components/settings/tenant-settings-view.tsx, 구독 components/subscriptions/subscription-view.tsx,
// 문의 components/support/inquiry-button.tsx, 화면 모드 components/settings-menu.tsx.
// 사양 조정이 인스턴스를 정지 → 유형 변경 → 기동한다는 것은 core-api services/aws/executor.py `execute_rightsizing`,
// 자동 원복과 결과 확인 불가의 갈림은 core-api workflows.py의 사양 조정 판정이 근거다. 그 동작이 바뀌면 이 문서도 고친다.
// 그림은 시연 데이터로 찍은 화면의 일부만 잘라 쓴다. 설명에는 잘린 그림에 보이는 것만 적는다.

import type { Metadata } from 'next';
import Link from 'next/link';

import { GuideFigure } from '@/components/guides/guide-figure';
import { LegalDocument, LegalList, LegalTable, type LegalSection } from '@/components/legal/legal-document';
import { EXECUTION_STATUS_LABELS, INCIDENT_STATUS_LABELS, RUNBOOK_LABELS } from '@/lib/enum-labels';
import { RESOLUTION_LABELS } from '@/lib/incident-closure';
import { SERVICE_NAME } from '@/lib/legal';
import { LOGIN_OVERLAY_HREF, ONBOARDING_GUIDE_PATH, SUPPORT_CENTER_PATH } from '@/lib/return-to';
import { ACCOUNT_KIND_LABELS, ACTION_FORBIDDEN, EVIDENCE_EXPORT_BLOCKED, VIEWER_ACTION_BLOCKED } from '@/lib/roles';
import { SETTINGS_READ_ONLY, SETTINGS_SAVED } from '@/lib/tenant-settings';
import { ROLE_LABELS } from '@/lib/user-admin';

export const metadata: Metadata = {
  title: `사용 가이드 · ${SERVICE_NAME}`,
  description: `${SERVICE_NAME} 고객사 계정으로 로그인부터 조치 승인 · 결과 확인 · 인시던트 종료까지 따라 하는 방법을 안내합니다.`,
};

const ADMIN = ACCOUNT_KIND_LABELS.TENANT_ADMIN;
const STAFF = ACCOUNT_KIND_LABELS.TENANT_STAFF;
const LINK = 'text-link hover:underline';

/** 상태 표시 이름표. 배지를 그리지 않는 값(null)은 이 문서가 쓰지 않으므로 키로 채운다. */
function labelsOf<K extends string>(map: Record<K, { label: string } | null>): Record<K, { label: string }> {
  return Object.fromEntries(
    (Object.keys(map) as K[]).map((key) => [key, { label: map[key]?.label ?? key }]),
  ) as Record<K, { label: string }>;
}

const S = labelsOf(INCIDENT_STATUS_LABELS);
const E = labelsOf(EXECUTION_STATUS_LABELS);
const NO_FURTHER = RESOLUTION_LABELS.NO_FURTHER_ACTION;

/**
 * 굵게 쓰는 곳은 두 가지뿐이다: 한 단계에서 눌러야 할 주 버튼, 그리고 다음 행동을 바꾸는 경고.
 * 메뉴 · 입력 칸 · 권한 · 상태 이름은 본문 그대로 쓴다.
 */
function Key({ children }: { children: React.ReactNode }) {
  return <strong className="text-foreground font-semibold">{children}</strong>;
}

/** 절 안의 할 일 하나. `id`가 있으면 다른 곳에서 그 할 일로 바로 건너뛸 수 있다. */
function Task({ id, title, children }: { id?: string; title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <h3 id={id} className="text-foreground scroll-mt-6 text-base font-semibold">
        {title}
      </h3>
      {children}
    </div>
  );
}

function Jump({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <a href={`#${to}`} className={LINK}>
      {children}
    </a>
  );
}

const SECTIONS: LegalSection[] = [
  {
    id: 'start',
    title: '1. 시작하기: 로그인과 계정 종류',
    tocTitle: '1. 시작하기',
    body: (
      <>
        <Task title="준비할 것">
          <LegalList
            items={[
              <>
                초대 메일. {ADMIN}은 직원을 초대하고 권한을 관리하는 회사의 계정입니다. 도입 절차에서 AWS 계정 연결이
                확인되면 도입 신청 담당자가 이 계정의 초대 메일을 받습니다(
                <Link href={ONBOARDING_GUIDE_PATH} className={LINK}>
                  도입 가이드
                </Link>
                ). 직원의 계정은 {ADMIN}으로 로그인한 담당자가 그 직원을 초대해 만듭니다(
                <Jump to="users">6절</Jump>). 초대 메일에 임시 비밀번호가 들어 있습니다.
              </>,
              '휴대전화에 설치한 인증 앱. 모든 계정이 권한과 관계없이 로그인할 때마다 인증 앱으로 2단계 인증을 거칩니다.',
              '초대 메일을 받은 메일함. 로그인 중에 이메일 확인 단계가 나오면 이 주소로 온 6자리 코드를 넣습니다.',
            ]}
          />
        </Task>
        <Task title="처음 로그인하기">
          <LegalList
            numbered
            items={[
              <>
                서비스 소개 화면 위쪽의 <Key>로그인</Key>을 눌러{' '}
                <Link href={LOGIN_OVERLAY_HREF} className={LINK}>
                  로그인 창
                </Link>
                을 엽니다.
              </>,
              <>
                이메일 칸에 이메일을, 비밀번호 칸에 초대 메일의 임시 비밀번호를 넣고 <Key>로그인</Key>을 누릅니다.
              </>,
              '새 비밀번호 설정 단계에서 임시 비밀번호를 새 비밀번호로 바꿉니다.',
              <>
                2단계 인증 등록 단계에서 인증 앱으로 QR 코드를 스캔하고, 앱에 나온 6자리 코드를 넣은 뒤{' '}
                <Key>등록 완료</Key>를 누릅니다. 스캔이 안 되면 ‘QR을 스캔할 수 없나요?’를 펼칩니다.
              </>,
              <>
                이어서 이메일 확인 단계가 나옵니다. 내 이메일로 온 6자리 코드를 이메일 인증 코드 칸에 넣고{' '}
                <Key>확인</Key>을 누릅니다. 이 단계까지 마쳐야 로그인이 끝납니다.
              </>,
            ]}
          />
        </Task>
        <Task title="다음부터 로그인하기">
          <LegalList
            numbered
            items={[
              <>
                이메일과 비밀번호를 넣고 <Key>로그인</Key>을 누릅니다.
              </>,
              <>
                2단계 인증 단계에서 인증 앱의 6자리 코드를 인증 코드 칸에 넣고 <Key>확인</Key>을 누릅니다.
              </>,
              '이메일 확인이 아직 기록되지 않은 계정이면 그다음에 이메일 확인 단계가 나옵니다. 처음 로그인할 때처럼 메일로 온 6자리 코드를 이메일 인증 코드 칸에 넣고 확인을 누릅니다. 이 단계가 나오지 않으면 2번에서 로그인이 끝납니다.',
            ]}
          />
          <GuideFigure
            id="3-28"
            alt="로그인 창의 2단계 인증 단계. 빈 인증 코드 입력란, 비활성 확인 버튼, 처음부터 다시 로그인 버튼이 있다."
            caption="코드를 넣기 전에는 확인 버튼이 눌리지 않습니다. 다른 계정으로 들어가려면 처음부터 다시 로그인을 누릅니다."
          />
          <p>
            로그인이 끝나면 대시보드가 열립니다. 다음은 <Jump to="dashboard">2절</Jump>입니다. 코드가 맞지 않거나
            비밀번호를 잊었다면 <Jump to="troubleshooting">10절</Jump>을 봅니다.
          </p>
        </Task>
        <Task title="계정 종류와 할 수 있는 일">
          <LegalTable
            head={['계정 · 권한', '할 수 있는 일']}
            rows={[
              [
                `${ADMIN} (권한 ${ROLE_LABELS.ADMIN})`,
                '모든 메뉴를 봅니다. 조치 실행 · 거절 · 인시던트 종료, 근거 내려받기, 문의 보내기, 직원 초대와 권한 변경, 고객사 설정과 구독 변경을 합니다.',
              ],
              [
                `${STAFF} · ${ROLE_LABELS.OPERATOR}`,
                <>
                  계정 메뉴를 뺀 화면을 보고, 고객사 설정과 구독은 볼 수만 있습니다. 조치 버튼은 눌리는 것처럼 보이지만, 실제로
                  할 수 있는 일은 권한 {ROLE_LABELS.OPERATOR}와 별도로 계정마다 받은 기능 권한만큼입니다. 조치 실행 기능
                  권한이 있어야 조치 실행 · 복원 · 근거 내려받기를, 인시던트 종료 기능 권한이 있어야 거절 · 종료를, 문의 등록
                  기능 권한이 있어야 문의 보내기를 할 수 있습니다. 계정 관리 화면에서 초대한 직원은 이 기능 권한 없이
                  시작합니다(<Jump to="users">6절</Jump>).
                </>,
              ],
              [
                `${STAFF} · ${ROLE_LABELS.VIEWER}`,
                '계정 메뉴를 뺀 화면을 볼 수만 있습니다. 조치 버튼은 숨겨지지 않고 비활성으로 보입니다.',
              ],
            ]}
          />
        </Task>
      </>
    ),
  },
  {
    id: 'dashboard',
    title: '2. 대시보드에서 처리할 일 찾기',
    tocTitle: '2. 대시보드',
    body: (
      <>
        <p>로그인하면 처음 보이는 화면입니다. 우리 회사 AWS 계정의 자산과 인시던트 현황을 모아 보여 줍니다.</p>
        <LegalList
          numbered
          items={[
            '왼쪽 사이드바에서 대시보드를 누릅니다.',
            <>
              제목 옆 관제 상태 알림에서 지금 상태를 한 줄로 봅니다. 처리할 건이 있으면 알림 끝의 <Key>보기</Key>로 그 목록에
              갑니다.
            </>,
            '숫자 타일을 누르면 그 숫자의 목록으로 갑니다. 관제 중 자산은 자산 목록, 월 절감 가능액은 자산 쪽 인시던트, 처리 중인 보안 위협은 보안 쪽 인시던트, AI 추천 조치는 승인을 기다리는 AI 제안으로 이어집니다.',
            'AI 추천 조치는 보안과 자산 양쪽에 승인 대기가 있으면 타일 자체가 눌리지 않습니다. 이때는 타일 아래 설명 줄의 보안 N건 또는 자산 N건 링크를 눌러 그쪽 목록으로 갑니다.',
            '아래쪽에서 유형별 자산 추이, 비용, 위협 이벤트를 봅니다.',
          ]}
        />
        <GuideFigure
          id="3-05"
          alt="대시보드 위쪽. 주의가 필요한 보안 항목이 1건 있다는 알림과 보기 링크, 관제 중 자산 16과 월 절감 가능액 $18.98(비용 제안 1건) 타일, 그 아래 자산 현황의 유형별 자산 추이와 최근 30일 비용."
          caption="월 절감 가능액을 누르면 비용 최적화 제안을 볼 수 있습니다. 화면의 금액은 예시입니다."
        />
        <p>
          다음 단계: 비용 절감 제안을 처리하려면 월 절감 가능액 타일을 누르거나, AI 추천 조치 타일을 누릅니다. AI 추천
          조치 타일이 눌리지 않으면 설명 줄의 자산 N건을 누릅니다. 그다음 <Jump to="incidents">4절</Jump>을 따라 합니다. 자산을 먼저 살펴보려면 <Jump to="monitoring">3절</Jump>로 갑니다.
        </p>
      </>
    ),
  },
  {
    id: 'monitoring',
    title: '3. 관제에서 자산과 위협 살펴보기',
    tocTitle: '3. 자산·보안 관제',
    body: (
      <>
        <LegalList
          numbered
          items={[
            '사이드바에서 관제를 누르고, 제목 줄에서 자산 또는 보안을 고릅니다. 자산은 자원 목록과 비용 최적화를, 보안은 그 자원의 노출과 위협을 봅니다.',
            '위쪽 지표 띠에서 자원 유형을 고르면 아래 내용이 그 유형으로 좁혀집니다.',
            '탭으로 보는 방식을 바꿉니다. 자산 쪽은 목록 · 요약 · 토폴로지 · 인시던트, 보안 쪽은 목록 · 토폴로지 · 인시던트 탭이 있습니다.',
            '목록 탭에서 자산을 고르면 상세가 열립니다. 상세의 연결된 인시던트에서 그 자산의 인시던트로 바로 갈 수 있습니다.',
            '인시던트 탭에서는 카드 아무 곳이나 누르면 인시던트 상세가 열립니다.',
          ]}
        />
        <GuideFigure
          id="3-06a"
          alt="관제 자산 목록 표. 자산, 판정, 헬스, 인시던트 열에 EC2와 보안 그룹 행이 있고, 판정 칸에 제외 · 활성 자산 · 최적화 후보 · 위협 · 확인 불가 표시가 있다. 자산 이름은 가려져 있다."
          caption="판정 칸에서 최적화 후보나 위협으로 판정된 자산을, 인시던트 칸에서 연결된 인시던트 수를 바로 봅니다."
        />
        <p>
          다음 단계: 처리할 인시던트를 찾았다면 <Jump to="incidents">4절</Jump>로 갑니다.
        </p>
      </>
    ),
  },
  {
    id: 'incidents',
    title: '4. 인시던트 처리: 비용 최적화 제안을 예로',
    tocTitle: '4. 인시던트 처리',
    body: (
      <>
        <p>
          인시던트는 보통 {S.AWAITING_APPROVAL.label} → {S.ACTION_IN_PROGRESS.label} → {S.AWAITING_CLOSURE.label} →{' '}
          {S.RESOLVED.label} 순서로 바뀝니다. 진행할 수 없거나 결과를 판정할 수 없으면 {S.FAILED.label}가 됩니다. 아래는
          EC2 인스턴스 유형을 줄여 비용을 아끼는 제안을 예로 듭니다.
        </p>
        <p>
          준비: 이 조치 실행 · {RUNBOOK_LABELS.RUNBOOK_EC2_REVERT_SIZE}은 조치 실행 기능 권한이, 제안 거절하고 종료 ·{' '}
          {NO_FURTHER}는 인시던트 종료 기능 권한이 있는 계정이어야 서버가 받아들입니다. {ADMIN}은 둘 다 갖습니다. 권한이{' '}
          {ROLE_LABELS.OPERATOR}인 {STAFF}은 이 기능 권한을 따로 받은 만큼만 할 수 있고, 계정 관리 화면에서 새로 초대한
          직원은 받지 않은 상태입니다. 그런 계정에서는 버튼이 눌리는 것처럼 보여도 서버가 거절하므로, 조치와 종료는{' '}
          {ADMIN}으로 로그인한 담당자에게 맡깁니다. 권한이 {ROLE_LABELS.VIEWER}인 계정은 버튼이 비활성이라 내용만
          확인합니다.
        </p>

        <Task id="incidents-review" title="4-1. 제안 검토하기">
          <LegalList
            numbered
            items={[
              <>
                관제 → 자산 → 인시던트 탭(또는 대시보드의 월 절감 가능액 타일)에서 최적화 · {S.AWAITING_APPROVAL.label}{' '}
                카드를 눌러 상세를 엽니다.
              </>,
              '절감 예상 (AI 추정)에서 지금 비용, 조정 뒤 비용, 월 절감액을 봅니다. AI가 추정한 값이라 실제 청구액과 다를 수 있습니다.',
              '대상 자산과 인시던트 분석 요약을 읽고 제안 이유와 승인 전에 확인할 점을 봅니다. 근거 원문이 필요하면 근거 데이터의 Markdown 내려받기 · JSON 내려받기를 씁니다.',
              <>
                제안 조치에서 실행될 조치(예: {RUNBOOK_LABELS.RUNBOOK_EC2_RIGHTSIZING})와 바꿀 값(예:{' '}
                <code>target_instance_type</code>)을 확인합니다.
              </>,
            ]}
          />
          <GuideFigure
            id="3-07"
            alt="승인 대기 중인 비용 최적화 인시던트의 아래쪽. 분석 요약 세 줄, 근거 데이터의 Markdown · JSON 내려받기, 제안 조치 EC2 스펙 조정(target_instance_type t3.small), 이 조치 실행과 제안 거절하고 종료 버튼."
            caption="예시 요약은 CPU 사용률이 낮아 t3.medium을 t3.small로 줄이자는 제안이며, 승인 전에 메모리 여유와 서비스 영향을 확인하라고 적혀 있습니다."
          />
          <p>
            다음 단계: 제안대로 바꾸려면 이 조치 실행(<Jump to="incidents-approve">4-2</Jump>), 바꾸지 않기로 했다면 제안
            거절하고 종료(<Jump to="incidents-reject">4-3</Jump>)로 갑니다. 비용 최적화 제안은 누군가
            승인하기 전에는 실행되지 않으므로, 판단을 미뤄도 됩니다.
          </p>
        </Task>

        <Task id="incidents-approve" title="4-2. 승인하고 실행하기">
          <p>
            실행 전에 알아 둘 점: {RUNBOOK_LABELS.RUNBOOK_EC2_RIGHTSIZING}은 인스턴스를 정지하고 유형을 바꾼 뒤 다시
            시작합니다. 그동안 그 인스턴스의 서비스가 멈추므로 영향이 적은 시간에 실행합니다.
          </p>
          <LegalList
            numbered
            items={[
              <>
                <Key>이 조치 실행</Key>을 누릅니다.
              </>,
              '‘선택한 조치를 실행합니다’ 확인 창에서 대상, 자산, 인스턴스 유형(지금 유형), 실행할 런북에서 고른 조치와 바꿀 값을 확인합니다.',
              <>
                맞으면 <Key>실행</Key>을, 아니면 취소를 누릅니다. 조치는 실행 전 4단계 검증을 통과해야만 실행됩니다.
              </>,
              <>
                접수되면 인시던트가 {S.ACTION_IN_PROGRESS.label}, 아래 실행 상태가 {E.IN_PROGRESS.label}으로 바뀝니다.
                실행이 끝날 때까지 새 실행과 종료는 막힙니다.
              </>,
            ]}
          />
          <GuideFigure
            id="3-17"
            alt="선택한 조치를 실행합니다 확인 창. 대상 자산, 인스턴스 유형, 실행할 런북 EC2 스펙 조정과 바꿀 인스턴스 유형, 취소와 실행 버튼."
            caption="지금 유형과 바꿀 유형을 이 창에서 마지막으로 비교합니다. 실행을 누르기 전에는 아무것도 바뀌지 않습니다."
          />
          <p>
            실행이 끝나면 수행된 조치의 {RUNBOOK_LABELS.RUNBOOK_EC2_RIGHTSIZING} 원래 실행 줄에 나온 상태에 따라 다음 단계가
            갈립니다. 아래 표는 그 원래 실행의 상태입니다. 나중에 직접 누른 {RUNBOOK_LABELS.RUNBOOK_EC2_REVERT_SIZE}이
            실패하면 원래 실행은 {E.ROLLBACK_FAILED.label}로 바뀌므로 그 행을 봅니다.
          </p>
          <LegalTable
            head={['실행 상태', '뜻', '다음 단계']}
            rows={[
              [
                E.SUCCESS.label,
                '제안대로 바뀌었습니다.',
                <Jump key="next" to="incidents-success">4-4 확인하고 종료하기</Jump>,
              ],
              [
                E.ROLLED_BACK.label,
                '실행 뒤 확인에서 문제가 나와 시스템이 이전 유형으로 되돌렸고, 되돌리기까지 끝났습니다.',
                <Jump key="next" to="incidents-rollback">4-5 자동 원복이 확인된 경우</Jump>,
              ],
              [
                E.UNVERIFIED.label,
                'AWS 상태를 읽지 못해 결과를 판정하지 못했습니다. 바뀌었는지 알 수 없고, 자동으로 되돌리지도 않았습니다. 지금 상태는 AWS에서 직접 확인해야 합니다.',
                <Jump key="next" to="incidents-unverified">4-6 결과를 확인할 수 없는 경우</Jump>,
              ],
              [
                E.FAILED.label,
                '원래 실행이 실패했습니다. 실행에 쓴 제안은 이미 쓴 것으로 처리되어 같은 제안으로 다시 실행할 수 없습니다.',
                <>
                  필요하면 실패 이유를 확인하고, 회사 AWS 계정에서 EC2를 조회할 권한이 있는 담당자에게 인스턴스의 지금 상태와
                  유형을 확인받습니다. 인시던트가 {S.AWAITING_APPROVAL.label}로 돌아와 남은 제안이 있으면 그 제안을
                  검토합니다(<Jump to="incidents-review">4-1</Jump>). 남은 제안이 없어 {S.FAILED.label}가 됐으면 확인을 마친 뒤
                  인시던트 종료의 {NO_FURTHER}로 마무리합니다.
                </>,
              ],
              [
                E.ROLLBACK_FAILED.label,
                '바뀐 상태를 되돌리지 못했습니다. 되돌리기가 중간에 멈춰 인스턴스가 정지된 채 남았을 수 있습니다.',
                <>
                  곧바로 자원 상태를 확인하고 <Jump to="support">문의하기</Jump>로 알립니다.
                </>,
              ],
            ]}
          />
        </Task>

        <Task id="incidents-reject" title="4-3. 제안 거절하고 종료하기 (선택)">
          <p>제안대로 바꾸지 않기로 했을 때만 합니다. 거절해도 지금 자원의 설정은 바뀌지 않습니다.</p>
          <LegalList
            numbered
            items={[
              <>
                <Key>제안 거절하고 종료</Key>를 누릅니다.
              </>,
              '같은 이름의 확인 창에서 남은 제안을 모두 거절하고 종료한다는 안내와 대상을 확인합니다.',
              '이유를 남기려면 사유 남기기 (선택)를 펼쳐 종료 사유 칸에 적습니다(1,000자까지).',
              <>
                창의 <Key>제안 거절하고 종료</Key>를 누릅니다.
              </>,
            ]}
          />
          <GuideFigure
            id="3-18"
            alt="제안 거절하고 종료 확인 창. 남은 제안 1건을 실행하지 않고 거절한다는 안내, 현재 설정을 바꾸지 않는다는 문구, 펼친 사유 입력란, 취소와 제안 거절하고 종료 버튼."
            caption="사유 칸은 비워 둬도 종료됩니다. 적어 두면 나중에 종료 기록에서 왜 거절했는지 알 수 있습니다."
          />
          <p>
            결과: 인시던트가 {S.RESOLVED.label}로 바뀌고 종료 기록에 종료 판단 ‘{NO_FURTHER}’, 종료 시각, 처리자, 사유가
            남습니다. 다음 단계: 이 기록은 <Jump to="logs">로그</Jump>에서도 찾을 수 있습니다.
          </p>
        </Task>

        <Task id="incidents-success" title="4-4. 성공한 조치 확인하고 종료하기">
          <p>
            실행이 {E.SUCCESS.label}이면 인시던트는 {S.AWAITING_CLOSURE.label}가 되고, 제안 조치에는 ‘실행할 제안이
            없습니다.’가 나옵니다. 종료는 자동으로 되지 않으니 직접 마무리합니다.
          </p>
          <LegalList
            numbered
            items={[
              '실행 상태의 자산 보기 →나 관제에서 인스턴스가 새 유형으로 정상 동작하는지 확인합니다.',
              <>
                이전 유형으로 되돌리고 싶을 때만: 수행된 조치의 완료된 실행 아래{' '}
                <Key>{RUNBOOK_LABELS.RUNBOOK_EC2_REVERT_SIZE}</Key>을 누릅니다. 확인 창의 해제 대상이{' '}
                {RUNBOOK_LABELS.RUNBOOK_EC2_REVERT_SIZE}인지 확인하고 해제 실행을 누릅니다.
              </>,
              <>
                문제가 없으면 인시던트 종료의 <Key>{NO_FURTHER}</Key>를 누르고, 필요하면 사유를 적은 뒤 창의 {NO_FURTHER}
                를 누릅니다.
              </>,
            ]}
          />
          <GuideFigure
            id="3-09"
            alt="수행된 조치의 EC2 스펙 조정이 완료이고 그 아래 이전 스펙 복원 버튼, 제안 조치의 실행할 제안이 없습니다 문구, 인시던트 종료의 추가 조치 없이 종료 버튼."
            caption="완료된 실행 아래의 이전 스펙 복원은 되돌릴 때만 누릅니다. 마무리는 맨 아래 추가 조치 없이 종료로 합니다."
          />
          <p>
            결과: 인시던트가 {S.RESOLVED.label}로 바뀌고 종료 기록이 남습니다. 종료는 처리를 마쳤다는
            판단의 기록이며, 그 자체로 최적화가 끝났다는 증명은 아닙니다.
          </p>
        </Task>

        <Task id="incidents-rollback" title="4-5. 자동 원복이 확인된 경우">
          <p>
            사양을 바꾼 뒤 인스턴스 상태 검사 같은 실행 뒤 확인에서 문제가 나오면, 시스템이 이전 인스턴스 유형으로 자동으로
            되돌립니다. 수행된 조치에서 원래 실행은 {E.ROLLED_BACK.label}이고, 그 아래 원복 줄에{' '}
            {RUNBOOK_LABELS.RUNBOOK_EC2_REVERT_SIZE} · {E.SUCCESS.label}와 요청 주체 ‘시스템 · 실패 후 자동 복구’가 보이면
            되돌리기까지 확인된 것입니다.
          </p>
          <GuideFigure
            id="3-10"
            alt="수행된 조치의 EC2 스펙 조정이 복구 완료이고, 그 아래 원복 줄에 이전 스펙 복원 완료와 요청 주체 시스템 · 실패 후 자동 복구가 있다. 아래에 실행할 제안이 없습니다와 추가 조치 없이 종료 버튼."
            caption="원래 실행과 시스템이 시작한 원복 실행이 한 묶음으로 보입니다. 원복 줄이 완료여야 되돌리기가 끝난 것입니다."
          />
          <LegalList
            numbered
            items={[
              '관제에서 인스턴스가 이전 유형으로 정상 동작하는지 확인합니다.',
              <>
                실패한 이유가 궁금하면 <Jump to="support">문의하기</Jump>로 이 인시던트를 알립니다. 이미 쓴 제안은 다시
                실행할 수 없습니다.
              </>,
              <>
                확인이 끝나면 인시던트 종료의 <Key>{NO_FURTHER}</Key>로 마무리합니다(4-4의 3번과 같습니다).
              </>,
            ]}
          />
        </Task>

        <Task id="incidents-unverified" title="4-6. 결과를 확인할 수 없는 경우">
          <p>
            실행 뒤 AWS 상태를 여러 번 읽지 못하면 성공인지 실패인지 판정하지 않고 멈춥니다. 실행은 {E.UNVERIFIED.label},
            인시던트는 {S.FAILED.label}가 됩니다.{' '}
            <Key>
              이것은 4-5의 자동 원복이 아닙니다. 사양이 바뀌었는지 알 수 없고 시스템이 되돌리지도 않았으므로, 자원 상태를
              직접 확인해야 합니다.
            </Key>
          </p>
          <p>
            자산 보기로 열리는 자산 화면과 관제의 값은 마지막 수집 때 저장한 값입니다(자산 상세의 수집 시각). 그래서 지금
            AWS에서 인스턴스가 어떤 상태인지 확인해 주지 않습니다. 종료할지는 아래 4번 종료 조건으로 정합니다.
          </p>
          <GuideFigure
            id="3-11"
            alt="수행된 조치의 EC2 스펙 조정이 결과 확인 불가이고, 결과 확인 보류 줄에 PRECHECK_AWS_ERROR · 조회 실패 3회 · 마지막 시각이 있다. 아래 실행 상태에 자동 복구는 하지 않았으며 자산 상태를 수동으로 확인해야 한다는 안내와 자산 보기 링크."
            caption="결과 확인 보류 줄의 값은 문의할 때 함께 알려 주세요. 자산 보기로 열리는 화면은 마지막 수집 때 저장한 값이라, 지금 상태는 AWS에서 따로 확인합니다."
          />
          <LegalList
            numbered
            items={[
              <>
                수행된 조치의 결과 확인 보류 줄에서 사유 코드(예: <code>PRECHECK_AWS_ERROR</code>), 조회 실패 횟수, 마지막
                시각을 확인합니다.
              </>,
              '회사 AWS 계정에서 EC2를 조회할 권한이 있는 담당자에게 확인을 요청합니다. 담당자는 EC2 콘솔(또는 AWS CLI)에서 그 인스턴스의 지금 인스턴스 상태와 인스턴스 유형을 보고, 확인한 시각과 함께 알려 줍니다.',
              '담당자가 알려 준 결과를 아래 확인 결과별 할 일에 맞춰 처리합니다.',
              <>
                종료 조건: 종료는 이 조건으로만 정합니다. 담당자가 AWS에서 확인한 인스턴스 상태가 실행 중이고, 유형이 제안에서
                바꾸려던 새 유형이거나 실행 전 유형이며, 아래 넷째 경우처럼 풀리지 않은 차이가 남아 있지 않을 때만 인시던트
                종료의 <Key>{NO_FURTHER}</Key>를 누릅니다. 사유 칸에 확인한 담당자, 시각, 인스턴스 상태와 유형을 적어 두고 창의{' '}
                {NO_FURTHER}를 누릅니다. 하나라도 맞지 않으면 종료하지 않습니다.
              </>,
            ]}
          />
          <p>확인 결과별 할 일:</p>
          <LegalList
            items={[
              <>
                실행 중이고 유형이 제안에서 바꾸려던 새 유형: 사양이 바뀐 것이 확인된 것입니다. 그대로 두려면 4번 종료 조건으로
                갑니다. 이전 유형으로 되돌리려면 실행 아래의 <Key>{RUNBOOK_LABELS.RUNBOOK_EC2_REVERT_SIZE}</Key>을
                누릅니다(<Jump to="incidents-success">4-4</Jump>의 2번과 같습니다). 되돌리기 실행이 끝나면 2번부터 다시 해서
                담당자에게 상태와 유형을 다시 확인받고, 그 결과를 이 목록과 4번 종료 조건에 다시 맞춥니다.
              </>,
              <>
                실행 중이고 유형이 실행 전 유형: 사양이 바뀌지 않은 것입니다. {RUNBOOK_LABELS.RUNBOOK_EC2_REVERT_SIZE}은
                누르지 않고 4번 종료 조건으로 갑니다.
              </>,
              <>
                중지됨이거나 시작 중 · 중지 중처럼 전환 중인 상태, 두 유형 중 어느 것도 아닌 유형, 담당자가 확인하지 못한
                경우: 종료하지 말고 {RUNBOOK_LABELS.RUNBOOK_EC2_REVERT_SIZE}도 누르지 않습니다.{' '}
                <Jump to="support">문의하기</Jump>에서 문의 종류를 장애로 골라 이 인시던트, 결과 확인 보류 줄의 값, AWS에서
                확인한 상태 · 유형 · 시각을 알립니다. 인시던트는 {S.FAILED.label}로 둡니다.
              </>,
              <>
                자산 화면이나 관제의 값이 AWS에서 확인한 값과 다를 때: 판단은 AWS에서 확인한 값으로 합니다. 자산 상세의
                수집 시각이 실행 뒤로 바뀐 다음에도 계속 다르면 종료하거나 되돌리지 말고 위와 같이 문의하기로 알립니다.
              </>,
            ]}
          />
        </Task>

        <Task title="보안 인시던트에서 다른 점">
          <LegalList
            items={[
              '승인 버튼 이름이 조치에 따라 승인하고 차단 · 승인하고 해제 · 승인하고 삭제로 바뀝니다. 누르면 4-2와 같은 확인 창이 열립니다.',
              <>
                위험도가 높은 일부 위협은 선차단 대응으로 먼저 격리된 뒤 {S.AWAITING_CLOSURE.label}로 옵니다. 격리를 풀려면{' '}
                <Key>{RUNBOOK_LABELS.RUNBOOK_EC2_UNISOLATE}</Key>를 누릅니다.
              </>,
              '상세에 ‘1분 안에 응답하지 않으면 서버가 자동으로 격리합니다.’가 보이면 그 안에 승인하거나 종료를 결정합니다.',
              `선차단 대응으로 ${RUNBOOK_LABELS.RUNBOOK_EC2_ISOLATE}가 끝나고 남은 제안이 없으면, 격리를 둔 채 끝낼 때 추가 조치 없이 종료를 누릅니다.`,
              `가장 최근 NACL 실행이 ${RUNBOOK_LABELS.RUNBOOK_NACL_ADD_DENY} · ${E.SUCCESS.label}이고 ${RUNBOOK_LABELS.RUNBOOK_NACL_RESTORE} 제안이 남아 있을 때만 차단 유지하고 종료 버튼이 나옵니다. 누르면 남은 제안을 모두 거절하고 차단을 둔 채 종료합니다. 그 밖에 제안이 남아 있으면 제안 거절하고 종료가 나옵니다.`,
            ]}
          />
        </Task>
      </>
    ),
  },
  {
    id: 'logs',
    title: '5. 로그에서 기록 찾기',
    tocTitle: '5. 로그 조회',
    body: (
      <>
        <LegalList
          numbered
          items={[
            '사이드바에서 로그를 누릅니다.',
            '기간을 하루 · 일주일 · 한 달 중에서 고릅니다.',
            '전체 · 조치 · 위협 · 인시던트로 구분을 좁히고, 누가에서 사람을 고릅니다.',
            '표의 시각(KST) · 구분 · 내용 · 누가 · 대상 열로 무슨 일이 있었는지 확인합니다.',
          ]}
        />
        <p>‘이 기간에 남은 기록이 없습니다.’가 나오면 기간을 넓힙니다.</p>
      </>
    ),
  },
  {
    id: 'users',
    title: '6. 직원 초대와 권한 관리',
    tocTitle: '6. 직원 계정과 권한',
    body: (
      <>
        <p>
          준비: {ADMIN}으로 로그인합니다. 계정 메뉴는 {ADMIN}에만 보입니다.
        </p>
        <Task title="직원 초대하기">
          <LegalList
            numbered
            items={[
              '사이드바에서 계정을 눌러 계정 관리를 엽니다.',
              <>
                <Key>계정 초대</Key>를 누릅니다.
              </>,
              <>
                초대할 직원의 이메일을 넣고 권한을 {ROLE_LABELS.ADMIN} · {ROLE_LABELS.OPERATOR} · {ROLE_LABELS.VIEWER}{' '}
                중에서 고릅니다. 처음 값은 {ROLE_LABELS.VIEWER}입니다. {ROLE_LABELS.OPERATOR} · {ROLE_LABELS.VIEWER}을
                고르면 {STAFF}이, {ROLE_LABELS.ADMIN} 권한을 고르면 {ADMIN}이 만들어집니다.
              </>,
              <>
                이 화면에서 고르는 것은 권한 하나뿐이고, 조치 실행 · 인시던트 종료 · 문의 등록 기능 권한은 함께 주어지지
                않습니다. 그래서 권한을 {ROLE_LABELS.OPERATOR}로 초대한 직원은 조치 · 종료 · 문의를 아직 할 수 없습니다. 그
                일은 {ADMIN}으로 로그인한 담당자나, 그 기능 권한을 이미 받은 계정을 쓰는 동료에게 맡깁니다.
              </>,
              <>
                <Key>초대</Key>를 누릅니다. ‘직원을 초대했습니다.’가 나오면 그 직원에게 초대 메일이 나간 것입니다.
              </>,
            ]}
          />
          <p>
            다음 단계: 초대받은 직원은 메일의 임시 비밀번호로 <Jump to="start">처음 로그인하기</Jump>를 따라 합니다.
          </p>
        </Task>
        <Task title="권한 바꾸기 · 계정 막기">
          <p>
            목록에서 그 직원 계정 줄의 <Key>관리</Key>를 누르면 권한 변경 · 비활성화(또는 활성화) · 세션 종료 · 이력을 고를 수
            있습니다. 아직 한 번도 로그인하지 않은 직원에게는 초대 재발송도 나옵니다. 내 계정의 권한과 상태는 바꿀 수 없습니다.
          </p>
        </Task>
      </>
    ),
  },
  {
    id: 'settings',
    title: '7. 보호 태그와 알림 설정',
    tocTitle: '7. 보호 태그·알림',
    body: (
      <>
        <p>
          준비: 고객사 계정이면 모두 볼 수 있고, 바꾸는 것은 {ADMIN}으로만 할 수 있습니다. {STAFF}에는 ‘
          {SETTINGS_READ_ONLY}’가 나옵니다.
        </p>
        <LegalList
          numbered
          items={[
            '사이드바에서 고객사 설정을 누릅니다.',
            <>
              보호 태그에서 <Key>규칙 추가</Key>를 누르고 태그 키와 값을 넣습니다. 값이 여럿이면 값 추가를 누릅니다. 규칙
              하나라도 맞는 태그가 붙은 자산이 보호 대상이 됩니다.
            </>,
            'Slack 알림의 Webhook URL 칸에 Slack Incoming Webhook 주소를 넣습니다.',
            '이메일 알림의 받는 주소 칸에 알림을 받을 주소를 넣습니다.',
            <>
              <Key>저장</Key>을 누릅니다. ‘{SETTINGS_SAVED}’가 나오면 끝입니다.
            </>,
          ]}
        />
        <p>
          다음 단계: 이메일 알림은 받는 주소로 온 구독 확인 메일에서 받는 사람이 확인해야 나가기 시작합니다. 확인한 뒤{' '}
          <Key>상태 새로고침</Key>으로 상태를 다시 봅니다.
        </p>
      </>
    ),
  },
  {
    id: 'subscription',
    title: '8. 구독 확인과 변경',
    tocTitle: '8. 구독 관리',
    body: (
      <>
        <p>
          고객사 계정이면 모두 구독 상태를 볼 수 있고, 바꾸는 것은 {ADMIN}으로만 할 수 있습니다. 사이드바의 구독에서 현재 구독 · 모의
          Marketplace 계약 · 요금제 안내를 봅니다. 요금 · 혜택 · SLA는 아직 확정 전입니다.
        </p>
        <LegalList
          items={[
            '모의 Marketplace 구매 요청: 요금제를 골라 구매를 요청합니다. 서버가 처리하면 화면에 반영됩니다.',
            '자동 갱신 끄기: 바로 해지하지 않습니다. 이용 기간이 끝난 뒤 만료되면 서비스가 정지되고, 이 화면에서 다시 켤 수 없습니다.',
            <>
              위험 작업의 서비스 이용 중단: 새 수집 · 분석 · 조치를 바로 멈춥니다. 계약은 해지하지 않으며, 재개는{' '}
              {SERVICE_NAME} 운영팀에 요청합니다.
            </>,
          ]}
        />
      </>
    ),
  },
  {
    id: 'support',
    title: '9. 문의하기와 화면 모드',
    tocTitle: '9. 문의·화면 모드',
    body: (
      <>
        <Task title="운영팀에 문의하기">
          <p>
            준비: {ADMIN}이나, 문의 등록 기능 권한을 받은 {STAFF}(권한 {ROLE_LABELS.OPERATOR})으로 로그인해야 보낼 수
            있습니다. 권한이 {ROLE_LABELS.VIEWER}인 계정이나 이 기능 권한을 받지 않은 계정(계정 관리 화면에서 새로 초대한
            직원 포함)은 보내면 거절됩니다. 그때는 {ADMIN}으로 로그인한 담당자에게 문의를 맡기거나 아래 고객센터 연락처를
            이용합니다.
          </p>
          <LegalList
            numbered
            items={[
              <>
                문제가 생긴 화면에서 사이드바 아래의 <Key>문의하기</Key>를 누릅니다. 지금 보던 화면 주소가 함께 갑니다.
              </>,
              '문의 종류에서 장애 · 버그 · 기타 중 하나를 고릅니다.',
              '제목과 내용을 적습니다. 화면에 나온 오류 문구가 있으면 함께 적고, 알면 문제가 생긴 시각(선택)도 넣습니다.',
              <>
                <Key>문의 보내기</Key>를 누릅니다. ‘문의를 보냈습니다’가 나오면 운영팀이 확인한 뒤 이 계정의 이메일로
                연락합니다.
              </>,
            ]}
          />
          <p>
            로그인할 수 없을 때는{' '}
            <Link href={SUPPORT_CENTER_PATH} className={LINK}>
              고객센터
            </Link>
            의 연락처를 이용합니다.
          </p>
        </Task>
        <Task title="화면 모드 바꾸기">
          <p>사이드바 아래의 설정을 누르고 화면 모드에서 주간 또는 야간을 고릅니다.</p>
        </Task>
      </>
    ),
  },
  {
    id: 'troubleshooting',
    title: '10. 문제 해결',
    body: (
      <>
        <LegalTable
          head={['이럴 때', '이렇게 합니다']}
          rows={[
            [
              '코드가 일치하지 않습니다. 다시 입력해 주세요.',
              '인증 앱에 새로 나온 6자리 코드를 다시 넣습니다. 휴대전화 시각이 자동 설정인지도 확인합니다.',
            ],
            [
              '로그인 시간이 지났습니다 · 시도가 너무 많습니다',
              <>
                잠시 기다린 뒤 <Key>처음부터 다시 로그인</Key>을 누릅니다.
              </>,
            ],
            [
              '비밀번호를 잊었을 때',
              <>
                로그인 창의 ‘비밀번호를 잊으셨나요?’ → 이메일 입력 → <Key>재설정 코드 받기</Key> → 메일로 받은 6자리 코드와
                새 비밀번호를 넣습니다.
              </>,
            ],
            [
              '비활성화된 계정입니다. 관리자에게 문의해 주세요.',
              `${ADMIN}을 쓰는 담당자에게 내 계정을 다시 활성화해 달라고 요청합니다.`,
            ],
            [
              VIEWER_ACTION_BLOCKED,
              <>
                내 계정의 권한이 {ROLE_LABELS.VIEWER}입니다. 조치가 필요하면 {ADMIN}으로 로그인한 담당자에게 맡깁니다. 권한을{' '}
                {ROLE_LABELS.OPERATOR}로 바꿔도 조치 실행 · 인시던트 종료 기능 권한은 따로 있어야 합니다(
                <Jump to="users">6절</Jump>).
              </>,
            ],
            [
              ACTION_FORBIDDEN,
              <>
                버튼을 눌렀지만 서버가 거절했습니다. 권한이 {ROLE_LABELS.OPERATOR}인 {STAFF}이면 그 일에 필요한 조치 실행
                또는 인시던트 종료 기능 권한이 없는 것일 수 있습니다. 계정 관리 화면에서 새로 초대한 직원이 그렇습니다. 그
                일은 {ADMIN}으로 로그인한 담당자에게 맡깁니다(<Jump to="incidents">4절</Jump> 준비). 전에는 되던 일이면 내
                계정의 권한이 바뀌었을 수 있으니 {ADMIN}을 쓰는 담당자에게 지금 권한을 확인합니다.
              </>,
            ],
            [
              '문의 보내기에서 ‘이 작업을 할 권한이 없습니다.’',
              <>
                문의 등록 기능 권한이 없는 계정입니다. {ADMIN}으로 로그인한 담당자에게 문의를 맡기거나{' '}
                <Link href={SUPPORT_CENTER_PATH} className={LINK}>
                  고객센터
                </Link>
                의 연락처를 이용합니다(<Jump to="support">9절</Jump>).
              </>,
            ],
            [
              EVIDENCE_EXPORT_BLOCKED,
              `근거 내려받기는 조치 실행 기능 권한이 있는 계정으로만 할 수 있습니다. ${ADMIN}으로 로그인한 담당자에게 요청합니다.`,
            ],
            [
              '진행 중인 실행이 끝난 뒤 종료할 수 있습니다.',
              `실행 상태가 ${E.IN_PROGRESS.label}에서 바뀔 때까지 기다린 뒤 다시 엽니다.`,
            ],
            [
              `실행 상태 ${E.UNVERIFIED.label}`,
              <>
                <Key>자동 원복이 아닙니다. 시스템이 되돌리지 않았으니 자원 상태를 직접 확인합니다</Key>. 자산 화면은 마지막 수집
                값이라 근거가 되지 않습니다. 확인 방법과 종료해도 되는지는{' '}
                <Jump to="incidents-unverified">4-6의 4번 종료 조건</Jump>으로만 정합니다.
              </>,
            ],
            [
              `실행 상태 ${E.ROLLBACK_FAILED.label}`,
              <>
                바뀐 상태가 그대로 남았을 수 있습니다. 곧바로 자원을 확인하고 <Jump to="support">문의하기</Jump>로
                알립니다.
              </>,
            ],
            [
              SETTINGS_READ_ONLY,
              `설정 변경은 ${ADMIN}을 쓰는 담당자에게 요청합니다.`,
            ],
            [
              '이메일 알림이 오지 않을 때',
              <>
                받는 주소로 온 구독 확인 메일을 확인했는지 봅니다. 고객사 설정에 <Key>같은 주소로 다시 연결</Key>이 보이면
                누르고, 잠시 뒤 상태 새로고침을 누릅니다.
              </>,
            ],
            ['이 기간에 남은 기록이 없습니다.', '로그에서 기간을 한 달로 넓힙니다.'],
            [
              '그 밖의 문제',
              <>
                <Jump to="support">문의하기</Jump>로 알립니다. 로그인할 수 없으면{' '}
                <Link href={SUPPORT_CENTER_PATH} className={LINK}>
                  고객센터
                </Link>
                를 이용합니다.
              </>,
            ],
          ]}
        />
        <GuideFigure
          id="3-16"
          alt="조회 전용 계정으로 본 승인 대기 인시던트. Markdown · JSON 내려받기 옆에 조치 실행 권한이 있는 계정만 근거를 내려받을 수 있다는 문구, 비활성인 이 조치 실행과 제안 거절하고 종료 옆에 조회 전용 계정은 조치할 수 없다는 문구."
          caption="조회 전용 계정에서는 버튼이 숨겨지지 않고 비활성으로 보이며, 옆에 누를 수 없는 이유가 나옵니다."
        />
      </>
    ),
  },
];

export default function UsageGuidePage() {
  return (
    <LegalDocument
      layout="guide"
      title="사용 가이드"
      dated={false}
      sections={SECTIONS}
    />
  );
}
