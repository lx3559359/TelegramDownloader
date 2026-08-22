# TG Quick Fetch Brand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every user-visible product surface with the approved “TG 快取 · 媒体下载器” brand and a production icon while preserving v0.16.0 upgrade and data compatibility.

**Architecture:** Centralize product strings and resource lookup in one branding module, generate deterministic multi-size assets from the approved vector glyph, and wire those assets into Qt, PyInstaller, and Inno Setup. Keep the Python package, repository, updater product identifier, install directory logic, and transition executable filename unchanged.

**Tech Stack:** Python 3.12, PySide6/QtSvg, SVG/PNG/ICO, PyInstaller, Inno Setup, pytest

---

## File map

- Create `src/telegram_downloader/branding.py`: product constants and frozen/source resource lookup.
- Create `src/telegram_downloader/resources/tg_quick_fetch.svg`: approved master vector.
- Create `src/telegram_downloader/resources/tg_quick_fetch.ico`: multi-size Windows application icon.
- Create `src/telegram_downloader/resources/tg_quick_fetch-256.png`: high-resolution Qt/documentation asset.
- Create `scripts/generate_brand_assets.py`: deterministic QtSvg renderer and ICO packer.
- Modify `src/telegram_downloader/app.py`: application display name and global icon.
- Modify `src/telegram_downloader/bootstrap.py`: startup display text.
- Modify `src/telegram_downloader/ui/main.py`: new navigation brand block and window title.
- Modify `src/telegram_downloader/ui/login.py`: branded authentication title.
- Modify `src/telegram_downloader/ui/settings.py`: branded About/Update page.
- Modify `src/telegram_downloader/ui/update_dialog.py`: branded update title.
- Modify `src/telegram_downloader/background.py`: tray icon, tray tooltip, and close-to-tray notification title.
- Modify `src/telegram_downloader/ui/theme.py`: image-based brand mark styling.
- Modify `TelegramDownloader.spec`: include brand resources and embed the ICO while retaining `TelegramDownloader.exe`.
- Modify `installer/TelegramDownloader.iss`: visible branding, setup icon, stable AppId, and old-shortcut cleanup.
- Modify `README.md`: user-facing product title and compatibility note.
- Create `tests/test_branding.py` and modify `tests/test_app.py`, `tests/test_bootstrap.py`, `tests/test_background.py`, `tests/ui/test_main_window.py`, `tests/ui/test_login_dialog.py`, `tests/ui/test_settings_dialog.py`, `tests/ui/test_update_dialog.py`, `tests/test_packaging_contract.py`, and `tests/test_installer_contract.py`.
- Create `docs/verification/2026-08-23-tg-quick-fetch-brand.md`: visual, packaging, and upgrade evidence.

### Task 1: Central branding contract

**Files:**
- Create: `src/telegram_downloader/branding.py`
- Create: `tests/test_branding.py`

- [ ] **Step 1: Write failing branding tests**

```python
from telegram_downloader.branding import (
    APP_DISPLAY_NAME,
    APP_NAME,
    APP_SUBTITLE,
    app_icon_path,
)


def test_approved_brand_contract() -> None:
    assert APP_NAME == "TG 快取"
    assert APP_SUBTITLE == "媒体下载器"
    assert APP_DISPLAY_NAME == "TG 快取 · 媒体下载器"


def test_brand_icon_path_exists() -> None:
    assert app_icon_path().name == "tg_quick_fetch.ico"
    assert app_icon_path().is_file()
```

- [ ] **Step 2: Run and verify the module is missing**

Run: `pytest tests/test_branding.py -q`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Add constants and resource lookup**

```python
from __future__ import annotations

from pathlib import Path

APP_NAME = "TG 快取"
APP_SUBTITLE = "媒体下载器"
APP_DISPLAY_NAME = f"{APP_NAME} · {APP_SUBTITLE}"
APP_CHANNEL = "stable"


def resource_directory() -> Path:
    return Path(__file__).resolve().parent / "resources"


def app_icon_path() -> Path:
    return resource_directory() / "tg_quick_fetch.ico"


def app_logo_path() -> Path:
    return resource_directory() / "tg_quick_fetch-256.png"
```

The one-folder PyInstaller build places data under the package path, so `__file__` remains the single lookup rule in source and frozen runs.

- [ ] **Step 4: Keep the existence test red until assets are generated**

Run: `pytest tests/test_branding.py -q`

Expected: one PASS for strings and one FAIL because the icon does not yet exist.

- [ ] **Step 5: Commit the contract**

```bash
git add src/telegram_downloader/branding.py tests/test_branding.py
git commit -m "test: define TG Quick Fetch brand contract"
```

### Task 2: Deterministic vector and icon assets

**Files:**
- Create: `src/telegram_downloader/resources/tg_quick_fetch.svg`
- Create: `scripts/generate_brand_assets.py`
- Generate: `src/telegram_downloader/resources/tg_quick_fetch.ico`
- Generate: `src/telegram_downloader/resources/tg_quick_fetch-256.png`
- Modify: `tests/test_branding.py`

