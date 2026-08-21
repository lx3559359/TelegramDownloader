# 缓冲下载 I/O 验证记录

验证日期：2026-08-21（Asia/Shanghai）

验证分支：`codex/performance-optimization`

验证源码提交：`6da711c610f31dcd155385688863ba43a893bd98`

## 自动化质量门禁

- [x] `python -m pytest tests/test_download_io.py tests/test_downloader.py tests/test_scheduler.py tests/test_download_queue_e2e.py tests/test_download_queue_stress.py -q --durations=10`
  - 结果：`42 passed in 35.77s`。
  - 最慢用例为 50 任务队列压力测试，耗时 `29.93s`。
  - 队列端到端下载、暂停、重启与恢复用例耗时 `4.07s`。
- [x] `python -m ruff check src tests`
  - 结果：`All checks passed!`。
- [x] 验证前 `git status --short`
  - 结果：工作树无未提交变更。

## 性能与完整性证据

- [x] 确定性 8 MiB 下载由 128 个 64 KiB 网络块组成，在 1 MiB 写入阈值下只执行 8 次非空数据批次写入；完成时另执行一次空数据持久化提交。
- [x] 8 MiB 用例完成字节数精确为 `8,388,608`，SHA-256 与完整输入数据一致；该用例耗时 `0.31s`。
- [x] 50 ms 人工慢磁盘用例通过，事件循环心跳计数超过门槛 `10`，证明文件写入等待期间事件循环仍可运行；该用例耗时 `0.29s`。
- [x] 进度只使用已确认写入字节，不领先于 `.part` 文件；暂停、取消和正常完成均经过持久化边界。
- [x] 取消发生在线程批次写入期间时，同一尾块不会被重复提交。
- [x] 队列端到端结果逐项验证最终文件存在、大小精确、SHA-256 非空且正确，并且没有残留 `.part` 文件。

本记录只包含合成测试数据和本地提交标识，不包含 Telegram 标识、真实文件名、凭据或用户绝对路径。
