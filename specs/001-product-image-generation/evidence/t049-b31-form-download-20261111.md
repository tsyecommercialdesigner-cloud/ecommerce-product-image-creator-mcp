# T049-B3.1 Form 下载与首次交付离线记录

日期：2026-11-11。范围为隔离环境，不构成生产授权或 T049 闭项。

`provider.deliver_downloaded_form` 使用既有候选下载、首次锚点、暂存及无覆盖发布／登记入口。下载后及交付事务中重新核验 Locator、原操作与成功观察、独立摘要来源和下载范围；生产默认验证器仍拒绝。测试使用测试来源验证、固定的受控 DNS 替身、私有 CA 和真实本地 Socket/TLS，文件及 SQLite 均为临时隔离资源。

首次测试 4 failed：两个断言误把原始 `accepted` 当成观察归约结果 `succeeded`；另两个揭示在可信成功观察存在但原始状态仍为 `accepted` 时，保存失败未持久化。修复通过已验证锚点和成功观察，在 SQLite 事务内登记本地 `save_failed`，不改写原始上游状态。之后增加候选篡改、来源失效及默认拒绝断言。

最终测试：

- `uv sync --locked --check`：退出码 0。
- `uv run --no-sync ruff format --check src tests`：退出码 0。
- `uv run --no-sync ruff check .`：退出码 0。
- `uv run --no-sync pyright`：退出码 0。
- `uv run --no-sync pytest -q tests/integration/test_form_download_delivery.py --basetemp=.cache/pytest-t049-b31-20261111-final`：7 passed，退出码 0。
- 联合专项：`uv run --no-sync pytest -q tests/integration/test_form_download_delivery.py tests/integration/test_download_safety.py tests/integration/test_dns_isolation.py tests/integration/test_pinned_https.py tests/integration/test_artifact_reconciliation.py tests/integration/test_first_artifact_acceptance.py tests/integration/test_local_artifact_copy.py tests/integration/test_crash_recovery.py tests/contract/test_provider_multipart.py tests/contract/test_provider_matrix.py tests/integration/test_provider_recovery_tick.py tests/integration/test_task_recovery_entry.py tests/integration/test_task_recovery_b2.py tests/unit/test_confirmation.py tests/unit/test_retry_policy.py tests/integration/test_unknown_recovery.py --basetemp=.cache/pytest-t049-b31-20261111-focused --tb=short`：295 passed，退出码 0。
- `uv run --no-sync pytest -q --basetemp=.cache/pytest-t049-b31-20261111-full --tb=short`：1034 passed，0 failed、0 xfailed、0 xpassed、0 skipped，退出码 0。运行期间未修改生产源码或测试。
- `git diff --check`：退出码 0；未跟踪文件不受其覆盖。工作区源码和测试已纳入上述格式、静态及 pytest 检查。
- 仓库级 `ruff format --check .`：退出码 1，三份既有供应商 Markdown 的代码块格式差异；`src tests` 通过。

历史 1027 passed 不是本轮结果。此前默认 pytest 系统临时目录的权限错误属于测试环境错误；本轮使用新的工作区 basetemp，保留测试目录，不清理未知 reparse point。

受控 DNS 子进程与 Form 下载交付是分层测试，新增成功链使用 DNS 替身。非空费用风险预留在本新增链中没有单独构造；既有 T048 风险专项是组合证据，不能描述为该链的直接端到端证明。Mock、ENOSPC、网络中断及完整跨进程崩溃对账由 B3.2 承接。未验证真实 Host、供应商、生产授权、账单、实际网络和生产崩溃恢复；下载授权政策仍为 `pending_policy_decision`。无 CI、干净检出或提交证据，T049 保持未完成。
