"""迁移自 FlowPay 的查询口径：历史记录不是当前供货能力的承诺。"""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from sourcing_agent.harness.contracts import Record

from .external import ExternalObservation


class Requirements(Record):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    hs_prefix: str | None = Field(default=None, pattern=r"^[0-9]{2,12}$")
    product_text: str | None = Field(default=None, min_length=2, max_length=160)
    supplier_country: str | None = Field(default=None, min_length=2, max_length=100)
    market_country: str | None = Field(default=None, min_length=2, max_length=100)
    start_date: date | None = None
    end_date: date | None = None
    exclude_supplier_id: int | None = Field(default=None, gt=0, strict=True)
    limit: int = Field(default=5, ge=1, le=10, strict=True)
    ranking: Literal["recent", "customer_coverage", "activity"] = "recent"
    unverified_conditions: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("起始日期不能晚于结束日期")
        return self

    def missing(self) -> list[str]:
        result = []
        if not self.hs_prefix and not self.product_text:
            result.append("商品名称或 HS 编码")
        if not self.start_date or not self.end_date:
            result.append("起止日期")
        return result


class TradeRecord(Record):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    record_id: int = Field(gt=0, strict=True)
    source_table: str = "dbo.panjivaCOImport"
    shipment_date: date | None
    supplier_id: int | None = Field(default=None, gt=0, strict=True)
    supplier_name: str | None = None
    supplier_country: str | None = None
    buyer_id: int | None = Field(default=None, gt=0, strict=True)
    buyer_name: str | None = None
    buyer_country: str | None = None
    hs_code: str | None = None
    goods_description: str | None = None
    cif_usd: float | None = None
    source_file_date: date | None = None


class Candidate(Record):
    supplier_id: int
    supplier_name: str
    supplier_country: str | None
    record_count: int
    buyer_count: int
    active_months: int
    latest_date: str


class EvidenceSource(TradeRecord):
    snapshot_sha256: str


class TradeEvidence(Record):
    id: str
    title: str
    text: str
    source: EvidenceSource


class MonthActivity(Record):
    month: str
    record_count: int


class BuyerActivity(Record):
    buyer_id: int
    buyer_name: str | None
    record_count: int


class ProductActivity(Record):
    hs_code: str | None
    record_count: int


class ProductMatch(Record):
    hs_code: str | None
    description: str | None
    record_count: int


class SupplierProfile(Record):
    supplier_id: int
    monthly_activity: list[MonthActivity]
    buyers: list[BuyerActivity]
    products: list[ProductActivity]
    record_count: int
    missing_buyer_ids: int


class Comparison(Record):
    supplier_ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(min_length=2, max_length=5)
    metric: Literal["buyer_count", "record_count", "active_months", "latest_date"] = "buyer_count"


class ComparisonResult(Record):
    metric: str
    winner_ids: list[int]
    candidates: list[Candidate]
    snapshot_sha256: str
    requirements: Requirements


class ResearchState(Record):
    format_version: Literal[1] = 1
    snapshot_sha256: str
    requirements: Requirements = Field(default_factory=Requirements)
    revision: int = 0
    queried: bool = False
    candidates: list[Candidate] = Field(default_factory=list)
    evidence: list[TradeEvidence] = Field(default_factory=list)
    profiles: list[SupplierProfile] = Field(default_factory=list)
    comparisons: list[ComparisonResult] = Field(default_factory=list)
    external_observations: list[ExternalObservation] = Field(default_factory=list)
    external_config_sha256: str | None = None
