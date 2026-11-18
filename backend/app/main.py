"""多维分析实验平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.store import store

app = FastAPI(title="多维分析实验平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()


@app.post("/api/test/reset-environment")
def reset_environment() -> dict[str, object]:
    """恢复内存仓库到种子初始状态。

    仅用于 E2E 与演示环境：让每次自动化运行都从确定数据开始。生产环境若接入
    持久化数据库，应删除或用权限保护该接口。
    """
    store.reset_all()
    return {"ok": True, "message": "演练环境已重置为种子状态", "modules": len(store.module_names())}
