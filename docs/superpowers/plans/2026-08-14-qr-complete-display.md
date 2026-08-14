# QR Complete Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render Telegram login QR codes at an integer module size that fits completely inside a fixed 300×300 login viewport without clipping, interpolation, or loss of the four-module quiet zone.

**Architecture:** Keep QR matrix generation and integer-pixel sizing in `ui/qr.py`; keep viewport geometry and page resizing in `ui/login.py`. Reproduce both the renderer boundary and the real Qt layout failure with focused tests before changing production code, then rebuild the project-local portable runtime and visually inspect a screenshot at the user's screen size.

**Tech Stack:** Python 3.12, PySide6 6.11.1, qrcode 8.2, pytest, pytest-qt, Ruff, PyInstaller onedir

---

## File map

```text
tests/ui/test_qr.py                    Integer sizing and quiet-zone regressions
src/telegram_downloader/ui/qr.py       Bounded integer-module QR renderer
tests/ui/test_login_dialog.py          Real Qt layout and complete-visibility regression
src/telegram_downloader/ui/login.py    Fixed QR viewport and post-switch size adjustment
.build-temp/qr-display-proof.png       Ignored visual proof generated during verification
dist/TelegramDownloader/**             Rebuilt project-local portable runtime
```

## Task 1: Bound QR rendering with integer modules

**Files:**
- Modify: `tests/ui/test_qr.py`
- Modify: `src/telegram_downloader/ui/qr.py:11-42`

- [ ] **Step 1: Extend the test helper to pass renderer options**

Change the helper in `tests/ui/test_qr.py` so tests can specify the maximum side without importing implementation constants:

```python
def render_qr_image(value: str, **options):
    module = importlib.import_module("telegram_downloader.ui.qr")
    return module.render_qr_image(value, **options)
```

- [ ] **Step 2: Write the failing size-bound regression**

Add this parametrized test:

```python
@pytest.mark.parametrize("token_length", [8, 43, 86, 128])
def test_render_qr_image_fits_300_pixels_with_integer_modules(token_length: int) -> None:
    image = render_qr_image("tg://login?token=" + "a" * token_length, max_side=300)

    assert image.width() == image.height()
    assert 200 <= image.width() <= 300
```

- [ ] **Step 3: Write the failing quiet-zone regression**

Add helpers and assertions that derive the integer module size from the top-left finder pattern and verify all four quiet-zone bands remain white:

```python
from PySide6.QtGui import QColor


def black_points(image) -> list[tuple[int, int]]:
    black = QColor("black")
    return [
        (x, y)
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y) == black
    ]


def test_render_qr_image_preserves_four_module_quiet_zone() -> None:
    image = render_qr_image("tg://login?token=" + "a" * 43, max_side=300)
    points = black_points(image)
    quiet = min(min(x for x, _ in points), min(y for _, y in points))

    assert quiet > 0
    assert quiet % 4 == 0
    module_pixels = quiet // 4
    assert image.width() % module_pixels == 0

    white = QColor("white")
    side = image.width()
    for offset in range(quiet):
        for coordinate in range(side):
            assert image.pixelColor(coordinate, offset) == white
            assert image.pixelColor(coordinate, side - 1 - offset) == white
            assert image.pixelColor(offset, coordinate) == white
            assert image.pixelColor(side - 1 - offset, coordinate) == white
```

- [ ] **Step 4: Write the failing impossible-viewport test**

```python
def test_render_qr_image_rejects_a_viewport_smaller_than_the_matrix() -> None:
    with pytest.raises(ValueError):
        render_qr_image("tg://login?token=" + "a" * 43, max_side=16)
```

- [ ] **Step 5: Run the new renderer tests and verify RED**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_qr.py -q
```

Expected: FAIL with `TypeError: render_qr_image() got an unexpected keyword argument 'max_side'`. This proves the tests exercise the missing bounded-rendering behavior.

- [ ] **Step 6: Implement the minimal bounded renderer**

Replace the fixed-size arithmetic in `src/telegram_downloader/ui/qr.py` with:

```python
_MAX_MODULE_PIXELS = 8
_DEFAULT_MAX_SIDE = 300


