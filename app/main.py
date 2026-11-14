"""FastAPI surface of the notebook recomputation service.

Endpoints
---------
POST /batches                  upload analysis.ipynb + observations.csv +
                               manuscript results JSON, start a rerun batch
GET  /batches                  list batches and slot usage
GET  /batches/{id}             poll batch status / verdict
GET  /batches/{id}/notebook    download the freshly executed notebook
GET  /batches/{id}/report      download the comparison report (JSON)

Batch state is kept in memory only.
"""
from __future__ import annotations

import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from .manager import STATUS_RUNNING, Batch, BatchManager, _iso

WORK_DIR = Path(os.environ.get("RECALC_WORK_DIR", "/tmp/recalc_batches"))
TIMEOUT_SEC = int(os.environ.get("RECALC_TIMEOUT_SEC", "60"))
MAX_PARALLEL = int(os.environ.get("RECALC_MAX_PARALLEL", "2"))

DESCRIPTION = f"""\
复算服务：用原始 CSV 在全新 Python 内核里重跑分析 notebook，核对稿件效应量。

* 每批在独立目录中顺序执行全部代码单元，**忽略 notebook 自带缓存输出**。
* 只比较本次生成的 `result.json` 中的 `estimate` / `ci_low` / `ci_high`，
  三者均须为有限数且绝对误差 ≤ 1e-6 才判定一致（`match`）。
* 最多并行 **{MAX_PARALLEL}** 批，满额返回 **429**；执行硬上限 **{TIMEOUT_SEC}s**。
* 超时、执行报错、缺 `result.json` 或结果非有限数均判 `failed`。
* 结论在内核与本批全部计算子进程退出后才公布；迟到写入不改变已公布结论。
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    # State is in-memory only; drop artifacts left over from a previous run.
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    app.state.manager = BatchManager(
        WORK_DIR, max_parallel=MAX_PARALLEL, timeout_sec=TIMEOUT_SEC
    )
    yield


app = FastAPI(
    title="Notebook Recomputation Service",
    version="1.0.0",
    description=DESCRIPTION,
    lifespan=lifespan,
)


def get_manager(request: Request) -> BatchManager:
    return request.app.state.manager


def _status_payload(batch: Batch) -> dict:
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "submitted_at": _iso(batch.submitted_at),
        "finished_at": _iso(batch.finished_at),
        "report": batch.report,
        "links": {
            "self": f"/batches/{batch.id}",
            "notebook": f"/batches/{batch.id}/notebook",
            "report": f"/batches/{batch.id}/report",
        },
    }


def _get_batch_or_404(manager: BatchManager, batch_id: str) -> Batch:
    batch = manager.get(batch_id)
    if batch is None:
        raise HTTPException(404, f"unknown batch_id: {batch_id}")
    return batch


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.post(
    "/batches",
    status_code=202,
    summary="提交复算批（notebook + CSV + 稿件结果 JSON）",
    responses={429: {"description": "并行批次已满，请稍后重试"}},
)
async def create_batch(
    request: Request,
    notebook: UploadFile = File(..., description="analysis.ipynb — 待重跑的分析 notebook"),
    observations: UploadFile = File(..., description="observations.csv — 原始随访数据"),
    expected: UploadFile = File(..., description="稿件结果 JSON（estimate/ci_low/ci_high）"),
):
    manager = get_manager(request)
    if manager.active >= manager.max_parallel:  # fast pre-check, saves the upload
        raise HTTPException(
            429,
            f"server is busy: at most {manager.max_parallel} batches in parallel; retry later",
        )

    nb_bytes = await notebook.read()
    csv_bytes = await observations.read()
    exp_bytes = await expected.read()

    try:
        nb_obj = json.loads(nb_bytes)
        if not (isinstance(nb_obj, dict) and isinstance(nb_obj.get("cells"), list)):
            raise ValueError("missing 'cells'")
    except Exception:
        raise HTTPException(400, "notebook file is not a valid .ipynb (JSON with 'cells')")
    try:
        expected_obj = json.loads(exp_bytes)
        if not isinstance(expected_obj, dict):
            raise ValueError
    except Exception:
        raise HTTPException(400, "expected results file must be a JSON object")

    batch = await manager.try_create(nb_bytes, csv_bytes, expected_obj)
    if batch is None:
        raise HTTPException(
            429,
            f"server is busy: at most {manager.max_parallel} batches in parallel; retry later",
        )
    return _status_payload(batch)


@app.get("/batches", summary="列出全部批次与槽位占用")
def list_batches(request: Request):
    return get_manager(request).summary()


@app.get("/batches/{batch_id}", summary="查询批次状态与结论")
def get_batch(request: Request, batch_id: str):
    batch = _get_batch_or_404(get_manager(request), batch_id)
    return _status_payload(batch)


@app.get(
    "/batches/{batch_id}/notebook",
    summary="下载本次执行后的 notebook",
    responses={
        409: {"description": "批次仍在执行"},
        404: {"description": "无已执行 notebook（如超时强杀）"},
    },
)
def get_executed_notebook(request: Request, batch_id: str):
    batch = _get_batch_or_404(get_manager(request), batch_id)
    if batch.status == STATUS_RUNNING:
        raise HTTPException(409, "batch is still running")
    path = batch.dir / "analysis_executed.ipynb"
    if not path.exists():
        raise HTTPException(
            404, "no executed notebook available (e.g. the run was killed on timeout)"
        )
    return FileResponse(
        path,
        media_type="application/x-ipynb+json",
        filename="analysis_executed.ipynb",
    )


@app.get(
    "/batches/{batch_id}/report",
    summary="下载比较报告 JSON",
    responses={409: {"description": "批次仍在执行"}},
)
def get_report(request: Request, batch_id: str):
    batch = _get_batch_or_404(get_manager(request), batch_id)
    if batch.status == STATUS_RUNNING:
        raise HTTPException(409, "batch is still running")
    path = batch.dir / "report.json"
    if not path.exists():
        raise HTTPException(404, "report file missing")
    return FileResponse(path, media_type="application/json", filename="report.json")
