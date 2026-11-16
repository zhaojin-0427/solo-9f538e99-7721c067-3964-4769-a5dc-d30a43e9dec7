"""请求与响应的 Pydantic 模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


def _non_empty(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("字符串不能为空")
    return value


# ---------- 请求模型 ----------

class ObservationIn(BaseModel):
    code: str = Field(..., min_length=1, description="观测唯一编号")
    name: str = Field(..., min_length=1, description="野外记录的物种名（可能是旧名/同义名）")

    @field_validator("code", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return _non_empty(v)


class BatchCreate(BaseModel):
    observations: list[ObservationIn] = Field(
        ..., min_length=1, max_length=100, description="一批观测，至多 100 条"
    )
    note: str | None = Field(None, description="备注")


class EdgeIn(BaseModel):
    synonym: str = Field(..., min_length=1, description="同义名（旧名）")
    target: str = Field(..., min_length=1, description="指向的名称（接受名或另一同义名）")

    @field_validator("synonym", "target")
    @classmethod
    def _strip(cls, v: str) -> str:
        return _non_empty(v)


class TaxonomyCreate(BaseModel):
    accepted_names: list[str] = Field(..., min_length=1, description="接受名列表")
    synonym_edges: list[EdgeIn] = Field(
        default_factory=list, max_length=40, description="同义名指向关系，至多 40 条，允许分叉"
    )
    note: str | None = None

    @field_validator("accepted_names")
    @classmethod
    def _strip_names(cls, values: list[str]) -> list[str]:
        cleaned = [_non_empty(v) for v in values]
        return cleaned


class ResultCreate(BaseModel):
    batch_id: int = Field(..., description="观测批次 ID")
    revision_id: int | None = Field(None, description="分类表修订 ID，缺省使用当前最新修订")


class ConfirmationCreate(BaseModel):
    result_id: int = Field(..., description="当前供数结果号")
    observation_code: str = Field(..., min_length=1, description="待核观测在批次内的编号")
    accepted_name: str = Field(
        ..., min_length=1, description="候选接受名：必须是该观测分叉路径的终点接受名之一"
    )
    basis: str = Field(..., min_length=1, description="人工核定依据（鉴定证据说明）")

    @field_validator("observation_code", "accepted_name", "basis")
    @classmethod
    def _strip(cls, v: str) -> str:
        return _non_empty(v)


# ---------- 响应模型 ----------

class ObservationOut(BaseModel):
    line_no: int
    code: str
    name: str


class BatchOut(BaseModel):
    id: int
    note: str | None
    created_at: str
    observation_count: int
    observations: list[ObservationOut]


class BatchSummaryOut(BaseModel):
    id: int
    note: str | None
    created_at: str
    observation_count: int


class EdgeOut(BaseModel):
    synonym: str
    target: str


class TaxonomyOut(BaseModel):
    revision_id: int
    note: str | None
    created_at: str
    superseded_at: str | None
    accepted_count: int
    edge_count: int
    accepted_names: list[str]
    synonym_edges: list[EdgeOut]


class TaxonomySummaryOut(BaseModel):
    revision_id: int
    note: str | None
    created_at: str
    superseded_at: str | None
    accepted_count: int
    edge_count: int


class ResultItemOut(BaseModel):
    line_no: int
    observation_code: str
    input_name: str
    item_status: str
    accepted_name: str | None
    reason_code: str | None
    reason: str | None
    confirmation_id: int | None = Field(
        None, description="套用的有效人工核定号（无核定供数时为 null）"
    )
    confirmation_basis: str | None = Field(
        None, description="人工核定依据（无核定供数时为 null）"
    )


class ResultSummaryOut(BaseModel):
    result_id: int
    batch_id: int
    revision_id: int
    status: str
    created_at: str
    included_count: int
    pending_count: int


class ResultDetailOut(ResultSummaryOut):
    items: list[ResultItemOut]


class ConfirmationOut(BaseModel):
    confirmation_id: int
    result_id: int
    batch_id: int
    observation_code: str
    accepted_name: str
    basis: str
    terminal_set: list[str] = Field(
        ..., description="核定时该观测所有路径的终点接受名集合（JSON 数组，按名称排序）"
    )
    revision_id: int = Field(..., description="核定时结果所固定的分类表修订")
    status: str = Field(..., description="valid 有效 / invalidated 已永久失效")
    invalidated_reason_code: str | None
    invalidated_reason: str | None
    created_at: str
    invalidated_at: str | None
