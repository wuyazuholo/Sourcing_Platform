"""业务编排只依赖交易能力契约，SQLite 实现由装配层注入。"""

from typing import Protocol

from .models import Candidate, ProductMatch, Requirements, SupplierProfile, TradeEvidence


class TradeSource(Protocol):
    sha256: str
    metadata: dict[str, str]
    start_date: str | None
    end_date: str | None

    def find_candidates(self, req: Requirements) -> list[Candidate]: ...
    def evidence(self, req: Requirements, supplier_id: int) -> list[TradeEvidence]: ...
    def profile(self, req: Requirements, supplier_id: int) -> SupplierProfile: ...
    def search_products(self, text: str) -> list[ProductMatch]: ...
