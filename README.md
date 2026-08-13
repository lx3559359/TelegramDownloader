# TelegramDownloader

## 正式版下载

当前正式版：`v0.1.0`（Windows 10/11 x64）

- [GitHub Release（安装包与便携包）](https://github.com/lx3559359/TelegramDownloader/releases/tag/v0.1.0)
- [魔搭镜像仓库](https://modelscope.cn/models/lx3559359/TelegramDownloader)
- 安装包会拒绝安装到 C 盘；便携包请解压到 D、E 等非 C 盘目录后运行。
- Windows 可能因未购买商业 Authenticode 证书显示 SmartScreen“未知发布者”；在线更新仍会强制验证 Ed25519 签名和 SHA-256。

一个面向 Windows 10/11 x64 的简体中文图形化 Telegram 下载器。它支持单条消息、相册，以及按日期、媒体类型和数量上限扫描频道/群组；下载任务可以暂停、断网续传并在程序重启后恢复。

项目同时交付绿色便携包和安装包，并使用 GitHub、魔搭双源的签名在线更新。所有由应用管理的配置、会话、数据库、日志、缓存、临时文件、更新备份和下载内容都位于应用目录，不把这些数据持久写入 C 盘。

## 主要功能

- 单条 `t.me` 消息链接下载，自动扩展同一相册。
- 频道/群组按包含式日期范围、图片、视频、音频、语音、文档、压缩包和数量上限扫描。
- 专业三栏任务工作台，显示状态、进度、大小、速度和剩余时间。
- `.part` 分块文件按实际字节偏移续传；异常分片会留档，不静默覆盖已有文件。
- SOCKS5 与 HTTP 代理，可在保存前测试连接。
- 启动检查 GitHub / 魔搭正式版更新；Ed25519 验证清单，SHA-256 验证安装包与便携包，替换失败自动回滚。
- 敏感信息通过当前 Windows 用户的 DPAPI 加密，日志自动屏蔽已登记凭据和完整手机号。

## 便携版使用

1. 下载 `TelegramDownloader-<版本>-win-x64-portable.zip`。
2. 解压到 D、E 等非 C 盘目录。
3. 双击 `TelegramDownloader.exe`，无需安装 Python，也不要求管理员权限。
4. 不要只复制 EXE；它需要同目录的 `_internal` 运行时文件。

首次启动会在 EXE 同级创建：

```text
TelegramDownloader/
├─ TelegramDownloader.exe
├─ _internal/
├─ data/
│  ├─ config/settings.json
│  ├─ config/secrets.dat
│  ├─ database/tasks.sqlite3
│  ├─ logs/app.log
│  ├─ cache/
│  ├─ temp/
│  └─ update/
└─ downloads/
   └─ <频道名>/<YYYY-MM>/<媒体类型>/<文件>
```

## 安装版使用

安装程序会阻止把应用安装到 C 盘，并要求选择其他固定磁盘。应用数据跟随安装目录。普通卸载默认保留 `data` 与 `downloads`；只有明确选择删除本地数据时才会移除。Windows 自己维护的快捷方式和卸载记录不属于应用业务数据。

未购买 Authenticode 商业证书，因此 Windows SmartScreen 可能显示“未知发布者”。这不影响应用内部的 Ed25519 更新签名与 SHA-256 完整性校验。

## Telegram 登录

第三方 Telegram 客户端必须使用用户自己的 API ID 和 API Hash：

1. 在浏览器打开 [my.telegram.org](https://my.telegram.org)。
2. 使用手机号登录，进入 **API development tools**。
3. 创建应用并复制 API ID、API Hash。
4. 在本程序登录向导中依次输入 API 凭据、手机号、验证码，以及账号启用时的两步验证密码。

API Hash、代理密码和 Telethon StringSession 会写入 `data/config/secrets.dat`，但内容由 DPAPI 加密。把整个便携目录复制到另一台电脑或另一个 Windows 用户后，密文通常无法解密，需要重新登录。

## 下载流程

### 单条消息或相册

粘贴类似 `https://t.me/example/42` 或 `https://t.me/c/123456/99` 的链接，点击“扫描预览”。如果消息属于相册，程序会把同组媒体一起列入预览。用户确认后才创建下载任务。

### 频道或群组批量任务

粘贴频道/群组链接，选择开始日期、结束日期（含当天）、媒体类型和数量上限。筛选在数量限制前执行，结果按从新到旧保留最新匹配项。默认上限为 500，不会无提示抓取全部历史。

### 暂停与恢复

下载中的内容先写入同目录 `.part` 文件。暂停、断网、关闭程序或重启后，会以 `.part` 实际大小作为续传偏移；完成并校验大小后才原子改名为最终文件。

## 代理

设置页支持：

- SOCKS5：地址、端口、可选用户名和密码。
- HTTP：地址、端口、可选用户名和密码。
- 不使用代理。

“测试代理”只测试当前表单，不会提前保存。代理密码不会进入 `settings.json`。

## 在线更新

程序启动后异步检查 GitHub 与魔搭的正式版指针。发现新版本时展示版本、说明和大小；只有用户确认后才下载。更新包保存在 `data/update/staging`，支持断点续传。独立更新助手会备份当前运行时、替换文件、启动健康检查；失败或超时时恢复旧版本。`data`、`downloads` 和用户自行放入根目录的非受管文件不会被更新器覆盖。

公开仓库目标：

- GitHub：`lx3559359/TelegramDownloader`
- 魔搭：`lx3559359/TelegramDownloader`

只在正式发布时同步 `main`、版本标签、源码、便携包、安装包、发行说明、更新清单和签名。

## 权限与隐私边界

本工具只下载当前登录账号能够正常查看的媒体，不绕过私有频道权限、内容保护、已删除内容或 Telegram 平台限制。请仅下载你有权访问和保存的内容，并遵守 Telegram 条款及所在地法律。

应用不使用系统凭据管理器、默认 QSettings 注册表后端或 Qt WebEngine。Windows 自身可能生成 Prefetch、崩溃记录或安装器短期临时文件，这些不受应用控制；应用配置、缓存、下载和更新状态不会持久放在 C 盘。

## 开发、测试与构建

要求 Python 3.12 x64。以下命令均把虚拟环境、pip 缓存、pytest 临时目录、PyInstaller 缓存和构建产物保存在项目目录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-dev.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-installer.ps1
```

基础便携构建输出：

```text
dist/TelegramDownloader/TelegramDownloader.exe
dist/TelegramDownloader-0.1.0-win-x64-portable.zip
dist/release/TelegramDownloader-0.1.0-win-x64-setup.exe
```

安装器构建会把 Inno Setup 7.0.2 以便携方式放在项目 `.tool-cache` 中，并在使用前核对官方固定 SHA-256 和 Authenticode 发布者；编译、安装及卸载冒烟测试的临时目录和日志都位于 `.build-temp`。安装验收会真实验证 C 盘拒绝、D 盘安装、自检、原位升级、在线更新运行时文件存在，以及普通卸载后用户数据仍保留。

正式发布流程还会生成带版本号的便携 ZIP、安装程序、`update-manifest.json` 与 `update-manifest.sig`。真实 Telegram 登录和下载测试必须由用户在应用内输入自己的凭据完成；测试代码、构建日志和发布资产不得包含这些凭据。

正式发布命令为 `scripts/release/release.ps1 -Version <X.Y.Z>`。它只允许从干净的 `main` 运行，先重跑测试与两种打包冒烟测试，再生成源码归档、规范清单和 Ed25519 签名；随后先创建 GitHub 草稿 Release 与魔搭候选目录，下载两端资产逐字节比对，最后才公开 Release 并推进双方版本指针。发布私钥只允许位于已忽略的 `.release-secrets` 或 CI secret，禁止提交、复制到产物或输出到日志。
