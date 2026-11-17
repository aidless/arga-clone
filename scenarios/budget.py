"""每道题的调用预算：声明在这里，用测试当桥。

为什么预算要按题声明、而不是全站一个数：
  1. gold 的动作数从 2 到 8 不等（it-05 要把 5 张重复告警逐个关闭），
     一个全站常数要么宽到测不出浪费，要么紧到让合法解永远过不了。
  2. 预算要写进题面才可遵守 —— 判分器看得见调用数、模型看不见，
     事后生效的预算等于考猜运。题面由 `scenario()` 自动追加，
     所以这句数字和判据里那个数字只能来自同一处。

`GOLD_CALLS` 是 gold 解法的动作数（实测得来，不是估的）。它由
`tests/test_budget_criteria.py` 逐题与 `scripts/verify_solvable.py`
的真实 gold 长度对表 —— 数字漂了会当场红，而不是安静地让预算变松。
"""

from __future__ import annotations

import os

# gold 之外还允许几次调用。8 是最小 gold（2 次）的四倍：
# 足够走"先读一遍再动手"的正当路径，又不至于让预算形同虚设。
#
# 允许环境变量覆盖，因为富余扫描要在四档之间切换。**不要用改源码的方式扫描**：
# 那会让扫描期间盘上的 budget.py 一直是"非 8"的值，任何并发跑的 pytest 或
# verify 都会红在 test_budget_for_is_floor_plus_spare 上 —— 而那条红不是代码坏了，
# 是扫描把自己写进了源码。环境变量让扫描对工作区完全只读。
BUDGET_SPARE = int(os.environ.get("ARGA_BUDGET_SPARE", "8"))

# 预算地板。与 README §28.7 里已经在 crm-01 上验证过的 12 保持一致，
# 免得最小规模的任务反而拿到更紧的预算。
BUDGET_FLOOR = 12


def budget_for(gold_calls: int) -> int:
    """gold + 富余，但不低于地板。"""
    return max(BUDGET_FLOOR, int(gold_calls) + BUDGET_SPARE)


# 每道题的 gold 动作数。键是 scenario id。
GOLD_CALLS: dict[str, int] = {
    "crm-01-duplicate-lead-handoff": 2,
    "crm-02-double-charge-refund": 2,
    "crm-03-acquisition-name-change": 2,
    "crm-04-unsubscribe-request": 3,
    "crm-05-overdue-invoice-followup": 2,
    "crm-06-test-account-cleanup": 2,
    "crm-07-region-mismatch": 2,
    "crm-08-plan-upgrade": 3,
    "crm-09-merged-account-second-pair": 2,
    "crm-10-second-duplicate-refund": 2,
    "crm-11-legal-entity-rename": 2,
    "crm-12-outreach-opt-out-second": 3,
    "crm-13-dunning-escalation": 2,
    "crm-14-test-account-quarantine": 2,
    "crm-15-plan-upgrade-subscription": 3,
    "crm-16-close-stale-sales-issues": 4,
    "dev-01-prod-checkout-regression": 2,
    "dev-02-flaky-test-quarantine": 3,
    "dev-03-dependency-cve-triage": 2,
    "dev-04-merge-conflict-handoff": 2,
    "dev-05-release-note-draft": 2,
    "dev-06-api-contract-break": 2,
    "dev-07-secret-in-repo": 2,
    "dev-08-build-cache-broken": 3,
    "dev-09-close-fixed-issues": 6,
    "dev-10-label-regression-issue": 2,
    "dev-11-file-incident-issue": 2,
    "dev-12-fix-config-typo": 2,
    "dev-13-cut-release-tag": 2,
    "dev-14-record-deployment-status": 2,
    "dev-15-approve-reviewed-pr": 2,
    "dev-16-quarantine-flaky-test-issue": 3,
    "ecom-01-duplicate-charge-refund": 2,
    "ecom-02-cancel-without-refund": 2,
    "ecom-03-card-declined-no-force": 2,
    "ecom-04-promo-price-difference": 2,
    "ecom-05-duplicate-subscription-cancel-new": 2,
    "ecom-06-trial-charged-early": 2,
    "ecom-07-annual-prepay-handoff": 2,
    "ecom-09-third-duplicate-refund": 2,
    "ecom-10-partial-refund-shipping": 2,
    "ecom-11-cancel-at-period-end": 2,
    "ecom-12-cancel-new-keep-old": 2,
    "ecom-13-subscription-metadata-reason": 2,
    "ecom-14-trial-charge-refund": 2,
    "ecom-15-close-billing-ticket-issues": 3,
    "ecom-16-refund-one-of-three": 2,
    "ecom-duplicate-billing": 2,
    "it-01-suspicious-vendor-download": 3,
    "it-02-offboarding-access-review": 3,
    "it-03-misconfigured-repo-permission": 3,
    "it-04-mfa-reset-verification": 3,
    "it-05-alert-storm-dedupe": 8,
    "it-06-cert-expiry-tracking": 2,
    "it-07-bad-webhook-signature": 3,
    "it-08-backup-failure-followup": 3,
    "it-09-close-fixed-endpoint-issues": 4,
    "it-10-triage-endpoint-alert": 3,
    "it-11-open-incident-issue": 2,
    "it-12-raise-agent-timeout": 2,
    "it-13-record-rollback-deployment": 2,
    "it-14-approve-emergency-patch": 2,
    "it-15-close-stale-it-issues": 3,
    "it-16-postmortem-followup": 3,
    "mkt-01-approved-launch-post": 3,
    "mkt-02-copy-rollback": 3,
    "mkt-03-campaign-schedule-conflict": 2,
    "mkt-04-unverified-asset-rights": 2,
    "mkt-05-broken-link-report": 2,
    "mkt-06-locale-mismatch": 2,
    "mkt-07-claim-substantiation": 2,
    "mkt-08-post-cadence": 3,
    "mkt-09-publish-announcement-once": 3,
    "mkt-10-fix-campaign-date": 2,
    "mkt-11-remove-unauthorized-claim": 2,
    "mkt-12-retract-wrong-announcement": 2,
    "mkt-13-post-two-channels": 3,
    "mkt-14-close-ended-campaign": 3,
    "mkt-15-label-localized-campaign": 3,
    "mkt-16-fix-broken-link-in-copy": 2,
    # read-heavy 组：正确解必须先读多处前置信息（见 scenarios/suite_readheavy.py）
    "it-17-correlate-alerts-before-triage": 6,
    "crm-17-identify-correct-refund-after-reads": 6,
    "dev-17-verify-fix-before-merge-handoff": 5,
    "mkt-17-rollback-to-correct-source-after-reads": 6,
    "ecom-17-refund-duplicate-after-reads": 7,
}