def render_qr_image(value: str, *, max_side: int = _DEFAULT_MAX_SIDE) -> QImage:
    if _QR_LOGIN_URL.fullmatch(value) is None:
        raise ValueError("二维码登录地址无效")
    if max_side <= 0:
        raise ValueError("二维码显示区域无效")

    qr = qrcode.QRCode(
        error_correction=ERROR_CORRECT_M,
        box_size=1,
        border=4,
    )
    qr.add_data(value)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    module_pixels = min(_MAX_MODULE_PIXELS, max_side // len(matrix))
    if module_pixels < 1:
        raise ValueError("二维码显示区域太小")

    side = len(matrix) * module_pixels
    image = QImage(side, side, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)

    painter = QPainter(image)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Qt.GlobalColor.black)
    for row, values in enumerate(matrix):
        for column, filled in enumerate(values):
            if filled:
                painter.drawRect(
                    column * module_pixels,
                    row * module_pixels,
                    module_pixels,
                    module_pixels,
                )
    painter.end()
    return image
```

- [ ] **Step 7: Run renderer tests and verify GREEN**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_qr.py -q
```

Expected: all renderer tests pass; no image files appear in the pytest temporary directory.

- [ ] **Step 8: Commit the renderer change**

```powershell
git add tests/ui/test_qr.py src/telegram_downloader/ui/qr.py
git commit -m "fix: bound QR rendering to an integer viewport"
```

## Task 2: Guarantee the pixmap fits the login viewport

**Files:**
- Modify: `tests/ui/test_login_dialog.py:59-80`
- Modify: `src/telegram_downloader/ui/login.py:143-170`
- Modify: `src/telegram_downloader/ui/login.py:275-285`

- [ ] **Step 1: Write the failing real-layout regression**

Add `QApplication` to the widget imports and add this test:

```python
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit


def test_qr_pixmap_fits_the_complete_300_pixel_viewport(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.show_qr(
        "tg://login?token=" + "a" * 43,
        datetime.now(UTC) + timedelta(seconds=60),
    )
    QApplication.processEvents()

    pixmap = dialog.qr_image.pixmap()
    assert dialog.qr_image.width() == 300
    assert dialog.qr_image.height() == 300
    assert pixmap.width() <= dialog.qr_image.width()
    assert pixmap.height() <= dialog.qr_image.height()
    assert dialog.qr_image.hasScaledContents() is False
```

- [ ] **Step 2: Run the layout regression and verify RED**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_login_dialog.py::test_qr_pixmap_fits_the_complete_300_pixel_viewport -q
```

Expected: FAIL because the current label is approximately `472×233`, so it is neither a 300×300 viewport nor tall enough for the pixmap.

- [ ] **Step 3: Implement the fixed viewport and centered layout**

In `_build_qr_page`, replace the minimum size and unaligned insertion with:

```python
self.qr_image = QLabel()
self.qr_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
self.qr_image.setFixedSize(300, 300)
self.qr_image.setScaledContents(False)
layout.addWidget(self.qr_image, 0, Qt.AlignmentFlag.AlignHCenter)
```

At the end of `show_qr`, after starting the countdown timer, request a fresh dialog size:

```python
self.qr_countdown_timer.start()
self.adjustSize()
```

- [ ] **Step 4: Run the focused dialog tests and verify GREEN**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_login_dialog.py tests/ui/test_qr.py -q
```

Expected: all QR renderer and login-dialog tests pass, including countdown, refresh buttons, page switching and cancellation cleanup.

