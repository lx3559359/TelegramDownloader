from __future__ import annotations

import argparse
import statistics
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telegram_downloader.content import SearchResult  # noqa: E402
from telegram_downloader.domain import MediaKind  # noqa: E402
from telegram_downloader.ui.content_models import SearchResultTableModel  # noqa: E402


def make_results(count: int) -> list[SearchResult]:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    base = SearchResult(
        "r0",
        "s1",
        "a1",
        "peer",
        count,
        None,
        "m0",
        MediaKind.PHOTO,
        "image.jpg",
        1024,
        now,
        "synthetic",
        "synthetic-thumb",
    )
    return [
        replace(
            base,
            id=f"r{index}",
            media_id=f"m{index}",
            thumbnail_key=f"t{index}",
        )
        for index in range(count)
    ]


def median_ms(operation: Callable[[], object], repeats: int) -> float:
    values: list[float] = []
    for _ in range(repeats):
        started = perf_counter()
        operation()
        values.append((perf_counter() - started) * 1000)
    return statistics.median(values)


def measure(count: int, repeats: int) -> tuple[float, float]:
    values = make_results(count)
    initial = median_ms(
        lambda: SearchResultTableModel().apply_results(values),
        repeats,
    )
    model = SearchResultTableModel()
    model.apply_results(values)
    updated = list(values)
    updated[-1] = replace(updated[-1], selected=True)
    changed = median_ms(lambda: model.apply_results(updated), repeats)
    return initial, changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats <= 0:
        raise SystemExit("repeats must be positive")
    measurements: dict[int, tuple[float, float]] = {}
    for count in (100, 500, 1_000, 2_000, 10_000):
        initial, changed = measure(count, args.repeats)
        measurements[count] = (initial, changed)
        print(
            f"ROWS={count} INITIAL_MEDIAN_MS={initial:.2f} "
            f"ONE_ROW_MEDIAN_MS={changed:.2f}"
        )
    ratio = measurements[10_000][1] / max(measurements[2_000][1], 0.001)
    print(f"UPDATE_SCALE_10000_OVER_2000={ratio:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
