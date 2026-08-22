from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum


class SubscriptionMatchMode(StrEnum):
    ANY = "any"
    ALL = "all"


def normalize_subscription_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _clean_terms(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    found: list[str] = []
    normalized: set[str] = set()
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        if len(value) > 100:
            raise ValueError(f"{label}单项不能超过 100 个字符")
        key = normalize_subscription_text(value)
        if key not in normalized:
            normalized.add(key)
            found.append(value)
    if len(found) > 20:
        raise ValueError(f"{label}最多 20 个")
    return tuple(found)


@dataclass(frozen=True, slots=True)
class SubscriptionCriteria:
    include_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...] = ()
    mode: SubscriptionMatchMode = SubscriptionMatchMode.ANY

    def __post_init__(self) -> None:
        includes = _clean_terms(self.include_keywords, "包含词")
        excludes = _clean_terms(self.exclude_keywords, "排除词")
        if not includes:
            raise ValueError("请至少输入一个包含词")
        if sum(map(len, includes + excludes)) > 2000:
            raise ValueError("全部订阅词组合计不能超过 2000 个字符")
        overlap = set(map(normalize_subscription_text, includes)) & set(
            map(normalize_subscription_text, excludes)
        )
        if overlap:
            raise ValueError("同一个词不能同时出现在包含词和排除词中")
        object.__setattr__(self, "include_keywords", includes)
        object.__setattr__(self, "exclude_keywords", excludes)

    @property
    def fingerprint(self) -> str:
        payload = {
            "exclude": sorted(
                map(normalize_subscription_text, self.exclude_keywords)
            ),
            "include": sorted(
                map(normalize_subscription_text, self.include_keywords)
            ),
            "mode": self.mode.value,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def summary(self) -> str:
        label = "全部" if self.mode is SubscriptionMatchMode.ALL else "任意"
        value = f"{label}：{'、'.join(self.include_keywords)}"
        if self.exclude_keywords:
            value += f"；排除：{'、'.join(self.exclude_keywords)}"
        return value

    def matches(self, text: str) -> bool:
        normalized = normalize_subscription_text(text)
        include_hits = [
            normalize_subscription_text(term) in normalized
            for term in self.include_keywords
        ]
        included = (
            all(include_hits)
            if self.mode is SubscriptionMatchMode.ALL
            else any(include_hits)
        )
        excluded = any(
            normalize_subscription_text(term) in normalized
            for term in self.exclude_keywords
        )
        return included and not excluded
