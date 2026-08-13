# TelegramDownloader v0.1.0 发布验收记录

验收时间：2026-08-13 23:13（Asia/Shanghai）  
发布源码提交：`4ae9341912d5c5315ebaeda1f9151ab6da853261`  
签名标签：`v0.1.0`

## 自动化质量门禁

- [x] `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1`
  - 结果：`141 passed`，Ruff `All checks passed!`。
- [x] `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1`
  - 结果：PyInstaller onedir 主程序和独立 `UpdateHelper.exe` 构建成功，打包自检通过。
- [x] `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-installer.ps1`
  - 结果：Inno Setup 7.0.2 编译成功；C 盘目标在复制应用文件前被拒绝；D 盘静默安装、自检、原位升级和卸载通过；数据哨兵在普通卸载后保留。
- [x] 更新故障注入测试覆盖双源分歧、签名/哈希失败、断点续传、助手健康检查、替换失败回滚、日志脱敏和防降级策略。

## 正式产物

| 文件 | 字节 | SHA-256 |
| --- | ---: | --- |
| `TelegramDownloader-0.1.0-win-x64-portable.zip` | 65,394,903 | `11d54616db58a6713748e764ce11d1326e4f06c7e2b5b04d97bee2807d4b546b` |
| `TelegramDownloader-0.1.0-win-x64-setup.exe` | 47,594,345 | `ac734b2d496178f25085a4bf1ede8a4929a54102555ee05c9aca0eced0081ef1` |
| `TelegramDownloader-0.1.0-source.zip` | 162,153 | `46a536e35639783f1c69e2e6af2e35759e75f6c9afbdf954cbe70f6755b16927` |
| `update-manifest.json` | 2,182 | `263120b9e99a038fb4728f1e63096bb52e9e529061753575b1616f911de84818` |
| `update-manifest.sig` | 89 | `a68b0a42c64d8701995cd1b998c0a632fc3846395aa0208ab6b5015594aacefe` |
| `latest.json` | 57 | `50f66619a4fd5c834e8f42b3064590ac5c4fddfbf949cc4c179e5b2c76edeb41` |

- [x] 便携 ZIP 共 192 个条目，不含 `data`、`downloads`、`.venv`、测试、令牌、会话或其他凭据。
- [x] 打包程序 `--self-test` 返回 `ok: true`、版本 `0.1.0`；所有可写路径均在 `dist/TelegramDownloader` 下。

## 图形界面人工检查

- [x] `dist/TelegramDownloader/TelegramDownloader.exe` 无系统 Python 依赖直接启动。
- [x] 主窗口标题、三栏工作台、任务表格、实时概览、版本号和“未登录”状态显示正常。
- [x] 登录向导显示 API ID、API Hash、SOCKS5/HTTP 代理字段，并明确说明 DPAPI 与应用目录存储。
- [x] 未登录时扫描会给出中文状态提示“请先登录 Telegram 账号”。
- [x] 关闭主窗口后不存在残留 `TelegramDownloader` 进程。

## 双平台发布与在线更新

- [x] GitHub 仓库为公开仓库，默认分支 `main`；Release `v0.1.0` 为非草稿、非预发布正式版。
- [x] GitHub Actions `verify` 运行成功：[31712506117](https://github.com/lx3559359/TelegramDownloader/actions/runs/31712506117)。
- [x] GitHub 与魔搭的 `v0.1.0` 注解标签均指向源码提交 `4ae9341912d5c5315ebaeda1f9151ab6da853261`。
- [x] 魔搭公开仓库列出 6 个版本资产和 `releases/stable/latest.json`。
- [x] 发布事务从两端回下载所有资产并逐项比较大小和 SHA-256，结果一致。
- [x] 两端公网 `latest.json`、`update-manifest.json`、`update-manifest.sig` 字节完全相同；内置公钥完成 Ed25519 验签。
- [x] 清单最低更新器版本策略会拒绝 `0.0.0`，使用正式初版更新器 `0.1.0` 验证成功。

公开地址：

- GitHub：https://github.com/lx3559359/TelegramDownloader
- GitHub Release：https://github.com/lx3559359/TelegramDownloader/releases/tag/v0.1.0
- 魔搭：https://modelscope.cn/models/lx3559359/TelegramDownloader

## 需要用户账号参与的验收

以下操作需要用户自己的 Telegram 凭据和有权访问的内容，发布过程没有索取、记录或伪造这些数据：

- [ ] 使用用户自己的 API ID/API Hash 完成手机号、验证码和可选两步验证登录。
- [ ] 使用账号有权访问的消息和频道验证真实单条下载、筛选批量下载及代理连接。

除上述两项真实账号验收外，所有非秘密验收项均已通过。
