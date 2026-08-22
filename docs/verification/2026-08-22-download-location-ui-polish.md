# 下载位置、手动更新与界面可读性优化验证记录

日期：2026-08-22

分支：`codex/download-location-ui-polish`

基线：`4719d89`

## 验收范围

本次验证覆盖以下五项需求：

1. 下载根目录由 Windows 系统文件夹选择器选择，设置框只读；切换后新任务使用新目录，已有任务继续使用原路径。
2. 设置页、搜索页和表格中的选中框显示清晰白色勾号，并覆盖未选中、选中和禁用状态。
3. 托盘右键菜单使用独立浅色主题，在系统深色调色板下仍保持文字、禁用项和选中项可读。
4. 程序启动时不自动检查或安装更新；只有设置页的“检查更新”按钮会发起检查，后续下载与安装仍需用户确认。
5. 搜索摘要取消省略号并完整自动换行；行高按可见行懒计算，长摘要可用像素级纵向滚动查看完整内容。

## 第一轮：功能定向自检

执行：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_settings.py tests/test_download_paths.py tests/test_download_location_e2e.py tests/test_naming_templates_e2e.py tests/test_planner.py tests/test_downloader.py tests/test_file_integrity.py tests/test_storage_models.py tests/test_storage_inventory.py tests/test_storage_cleanup.py tests/test_storage_maintenance_e2e.py tests/test_diagnostic_probes.py tests/test_background.py tests/update/test_update_coordinator.py tests/test_controller.py tests/test_app.py tests/ui/test_checkmark_style.py tests/ui/test_wrapped_text.py tests/ui/test_settings_dialog.py tests/ui/test_main_window.py tests/ui/test_content_browser.py -q
git diff --check
```

结果：`408 passed in 18.33s`；`git diff --check` 无输出。

定向测试覆盖设置迁移、目录写入探测与路径越界保护、新旧任务跨根目录续传、文件完整性、存储维护、打开目录、启动零更新检查、手动检查防重、复选框离屏绘制、托盘菜单调色板，以及 10,000 行搜索结果的可见行懒测量。

## 第二轮：完整工程自检

执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1
.venv\Scripts\python.exe -m compileall -q src tests
.venv\Scripts\python.exe -m pip check
```

结果：

- 完整测试：`1061 passed in 50.90s`
- Ruff：`All checks passed!`
- `compileall`：退出码 0，无输出
- 依赖一致性：`No broken requirements found.`

## 第三轮：冻结程序、安装器与视觉自检

执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-installer.ps1 -SkipAppBuild
```

结果：

- 应用构建内置回归：`1061 passed in 51.24s`
- Ruff：`All checks passed!`
- 冻结程序冒烟：`PACKAGED_SMOKE_OK`
- 安装器冒烟：`INSTALLER_SMOKE_OK`
- 从便携 ZIP 解压到项目 `.build-temp` 下的全新隔离目录后运行 `TelegramDownloader.exe --self-test`：`ok=true`
- 冻结程序返回的 `runtime_root` 位于该隔离解压目录内，没有落入真实用户配置目录

构建产物：

| 产物 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `TelegramDownloader-0.15.0-win-x64-portable.zip` | 65,953,899 | `8BB56202EF9EDC1A55564887A1B11AA383AEAA46B3020A62459EA7E3DEB59F51` |
| `TelegramDownloader-0.15.0-win-x64-setup.exe` | 48,140,025 | `3E9C2C1C17EF8A4201259D4CBF31B6E48EEB05010F981CE963B04EF9F117BC5C` |

## 视觉证据

视觉检查使用生产代码中的真实 Qt 控件、代理样式和主题，只注入临时合成数据。由于系统中已有真实 Telegram 下载器实例并受单实例锁保护，没有操作、关闭或复用该真实实例。

| 检查项 | 100% 浅色 | 125% 深色调色板 |
| --- | --- | --- |
| 系统目录选择后的只读路径与“浏览…”按钮 | [截图](evidence/2026-08-22-download-location-ui-polish/settings-download-100-light.png) | [截图](evidence/2026-08-22-download-location-ui-polish/settings-download-125-dark.png) |
| 普通、选中和禁用复选框勾号 | [截图](evidence/2026-08-22-download-location-ui-polish/settings-checks-100-light.png) | [截图](evidence/2026-08-22-download-location-ui-polish/settings-checks-125-dark.png) |
| 托盘菜单文字、禁用项和选中背景 | [截图](evidence/2026-08-22-download-location-ui-polish/tray-menu-100-light.png) | [截图](evidence/2026-08-22-download-location-ui-polish/tray-menu-125-dark.png) |
| 手动更新空闲状态 | [截图](evidence/2026-08-22-download-location-ui-polish/settings-update-100-light.png) | [截图](evidence/2026-08-22-download-location-ui-polish/settings-update-125-dark.png) |
| 手动更新忙碌状态 | [截图](evidence/2026-08-22-download-location-ui-polish/settings-update-busy-100-light.png) | [截图](evidence/2026-08-22-download-location-ui-polish/settings-update-busy-125-dark.png) |
| 手动更新结果状态 | [截图](evidence/2026-08-22-download-location-ui-polish/settings-update-result-100-light.png) | [截图](evidence/2026-08-22-download-location-ui-polish/settings-update-result-125-dark.png) |
| 500 字摘要顶部与完整换行 | [截图](evidence/2026-08-22-download-location-ui-polish/search-wrap-100-light.png) | [截图](evidence/2026-08-22-download-location-ui-polish/search-wrap-125-dark.png) |
| 像素滚动后的摘要结尾 | [截图](evidence/2026-08-22-download-location-ui-polish/search-wrap-bottom-100-light.png) | [截图](evidence/2026-08-22-download-location-ui-polish/search-wrap-bottom-125-dark.png) |

视觉脚本报告 100% 下摘要行高 593、视口高 530；125% 下摘要行高 594、视口高 315。顶部截图包含 `【摘要开始】`，滚动后的底部截图包含 `【摘要结束】`，且表格没有水平滚动条。

## 隔离与安全边界

- 未登录或调用真实 Telegram 账号。
- 未读取、写入、移动或清理真实用户媒体目录；目录验证只使用 pytest 临时目录或项目 `.build-temp`。
- 未发起真实网络更新请求；手动更新的空闲、忙碌和结果画面通过生产方法驱动，更新协调器行为由自动化测试验证。
- 未操作系统中已运行的真实下载器实例。
- 本轮只生成本地构建和未发布分支；未推送、未打标签、未合并、未发布。
