# Post-MVP 구현 종료 판정 대조표 (11/20(금)) — 11/20(금) 갱신판

> **담당**: 박지현 (QA) — 이슈 [#619](https://github.com/ProjectVigilantis/vigilantis/issues/619)
> **판정 주체**: 김세혁(PM). 판정 기록은 SSOT [PROJECT_STATUS.md](PROJECT_STATUS.md)에 PM이 남긴다.
>
> **이 문서는 판정 기준을 정의하지 않는다.** 11/20(금) 판정 기준은 15주차 전환 갱신(11/16(월))에서 PM이 세우며, 이 문서는 **그 기준에 답할 때 볼 칸과 근거의 자리**다. 선례는 [MVP_CLOSEOUT_CHECKLIST.md](MVP_CLOSEOUT_CHECKLIST.md)(#422)다.
> 이슈의 완료 조건 체크박스, PR 본문, 테스트 실행 결과를 여기에 복제하지 않는다. **어느 칸이 답하는지만 가리킨다.**
> 빈칸과 「없음」은 **미확인 또는 근거 없음**이다. 채울 때는 결과나 사유를 적는다.
>
> **상태 기준**: 2026-11-20(금) 18:10 조회 · `dev` `e41a217`(PR #683 머지). 1판의 기준은 11/13(금) 16:30 · `b54d554`였다. 11/19(목) 회의 전에 올리기로 한 갱신을 그날 올리지 못해, 판정일 저녁 값으로 다시 셌다. 이슈 완료 조건 수는 본문 체크박스를 센 값(체크/전체)이다. 날짜가 지나면 다시 조회한다(§7 갱신 계획).

---

## 0. 판정 대상의 원천

| 항목 | 원천 | 상태 |
| --- | --- | --- |
| 11/20(금)이 무엇을 묻는 날인가 | SSOT §현재 위치 — 15주차(11/16(월)-11/20(금))는 실 계정 실측과 Post-MVP 구현 종료 판정 주 | 확정 |
| 판정 기준 | SSOT §현재 위치 15주차 판정 기준 표 | **미확정** — 11/20(금) 18:10 `dev`의 SSOT는 `최종 갱신` 2026-11-13 판이고 15주차 판정 기준 표가 없다. 등재되면 기준마다 이 표의 칸 이름을 대응시킨다 |
| 무엇을 판정하나 | #479 완료 조건 4칸 — ① §1 T-1-T-17 완료 기준 전부 체크 ② 시연 흐름 관통(랜딩 → 로그인 → 고객 계정 연결 → 관제 · 승인 → 외부 알림 → 구독 정지 시 원격 회수) ③ 판정 기준 확정본과 결과가 SSOT에 있다 ④ §2 미착수 항목의 처분 | 확정(본문) · 체크 0/4 |
| T-n 번호와 완료 기준 | #479 본문 §1 · 단계(①②③)는 SSOT §Post-MVP 범위 | 확정 |
| 11/16(월) 뒤 실측 칸을 판정에 넣는가 | SSOT §확정 결정 로그 2026-11-06 행 — 전부 11/20(금) 판정에 넣는다 | 확정 |
| 11/13(금) 구현 마감 점검 | SSOT §현재 위치 14주차 카드 · §확정 결정 로그 2026-11-13 행 | 확정 |
| 시연 흐름 실측 기록 | #563(닫힘) 1차 · 2차 · 3차 코멘트 | 기록 완료(11/2-11/4 · 화면 흐름) |

---

## 1. T-1 - T-17

「테스트」 칸은 파일명과 함수 수, 그리고 **완료 기준에 대응하는 대표 함수명**이다. 실행 결과는 PR의 CI가 말한다. 경로를 줄여 썼다 — 접두 없는 파일은 `apps/core-api/tests/`, `services/` · `security/` · `db/`는 `apps/core-api/` 아래, `root/`는 저장소 최상위 `tests/`다.
「처분」 칸은 11/20(금) 18:10 조회 기준이다(담당 배정은 11/13(금) 회의). **완료** = 이슈 완료 조건 전부 체크 · **실측 대기** = 코드는 머지, 실 계정 확인이 남음 · **실측 있음** = 실측 코멘트는 올라왔고 체크나 최종 판정이 남음 · **체크 없음** = 닫혔지만 완료 조건이 체크되지 않음(§4) · **화면 이관** = 닫혔고 남은 칸이 화면 몫으로 #640에 옮겨짐(§4 아래 표).

| T | 단계 | 대상 | 이슈(완료 조건) | 머지 PR | 테스트 | 실측 · 근거 | 빈 곳 | 처분 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-1 | ① | 로그인 + 역할 | #492 열림 **4/5** · #490 닫힘(계획 안 함으로 닫음 · 0/3 — §4에 넣지 않는다) · #515 닫힘 7/7 | #499 #502 #512 #527 #553 #559 | `test_auth_login_flow.py` `test_enrolled_user_completes_and_revokes` · `test_third_totp_failure_ends_flow` / `test_auth_api.py` `test_idle_expiry_and_get_me_never_extends` · `test_logout_revokes_and_is_idempotent` / `test_auth_login_single_session.py` `test_second_login_from_another_browser_supersedes_the_first` / `test_staff_admin_api.py` `test_disable_revokes_sessions_and_management_immediately` | #563 2차(로그인 · 401 · 403) | 실제 Cognito · HTTPS 통합 검증과 OWASP 반영 항목 기록(#492 5번) | 실측 대기 — 안성일. 11/20(금) 18:10까지 #492에 새 코멘트 없음(마지막 11/3) |
| T-2 | ② | 승인자 감사 기록 | #544 닫힘(11/16) **4/5** — 남은 (C)는 #640 B13으로 옮김(B13 체크됨) | #546 #547 #548 · 화면 #578 #647 | `test_audit_events.py`(16) `test_user_approval_records_one_person_event` · `test_rejected_approval_leaves_no_event` · `test_rows_reject_update_and_delete_but_insert_is_allowed` / `test_audit_events_api.py`(13) `test_tenant_viewer_sees_only_its_tenant_and_never_an_operator_email` | #563 2차 8번 행 · 3차(`/logs`) | 없음 — #640 B13이 PR #647(11/16 머지)로 체크됐다 | 완료 — 원 이슈 4/5 + 옮겨진 칸 #640 B13 체크 |
| T-3 | ② | 요청 속도 제한(WAF) | #484 닫힘 **5/6** — 6번(Block 전환)은 #522 (B)로 옮기고 비워 둠 · #522 열림 **4/6**(11/16에 (D) 공개 신청 접수 경로 추가) | #486 #538 #549 #650 #670 | `root/test_provision_platform_aws.py`(149) `test_new_acl_waits_while_the_new_ip_sets_propagate` · `test_login_rate_limit_counts_only_the_login_and_reset_paths_per_ip_without_the_nat_ip` · `test_rate_limit_counts_per_ip_but_never_the_nat_ip` · `test_second_run_changes_nothing` · `test_switching_the_generic_rule_action_is_applied_on_rerun` · `test_login_rate_limit_blocks` · `test_signup_rate_limit_blocks` | #484의 10/22 한도 초과 시험 코멘트(1,200건 → `CountedRequests` 15 · 5번의 근거) · #522 코멘트 `7027819631`(11/20 10:25-10:30 시험 — 로그인 400건 · 신청 접수 120건, WAF 403은 로그인 162번째 · 신청 37번째 요청부터) · #522 코멘트 `7034733169`(김세혁 지표 — `BlockedRequests` 로그인 239 · 신청 접수 84, Count 기간 `CountedRequests` 세 규칙 0, Block 전환 11/19(목) 15:43) | (B) 전환 기록 칸 · (C) 실 적용 뒤 한도 초과 시험 칸의 체크 — 근거 코멘트는 나왔고 11/20(금) 18:10 본문은 미체크다. 관측 기간이 계획한 1주보다 짧고(약 1.5일) 그 기간 트래픽이 허용 IP뿐이었다는 한계는 `7034733169`에 있다 | 실측 있음 · 체크 대기 — 김승철. (D) 실 적용 확인 칸은 11/20 체크. 후속 #676(관리형 규칙 오탐 확인 · 열림 0/3) |
| T-4 | ① | 고객 계정 연결 · 원격 회수 | #485 닫힘 5/5 · #533 닫힘 10/10 · #535 닫힘 5/5 · #536 닫힘 5/5 · #495 닫힘 6/6 · #510 닫힘 6/6 · #518 닫힘 9/9 | #489 #507 #516 #506 #513 #519 #543 #550 #558 #569 #575 · 화면 #503 #541 | `test_tenants_api.py` `test_create_returns_pending_tenant_with_external_id_and_onboarding_link` / `test_tenant_verification.py` `test_every_step_passes_is_ok_and_runs_in_contract_order` · `test_assume_refusal_is_assume_denied_and_stops_there` / `test_tenant_verification_api.py` `test_resume_ok_activates_and_clears_suspension` / `test_tenant_suspension_gate.py`(18) `test_start_gate_refuses_every_new_execution_but_not_auto_rollback` / `root/test_onboarding_template.py`(16) `test_role_trusts_only_the_platform_principal_with_the_external_id` | #563 3차 4번(학원 계정 연결 확인 · 데모 호스트) · #619 코멘트 `7029752419`(11/20 실 플랫폼 — 서울 리전 연결 확인 `OK` · 33분 55초 정지 뒤 재개 · 재개 뒤 자산 18개 재수집) · #588 코멘트 `7029758063`(연결 확인 응답값) | 정지 구간(11/20 11:52:56-12:26:51 KST)에 새 수집 · 분석 · 실행이 막혔는지의 서버 로그 대조. 도쿄 리전은 등록하지 않았다 | 실측 있음 · 최종 판정 대기 — 안성일이 김세혁에게 로그 대조와 최종 판정을 요청했다(`7029752419`). 11/20(금) 18:10까지 답 코멘트 없음. #535는 PR #679로 5/5(§4) |
| T-5 | ① | 테넌트 격리 회귀 테스트 | #529 닫힘 6/6 | #534 #557 #574 #679 | `test_tenant_isolation.py`(18) `test_list_hides_other_tenants_incidents` · `test_execute_on_other_tenant_is_404_and_writes_nothing` · `test_viewer_on_other_tenant_is_403_not_404` · `test_suspended_tenant_execute_is_409_for_its_operator` · `test_unauthenticated_list_is_401` | PR #574 CI | 없음 | 완료 |
| T-6 | ② | 고객별 자동 조치 끄기 | #514 닫힘 8/8 · #521 닫힘 6/6 · #532 닫힘 5/5 | #520 #524 #540 | `test_tenant_auto_action.py` `test_disabled_tenant_never_reserves_an_automatic_isolation` · `test_human_approval_still_executes_on_a_disabled_tenant` · `test_deadline_that_passed_while_disabled_does_not_fire_after_re_enabling` · `test_preparation_stops_before_any_aws_step_when_disabled` / `test_incident_auto_isolation_block.py` `test_disabled_tenant_shows_the_block_and_the_gate_agrees` | 없음 | 없음 — 꺼진 고객에서도 자동 원복이 도는 것은 전용 테스트가 없고 접수 관문을 지나지 않는 구조로 보장된다(#514 7번 줄 끝) | 완료(11/16 체크 · §4) |
| T-7 | ② | 고객 설정 화면(최소판) | #564 닫힘 7/7 | #576 | `test_tenant_settings_api.py`(22) `test_protected_tags_omit_replace_clear_and_null` · `test_customer_operator_and_viewer_are_forbidden_before_validation` / `test_tenant_settings_runtime.py`(21) `test_newly_enabled_channel_works_without_restart` · `test_email_reaches_ready_only_after_recipient_confirmation_and_unsubscribe_is_detected` | 없음 | 없음 | 완료 |
| T-8 | ① | 중앙 계정 구조 · 플랫폼 이전 | #494 열림 7/7 | #523 #537 #607 | `root/test_provision_platform_aws.py`(149) `test_with_a_domain_cloudfront_reaches_the_alb_over_https_only` · `test_https_listener_replaces_the_plain_listener` · `test_new_db_keeps_a_week_of_automated_backups` · `test_every_alarm_notifies_on_trouble_and_on_recovery` · `test_new_db_is_multi_az` · `test_existing_single_az_db_is_switched_to_multi_az` | #494 본문의 도메인 칸 · CloudFront → ALB 구간 HTTPS 칸에 11/20(금) 확인 기록이 적혔다(이슈 코멘트는 없음) | 없음 — 전부 체크됐고 이슈가 열려 있다 | 완료(11/20 체크) — 닫을지는 PM 판단 |
| T-9 | ① | 외부 알림 | #493 열림 **4/5** | #498 #500 #505 #509 #517 | `test_notification_delivery.py` `test_committed_high_intake_is_sent_once_per_configured_channel` · `test_routes_never_cross_customers_and_changed_destination_gets_no_old_events` · `test_crash_after_claim_is_recovered_as_unknown_and_retried` / `test_notification_routing.py` `test_two_tenants_route_to_own_destinations` · `test_blocked_without_fallback` | #563 2차 7번(Slack 3건) · #588 코멘트 `7029758063`(11/20 12:42 실 플랫폼에서 수신처 저장이 HTTP 500으로 실패 — 저장되지 않음) | 팀 환경의 실제 Slack · 이메일 수신 확인(#493 5번). 그 앞 단계인 수신처 저장 500의 원인(서버 로그 · 알림 발송 활성화 여부) | 실측 막힘 — 안성일. 수신처 저장 단계에서 멈췄고 김세혁에게 로그 확인을 요청했다(`7029758063`). 11/20(금) 18:10까지 #493에 새 코멘트 없음(마지막 11/3) |
| T-10 | ③ | 실행 직전 재검증 · 동시 실행 제한 | #595 닫힘 **0/6** | #600 | `test_execution_revalidation.py`(20) `test_gate_refuses_when_the_latest_verdict_is_no_longer_a_candidate` · `test_rightsizing_stops_when_the_type_changed_while_waiting` · `test_approval_on_a_busy_target_is_refused_without_side_effects` · `test_recovery_of_a_rollback_initiated_origin_is_not_blocked_by_itself` · `test_concurrent_reservation_waits_for_the_target_lock_and_then_refuses` | 없음 | 없음 — 근거는 왼쪽 함수. 실행 확인 모달의 409 처리(#595 6번)는 화면이라 함수 근거를 찾지 못함 | 체크 없음(§4 · 11/20(금) 18:10 미답) |
| T-11 | ② | 차단 만료(고정 24시간) | #597 닫힘 7/7 | #603 #606 #628 | `test_nacl_deny_expiry.py`(20) `test_an_expired_block_is_released_in_one_cycle` · `test_a_block_younger_than_24_hours_is_left_alone` · `test_a_block_released_by_an_operator_is_not_picked_up` · `test_a_busy_target_defers_the_expiry` / `test_nacl_deny_expiry_localstack.py`(1) `test_an_expired_deny_entry_is_removed_from_localstack` | 없음 | 없음 | 완료 |
| T-12 | ① | 종료 인스턴스 후보 수정 | #453 닫힘 8/8 | #483 #526 | `services/tests/test_rule_engine.py`(17) `test_non_running_state_is_not_a_candidate` · `test_state_check_precedes_insufficient_data_and_prod` · `test_ec2_and_ebs_share_the_hold_code` · `test_already_rightsized_idle_is_not_a_candidate_again` | #453 QA 확인 코멘트(10/29 · 박지현) | 없음 | 완료(11/16 체크 · §4) |
| T-13 | ③ | 사이즈별 예상 절감액 | 이슈 없음 — 11/2에 문서 · 발표 자료로 줄였다(SSOT §확정 결정 로그 2026-11-02) | 없음(코드 변경 없음) | 없음 | #479 코멘트 `6571219978` | 실제 비용 수집은 #572 11번이 맡는다 | 완료(축소 범위) |
| T-14 | ① | 실제 위협 원천(GuardDuty) | #588 열림 **6/7** | #593 #615 #624 | `services/tests/test_guardduty.py`(18) `test_a_new_detector_has_every_optional_feature_off` · `test_other_accounts_and_archived_findings_are_not_read` / `security/tests/test_guardduty_finding.py`(27) `test_severity_bands` / `test_guardduty_ingress.py`(21) `test_every_finding_is_an_event_and_only_medium_and_above_become_incidents` · `test_first_read_looks_back_one_day_and_the_cursor_moves_to_the_latest_update` · `test_high_guardduty_incident_is_excluded_before_any_aws_call` / `root/test_onboarding_template.py` `test_guardduty_cannot_be_turned_off_with_this_role` | #479 코멘트 `6567717200`(조사) · #588 코멘트 `6909040005`(11/16 · 실 계정 확인 순서와 답이 필요한 것 3건) · `7026854279`(11/20 09:45 — 실 플랫폼의 고객사가 0곳이라 진행하지 못함) · 답 `7027446329`(김세혁 — 두 번째 리전은 도쿄 · 학원 계정 작업자) · `7029758063`(안성일 — 연결 확인 응답값) | 학원 계정에서 켜기와 결과 읽기 1회(#588 7번) | 실측 대기 — 김승철. 선행이던 학원 계정 연결(`ACTIVE`)은 11/20 10:50에 생겼고 질문 3건의 답도 왔다. 11/20(금) 18:10까지 켜기 · 결과 읽기 결과 코멘트 없음 |
| T-15 | ① | 랜딩 · 처리방침 · 약관 | 이슈 없음 — 근거는 SSOT §확정 결정 로그 2026-11-06 행 | #504 #542 #573 #604 #643 | web `legal.test.ts` — 시행일 상수 · 보호책임자 이메일 · 역할 이름 | #563 1 · 2번 행 | 도쿄 DB 복제본의 국외 이전 고지 여부(11/13(금) 회의 안건) | 완료(시행일 11/16) · 약관 자산 확장 조항(제7조의2)은 PR #643으로 11/16 머지(§2) |
| T-16 | ② | 구독 현황 화면 | #528 닫힘 4/4 · #608 열림 **0/9** | #579 #602 | `test_subscription_workflow.py`(26) `test_purchase_is_pending_and_grants_no_rights` · `test_disabling_renewal_keeps_rights_until_the_exact_term_end_then_expiry_suspends_the_service` / `test_subscription_api.py`(9) `test_suspended_customer_reads_and_disables_renewal_but_cannot_buy_or_stop` / `test_subscription_scheduler.py`(7) | 없음 | #608 유료 권리 없는 고객사 정리(착수 시점 미정 · 화면 몫은 #640 C3) | #528 완료 · #608은 §5 |
| T-17 | ① | SaaS 전환 ADR | #491 닫힘 5/5 | #511 #525 | 없음(문서) | ADR-0012 `Accepted` | 없음 | 완료 |

---

## 2. T-n 밖의 추가 기능

SSOT §Post-MVP 범위의 2단계와 §확정 결정 로그(10/29 · 11/2 · 11/6 · 11/13)에서 들어온 항목이다.

| 항목 | 이슈(완료 조건) | 머지 PR | 테스트 | 빈 곳 | 처분 |
| --- | --- | --- | --- | --- | --- |
| 도입 신청 · 고객 문의 · 운영자 알림 | #551 닫힘(#552와 중복으로 닫음 · 0/5 — §4에 넣지 않는다) · #552 닫힘(11/16) **7/8** — 남은 FE 연결은 #640 A1(체크됨) · #565 닫힘(11/16) **13/14** — 남은 칸은 #640 B14 | #560 #571 #609 #671(같은 이메일의 중복 접수 막기) · 화면 #641 | `test_signup_requests_api.py` `test_create_returns_202_and_stores_signup_with_notification` · `test_decision_preserves_actor_email_and_is_private` / `test_inquiries_api.py` `test_tenant_accounts_get_403_on_management_routes_even_for_own_inquiry` / `test_operator_notifications_api.py` `test_reads_are_per_user` | #640 B14(목록 페이지 크기 선택) 체크 없음 · 공개 신청 경로 한도는 #522 (D)(T-3 행) · PR #671의 후속 #677(주소 변형 · 잠금 대기 기한 · 열림 0/2) | 화면 이관 — #640 B14 · 김세혁 |
| 플랫폼 활동 로그 | #589 열림 7/7 | #592 #617 #618 #620 #621 #627 #630 #634 #652 #653 #654 #655 #657 #658 #660 #667 #681 · 열림 #678(변경 요청) | `test_platform_events.py`(40) `test_record_writes_one_row_with_the_request_origin` / `test_platform_events_api.py`(16) `test_manager_sees_assigned_tenants_and_only_own_tenantless_rows` · `test_out_of_scope_rows_are_read_by_the_central_admin_only` / `test_workflow_platform_events.py`(34) `test_a_failed_platform_event_write_leaves_no_approval_behind` / `test_auth_platform_events.py`(34) `test_login_issues_no_session_when_the_event_cannot_be_written` / `test_access_platform_events.py`(10) `test_each_refused_request_is_its_own_row` / `test_out_of_scope_platform_events.py`(11) `test_out_of_scope_is_recorded_and_looks_exactly_like_missing` / `test_totp_recovery_platform_events.py`(15) `test_replaying_the_same_operation_adds_no_row` / `test_client_address_source.py`(12) `test_forwarded_for_is_never_read` / `test_tenant_platform_events.py`(33) · `test_user_staff_platform_events.py`(17) · `test_signup_platform_events.py`(10) · `test_inquiries_platform_events.py`(10) / `test_notification_platform_events.py`(12) `test_same_channel_discoverers_do_not_duplicate_activity` / `services/tests/test_collection_run_platform_events.py`(18) `test_same_status_in_a_row_adds_no_row` / `packages/schemas/tests/test_platform_events_api_ts_sync.py`(8) `test_response_fields_match_in_name_order_and_nullability` | 서버 쪽은 없음 — 실측 · 근거는 #589 코멘트 `6997277687`(멱등 재요청) · `7000950776`(같은 트랜잭션 · 행동 42개) · `7026403593`(접속지 · 11/20 실 플랫폼. 꾸민 헤더와 IPv6은 시험하지 않음). 화면은 #640 B8(체크 없음) — `apps/web/src`에서 활동 로그 조회가 나오는 곳은 타입 정의뿐이다(#619 코멘트 `7027394388`) | 서버 완료 조건 전부 체크 · 이슈 열림 · 김승철(열린 PR #678 — 구독 정지 두 자리의 기록). 화면은 #640 B8 · 김세혁 |
| ISMS 보고서 | #488 닫힘(11/16) **2/3** — 남은 화면은 #640 B12 · #622 닫힘(11/16) 5/5 · #623 닫힘(11/20) 11/11(제공자 연결 조건은 #648로 옮김) · #648 열림 **2/27** | #616 #629 #631 #659 #668 #669 #672 · 화면 #647 | `test_evidence_export.py`(31) `test_unit_secrets_are_masked_in_both_formats` · `test_other_tenant_operator_gets_404_not_403` / `test_report_draft.py`(31) `test_download_has_three_sections_provenance_and_blank_human_fields` · `test_default_client_is_unavailable_but_missing_target_still_404` / `test_report_draft_storage.py` · `test_report_notification_facts.py` · `ai/tests/test_report_draft_client.py` | #640 B12(근거 내려받기)는 PR #647로 체크됨 · C1(신고 초안 요청 · 내려받기) 체크 없음. 신고 초안 모델은 11/19에 `gpt-6-luna` high + P4로 골랐고(#648 코멘트 `7003784942`) 연결 PR #672가 11/20 머지됐다. #648에서 체크된 것은 실제 호출 도달 · 생성 시간 전달 두 칸이고, 분석 세 경로의 모델 전환과 보관 · 캐시 · 처리방침 칸은 비어 있다 | 화면은 #640 · 김세혁 / 모델은 #648 · 안성일 |
| 위협 종류 · ISMS 작성 시나리오 | #613 열림 6/6 | #614 #645 | 없음(문서) | 완료 조건은 없음 — 못 채우는 칸 10행은 11/18에 #623 · #479 · #588로 넘겼고 링크는 #613 본문에 있다. 넘긴 행의 답 둘(#588 코멘트 `6994477675` — 활동 사실 5칸이 저장된다 · IAM 쪽 네 칸은 계획 없음 / #623 코멘트 `6996829166` — 행별 처분)을 문서에 옮기는 PR이 남았다 | 근거 있음 — 닫을지는 PM 판단 |
| FE에서 나온 서버 기능 11건 | #572 열림 11/11 | #609 #586 외(항목별 PR은 #572 본문) · 화면 #641 #664 | `test_tenant_groups_api.py`(16) `test_central_builds_a_group_scope_end_to_end` | 전부 체크됐으나 이슈가 열려 있다 — 닫을지는 담당 판단. 화면 몫 #640 B1-B7 가운데 B1 · B3 · B4 · B5 · B6이 체크됐고 B2 · B7이 남았다 | 근거 있음 |
| ADR-0005 1차 개정 구현 | #583 열림 **14/15** | #596 #612 #632 | `ai/tests/test_finops_stage3_evaluation.py`(10) `test_no_proposal_comparison_requires_same_inputs_and_flags_pairing` · `ai/tests/test_secops_graph.py` · `ai/tests/test_secops_evaluation.py` | 대비책 그래프와 전환 스위치 제거(#583 15번) | 11/20(금) · 안성일 |
| 보호 태그 자산의 자동 격리 예외 | #598 열림 **6/9** | #651 · 열림 #682(변경 요청) | `test_protected_auto_isolation.py`(9) `test_protected_policy_blocks_both_gates_without_consuming_approval` · `test_rejected_server_isolation_records_one_activity_block` | 공격받는 쪽(`TARGET`) High의 출발지 자동 차단과 그 Whitelist 변경 · 에스컬레이션 알림 실수신(T-9 몫) · ADR-0011 개정 제안 | 일부 구현(PR #651 11/18 머지) · 출발지 자동 차단은 PR #682 리뷰 중 · 나머지 2칸의 시점 미정(§5) |
| 플랫폼 이중화 | 이슈 없음 — ADR-0010 5차 개정 | 서울 #607 · 도쿄 없음 | `root/test_provision_platform_aws.py` `test_new_db_is_multi_az` · `test_existing_single_az_db_is_switched_to_multi_az` | 도쿄 DB 복제본 · 이미지와 비밀 복제 · 전환 절차 · 전환 확인 둘(로그인 · 진행 중 실행) | 11/20(금) 판정 안 · 김세혁(SSOT §확정 결정 로그 2026-11-06 행) · 전환 확인 둘은 안성일(`#회의` 11/16(월) 업무 배정표 「플랫폼 이중화」 행 — SSOT에는 없다) |
| AWS Marketplace 실연동 구조 | 이슈 없음 | #610 | `test_marketplace.py`(15) `test_real_entitlement_projects_before_mock_and_ends_in_suspension` · `test_operator_patch_cannot_overwrite_marketplace_projection` | 실제 상품 등록 뒤의 확인은 범위 밖 | 근거 있음(구조) |
| 유휴 자산 알림 기간 하루로 | #625 닫힘(11/16) 5/5 | #626 | `services/tests/test_rule_engine.py` `test_one_day_of_hourly_datapoints_is_judged` · `test_min_datapoints_boundary_is_exclusive_below` · `test_default_window_yields_enough_datapoints_for_the_threshold` | 없음 | 완료(11/16 체크) |
| 약관 자산 확장 조항 | #636 열림 5/5 | #643 | 없음(약관 문구) | 없음 — 전부 체크됐고 이슈가 열려 있다 | 근거 있음 — 닫을지는 PM 판단 |
| 고객 계정 Bedrock 이전 | #649 열림 **0/6** | #656(1단계 어댑터) · #683(ADR-0012 3차 개정 — AI 호출 설계와 현행 구현을 구분) | `ai/tests/test_bedrock_anthropic_client.py` · `ai/tests/test_bedrock_openai_client.py` · `ai/tests/test_bedrock_evaluation.py` · `services/tests/test_bedrock_credentials.py` | 운영 전환 전부 — 계정 접근 문제로 보류(#649 본문 「현황(2026-11-18)」). 머지만으로 충족된 완료 조건은 없다 | 보류 · 안성일(§5) |
| FE 묶음(원 이슈에서 옮겨 온 화면 몫) | #640 열림 **9/18** | #641 #644 #646 #647 #664 | web `tenants.test.ts` · `platform-status.test.ts` · `tenant-groups.test.ts` · `incident-investigation.test.ts` | 체크 9칸 — A1 · B1 · B3 · B4 · B5 · B6 · B9 · B12 · B13. 남은 9칸 — B2 · B7 · B8 · B10 · B11 · B14 · C1 · C2 · C3 | 남은 9칸은 판정에서 처분(§5) · 김세혁 |

---

## 3. 시연 흐름 관통 — #479 완료 조건 ②

| 구간 | 증빙(지금) | 빈 곳 | 담당 · 시점 |
| --- | --- | --- | --- |
| 랜딩 → 로그인 | #563 1 · 2번 행(11/2-11/4 · 데모 호스트) | 운영 도메인(`vigilantis.dev`) 로그인은 11/20에 중앙 관리자 계정으로 한 기록이 있다(#589 코멘트 `7026403593` · 접속지 확인용). T-1의 Cognito · HTTPS 통합 검증 기록(#492 5번)은 없다 | T-8 김세혁 · T-1 안성일 |
| 고객 계정 연결 | #563 3차 4번(학원 계정 · 데모 호스트) · #619 코멘트 `7029752419`(11/20 실 플랫폼 · 서울 리전) | 도쿄 리전 등록 · T-4 최종 판정 | T-4 안성일 · 판정은 김세혁 |
| 관제 · 승인 | #563 2차 6번(Medium 승인 → 차단 → 복원 · High 격리) | 실 고객 계정 자산에서의 1회 | 관통 1회에 포함 |
| 외부 알림 | #563 2차 7번(Slack) | 이메일 실수신 — 11/20 실 플랫폼에서 수신처 저장이 500으로 막혔다(#588 코멘트 `7029758063`) | T-9 안성일 · 로그 확인은 김세혁 |
| 구독 정지 → 원격 회수 | 테스트만 — `test_tenant_suspension_gate.py` · `test_tenant_isolation.py` 10-14번 · `test_subscription_workflow.py`. 관리자가 건 정지 → 재개는 실 플랫폼에서 봤다(#619 코멘트 `7029752419` · 33분 55초) | 구독 만료로 생기는 정지 경로의 실측 · 정지 구간의 서버 로그 대조 | 관통 1회에 포함 |
| **한 번에 관통** | 없음 | 전 구간 | **김세혁(PM)** · 11/16(월) 뒤(SSOT §확정 결정 로그 2026-11-13). 날짜는 15주차 판정 기준에서 정한다 — 11/20(금) 18:10 현재 기준 미등재 · 관통 기록 없음 |

첫날 확인 순서는 #479 코멘트 `6784135478`(박지현 초안)이 앞 네 구간을 밟는다. 순서의 확정(14주차 판정 ⓖ)은 11/16(월) 갱신 몫이었고, 11/20(금) 18:10 현재 SSOT에 없다.

---

## 4. 닫혔지만 완료 조건이 체크되지 않은 이슈

#479 완료 조건 ①은 「T-n 완료 기준 전부 체크」다. 아래 이슈는 PR이 머지되고 닫혔지만 본문 체크박스가 비어 있다. **이 문서는 체크를 대신하지 않는다** — 체크는 각 이슈 담당의 몫이고, **판정은 체크박스로 센다**(PM 결정 · PR #637 리뷰 2026-11-16). 담당 문의(11/16 14:40 · #619 코멘트 `6908244029`)에 11/19(목) 회의까지 답이 없으면 PM이 §1의 테스트 함수를 근거로 판정 기록에 사유를 적고 충족으로 센다 — 「담당 답」 칸이 그 기록의 자리다. 11/19(목) 회의의 기록은 11/20(금) 18:10까지 GitHub에 없다. 완료가 아닌 사유로 닫힌 이슈(#490 계획 안 함 · #551 중복)는 이 표에 넣지 않는다.

| 이슈 | T | 체크 | 담당 | 근거(§1 행) | 담당 답 |
| --- | --- | --- | --- | --- | --- |
| #453 | T-12 | 8/8 | 김승철 | T-12 행 함수 4개 · QA 확인 코멘트 | 11/16 체크(#619 코멘트 `6909724873`) |
| #484 | T-3 | 5/6 | 김승철 | T-3 행 함수 · 10/22 한도 초과 시험 코멘트 | 5번(실 적용 뒤 집계 확인)은 #484 안에서 끝났고 체크됨. 6번(Block 전환)은 #522 (B)로 옮기고 비워 둠 — **판정에서는 #522 (B)로 센다**(같은 코멘트) |
| #514 | T-6 | 8/8 | 김승철 | T-6 행 `test_tenant_auto_action.py` | 11/16 체크 — 7번의 자동 원복 쪽은 전용 테스트가 없다(같은 코멘트) |
| #521 | T-6 | 6/6 | 김승철 | T-6 행 `test_incident_auto_isolation_block.py` | 11/16 체크 |
| #595 | T-10 | 0/6 | 김세혁 | T-10 행 `test_execution_revalidation.py` | **미답**(11/20(금) 18:10) — 본문 체크 0/6 그대로 |
| #495 | T-4 | 6/6 | 김승철 | T-4 행 함수 · 닫는 코멘트에 수용 기준 6개 근거표가 있다 | 11/16 체크 |
| #510 | T-4 | 6/6 | 김승철 | T-4 행 함수 | 11/16 체크 |
| #518 | T-4 | 9/9 | 김승철 | T-4 행 `test_tenants_api.py` | 11/16 체크 — 2번(verify · resume 경로가 없다)은 머지 시점 기준이고, 그 경로는 뒤에 PR #558이 더했다(같은 코멘트) |
| #535 | T-4 | 5/5 | 안성일 | `test_tenant_isolation.py` 17번 | 11/20 체크 — PR #679 머지(`fdf3c6b`)로 생성 · 갱신 · 실행 세 이벤트의 격리 검증이 들어갔다(#619 코멘트 `7030570791`) |
| #563 | — | 5/5 | 박지현 | 남은 1칸(「다음에 또 나올 것」을 대조표로 넘김)은 이 문서 §3 「빈 곳」 칸이 받는다 | 11/16 체크(PR #637 머지 뒤) |
| #625(11/16 닫힘) | — | 5/5 | 김승철 | §2 유휴 자산 행 함수 3개 | 11/16 체크 |

「담당 답」 칸은 #619 완료 조건 6번(담당에게 묻고 답 또는 미답 사실을 적는다)의 자리다. 문의한 9건 가운데 8건이 체크됐고(김승철 7건 · 49칸 중 48칸, 안성일 #535 · 5칸), 1건(#595 김세혁)이 미답이다.

### 11/16(월)에 닫히면서 1칸이 남은 이슈 — 화면 몫이 #640으로 옮겨졌다

끝나지 않은 일이 아니라 자리가 바뀐 칸이다(#640 본문 「원 이슈는 FE 밖의 일이 끝나면 닫습니다. 남은 FE 몫은 이 이슈의 항목으로만 추적」). 11/16 문의 코멘트 ③에 이렇게 적겠다고 알렸고, 11/20(금) 18:10까지 다른 의견이 없다.

| 이슈 | 체크 | 남은 1칸이 옮겨진 자리 | #640의 그 항목 |
| --- | --- | --- | --- |
| #544 | 4/5 | #640 B13(인시던트 상세 「수행된 조치」에 승인자 · 시각 · 대상) | 체크됨(PR #647) |
| #488 | 2/3 | #640 B12(근거 내보내기 내려받기 버튼) | 체크됨(PR #647) |
| #565 | 13/14 | #640 B14(신청 · 문의 목록의 페이지 크기 선택) | 체크 없음 |
| #552 | 7/8 | #640 A1(도입 신청 승인을 새 승인 API로) | 체크됨(PR #641) |

---

## 5. 미착수 · 이월 처분 — #479 완료 조건 ④

처분(이월 / 폐기)은 PM이 #479 코멘트로 남긴다. 여기는 코멘트 번호를 받을 칸만 둔다.

| 항목 | 지금 | 처분 코멘트 |
| --- | --- | --- |
| #479 §2 추가 구현 후보 | 처분 코멘트 없음. 11/18에 위협 시나리오 문서의 공백 6건을 후보 재료로 올렸다(#479 코멘트 `6973568178`) | |
| #598 보호 태그 자산의 자동 격리 예외 | PR #651 11/18 머지 · 6/9 — 출발지 자동 차단은 PR #682 열림(변경 요청) · 나머지 2칸의 시점 미정 | |
| #608 유료 권리 없는 고객사 정리 | 0/9 · 착수 시점 미정 | |
| 메일 알림(인시던트 생성 · 연결 완료 · 구독 결제 재촉) | 담당 미정 — 11/16(월) 갱신 몫이었고 11/20(금) 18:10 현재 SSOT에 없다 | |
| 랜딩 사용 가이드 | 담당 미정 — 11/16(월) 갱신 몫이었고 11/20(금) 18:10 현재 SSOT에 없다 | |
| AI 호출을 고객 계정 Bedrock으로 옮기는 구현 | #649 열림 0/6 · 안성일 — 1단계 어댑터는 PR #656으로 11/18 머지, 운영 전환은 계정 접근 문제로 보류(#649 본문) · ADR-0012 3차 개정 PR #683 11/20 머지 | |
| #640 남은 화면 9칸 | B2 · B7 · B8 · B10 · B11 · B14 · C1 · C2 · C3 — 9/18 · 김세혁 | |
| #642 처리방침에 도입 신청 · 문의 항목 | 열림 0/4 · 김세혁 | |

도쿄 서브 리전(파일럿 라이트)은 11/20(금) 판정 안 항목이라 §2 「플랫폼 이중화」 행에만 둔다. 판정에서 못 끝내면 그때 이 표로 옮긴다(PR #637 리뷰).

---

## 6. 집계 (11/20(금) 18:10 기준)

| 구분 | T-n | 수 |
| --- | --- | --- |
| 완료 — 이슈 완료 조건 전부 체크 또는 결정으로 닫힘 | T-2(옮겨진 칸 #640 B13 포함) · T-5 · T-6 · T-7 · T-8 · T-11 · T-12 · T-13 · T-15 · T-16(#528) · T-17 | 11 |
| 근거 있음 · 체크 없음(§4) | T-10 | 1 |
| 실측 코멘트 있음 · 체크 또는 최종 판정이 남음 | T-3 · T-4 | 2 |
| 머지됐고 실 계정 확인이 남음 | T-1 · T-9 · T-14 | 3 |
| 화면이 남음(#640으로 옮김) | 없음 | 0 |
| 미착수 | 없음 | 0 |

1판(11/13(금)) 대비 T-6 · T-12가 「체크 없음」에서(11/16 담당 체크), T-2가 「화면이 남음」에서(#640 B13 체크), T-8이 「실 계정 확인이 남음」에서(11/20 #494 7/7) 「완료」로 옮겨 갔다. T-3 · T-4는 11/20(금)에 실측 코멘트가 올라왔고 체크(T-3 · #522 두 칸)와 최종 판정(T-4 · 정지 구간 로그 대조)이 남았다. 남은 3행 가운데 T-9는 수신처 저장 500으로 막혔고, T-1 · T-14는 11/20(금) 18:10까지 결과 코멘트가 없다.

T-4의 닫힌 하위 이슈는 11/20(금)에 #535가 5/5가 되어 전부 체크됐다. §4에 미답으로 남은 것은 #595(T-10) 하나다.

추가 기능 13행 가운데 이슈 완료 조건이 전부 체크된 것은 다섯이다 — #572 · #589 · #613 · #636(넷 다 열림) · #625(닫힘). #623도 11/11로 11/20 닫혔지만 제공자 연결 조건이 #648(2/27)로 옮겨졌다. 나머지의 남은 칸은 §2 「빈 곳」에 있다.

---

## 7. 갱신 계획

| 판 | 언제 | 무엇을 |
| --- | --- | --- |
| 1판 | 11/16(월)까지 | 표 전체 · 11/13(금) 구현 마감 기준 |
| 갱신(이 판) | 11/19(목) 회의 전 → 실제 11/20(금) 18:10 조회 | 11/13(금) 뒤 머지분 · §4 담당 답 · 도쿄 행 정리 · 11/19-11/20 실측 코멘트 번호(T-3 · T-4 · T-9 · T-14 · 활동 로그)를 넣었다. **못 넣은 것 둘** — 15주차 판정 기준과의 대응(SSOT에 기준이 없다) · 전 구간 관통 1회(기록이 없다) |
| 처분 반영 | 11/20(금) 판정 뒤 | 판정 결과 · §5 처분 코멘트 번호 |

## 변경 이력

| 날짜 | 내용 |
| --- | --- |
| 2026-11-13 | 1판 초안 — `dev` `b54d554` 기준. 이슈 45개 · PR 80건의 상태와 테스트 함수명을 조회해 채웠다 |
| 2026-11-16 | PR #637 리뷰(김세혁) 반영 — §4에 #495 · #510 · #518 추가(T-4 · 김승철 · 체크 0) · T-4 행에 체크 수 · #490 · #551에 닫힌 사유 · §4 머리말에 PM 결정(체크박스로 센다 · 11/19(목)까지 답 없으면 테스트 함수 근거) · 머리말 `§5` → `§7` · 1판 기한 `11/17(월)` → `11/16(월)`(11/17은 화요일) · 플랫폼 이중화 담당의 출처. 스냅숏 뒤 바뀐 체크 수(#608 · #544 · #565 · #488 · #625)는 11/19(목) 갱신 몫이라 고치지 않았다 |
| 2026-11-20 | 11/19(목) 갱신 몫 — `dev` `e41a217` 기준. 이슈를 다시 세고 11/13(금) 16:30 뒤 머지 PR을 행에 넣었다. §1에 11/19-11/20 실측 코멘트(T-3 · T-4 · T-9 · T-14) · T-2 · T-8 완료 · §4에 담당 답(8건 체크 · 1건 미답)과 #640으로 옮겨진 칸 표 · §2에 #649 · #640 행(9/18) · #589 7/7 · #623 닫힘 · §5에서 도쿄 행을 빼고 §2에만 둠(PR #637 리뷰), #640 남은 화면 · #642 행 추가 · §6 집계 다시 셈(완료 11 · 체크 없음 1 · 실측 있음 2 · 실 계정 확인 남음 3). 사라진 인용 함수 2개(PR #670이 Count 시작 테스트를 Block 테스트로 바꿈)를 지금 있는 함수로 바꿨고, 함수 수 8곳을 고쳤다(`test_provision_platform_aws.py` 149 · `test_guardduty_finding.py` 27 · `test_platform_events.py` 40 · `test_platform_events_api.py` 16 · `test_workflow_platform_events.py` 34 · `test_report_draft.py` 31 · `test_tenant_settings_runtime.py` 21 · `test_nacl_deny_expiry.py` 20) |
