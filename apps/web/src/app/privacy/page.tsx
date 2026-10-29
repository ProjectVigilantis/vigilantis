// 개인정보 처리방침(Post-MVP T-15). 로그인하면 이메일을 저장하므로 둔다(#479 T-15 완료 기준).
//
// 항목은 개인정보 보호법 제30조 · 시행령 제31조의 기재 사항 순서를 따른다. 사실은 코드에서 확인한 것만 적는다:
// 저장 항목은 db/models.py(AppUser · AuthSession · UserAdminEvent), 쿠키는 core-api auth/http.py, 세션 수명은
// config.py AUTH_SESSION_*, 로그 · 백업 보존은 scripts/provision_platform_aws.py, AI 전송 가림은 ai/model_client.py.
// 이 값들이 바뀌면 이 문서와 lib/legal.ts의 시행일을 함께 고친다.

import type { Metadata } from 'next';
import Link from 'next/link';

import { LegalDocument, LegalList, LegalTable, type LegalSection } from '@/components/legal/legal-document';
import { OPERATOR_NAME, PRIVACY_OFFICER, ROLE_NAMES, SERVICE_NAME } from '@/lib/legal';
import { TERMS_PATH } from '@/lib/return-to';

export const metadata: Metadata = {
  title: '개인정보처리방침 · Vigilantis',
  description: 'Vigilantis가 처리하는 개인정보의 항목, 목적, 보유 기간과 이용자의 권리를 안내합니다.',
};

const RELIEF_AGENCIES: [string, string, string][] = [
  ['개인정보분쟁조정위원회', '1833-6972', 'www.kopico.go.kr'],
  ['개인정보침해신고센터', '국번 없이 118', 'privacy.kisa.or.kr'],
  ['대검찰청', '국번 없이 1301', 'www.spo.go.kr'],
  ['경찰청', '국번 없이 182', 'ecrm.police.go.kr'],
];