- [ ] **Step 5: Re-run the original dimension reproduction**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -c "import json; from datetime import UTC,datetime,timedelta; from PySide6.QtWidgets import QApplication; from telegram_downloader.ui.login import LoginDialog; app=QApplication.instance() or QApplication([]); d=LoginDialog(); d.show(); app.processEvents(); d.show_qr('tg://login?token='+'a'*43,datetime.now(UTC)+timedelta(seconds=60)); app.processEvents(); p=d.qr_image.pixmap(); print(json.dumps({'dialog':[d.width(),d.height()],'label':[d.qr_image.width(),d.qr_image.height()],'pixmap':[p.width(),p.height()],'fully_visible':d.qr_image.width()>=p.width() and d.qr_image.height()>=p.height()}))"
```

Expected: label is `[300, 300]`, pixmap dimensions are at most 300, and `fully_visible` is `true`.

- [ ] **Step 6: Commit the layout fix**

```powershell
git add tests/ui/test_login_dialog.py src/telegram_downloader/ui/login.py
git commit -m "fix: show complete Telegram login QR codes"
```

## Task 3: Run full verification and rebuild the local Windows runtime

**Files:**
- Modify only through build tooling: `dist/TelegramDownloader/**`
- Modify only through build tooling: `dist/TelegramDownloader-0.2.0-win-x64-portable.zip`
- Create ignored proof: `.build-temp/qr-display-proof.png`

- [ ] **Step 1: Run the complete source verification**

Run:

```powershell
& .\scripts\test.ps1
```

Expected: all tests pass and Ruff prints `All checks passed!`.

- [ ] **Step 2: Generate a visual proof at the reported token length**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -c "from datetime import UTC,datetime,timedelta; from PySide6.QtWidgets import QApplication; from telegram_downloader.ui.login import LoginDialog; app=QApplication.instance() or QApplication([]); d=LoginDialog(); d.show(); d.show_qr('tg://login?token='+'a'*43,datetime.now(UTC)+timedelta(seconds=60)); app.processEvents(); assert d.grab().save('.build-temp/qr-display-proof.png')"
```

Inspect `.build-temp/qr-display-proof.png` at original resolution. Expected: four complete white quiet-zone bands, all three finder patterns, countdown, status and all three action buttons are visible.

- [ ] **Step 3: Back up and hash-check project-local user data before rebuilding**

Run:

```powershell
$qrBackupRoot = Join-Path $PWD '.local\state\runtime-backup\pre-qr-display-fix'
if (Test-Path -LiteralPath $qrBackupRoot) { throw "Backup already exists: $qrBackupRoot" }
New-Item -ItemType Directory -Path $qrBackupRoot | Out-Null
foreach ($runtimeName in ('data', 'downloads')) {
    $runtimeSource = Join-Path $PWD "dist\TelegramDownloader\$runtimeName"
    if (Test-Path -LiteralPath $runtimeSource) {
        Copy-Item -LiteralPath $runtimeSource -Destination $qrBackupRoot -Recurse
    }
}
$qrBackupPrefix = $qrBackupRoot.TrimEnd('\') + '\'
$qrManifest = Get-ChildItem -LiteralPath $qrBackupRoot -Recurse -File | ForEach-Object {
    if (-not $_.FullName.StartsWith($qrBackupPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Backup file escaped backup root'
    }
    [pscustomobject]@{
        relativePath = $_.FullName.Substring($qrBackupPrefix.Length)
        length = $_.Length
        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }
}
$qrManifest | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $qrBackupRoot 'manifest.json') -Encoding UTF8
Write-Output "QR_BACKUP_FILES=$($qrManifest.Count)"
```

Expected: backup remains below `D:\Codex Project\Telegram下载器` and includes current `data/` and `downloads/` if present.

- [ ] **Step 4: Rebuild and smoke-test the portable runtime**

Run:

```powershell
& .\scripts\build.ps1
& .\scripts\smoke.ps1
```

Expected: build exits 0, `PACKAGED_SMOKE_OK` is printed, and the rebuilt executable exists at `dist\TelegramDownloader\TelegramDownloader.exe`.

- [ ] **Step 5: Verify preserved data after rebuilding**

Run:

```powershell
$qrBackupRoot = Join-Path $PWD '.local\state\runtime-backup\pre-qr-display-fix'
$qrManifest = Get-Content -Raw -LiteralPath (Join-Path $qrBackupRoot 'manifest.json') | ConvertFrom-Json
$qrRuntimeRoot = (Resolve-Path -LiteralPath 'dist\TelegramDownloader').Path
$qrMissing = 0
$qrChanged = 0
foreach ($entry in $qrManifest) {
    if ($entry.relativePath -match '^data\\logs\\') { continue }
    $currentPath = Join-Path $qrRuntimeRoot $entry.relativePath
    if (-not (Test-Path -LiteralPath $currentPath -PathType Leaf)) {
        $qrMissing += 1
        continue
    }
    $currentHash = (Get-FileHash -LiteralPath $currentPath -Algorithm SHA256).Hash
    if ($currentHash -ne $entry.sha256) { $qrChanged += 1 }
}
[pscustomobject]@{
    missing = $qrMissing
    changed = $qrChanged
    preserved = ($qrMissing -eq 0 -and $qrChanged -eq 0)
} | ConvertTo-Json -Compress
if ($qrMissing -ne 0 -or $qrChanged -ne 0) { exit 1 }
```

Expected: encrypted credentials, settings, task database and downloads have `missing=0` and `changed=0`.

- [ ] **Step 6: Launch the rebuilt executable and perform visual smoke**

Run:

```powershell
Start-Process -FilePath '.\dist\TelegramDownloader\TelegramDownloader.exe' -WorkingDirectory '.\dist\TelegramDownloader'
```

Open account login and display a QR page. Expected: the complete square QR code is visible with white margins on all four sides; no module is clipped by status text or buttons. Close the process after inspection so no orphan process remains.

- [ ] **Step 7: Final repository checks**

Run:

```powershell
git diff --check
git status --short --branch
```

Expected: no whitespace errors; only intentional source/test changes and ignored project-local build artifacts exist.
