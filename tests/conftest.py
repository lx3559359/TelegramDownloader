import os
from pathlib import Path

_ROOT = Path(__file__).parents[1]
_TEST_STATE = _ROOT / ".build-temp" / "pytest-environment"
_TEMP = _TEST_STATE / "temp"
_ROAMING = _TEST_STATE / "user-profile" / "Roaming"
_LOCAL = _TEST_STATE / "user-profile" / "Local"
for _directory in (_TEMP, _ROAMING, _LOCAL):
    _directory.mkdir(parents=True, exist_ok=True)

os.environ["TEMP"] = str(_TEMP)
os.environ["TMP"] = str(_TEMP)
os.environ["APPDATA"] = str(_ROAMING)
os.environ["LOCALAPPDATA"] = str(_LOCAL)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
