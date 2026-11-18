"""异常检测接口：维护检测规则，并提供信号表的窗口复用构建、显示、确认与跨模块核对。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, PageResult
from app.services.anomaly_detect import AnomalyDetectService
from app.services.anomaly_signals import CHECK_STANDARD, VALID_MODES, signal_service

router = APIRouter(prefix="/api/anomaly_detect", tags=["异常检测"])

service = AnomalyDetectService()

LIST_FIELDS = ["规则编号", "监控指标", "检测算法", "基线窗口", "波动阈值", "告警级别", "静默时段", "规则版本"]
STATUSES = ["草稿", "待校验", "已生效", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已生效、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、规则编号关键字与状态过滤异常检测列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="anomaly_detect.csv"'})


# ----------------------------------------------------------------------
# 异常检测信号表（V005）：历史数据、基线缓存、构建/续写/显示/确认/消费/核对
# ----------------------------------------------------------------------
@router.get("/history/series")
def get_history(metric: str | None = Query(default=None, description="按监控指标过滤")) -> dict:
    """读取历史监控数据；按指标过滤，避免每条规则整段重扫。"""
    rows = signal_service.list_history(metric)
    return {"ok": True, "total": len(rows), "items": rows}


@router.get("/baseline/cache")
def get_baseline_cache() -> dict:
    """基线窗口缓存注册表：展示窗口范围、统计量与规则版本同步登记情况。"""
    rows = signal_service.list_cache_registry()
    return {"ok": True, "total": len(rows), "items": rows}


@router.post("/{entry_id}/signals/build")
def build_signals(entry_id: int, payload: dict | None = None) -> dict:
    """构建异常检测信号表：复用可重叠窗口，按 V005 核对阈值、范围和上限。

    payload: {"mode": "auto|reuse_patch|full_rebuild", "fail_after": 可选，用于模拟中断}
    """
    payload = payload or {}
    mode = str(payload.get("mode", "auto"))
    if mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"mode 仅支持 {VALID_MODES}")
    fail_after = payload.get("fail_after")
    result = signal_service.build_signal_table(
        entry_id, mode=mode, fail_after=int(fail_after) if fail_after is not None else None)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "信号表构建失败"))
    return result


@router.post("/{entry_id}/signals/resume")
def resume_signals(entry_id: int) -> dict:
    """窗口重建中断后按缺失项续写，不覆盖上一完整信号表。"""
    result = signal_service.resume_build(entry_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "续写失败"))
    return result


@router.get("/{entry_id}/signals/display")
def display_signals(entry_id: int) -> dict:
    """信号表显示窗：残留事务一旦发现便回滚，并按 V005 留档核对。"""
    result = signal_service.display_signal_table(entry_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.post("/{entry_id}/signals/confirm")
def confirm_signals(entry_id: int) -> dict:
    """V005 核对通过后确认信号表，交消费端取得全量信号表。"""
    result = signal_service.confirm_signal_table(entry_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.get("/signals/overview")
def signals_overview() -> dict:
    """全量 V005 异常检测信号表概览，列表侧栏与详情页共用同一组数据。"""
    return signal_service.list_signal_tables()


@router.get("/signals/consumer")
def consumer_pull() -> dict:
    """消费端取得全量已确认的 V005 异常检测信号表。"""
    return signal_service.consumer_pull()


@router.get("/signals/cross-check")
def cross_check(
    metric: str = Query(description="指标定义模块传入的指标编号"),
    metric_version: str | None = Query(default=None, description="指标定义口径版本"),
) -> dict:
    """跨模块联动（V005）：核对信号表中的指标编号、规则版本与复用窗口基线范围。"""
    return signal_service.cross_check_metric(metric, metric_version)


@router.get("/signals/audit")
def signal_audit(rule_id: int | None = Query(default=None)) -> dict:
    """V005 留档核对记录：构建、回滚、续写、确认、跨模块联动全程可追溯。"""
    rows = signal_service.list_audit(rule_id)
    return {"ok": True, "核对标准": CHECK_STANDARD, "total": len(rows), "items": rows}


@router.post("/signals/reset")
def reset_signal_environment() -> dict:
    """演练环境重置：恢复种子规则并清空缓存与信号表（E2E/演示用维护接口）。"""
    return signal_service.reset_demo_environment()


@router.patch("/{entry_id}/rule-config")
def update_rule_config(entry_id: int, payload: dict) -> dict:
    """调整基线窗口/波动阈值/上限策略/规则版本，缓存注册表同步登记新版本。"""
    result = signal_service.change_rule(entry_id, payload)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条检测规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"检测规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条检测规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="检测规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条检测规则执行提交校验、标记生效、停用规则；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