const SECTIONS: LegalSection[] = [
  {
    id: 'purpose',
    title: '1. 개인정보의 처리 목적',
    body: (
      <>
        <p>{OPERATOR_NAME}(이하 &ldquo;회사&rdquo;)는 다음 목적으로만 개인정보를 처리합니다. 목적이 바뀌면 미리 동의를 받습니다.</p>
        <LegalList
          items={[
            '이용자 계정 발급, 로그인과 2단계 인증을 통한 본인 확인',
            `역할(${ROLE_NAMES})과 소속 고객사에 따른 접근 권한 부여`,
            '계정 관리와 조치 승인 기록의 보관(보안 사고 조사와 분쟁 대응)',
            '서비스 장애 대응과 비정상 접속 차단',
            '고객사가 지정한 곳으로 보안 위협 · 조치 결과 알림 발송',
          ]}
        />
      </>
    ),
  },
  {
    id: 'items',
    title: '2. 처리하는 개인정보 항목',
    body: (
      <>
        <LegalTable
          head={['구분', '항목', '수집 방법']}
          rows={[
            ['계정 정보', '이메일 주소, 비밀번호, 2단계 인증 등록 정보, 역할, 소속 고객사', '관리자의 계정 발급, 이용자의 첫 로그인 설정'],
            ['로그인 기록', '로그인 세션의 시작 · 마지막 활동 · 만료 · 종료 시각과 종료 사유', '서비스 이용 중 자동 생성'],
            ['계정 관리 기록', '누가 언제 어느 계정의 역할이나 상태를 바꿨는지', '서비스 이용 중 자동 생성'],
            ['접속 정보', '접속 IP 주소, 요청 시각과 경로', '서비스 이용 중 자동 생성'],
            ['알림 수신처', '고객사가 지정한 이메일 주소, Slack 웹훅 주소', '고객사의 요청'],
          ]}
        />
        <p>
          비밀번호와 2단계 인증 등록 정보는 인증 서비스(Amazon Cognito)가 보관하며 회사의 데이터베이스에는 저장하지
          않습니다. 로그인 세션은 복원할 수 없는 해시 값으로만 저장합니다.
        </p>
        <p>
          회사는 고객사와의 이용계약에 따라 고객사 AWS 계정의 자원 정보, 지표, 보안 이벤트를 수집하고 분석합니다. 보안
          이벤트에는 고객사 서버에 접속을 시도한 외부 IP 주소가 들어 있을 수 있으며, 회사는 이 정보를 고객사를 위해
          보안 위협 탐지와 차단 목적으로만 처리합니다.
        </p>
      </>
    ),
  },
  {
    id: 'retention',
    title: '3. 처리 및 보유 기간',
    body: (
      <>
        <LegalTable
          head={['항목', '보유 기간']}
          rows={[
            ['계정 정보 · 로그인 기록 · 계정 관리 기록', '회원 탈퇴 또는 고객사 이용계약 종료 시까지'],
            ['서버 운영 로그(접속 정보 포함)', '생성 후 30일'],
            ['데이터베이스 백업', '생성 후 최대 7일'],
          ]}
        />
        <p>관계 법령이 보존을 요구하는 정보는 그 법령이 정한 기간 동안 보관합니다.</p>
      </>
    ),
  },
  {
    id: 'destruction',
    title: '4. 개인정보의 파기 절차와 방법',
    body: (
      <>
        <p>보유 기간이 끝나거나 처리 목적을 이룬 개인정보는 지체 없이 파기합니다.</p>
        <LegalList
          items={[
            '전자 파일은 복구할 수 없는 방법으로 삭제합니다. 백업에 남은 정보는 백업 보유 기간이 지나면 자동으로 지워집니다.',
            '종이에 출력한 정보는 분쇄하거나 소각합니다.',
          ]}
        />
      </>
    ),
  },
  {
    id: 'third-party',
    title: '5. 개인정보의 제3자 제공',
    body: (
      <p>
        회사는 이용자의 개인정보를 제3자에게 제공하지 않습니다. 다만 이용자가 동의했거나 법률에 특별한 규정이 있는 경우는
        예외로 합니다.
      </p>
    ),
  },
  {
    id: 'outsourcing',
    title: '6. 개인정보 처리의 위탁',
    body: (
      <>
        <p>회사는 서비스 제공을 위해 다음과 같이 개인정보 처리를 위탁합니다.</p>
        <LegalTable
          head={['수탁자', '위탁 업무', '처리 위치']}
          rows={[
            [
              'Amazon Web Services, Inc.',
              '서비스 운영 서버와 데이터베이스, 로그인 · 2단계 인증(Amazon Cognito), 이메일 알림 발송(Amazon SNS)',
              '대한민국(서울 리전)',
            ],
            ['OpenAI', '보안 위협과 비용 낭비의 분석, 조치 추천', '미국(아래 7항)'],
          ]}
        />
        <p>위탁 계약에는 개인정보를 위탁 업무 밖에서 쓰지 않을 것과 안전하게 관리할 것을 정하고, 수탁자가 바뀌면 이 방침으로 알립니다.</p>
      </>
    ),
  },
  {
    id: 'overseas',
    title: '7. 개인정보의 국외 이전',
    body: (
      <>
        <p>회사는 인시던트 분석을 위해 다음과 같이 정보를 국외로 보냅니다.</p>
        <LegalTable
          head={['구분', '내용']}
          rows={[
            ['이전받는 자', 'OpenAI(미국)'],
            ['이전 항목', '분석 근거: 고객사 AWS 자원의 식별자와 설정, 지표, 보안 이벤트(외부 접속 IP 주소가 들어 있을 수 있음)'],
            ['이전 시기와 방법', '인시던트를 분석할 때마다 암호화된 통신(HTTPS)으로 전송'],
            ['이용 목적', '위협 판단 근거 요약과 조치 런북 추천'],
            ['보유 기간', 'OpenAI의 API 데이터 정책에 따름'],
          ]}
        />
        <p>
          이용자의 이메일 주소 같은 계정 정보는 보내지 않습니다. 분석 근거에 비밀번호 · 접근 키처럼 보이는 값이 있으면
          가린 뒤 보냅니다.
        </p>
        <p>
          고객사는 이용계약을 해지해 국외 이전을 거부할 수 있습니다. 이 경우 AI 분석과 조치 추천을 이용할 수 없습니다.
        </p>
      </>
    ),
  },
  {
    id: 'rights',
    title: '8. 정보주체의 권리와 행사 방법',
    body: (
      <>
        <p>
          이용자는 언제든지 자신의 개인정보 열람, 정정, 삭제, 처리 정지를 요구할 수 있습니다. 아래 11항의 개인정보
          보호책임자에게 이메일로 요구하면 회사는 10일 안에 조치하고 결과를 알립니다.
        </p>
        <LegalList
          items={[
            '법정대리인이나 위임을 받은 사람을 통해서도 요구할 수 있습니다. 이때는 위임장을 함께 보내야 합니다.',
            '고객사에 소속된 이용자는 고객사 관리자에게 계정 정정이나 비활성화를 요청할 수도 있습니다.',
            '법령이 보존을 요구하는 정보는 삭제를 요구해도 그 기간 동안 보관합니다.',
          ]}
        />
      </>
    ),
  },
  {
    id: 'security',
    title: '9. 개인정보의 안전성 확보 조치',
    body: (
      <LegalList
        items={[
          '관리자를 포함한 모든 계정은 인증 앱 코드를 입력하는 2단계 인증을 거쳐야 로그인할 수 있습니다.',
          '비밀번호는 회사가 저장하지 않고 인증 서비스가 보관합니다.',
          '로그인은 30분 동안 활동이 없거나 시작 후 8시간이 지나면 끝납니다.',
          '역할과 소속 고객사에 따라 볼 수 있는 화면과 정보를 제한합니다.',
          '계정 관리 기록은 고치거나 지울 수 없게 보관합니다.',
          '이용자의 브라우저와 서비스 사이의 통신은 HTTPS로 암호화합니다.',
          '고객사 AWS 계정에는 장기 접근 키 대신 고객사가 만든 역할의 임시 자격으로 접근합니다.',
        ]}
      />
    ),
  },
  {
    id: 'cookies',
    title: '10. 쿠키의 설치 · 운영과 거부',
    body: (
      <>
        <p>회사는 로그인을 유지하는 데 필요한 쿠키만 씁니다. 광고나 이용 행태 분석을 위한 쿠키는 쓰지 않습니다.</p>
        <LegalTable
          head={['이름', '용도', '유지 기간']}
          rows={[
            [<code key="s">__Host-vg_session</code>, '로그인 상태 유지', '로그인이 끝날 때까지(최대 8시간)'],
            [<code key="l">__Host-vg_login</code>, '로그인 단계(비밀번호 → 2단계 인증) 진행', '5분'],
          ]}
        />
        <p>
          주간 · 야간 화면 설정은 이용자 브라우저 저장소에만 남고 회사로 보내지 않습니다. 브라우저 설정에서 쿠키를 막을
          수 있으나, 이 경우 로그인할 수 없습니다.
        </p>
      </>
    ),
  },
  {
    id: 'officer',
    title: '11. 개인정보 보호책임자',
    body: (
      <>
        <p>
          개인정보 처리에 관한 문의, 불만, 피해 구제와 8항의 열람 · 정정 · 삭제 · 처리 정지 요구는 아래로 연락해
          주세요. 지체 없이 답변하고 처리합니다.
        </p>
        <LegalTable
          head={['구분', '내용']}
          rows={[
            ['성명', PRIVACY_OFFICER.name],
            ['직책', PRIVACY_OFFICER.position],
            [
              '이메일',
              <a key="e" href={`mailto:${PRIVACY_OFFICER.email}`} className="text-primary hover:underline">
                {PRIVACY_OFFICER.email}
              </a>,
            ],
          ]}
        />
      </>
    ),
  },
  {
    id: 'relief',
    title: '12. 권익 침해 구제 방법',
    body: (
      <>
        <p>개인정보 침해에 대한 상담이나 신고는 아래 기관에도 할 수 있습니다.</p>
        <LegalTable head={['기관', '전화', '누리집']} rows={RELIEF_AGENCIES} />
      </>
    ),
  },
  {
    id: 'changes',
    title: '13. 처리방침의 변경',
    body: (
      <p>
        이 처리방침을 바꾸면 시행 7일 전부터 서비스 화면에 알립니다. 이용자 권리에 중요한 변경은 시행 30일 전에 알립니다.
        서비스 이용 조건은 <Link href={TERMS_PATH} className="text-primary hover:underline">이용약관</Link>을 따릅니다.
      </p>
    ),
  },
];

export default function PrivacyPage() {
  return (
    <LegalDocument
      title="개인정보처리방침"
      intro={
        <p>
          {OPERATOR_NAME}는 {SERVICE_NAME} 이용자의 개인정보를 개인정보 보호법에 따라 처리합니다. 이 방침은 어떤 정보를 왜
          처리하고 얼마나 보관하는지, 이용자가 무엇을 요구할 수 있는지를 알립니다.
        </p>
      }
      sections={SECTIONS}
    />
  );
}
