"""显式长期偏好的有界召回；它们不自动成为交易查询的硬条件。"""

import re

from .store import Memory, RuntimeStore


class MemoryService:
    def __init__(self, store: RuntimeStore, namespace: str) -> None:
        self.store = store
        self.namespace = namespace

    def recall(self, query: str, limit: int = 5, max_chars: int = 2400) -> list[Memory]:
        terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", query.lower()))

        def score(item: Memory) -> int:
            text = (item.key + " " + item.value).lower()
            return sum(term in text for term in terms)

        ranked = sorted(self.store.memories(self.namespace), key=score, reverse=True)
        result: list[Memory] = []
        used = 0
        for item in ranked:
            size = len(item.key) + len(item.value)
            if score(item) == 0 or len(result) >= limit or used + size > max_chars:
                continue
            result.append(item)
            used += size
        return result
