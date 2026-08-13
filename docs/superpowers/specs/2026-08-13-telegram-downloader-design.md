# Windows 便携式 Telegram 下载器设计

- **状态：** 已确认
- **日期：** 2026-08-13
- **目标平台：** Windows 10/11 x64
- **界面语言：** 简体中文

## 1. 目标

开发一个解压后可直接双击 `TelegramDownloader.exe` 启动的图形化 Telegram 下载器。它支持单条消息链接与频道/群组批量下载，使用用户自己的 Telegram API 凭据和账号权限，下载图片、视频、音频、语音、文档与压缩包。

应用自身产生的配置、授权会话、数据库、日志、缓存、临时文件、下载文件及崩溃恢复数据必须位于可执行程序所在目录。应用不主动向 C 盘的 AppData、TEMP、注册表、系统凭据管理器或其他用户目录写入数据。Windows 自己生成的系统级记录（例如 Prefetch、系统崩溃记录）不在应用控制范围内。

## 2. 已确认的产品决策

- 同时支持单条 Telegram 消息链接和频道/群组批量任务。
- 批量任务必须选择日期范围、媒体类型和数量上限，不提供无意间抓取全部历史的默认行为。
- 使用 API ID、API Hash、手机号、验证码和可选两步验证登录个人账号。
- 支持图片、视频、音频、语音、文档和压缩包。
- 使用“专业任务工作台”布局：左侧导航，中间任务队列，右侧统计与当前任务详情。
- 程序或网络中断后自动恢复，已完成文件不重复下载。
- 下载目录为 `downloads/<频道名>/<年份-月份>/<媒体类型>/<文件>`。
- 支持手动配置 SOCKS5 或 HTTP 代理并测试连接。
- 交付绿色便携目录，不提供安装程序，不采用运行时向系统临时目录解包的单文件模式。

## 3. 技术方案

采用 Python、PySide6、Telethon、SQLite 和 pytest：

- **PySide6** 提供原生 Windows 桌面界面。
- **Telethon** 负责 Telegram 用户授权、消息遍历、媒体元数据和分块下载。
- **qasync/asyncio** 把异步 Telegram 工作负载接入 Qt 事件循环，避免阻塞界面。
- **SQLite** 持久化任务、媒体项、进度、重试和错误状态；使用 WAL 模式，数据库辅助文件仍位于同一目录。
- **Windows DPAPI** 通过 `ctypes` 加密 API Hash、代理密码和 Telethon StringSession；不使用会向系统用户目录写数据的凭据管理器。
- **PyInstaller onedir** 生成独立便携目录；不使用 onefile，避免启动时默认解压到系统 TEMP。