- [ ] **Step 1: Add the approved master SVG**

Use a 256×256 view box, a rounded cyan-to-blue square, and a white outlined collection box with a download arrow. The checked-in SVG must contain this deterministic structure:

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
  <defs>
    <linearGradient id="brand" x1="36" y1="28" x2="220" y2="228" gradientUnits="userSpaceOnUse">
      <stop stop-color="#19C0D5"/>
      <stop offset="1" stop-color="#3C6EE8"/>
    </linearGradient>
  </defs>
  <rect x="16" y="16" width="224" height="224" rx="58" fill="url(#brand)"/>
  <path d="M57 76h92l43 43v70H57z" fill="none" stroke="#fff" stroke-width="18" stroke-linejoin="round"/>
  <path d="M128 82v75m0 0-31-31m31 31 31-31" fill="none" stroke="#fff" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
```

- [ ] **Step 2: Add the asset generator**

`scripts/generate_brand_assets.py` must render 16, 20, 24, 32, 48, 64, 128, and 256 pixel transparent PNG payloads with `QSvgRenderer` and `QPainter`. Pack the PNG payloads into an ICO using the standard ICONDIR and ICONDIRENTRY structures from `struct.pack`; write only the 256 PNG separately. The script accepts `--check` to render in memory and compare bytes with checked-in outputs without rewriting them.

The ICO header logic must use:

```python
header = struct.pack("<HHH", 0, 1, len(images))
offset = 6 + 16 * len(images)
entries.append(
    struct.pack(
        "<BBBBHHII",
        0 if size == 256 else size,
        0 if size == 256 else size,
        0,
        0,
        1,
        32,
        len(payload),
        offset,
    )
)
```

- [ ] **Step 3: Generate and inspect assets**

Run: `python scripts/generate_brand_assets.py`

Expected: creates the ICO and 256 PNG under `src/telegram_downloader/resources/`.

Run: `python scripts/generate_brand_assets.py --check`

Expected: exit code 0 and `brand assets are reproducible`.

- [ ] **Step 4: Add binary contract tests**

Parse the ICO header in `tests/test_branding.py` and assert exactly the eight required sizes, PNG signatures for each image payload, and a transparent corner in the 256 PNG. Assert the master SVG contains no `<text>` node.

Run: `pytest tests/test_branding.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_downloader/resources scripts/generate_brand_assets.py tests/test_branding.py
git commit -m "feat: add TG Quick Fetch icon assets"
```

### Task 3: Apply the brand across Qt surfaces

**Files:**
- Modify: `src/telegram_downloader/app.py`
- Modify: `src/telegram_downloader/bootstrap.py`
- Modify: `src/telegram_downloader/ui/main.py`
- Modify: `src/telegram_downloader/ui/login.py`
- Modify: `src/telegram_downloader/ui/settings.py`
- Modify: `src/telegram_downloader/ui/update_dialog.py`
- Modify: `src/telegram_downloader/background.py`
- Modify: `src/telegram_downloader/ui/theme.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_bootstrap.py`
- Modify: `tests/ui/test_main_window.py`
- Modify: `tests/ui/test_login_dialog.py`
- Modify: `tests/ui/test_settings_dialog.py`
- Modify: `tests/ui/test_update_dialog.py`
- Modify: `tests/test_background.py`

- [ ] **Step 1: Write failing user-visible brand tests**

Assert all of the following:

```python
assert window.windowTitle() == APP_NAME
assert window.brand_name.text() == APP_NAME
assert window.brand_caption.text() == APP_SUBTITLE
assert not window.windowIcon().isNull()
assert login_dialog.windowTitle() == f"登录 {APP_NAME}"
assert update_dialog.title_label.text() == f"{APP_NAME} 有新版本"
```

Also assert startup status starts with `正在启动 TG 快取` and the tray tooltip contains `TG 快取`.

- [ ] **Step 2: Set application metadata and icon once**

Immediately after creating/reusing `QApplication` in `create_application()`:

```python
application.setApplicationName(APP_NAME)
application.setApplicationDisplayName(APP_NAME)
application.setWindowIcon(QIcon(str(app_icon_path())))
```

Every top-level window inherits the global icon; explicit window icons may use the same `QIcon` where Qt inheritance is unreliable in tests.

- [ ] **Step 3: Replace the sidebar text mark with the approved image**

In `MainWindow._build_navigation()`, store `self.brand_mark`, `self.brand_name`, and `self.brand_caption`. Load the 256 PNG into a smooth 34×34 pixmap. Set name to `APP_NAME` and caption to `APP_SUBTITLE`. Remove the literal “T” and update `theme.py` so the mark has no colored text background that competes with the image.

- [ ] **Step 4: Replace product strings, not Telegram service references**

Use brand constants for window titles, startup text, update title, tray tooltip, notifications, and About/Update content. Keep phrases such as `Telegram 登录已失效` and `Telegram 网络连接失败` because they describe the external service, not the product.

- [ ] **Step 5: Run Qt and background tests**

Run: `pytest tests/test_app.py tests/test_bootstrap.py tests/test_background.py tests/ui/test_main_window.py tests/ui/test_login_dialog.py tests/ui/test_settings_dialog.py tests/ui/test_update_dialog.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_downloader/app.py src/telegram_downloader/bootstrap.py src/telegram_downloader/background.py src/telegram_downloader/ui tests/test_app.py tests/test_bootstrap.py tests/test_background.py tests/ui
git commit -m "feat: apply TG Quick Fetch desktop brand"
```

### Task 4: Package and installer branding with upgrade compatibility

**Files:**
- Modify: `TelegramDownloader.spec`
- Modify: `installer/TelegramDownloader.iss`
- Modify: `tests/test_packaging_contract.py`
- Modify: `tests/test_installer_contract.py`

- [ ] **Step 1: Write failing packaging tests**

Assert:

- PyInstaller `EXE` keeps `name="TelegramDownloader"` and adds `icon=<tg_quick_fetch.ico>`.
- `datas` includes the SVG, ICO, and 256 PNG under `telegram_downloader/resources`.
- Inno Setup keeps `AppId={{B19D534A-A414-4D17-9BB6-CE9A60D8243C}`.
- `AppName`, `DefaultGroupName`, `UninstallDisplayName`, shortcut names, and Run description use `TG 快取`.
- `SetupIconFile` points to `tg_quick_fetch.ico`.
- output setup filename and required runtime executable remain `TelegramDownloader-*` and `TelegramDownloader.exe` for compatibility.
- `[InstallDelete]` removes only the two old `Telegram 下载器.lnk` shortcut paths.

- [ ] **Step 2: Run and verify failures**

Run: `pytest tests/test_packaging_contract.py tests/test_installer_contract.py -q`

Expected: FAIL on missing icon and old visible names.

- [ ] **Step 3: Update PyInstaller spec**

Add brand resources to `datas` and pass:

```python
icon=str(root / "src" / "telegram_downloader" / "resources" / "tg_quick_fetch.ico")
```

to the main `EXE`. Do not rename the `EXE`, `COLLECT`, helper, or runtime manifest identifiers.

- [ ] **Step 4: Update Inno Setup visible branding**

Set:

```ini
AppName=TG 快取
DefaultGroupName=TG 快取
UninstallDisplayName=TG 快取
SetupIconFile=..\src\telegram_downloader\resources\tg_quick_fetch.ico
```

Rename both shortcuts and the post-install description. Add narrowly scoped old shortcut deletion. Preserve AppId, `DefaultDirName`, protected data directories, executable names, output filename, and repository/update URLs.

- [ ] **Step 5: Run packaging contracts**

Run: `pytest tests/test_packaging_contract.py tests/test_installer_contract.py -q`

Expected: PASS.

- [ ] **Step 6: Build and inspect**

Run the existing Windows packaging and installer scripts. Inspect the EXE and installer icons at 16–256 pixels, then run the installer smoke test against an isolated v0.16.0 install. Confirm data, encrypted session, task/content databases, downloads, and install directory remain intact while shortcuts show the new brand.

- [ ] **Step 7: Commit**

```bash
git add TelegramDownloader.spec installer/TelegramDownloader.iss tests/test_packaging_contract.py tests/test_installer_contract.py
git commit -m "build: brand installer as TG Quick Fetch"
```

### Task 5: Documentation and three-pass release-candidate verification

**Files:**
- Modify: `README.md`
- Create: `docs/verification/2026-08-23-tg-quick-fetch-brand.md`
- Create: `docs/verification/evidence/2026-08-23-tg-quick-fetch-brand/`

- [ ] **Step 1: Update user-facing documentation**

Change the README product title and descriptions to “TG 快取 · 媒体下载器”. Add one compatibility note: the repository, install folder, and transition executable retain the old technical identifier so existing installations can upgrade without moving data.

- [ ] **Step 2: First self-check — focused suites**

Run all focused suites from the account, update, search, brand, packaging, and installer plans.

Expected: PASS.

- [ ] **Step 3: Second self-check — full engineering suite**

Run: `pytest -q`

Expected: all tests pass.

Run: `ruff check .`

Expected: `All checks passed!`

Run: `git diff --check`

Expected: no output.

- [ ] **Step 4: Third self-check — Windows visual and upgrade matrix**

Capture and inspect light/dark at 100%/125% for the main window, account status, login, About/Update tab, update dialog, single-line search results, and tray menu. Inspect title bar, taskbar, tray, desktop shortcut, Start menu, installer, and uninstall entry icons. Execute a non-destructive v0.16.0-to-current upgrade and record retained files by category without exposing names or private content.

- [ ] **Step 5: Record fresh evidence**

Write exact commands, pass counts, timestamps, screenshots, icon-size checks, zero startup-update evidence, no-QR-on-account-navigation evidence, and upgrade retention results. Never reuse v0.16.0 screenshots as current evidence.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/verification/2026-08-23-tg-quick-fetch-brand.md docs/verification/evidence/2026-08-23-tg-quick-fetch-brand
git commit -m "docs: verify TG Quick Fetch release candidate"
```
