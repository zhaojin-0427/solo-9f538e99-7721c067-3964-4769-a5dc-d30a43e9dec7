"""FastAPI 应用：观测录入、分类表整版更正、供数结果生成与查询。

启动后访问 /docs 可直接在 Swagger UI 中录入、生成并查询结果。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlite3 import Connection

from . import services
from .database import connect, init_db
from .models import (
    BatchCreate,
    BatchOut,
    BatchSummaryOut,
    ConfirmationCreate,
    ConfirmationOut,
    EdgeOut,
    ObservationOut,
    ResultCreate,
    ResultDetailOut,
    ResultItemOut,
    ResultSummaryOut,
    TaxonomyCreate,
    TaxonomyOut,
    TaxonomySummaryOut,
)
from .services import ConflictError, NotFoundError


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    init_db(conn)
    conn.close()
    yield


app = FastAPI(
    title="生态监测名称归并供数 API",
    description=(
        "把野外观测的旧物种名沿分类表同义名关系追踪到接受名。"
        "观测至多 100 条/批；分类表整版更正生成新修订（同义关系至多 40 条，允许分叉）；"
        "结果固定批次与修订，可按结果号重读，更正后旧结果自动过期。"
        "复核员可对无环、所有路径均终止于接受名的分叉待核观测做人工核定，"
        "后续供数自动套用有效核定。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def get_conn() -> Connection:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _row_to_batch_out(conn: Connection, batch_id: int):
    row = services.get_batch(conn, batch_id)
    obs_rows = services.list_observations(conn, batch_id)
    return BatchOut(
        id=row["id"],
        note=row["note"],
        created_at=row["created_at"],
        observation_count=len(obs_rows),
        observations=[
            ObservationOut(line_no=r["line_no"], code=r["code"], name=r["name"])
            for r in obs_rows
        ],
    )


@app.get("/health", tags=["系统"], summary="健康检查")
def health():
    return {"status": "ok"}


# ---------- 观测批次 ----------

@app.post(
    "/batches",
    response_model=BatchOut,
    status_code=201,
    tags=["观测批次"],
    summary="录入一批观测（至多 100 条，编号在批内唯一）",
)
def create_batch(payload: BatchCreate, conn: Connection = Depends(get_conn)):
    try:
        batch_id = services.create_batch(conn, payload)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _row_to_batch_out(conn, batch_id)


@app.get(
    "/batches",
    response_model=list[BatchSummaryOut],
    tags=["观测批次"],
    summary="列出观测批次",
)
def list_batches(conn: Connection = Depends(get_conn)):
    return [dict(r) for r in services.list_batches(conn)]


@app.get(
    "/batches/{batch_id}",
    response_model=BatchOut,
    tags=["观测批次"],
    summary="读取一个观测批次（含全部观测）",
)
def get_batch(batch_id: int, conn: Connection = Depends(get_conn)):
    if services.get_batch(conn, batch_id) is None:
        raise HTTPException(status_code=404, detail=f"观测批次不存在：batch_id={batch_id}")
    return _row_to_batch_out(conn, batch_id)


# ---------- 分类表修订 ----------

@app.post(
    "/taxonomies",
    response_model=TaxonomyOut,
    status_code=201,
    tags=["分类表"],
    summary="整版提交分类表（自动生成新修订，旧修订与旧结果过期）",
)
def create_taxonomy(payload: TaxonomyCreate, conn: Connection = Depends(get_conn)):
    try:
        revision_id = services.create_taxonomy_revision(conn, payload)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _row_to_taxonomy_out(conn, revision_id)


def _row_to_taxonomy_out(conn: Connection, revision_id: int) -> TaxonomyOut:
    row = services.get_revision(conn, revision_id)
    accepted = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM accepted_name WHERE revision_id = ? ORDER BY rowid",
            (revision_id,),
        )
    ]
    edges = [
        EdgeOut(synonym=r["synonym"], target=r["target"])
        for r in conn.execute(
            "SELECT synonym, target FROM synonym_edge WHERE revision_id = ? ORDER BY rowid",
            (revision_id,),
        )
    ]
    return TaxonomyOut(
        revision_id=row["revision_id"],
        note=row["note"],
        created_at=row["created_at"],
        superseded_at=row["superseded_at"],
        accepted_count=len(accepted),
        edge_count=len(edges),
        accepted_names=accepted,
        synonym_edges=edges,
    )


@app.get(
    "/taxonomies",
    response_model=list[TaxonomySummaryOut],
    tags=["分类表"],
    summary="列出分类表修订",
)
def list_taxonomies(conn: Connection = Depends(get_conn)):
    return [dict(r) for r in services.list_revisions(conn)]


@app.get(
    "/taxonomies/{revision_id}",
    response_model=TaxonomyOut,
    tags=["分类表"],
    summary="读取一个分类表修订的完整内容",
)
def get_taxonomy(revision_id: int, conn: Connection = Depends(get_conn)):
    if services.get_revision(conn, revision_id) is None:
        raise HTTPException(
            status_code=404, detail=f"分类表修订不存在：revision_id={revision_id}"
        )
    return _row_to_taxonomy_out(conn, revision_id)


# ---------- 供数结果 ----------

@app.post(
    "/results",
    response_model=ResultDetailOut,
    status_code=201,
    tags=["供数结果"],
    summary="生成供数结果（固定批次与修订，按结果号可重读）",
)
def generate_result(payload: ResultCreate, conn: Connection = Depends(get_conn)):
    try:
        result_id = services.generate_result(conn, payload)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _row_to_result_detail(conn, result_id)


def _row_to_result_detail(conn: Connection, result_id: int) -> ResultDetailOut:
    row = services.get_result(conn, result_id)
    items = services.list_result_items(conn, result_id)
    return ResultDetailOut(
        result_id=row["result_id"],
        batch_id=row["batch_id"],
        revision_id=row["revision_id"],
        status=row["status"],
        created_at=row["created_at"],
        included_count=row["included_count"],
        pending_count=row["pending_count"],
        items=[
            ResultItemOut(
                line_no=r["line_no"],
                observation_code=r["observation_code"],
                input_name=r["input_name"],
                item_status=r["item_status"],
                accepted_name=r["accepted_name"],
                reason_code=r["reason_code"],
                reason=r["reason"],
                confirmation_id=r["confirmation_id"],
                confirmation_basis=r["confirmation_basis"],
            )
            for r in items
        ],
    )


def _row_to_confirmation_out(row) -> ConfirmationOut:
    return ConfirmationOut(
        confirmation_id=row["confirmation_id"],
        result_id=row["result_id"],
        batch_id=row["batch_id"],
        observation_code=row["observation_code"],
        accepted_name=row["accepted_name"],
        basis=row["basis"],
        terminal_set=json.loads(row["terminal_set"]),
        revision_id=row["revision_id"],
        status=row["status"],
        invalidated_reason_code=row["invalidated_reason_code"],
        invalidated_reason=row["invalidated_reason"],
        created_at=row["created_at"],
        invalidated_at=row["invalidated_at"],
    )


@app.get(
    "/results",
    response_model=list[ResultSummaryOut],
    tags=["供数结果"],
    summary="列出供数结果（可按批次、状态过滤）",
)
def list_results(
    conn: Connection = Depends(get_conn),
    batch_id: int | None = Query(None, description="按观测批次过滤"),
    status: str | None = Query(None, pattern="^(active|expired)$", description="active/expired"),
):
    return [dict(r) for r in services.list_results(conn, batch_id, status)]


@app.get(
    "/results/{result_id}",
    response_model=ResultDetailOut,
    tags=["供数结果"],
    summary="按结果号重读供数结果（过期结果仍可读取，status 标记为 expired）",
)
def get_result(result_id: int, conn: Connection = Depends(get_conn)):
    if services.get_result(conn, result_id) is None:
        raise HTTPException(status_code=404, detail=f"供数结果不存在：result_id={result_id}")
    return _row_to_result_detail(conn, result_id)


# ---------- 人工核定 ----------

@app.post(
    "/confirmations",
    response_model=ConfirmationOut,
    tags=["人工核定"],
    summary="提交人工核定（仅无环全接受名终点的分叉待核记录；同内容重试幂等，异内容 409）",
    status_code=201,
)
def create_confirmation(payload: ConfirmationCreate, conn: Connection = Depends(get_conn)):
    try:
        confirmation_id, replayed = services.create_confirmation(conn, payload)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    row = services.get_confirmation(conn, confirmation_id)
    response = _row_to_confirmation_out(row)
    # 同内容重试返回原核定（不新建），用 200 与新建的 201 区分
    return JSONResponse(
        content=response.model_dump(),
        status_code=200 if replayed else 201,
    )


@app.get(
    "/confirmations",
    response_model=list[ConfirmationOut],
    tags=["人工核定"],
    summary="列出人工核定（可按结果号、状态 valid/invalidated 过滤）",
)
def list_confirmations(
    conn: Connection = Depends(get_conn),
    result_id: int | None = Query(None, description="按供数结果号过滤"),
    status: str | None = Query(
        None, pattern="^(valid|invalidated)$", description="valid/invalidated"
    ),
):
    return [_row_to_confirmation_out(r) for r in
            services.list_confirmations(conn, result_id, status)]


@app.get(
    "/confirmations/{confirmation_id}",
    response_model=ConfirmationOut,
    tags=["人工核定"],
    summary="按核定号读取核定内容、依据与失效状态",
)
def get_confirmation(confirmation_id: int, conn: Connection = Depends(get_conn)):
    row = services.get_confirmation(conn, confirmation_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"人工核定不存在：confirmation_id={confirmation_id}",
        )
    return _row_to_confirmation_out(row)