Telegram 要求第三方客户端使用从 `my.telegram.org` 获取的 API ID 与 API Hash，并遵守其 API 条款。参考：[Telegram API 凭据说明](https://core.telegram.org/api/obtaining_api_id)。Telethon 的 `iter_download` 支持按字节偏移进行可暂停、可恢复的流式下载。参考：[Telethon 下载接口](https://docs.telethon.dev/en/stable/modules/client.html)。Qt 支持把 PySide6 应用冻结为独立目录。参考：[Qt for Python 部署](https://doc.qt.io/qtforpython-6/deployment/index.html)。

## 4. 便携数据边界

### 4.1 目录结构

```text
TelegramDownloader/
├─ TelegramDownloader.exe
├─ runtime/                       # 打包依赖，只读
├─ data/
│  ├─ config/settings.json        # 非敏感设置
│  ├─ config/secrets.dat          # DPAPI 加密凭据与会话
│  ├─ database/tasks.sqlite3
│  ├─ logs/app.log
│  ├─ cache/
│  └─ temp/
└─ downloads/
   └─ <频道名>/<YYYY-MM>/<媒体类型>/<文件>
```

开发运行时，项目仓库根目录视为便携根目录；打包运行时，`TelegramDownloader.exe` 所在目录视为便携根目录。

### 4.2 启动约束

入口文件在导入 PySide6、Telethon 或其他可能读取系统路径的第三方模块之前执行路径引导：

1. 解析真实便携根目录并创建 `data` 与 `downloads`。
2. 将进程的 `TEMP` 和 `TMP` 指向 `data/temp`。
3. 显式指定 Qt 设置、日志、数据库、缓存和凭据密文路径。
4. 所有写入均通过 `PortablePaths` 服务获取路径。
5. `PortablePaths` 在写入前解析绝对路径并检查其仍位于便携根目录；路径穿越直接拒绝并记录错误。

应用不使用 Qt WebEngine，因此不会产生 Chromium 用户数据目录。设置使用项目内 JSON，不依赖注册表或默认 `QSettings` 后端。

### 4.3 开发与构建过程

开发和打包同样遵守项目内写入边界：虚拟环境使用项目根目录下的 `.venv`，临时目录使用 `.build-temp`，工具缓存使用 `.tool-cache`，构建结果使用 `build` 和 `dist`。安装依赖与执行打包脚本前显式设置 `TEMP`、`TMP`、`PIP_CACHE_DIR` 和 PyInstaller 工作路径，不使用 C 盘上的用户级 pip 缓存或临时目录。依赖运行时可以从系统只读位置加载，但不得在那里创建项目数据。

## 5. 架构与职责

### 5.1 界面层

- **登录向导：** API 凭据、代理、手机号、验证码和两步验证。
- **任务工作台：** 链接输入、筛选器、扫描结果、任务队列、总速度和当前任务详情。
- **任务详情：** 文件级状态、失败原因、暂停/继续、只重试失败项、打开目标目录。
- **设置页：** SOCKS5/HTTP 代理、连接测试、并发数（1–5，默认 3）和路径自检。

界面层只发送命令和呈现状态，不直接访问 Telegram、SQLite 或大文件。

### 5.2 应用核心

- **LinkParser：** 识别公开/私有消息链接、频道链接和群组链接，输出规范化来源。
- **TelegramGateway：** 封装授权、实体解析、消息查询、媒体元数据、代理和 FloodWait。
- **TaskPlanner：** 将来源、日期、媒体类型和数量上限转换为稳定的媒体清单。
- **DownloadManager：** 队列调度、并发限制、分块写入、暂停、恢复、重试和磁盘检查。
- **TaskRepository：** 用 SQLite 原子保存任务、媒体项、进度和状态迁移。
- **CredentialVault：** 使用 DPAPI 加解密敏感配置和 StringSession。
- **PortablePaths：** 唯一的应用写入路径来源与越界保护边界。

### 5.3 单条链接语义

单条消息链接下载该消息中的媒体。如果消息属于 Telegram 分组相册，则自动包含同一分组中的全部媒体项。没有媒体的消息在扫描阶段显示“消息不含可下载媒体”，不创建空任务。

### 5.4 批量筛选语义

- 起止日期均包含在内，按 Windows 当前本地时区解释，数据库中统一保存为 UTC。
- 媒体类型筛选在数量上限之前应用。
- 数量上限指筛选后最多加入任务的媒体项数量。
- 扫描顺序为从新到旧，因此达到上限时保留日期范围内最新的匹配媒体。
- 扫描结果显示媒体项数量、已知总大小和大小未知项数量，用户确认后才进入下载队列。

## 6. 数据模型

### 6.1 tasks

- `id`：UUID。
- `source_kind`：`single_message`、`channel` 或 `group`。
- `source_peer_id`、`source_title`、`source_url`。
- `date_from_utc`、`date_to_utc`、`media_types`、`item_limit`。
- `status`、`created_at`、`updated_at`、`last_error`。

### 6.2 media_items

- `id`：UUID。
- `task_id`、`peer_id`、`message_id`、`grouped_id`、`media_id`。
- `media_type`、`original_name`、`target_path`、`expected_size`。
- `downloaded_bytes`、`status`、`retry_count`、`last_error`。
- `(peer_id, message_id, media_id)` 建立唯一约束，作为来源级去重键。

### 6.3 状态

任务状态：

```text
draft -> scanning -> queued -> downloading -> completed
                    downloading <-> paused
                    downloading -> waiting_retry -> downloading
                    downloading -> partial_failure
```

媒体项状态为 `queued`、`downloading`、`paused`、`waiting_retry`、`completed` 或 `failed`。任务只有在所有媒体项完成后才为 `completed`；存在最终失败项时为 `partial_failure`。

## 7. 下载与恢复

1. 开始文件前检查可用空间，至少保留 `max(512 MB, 文件预计大小的 5%)` 作为安全余量。
2. 数据写入最终路径旁的 `<文件名>.part`。
3. 使用 Telethon `iter_download(offset=<现有字节数>)` 分块续传。
4. 每完成一个分块就更新内存进度，并按节流频率把字节偏移提交到 SQLite，避免每个分块都同步刷盘。
5. 恢复时以 `.part` 实际长度为准，并与数据库和远端预计大小交叉检查。长度异常或大于远端大小时将损坏分片改名留档并从零重试。
6. 完整下载并校验总大小后，刷新数据库状态，再以原子重命名替换 `.part` 后缀。
7. 不同消息的同名文件追加 `_<message_id>`；应用绝不静默覆盖已有非目标文件。
8. 重启时把遗留的 `scanning`、`downloading` 和 `waiting_retry` 任务恢复为可调度状态；完整项直接跳过。

## 8. 错误处理

- **FloodWait：** 保存等待截止时间，界面显示倒计时，到时自动恢复；不进行忙循环请求。
- **代理或连接失败：** 指数退避重试 3 次，然后暂停任务。代理测试成功后允许一键恢复。
- **无效链接或无访问权限：** 在扫描阶段失败，不创建空下载任务，显示中文原因。
- **磁盘空间不足：** 保留 `.part` 并暂停任务；空间恢复后继续。
- **单文件失败：** 自动重试；仍失败时继续其他文件，任务最终标记为“部分失败”。
- **文件引用过期：** 重新获取来源消息并刷新媒体引用，然后重试。
- **退出应用：** 停止接收新分块，提交当前偏移，关闭 Telegram 和 SQLite，再退出；超时退出时下次启动仍可恢复。

## 9. 安全与隐私

- API Hash、代理密码和 Telegram StringSession 使用当前 Windows 用户的 DPAPI 加密，密文写入 `data/config/secrets.dat`。
- API ID、非敏感代理地址、并发数和界面偏好写入 `settings.json`。
- 日志不记录验证码、两步验证密码、API Hash、代理密码、StringSession 或完整手机号。
- 复制便携目录到另一台电脑或另一个 Windows 用户后，DPAPI 密文不可解密，应用保留普通设置并要求重新登录。
- 只下载当前账号能正常查看的消息媒体，不绕过 Telegram 权限、内容保护或平台限制。

## 10. 测试策略

### 10.1 单元测试

- 链接解析与相册扩展。
- 日期边界、媒体筛选和最新项上限。
- 文件类型映射、文件名清理、冲突命名和路径越界拒绝。
- 任务与媒体项状态迁移。
- `.part` 偏移恢复、异常长度处理和已完成项去重。
- 代理配置校验、日志脱敏和 DPAPI 适配器。

### 10.2 集成测试

使用可编程的假 TelegramGateway，不访问真实账号：

- 登录状态机，包括验证码和两步验证分支。
- 单条、相册和批量扫描。
- 正常下载、暂停、恢复、FloodWait、连接失败、磁盘不足和文件引用刷新。
- 进程重启后的任务恢复与 SQLite 一致性。

### 10.3 GUI 与打包验证

- 用 pytest-qt 覆盖首次登录、创建任务、暂停/继续、失败重试和代理设置。
- 构建 PyInstaller onedir 便携目录并直接启动 EXE。
- 启动后运行“路径自检”，确认所有应用管理的可写路径都在 EXE 目录内。
- 在不依赖系统 Python 的情况下完成打包版启动冒烟测试。
- 真实 Telegram 登录和下载由用户在应用内输入自己的凭据后验证，不在测试或日志中保存测试凭据。

## 11. 验收标准

1. 解压便携目录后可双击 `TelegramDownloader.exe` 启动，无需安装 Python或管理员权限。
2. 首次启动只在 EXE 目录下创建 `data` 和 `downloads`。
3. 用户可完成 API 凭据、手机号、验证码和可选两步验证登录。
4. 单条消息链接可下载媒体；相册链接可下载完整相册。
5. 频道/群组任务可按包含式日期范围、媒体类型和最新项数量上限扫描并下载。
6. SOCKS5/HTTP 代理可以保存、测试并用于 Telegram 连接。
7. 暂停、断网、关闭程序或重启后可从 `.part` 偏移恢复。
8. 已完成媒体不会重复下载，不同消息同名文件不会互相覆盖。
9. FloodWait、无权限、磁盘不足和单文件失败均显示可操作的中文状态。
10. 自动化测试通过，打包版启动冒烟测试通过，并提供中文 README 与构建说明。

## 12. 首版不包含

- 绕过 Telegram 的访问权限、内容保护或平台限制。
- 下载当前账号不可见、已删除或秘密聊天中的内容。
- 上传、转发、自动更新、定时下载和多账号同时在线。
- 安装程序、系统托盘常驻、浏览器扩展和远程控制。
