"""异常检测信号表引擎。

解决的问题：旧实现对每条检测规则都整段重扫历史监控数据、重算重叠基线，
长窗口任务耗时随规则数与历史长度持续升高。

本模块围绕三张可追溯的数据表组织：

1. ``anomaly_history``  历史监控数据（指标时间序列），只按需读取；
2. ``anomaly_baseline_cache``  基线窗口缓存注册表，按「指标 + 窗口范围」复用
   可重叠的窗口结果，并同步登记使用它的规则版本（避免规则更新后读到旧基线）；
3. ``anomaly_signals`` / ``anomaly_signals_staging`` / ``anomaly_signals_archived``
   异常检测信号表：活动表、构建暂存表（事务）、历史归档表。每条信号都保留
   基线范围（起止下标/时间戳）与规则版本。

判定标准统一按 **V005** 留档：
- 范围核对（range_check）：基线样本数必须等于规则声明的基线窗口；
- 阈值核对（threshold_check）：偏差比 ``|最新值-均值|/标准差`` 必须超过波动阈值；
- 上限核对（upper_limit_check）：最新值必须超过基线上限
  ``上限 = 均值 + 波动阈值 × 标准差``。
三项全部通过才记为异常信号，核对明细随信号留档。

只依赖标准库与内存仓库 ``app.store``，与项目其它服务保持同一套约定。
"""
from __future__ import annotations

import math
import random
import time
from typing import Any

from app.store import store

# ---------------------------------------------------------------------------
# V005 条件标准与表名常量
# ---------------------------------------------------------------------------
CHECK_STANDARD = "V005"
HISTORY_MODULE = "anomaly_history"
CACHE_MODULE = "anomaly_baseline_cache"
SIGNAL_MODULE = "anomaly_signals"
STAGING_MODULE = "anomaly_signals_staging"
ARCHIVED_MODULE = "anomaly_signals_archived"
AUDIT_MODULE = "anomaly_signal_audit"

# 触发「切换基线窗口 + 读缓存」版本冲突时，两种处理策略
MODE_REUSE_PATCH = "reuse_patch"        # 复用重叠窗口 + 补算受影响边界
MODE_FULL_REBUILD = "full_rebuild"      # 按新窗口全量重建
VALID_MODES = ("auto", MODE_REUSE_PATCH, MODE_FULL_REBUILD)

# auto 模式下，重叠窗口占比达到该比例才走复用补算，否则全量重建
REUSE_OVERLAP_RATIO = 0.5

# 种子历史与种子规则（首次访问时惰性写入，测试清空仓库后也能自动重建）
SEED_METRICS = ["点击率", "支付转化率", "人均停留时长"]
HISTORY_POINTS = 48

# 规则字段 -> 默认值。种子数据里的异常检测规则会按这里补齐为可运行配置。
DEFAULT_WINDOW = 7
DEFAULT_THRESHOLD = 0.3
DEFAULT_UPPER_LIMIT_POLICY = "mean + 波动阈值 × std"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _to_int(value: Any, default: int) -> int:
    try:
        number = int(float(value))
        return number if number > 0 else default
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class AnomalySignalService:
    """异常检测信号表：窗口复用、增量重算、事务构建与跨模块核对。"""

    _initialized = False

    # ------------------------------------------------------------------
    # 初始化与基础读取
    # ------------------------------------------------------------------
    def _ensure_initialized(self) -> None:
        """惰性写入历史监控数据，并把检测规则补齐为可运行配置。

        测试会整体清空内存仓库，因此不能只看类级标记：历史表为空时也要重新播种。
        """
        if self._initialized and store.rows(HISTORY_MODULE):
            return
        self._seed_history()
        self._normalize_rules()
        for module in (CACHE_MODULE, STAGING_MODULE, ARCHIVED_MODULE, AUDIT_MODULE):
            store.rows(module)
        self._initialized = True

    def _seed_history(self) -> None:
        """生成确定性的历史监控序列：周期波形 + 可复现的异常尖峰。"""
        rows = store.rows(HISTORY_MODULE)
        if rows:
            return
        rng = random.Random(20261118)
        base_levels = {"点击率": 0.12, "支付转化率": 0.05, "人均停留时长": 90.0}
        for metric in SEED_METRICS:
            base = base_levels[metric]
            for idx in range(HISTORY_POINTS):
                value = base * (1 + 0.08 * math.sin(idx / 3.0)) + rng.uniform(-0.01, 0.01) * base
                # 每个指标尾部放一个尖峰，保证 V005 能核对出异常信号
                if idx in (HISTORY_POINTS - 2, HISTORY_POINTS - 6):
                    value *= 1.9
                point_ts = time.strftime(
                    "%Y-%m-%d %H:%M",
                    time.localtime(time.time() - (HISTORY_POINTS - idx) * 3600),
                )
                rows.append({
                    "id": store.next_id(HISTORY_MODULE),
                    "指标编号": metric,
                    "指标名称": metric,
                    "时间点": idx,
                    "监控时间": point_ts,
                    "监控值": round(value, 6),
                    "数据来源": "历史监控数据",
                })

    def _normalize_rules(self) -> None:
        """把检测规则的窗口/阈值解析为数值，并补全波动上限策略与规则版本。"""
        rules = store.rows("anomaly_detect")
        presets = {
            "点击率": (7, 0.30, "v1"),
            "支付转化率": (5, 0.25, "v1"),
            "人均停留时长": (10, 0.35, "v1"),
        }
        for rule in rules:
            metric = str(rule.get("监控指标") or "")
            preset = presets.get(metric)
            if preset and str(rule.get("基线窗口", "")).strip() in ("", "异常检测样例1",
                                                                     "异常检测样例2",
                                                                     "异常检测样例3"):
                rule["基线窗口"], rule["波动阈值"], rule["规则版本"] = preset
            rule["_window"] = _to_int(rule.get("基线窗口"), DEFAULT_WINDOW)
            rule["_threshold"] = _to_float(rule.get("波动阈值"), DEFAULT_THRESHOLD)
            rule.setdefault("波动上限策略", DEFAULT_UPPER_LIMIT_POLICY)
            rule.setdefault("规则版本", "v1")

    def list_history(self, metric: str | None = None) -> list[dict[str, Any]]:
        self._ensure_initialized()
        rows = store.rows(HISTORY_MODULE)
        if metric:
            rows = [row for row in rows if row.get("指标名称") == metric or row.get("指标编号") == metric]
        return rows

    def _series(self, metric: str) -> list[float]:
        rows = [row for row in store.rows(HISTORY_MODULE)
                if row.get("指标名称") == metric or row.get("指标编号") == metric]
        rows.sort(key=lambda row: int(row.get("时间点", 0)))
        return [float(row["监控值"]) for row in rows]

    def _rule(self, rule_id: int) -> dict[str, Any] | None:
        self._ensure_initialized()
        rule = store.find("anomaly_detect", rule_id)
        if rule is not None:
            rule["_window"] = _to_int(rule.get("基线窗口"), DEFAULT_WINDOW)
            rule["_threshold"] = _to_float(rule.get("波动阈值"), DEFAULT_THRESHOLD)
            rule.setdefault("规则版本", "v1")
        return rule

    # ------------------------------------------------------------------
    # 基线窗口缓存注册表（复用 + 规则版本同步登记）
    # ------------------------------------------------------------------
    def list_cache_registry(self) -> list[dict[str, Any]]:
        self._ensure_initialized()
        return store.rows(CACHE_MODULE)

    @staticmethod
    def _cache_key(metric: str, start: int, end: int) -> str:
        return f"{metric}|{start}:{end}"

    def _find_cache(self, metric: str, start: int, end: int) -> dict[str, Any] | None:
        key = self._cache_key(metric, start, end)
        for entry in store.rows(CACHE_MODULE):
            if entry.get("缓存键") == key:
                return entry
        return None

    def _baseline(self, metric: str, series: list[float], start: int, end: int,
                  rule: dict[str, Any], *, force_recompute: bool) -> tuple[dict[str, Any], bool]:
        """读取或计算一个基线窗口，返回 (缓存条目, 是否命中复用)。

        命中条件：指标与窗口范围完全一致，且缓存登记过当前规则版本。
        规则版本不在登记列表里时视为旧基线，必须重算并同步登记，
        从而保证「复用窗口与规则版本必须同步登记」。
        """
        cached = self._find_cache(metric, start, end)
        rule_version = str(rule.get("规则版本"))
        if cached is not None and not force_recompute and rule_version in (cached.get("规则版本登记") or []):
            return cached, True

        samples = series[start:end]
        count = len(samples)
        mean = sum(samples) / count if count else 0.0
        variance = sum((sample - mean) ** 2 for sample in samples) / count if count else 0.0
        std = math.sqrt(variance)
        history_rows = [row for row in store.rows(HISTORY_MODULE)
                        if (row.get("指标名称") == metric or row.get("指标编号") == metric)
                        and start <= int(row.get("时间点", -1)) < end]
        history_rows.sort(key=lambda row: int(row.get("时间点", 0)))
        timestamps = [str(row.get("监控时间")) for row in history_rows]
        if cached is None:
            entry = {
                "id": store.next_id(CACHE_MODULE),
                "缓存键": self._cache_key(metric, start, end),
                "指标编号": metric,
                "窗口起": start,
                "窗口止": end,
                "窗口大小": end - start,
                "起始时间": timestamps[0] if timestamps else "",
                "结束时间": timestamps[-1] if timestamps else "",
                "样本数": count,
                "基线均值": round(mean, 6),
                "基线标准差": round(std, 6),
                "规则版本登记": [rule_version],
                "来源规则编号": rule.get("规则编号"),
                "复用次数": 0,
                "创建时间": _now(),
                "更新时间": _now(),
            }
            store.rows(CACHE_MODULE).append(entry)
        else:
            entry = cached
            entry["基线均值"] = round(mean, 6)
            entry["基线标准差"] = round(std, 6)
            entry["样本数"] = count
            entry["窗口大小"] = end - start
            entry["更新时间"] = _now()
            registrations = entry.setdefault("规则版本登记", [])
            if rule_version not in registrations:
                registrations.append(rule_version)
        return entry, False

    # ------------------------------------------------------------------
    # V005 判定：范围、阈值、上限三项核对
    # ------------------------------------------------------------------
    @staticmethod
    def _v005_checks(value: float, baseline: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
        window = int(rule["_window"])
        threshold = float(rule["_threshold"])
        mean = float(baseline["基线均值"])
        std = float(baseline["基线标准差"])
        sample_count = int(baseline.get("样本数", 0))
        deviation = abs(value - mean) / std if std > 0 else 0.0
        upper_limit = mean + threshold * std

        range_ok = sample_count == window
        threshold_ok = deviation > threshold
        upper_ok = value > upper_limit
        checks = {
            "range_check": {"通过": range_ok, "标准": f"样本数={sample_count} 应等于基线窗口={window}"},
            "threshold_check": {"通过": threshold_ok,
                                "标准": f"偏差比={deviation:.4f} 应超过波动阈值={threshold}"},
            "upper_limit_check": {"通过": upper_ok,
                                  "标准": f"监控值={value:.4f} 应超过上限={upper_limit:.4f}"},
        }
        return {
            "checks": checks,
            "is_anomaly": range_ok and threshold_ok and upper_ok,
            "deviation": round(deviation, 6),
            "upper_limit": round(upper_limit, 6),
            "mean": round(mean, 6),
            "std": round(std, 6),
        }

    def _audit(self, rule_id: int, rule_version: str, stage: str, message: str,
               standard: str = CHECK_STANDARD) -> None:
        store.rows(AUDIT_MODULE).append({
            "id": store.next_id(AUDIT_MODULE),
            "时间": _now(),
            "规则id": rule_id,
            "规则版本": rule_version,
            "环节": stage,
            "核对标准": standard,
            "说明": message,
        })

    # ------------------------------------------------------------------
    # 版本冲突判定与两种做法的权衡留档
    # ------------------------------------------------------------------
    def _conflict_decision(self, rule: dict[str, Any], mode: str,
                           previous: dict[str, Any] | None) -> dict[str, Any]:
        """同一指标切换基线窗口与读取已有缓存同时发生时的冲突判定。"""
        metric = str(rule.get("监控指标"))
        window = int(rule["_window"])
        cached_windows = sorted({
            int(entry["窗口大小"])
            for entry in store.rows(CACHE_MODULE)
            if entry.get("指标编号") == metric
        })
        conflict = bool(previous and cached_windows and window not in cached_windows)
        call_chain = [
            "anomaly_detect 路由构建信号表",
            "AnomalySignalService.build_signal_table",
            "_conflict_decision 比对规则窗口与基线缓存注册表",
            "_baseline 复用重叠窗口或补算边界",
            "_v005_checks 按 V005 完成范围/阈值/上限核对",
        ]
        if not conflict:
            chosen = MODE_REUSE_PATCH if mode != MODE_FULL_REBUILD else MODE_FULL_REBUILD
            return {
                "版本冲突": False,
                "选择策略": chosen,
                "调用链": call_chain,
                "权衡": {
                    "reuse_patch": "窗口未变化时直接命中缓存，成本最低，仅补算新增/边界窗口",
                    "full_rebuild": "丢弃缓存全量重算，结果最干净但长窗口耗时最高",
                },
            }

        overlap_ratio = 0.0
        if previous:
            old_window = int(previous.get("基线窗口", window))
            overlap = max(0, min(window, old_window))
            overlap_ratio = overlap / max(window, old_window)
        if mode == "auto":
            chosen = MODE_REUSE_PATCH if overlap_ratio >= REUSE_OVERLAP_RATIO else MODE_FULL_REBUILD
        else:
            chosen = mode
        return {
            "版本冲突": True,
            "选择策略": chosen,
            "重叠窗口占比": round(overlap_ratio, 4),
            "调用链": call_chain,
            "权衡": {
                "reuse_patch": "信号表显示窗口复用范围并补算受影响边界，历史窗口命中率高、耗时短；"
                               "代价是需逐信号核对旧缓存的规则版本，边界段仍要重算",
                "full_rebuild": "信号表切到全量重建结果并列出重算范围，口径最一致、无旧基线污染；"
                                "代价是整段历史重扫，长窗口任务耗时随数据量线性升高",
            },
        }

    # ------------------------------------------------------------------
    # 信号表构建（事务化：先写暂存表，成功后整体替换活动表）
    # ------------------------------------------------------------------
    def build_signal_table(self, rule_id: int, *, mode: str = "auto",
                           fail_after: int | None = None) -> dict[str, Any]:
        self._ensure_initialized()
        rule = self._rule(rule_id)
        if rule is None:
            return {"ok": False, "message": f"检测规则 {rule_id} 不存在或已归档"}
        if mode not in VALID_MODES:
            return {"ok": False, "message": f"未知构建策略 {mode}，可选 {VALID_MODES}"}

        metric = str(rule.get("监控指标"))
        series = self._series(metric)
        window = int(rule["_window"])
        if len(series) <= window:
            return {"ok": False, "message": "历史监控数据不足，无法形成基线窗口"}

        previous = store.find(SIGNAL_MODULE, rule_id)
        decision = self._conflict_decision(rule, mode, previous)
        force_recompute = decision["选择策略"] == MODE_FULL_REBUILD
        rule_version = str(rule.get("规则版本"))

        staging = store.rows(STAGING_MODULE)
        # 同规则已有暂存事务：构建中的视为残留由显示窗回滚，已中断的走续写接口
        for stale in staging:
            if stale.get("规则id") == rule_id and stale.get("构建状态") == "构建中":
                stale["构建状态"] = "已回滚"
                self._audit(rule_id, rule_version, "信号表显示窗",
                            "检测到残留构建事务，回滚暂存表且不覆盖上一完整信号表")
        staging[:] = [row for row in staging if row.get("规则id") != rule_id]

        old_baseline_map: dict[int, dict[str, Any]] = {}
        if previous:
            for signal in previous.get("signals", []):
                old_baseline_map[int(signal["时间点"])] = signal

        self._audit(rule_id, rule_version, "信号表构建",
                    f"开始构建，策略={decision['选择策略']}，冲突={decision['版本冲突']}")

        table = {
            "id": rule_id,
            "表编号": f"SIG-TBL-{rule_id}",
            "规则id": rule_id,
            "规则编号": rule.get("规则编号"),
            "指标编号": metric,
            "规则版本": rule_version,
            "核对标准": CHECK_STANDARD,
            "波动阈值": float(rule["_threshold"]),
            "基线窗口": window,
            "上限策略": rule.get("波动上限策略", DEFAULT_UPPER_LIMIT_POLICY),
            "构建策略": decision["选择策略"],
            "版本冲突": decision["版本冲突"],
            "决策留档": decision,
            "复用窗口数": 0,
            "补算窗口数": 0,
            "重算窗口数": 0,
            "复用范围": [],
            "重算覆盖区间": [],
            "signals": [],
            "构建状态": "构建中",
            "构建时间": _now(),
        }
        staging.append(table)

        first_signal = window
        last_signal = len(series) - 1
        try:
            for point in range(first_signal, last_signal + 1):
                start, end = point - window, point
                baseline, reused = self._baseline(
                    metric, series, start, end, rule, force_recompute=force_recompute)

                # reuse_patch：窗口切换时先沿用上一完整信号表里范围重叠的旧基线，
                # 仅对无重叠的边界窗口补算。
                overlap_window = None
                if not force_recompute and point in old_baseline_map:
                    old = old_baseline_map[point]
                    old_start, old_end = int(old["基线起"]), int(old["基线止"])
                    if max(0, min(end, old_end) - max(start, old_start)) > 0:
                        overlap_window = old

                if force_recompute:
                    # 全量重建：每个窗口都重算，重算覆盖整段区间
                    source = "全量重算"
                    table["重算窗口数"] += 1
                    table["重算覆盖区间"].append(point)
                elif reused:
                    # 精确命中已登记当前规则版本的缓存窗口
                    source = "窗口复用"
                    table["复用窗口数"] += 1
                    table["复用范围"].append(point)
                    baseline["复用次数"] = int(baseline.get("复用次数", 0)) + 1
                elif overlap_window is not None:
                    # 复用旧窗口的重叠范围，仅补算受影响边界；
                    # 旧基线登记的是旧规则版本，必须补算并同步登记新版本
                    baseline, _ = self._baseline(
                        metric, series, start, end, rule, force_recompute=True)
                    source = "边界补算"
                    table["复用窗口数"] += 1
                    table["补算窗口数"] += 1
                    table["复用范围"].append(point)
                    table["重算覆盖区间"].append(point)
                else:
                    source = "新增重算"
                    table["重算窗口数"] += 1
                    table["重算覆盖区间"].append(point)

                verdict = self._v005_checks(series[point], baseline, rule)
                table["signals"].append({
                    "信号编号": f"SIG-{rule_id}-{point:03d}",
                    "时间点": point,
                    "监控时间": next((str(r.get("监控时间")) for r in store.rows(HISTORY_MODULE)
                                      if (r.get("指标名称") == metric or r.get("指标编号") == metric)
                                      and int(r.get("时间点", -1)) == point), ""),
                    "监控值": round(series[point], 6),
                    "指标编号": metric,
                    "规则版本": rule_version,
                    "核对标准": CHECK_STANDARD,
                    "基线起": start,
                    "基线止": end,
                    "基线范围": f"[{start},{end})",
                    "基线起始时间": baseline.get("起始时间"),
                    "基线结束时间": baseline.get("结束时间"),
                    "基线均值": verdict["mean"],
                    "基线标准差": verdict["std"],
                    "波动阈值": float(rule["_threshold"]),
                    "波动上限": verdict["upper_limit"],
                    "偏差比": verdict["deviation"],
                    "范围核对": verdict["checks"]["range_check"],
                    "阈值核对": verdict["checks"]["threshold_check"],
                    "上限核对": verdict["checks"]["upper_limit_check"],
                    "是否异常": verdict["is_anomaly"],
                    "窗口来源": source,
                    "缓存键": baseline.get("缓存键"),
                })

                if fail_after is not None and (point - first_signal + 1) >= fail_after:
                    raise RuntimeError("模拟窗口重建中断")
        except RuntimeError as exc:
            table["构建状态"] = "已中断"
            table["中断原因"] = str(exc)
            table["已完成区间"] = [first_signal, table["signals"][-1]["时间点"]] if table["signals"] else []
            self._audit(rule_id, rule_version, "信号表构建",
                        f"窗口重建中断：{exc}，暂存表保留缺失项待续写，不覆盖上一完整信号表")
            return {"ok": False, "status": "已中断", "message": str(exc),
                    "table": self._public_table(table)}

        # 事务提交：暂存表整体替换活动表，上一完整信号表进归档
        table["构建状态"] = "已完成"
        table["完成时间"] = _now()
        active = store.rows(SIGNAL_MODULE)
        archived = store.rows(ARCHIVED_MODULE)
        for index, old in enumerate(active):
            if old.get("规则id") == rule_id:
                old_copy = dict(old)
                old_copy["归档时间"] = _now()
                archived.append(old_copy)
                active.pop(index)
                break
        active.append(dict(table))
        staging[:] = [row for row in staging if row.get("规则id") != rule_id]
        store.bump_version(SIGNAL_MODULE)
        self._audit(rule_id, rule_version, "信号表构建",
                    f"构建完成并提交活动表，复用={table['复用窗口数']}，"
                    f"补算={table['补算窗口数']}，重算={table['重算窗口数']}")
        return {"ok": True, "status": "已完成", "message": "异常检测信号表构建完成",
                "table": self._public_table(table)}

    def resume_build(self, rule_id: int) -> dict[str, Any]:
        """窗口重建中断后按缺失项续写，不覆盖上一完整信号表。"""
        self._ensure_initialized()
        staging = store.rows(STAGING_MODULE)
        table = next((row for row in staging if row.get("规则id") == rule_id), None)
        if table is None:
            return {"ok": False, "message": f"规则 {rule_id} 没有可续写的中断构建"}
        if table.get("构建状态") != "已中断":
            return {"ok": False, "message": f"暂存构建状态为 {table.get('构建状态')}，无需续写"}

        rule = self._rule(rule_id)
        if rule is None:
            return {"ok": False, "message": f"检测规则 {rule_id} 不存在或已归档"}
        metric = str(rule.get("监控指标"))
        series = self._series(metric)
        window = int(rule["_window"])
        rule_version = str(rule.get("规则版本"))
        done_points = {int(signal["时间点"]) for signal in table["signals"]}
        first_signal, last_signal = window, len(series) - 1
        missing = [point for point in range(first_signal, last_signal + 1) if point not in done_points]
        table["构建状态"] = "构建中"
        table.setdefault("补算窗口数", 0)
        self._audit(rule_id, rule_version, "信号表续写",
                    f"按缺失项续写，缺失区间={[missing[0], missing[-1]] if missing else []}")
        try:
            for point in missing:
                start, end = point - window, point
                baseline, reused = self._baseline(
                    metric, series, start, end, rule,
                    force_recompute=table.get("构建策略") == MODE_FULL_REBUILD)
                if reused:
                    source = "窗口复用"
                    table["复用窗口数"] += 1
                    table["复用范围"].append(point)
                    baseline["复用次数"] = int(baseline.get("复用次数", 0)) + 1
                elif table.get("构建策略") == MODE_FULL_REBUILD:
                    source = "全量重算"
                    table["重算窗口数"] += 1
                    table["重算覆盖区间"].append(point)
                else:
                    source = "边界补算"
                    table["复用窗口数"] += 1
                    table["补算窗口数"] += 1
                    table["重算覆盖区间"].append(point)
                verdict = self._v005_checks(series[point], baseline, rule)
                table["signals"].append({
                    "信号编号": f"SIG-{rule_id}-{point:03d}",
                    "时间点": point,
                    "监控时间": next((str(r.get("监控时间")) for r in store.rows(HISTORY_MODULE)
                                      if (r.get("指标名称") == metric or r.get("指标编号") == metric)
                                      and int(r.get("时间点", -1)) == point), ""),
                    "监控值": round(series[point], 6),
                    "指标编号": metric,
                    "规则版本": rule_version,
                    "核对标准": CHECK_STANDARD,
                    "基线起": start,
                    "基线止": end,
                    "基线范围": f"[{start},{end})",
                    "基线起始时间": baseline.get("起始时间"),
                    "基线结束时间": baseline.get("结束时间"),
                    "基线均值": verdict["mean"],
                    "基线标准差": verdict["std"],
                    "波动阈值": float(rule["_threshold"]),
                    "波动上限": verdict["upper_limit"],
                    "偏差比": verdict["deviation"],
                    "范围核对": verdict["checks"]["range_check"],
                    "阈值核对": verdict["checks"]["threshold_check"],
                    "上限核对": verdict["checks"]["upper_limit_check"],
                    "是否异常": verdict["is_anomaly"],
                    "窗口来源": source,
                    "缓存键": baseline.get("缓存键"),
                })
        except RuntimeError as exc:
            table["构建状态"] = "已中断"
            table["中断原因"] = str(exc)
            return {"ok": False, "status": "已中断", "message": str(exc),
                    "table": self._public_table(table)}

        table["构建状态"] = "已完成"
        table["完成时间"] = _now()
        table.pop("中断原因", None)
        table.pop("已完成区间", None)
        active = store.rows(SIGNAL_MODULE)
        archived = store.rows(ARCHIVED_MODULE)
        for index, old in enumerate(active):
            if old.get("规则id") == rule_id:
                old_copy = dict(old)
                old_copy["归档时间"] = _now()
                archived.append(old_copy)
                active.pop(index)
                break
        active.append(dict(table))
        staging[:] = [row for row in staging if row.get("规则id") != rule_id]
        store.bump_version(SIGNAL_MODULE)
        self._audit(rule_id, rule_version, "信号表续写", "续写完成并提交活动表")
        return {"ok": True, "status": "已完成", "message": "中断构建已续写完成",
                "table": self._public_table(table)}

    # ------------------------------------------------------------------
    # 信号表显示窗：残留事务回滚 + V005 留档
    # ------------------------------------------------------------------
    def display_signal_table(self, rule_id: int) -> dict[str, Any]:
        self._ensure_initialized()
        rule = self._rule(rule_id)
        if rule is None:
            return {"ok": False, "message": f"检测规则 {rule_id} 不存在或已归档"}
        staging = store.rows(STAGING_MODULE)
        rollbacks: list[str] = []
        for table in staging:
            if table.get("规则id") == rule_id and table.get("构建状态") == "构建中":
                table["构建状态"] = "已回滚"
                rollbacks.append(str(table.get("表编号")))
                self._audit(rule_id, str(rule.get("规则版本")), "信号表显示窗",
                            "显示窗发现残留事务，执行回滚，活动表保持上一完整版本")
        if rollbacks:
            staging[:] = [row for row in staging
                          if not (row.get("规则id") == rule_id and row.get("构建状态") == "已回滚")]

        active = store.find(SIGNAL_MODULE, rule_id)
        interrupted = next((row for row in staging if row.get("规则id") == rule_id), None)
        return {
            "ok": True,
            "rollback": rollbacks,
            "active": self._public_table(active) if active else None,
            "interrupted": self._public_table(interrupted) if interrupted else None,
            "audit_tail": [row for row in store.rows(AUDIT_MODULE) if row.get("规则id") == rule_id][-10:],
        }

    def list_signal_tables(self) -> dict[str, Any]:
        """消费端/列表侧栏读取：返回全量 V005 异常检测信号表概览。"""
        self._ensure_initialized()
        tables = [self._public_table(row) for row in store.rows(SIGNAL_MODULE)]
        return {"ok": True, "核对标准": CHECK_STANDARD, "total": len(tables), "items": tables}

    def confirm_signal_table(self, rule_id: int) -> dict[str, Any]:
        """结果确认：活动表加确认标记后交消费端取得全量信号表。"""
        self._ensure_initialized()
        table = store.find(SIGNAL_MODULE, rule_id)
        if table is None:
            return {"ok": False, "message": f"规则 {rule_id} 尚无完整信号表，无法确认"}
        table["确认状态"] = "已确认"
        table["确认时间"] = _now()
        table["消费端可取"] = True
        store.bump_version(SIGNAL_MODULE)
        self._audit(rule_id, str(table.get("规则版本")), "结果确认",
                    "V005 核对通过，信号表确认并开放消费端全量读取")
        return {"ok": True, "message": "信号表已确认，消费端可取得全量 V005 信号",
                "table": self._public_table(table)}

    def consumer_pull(self) -> dict[str, Any]:
        """消费端只取已确认的全量 V005 异常检测信号表。"""
        self._ensure_initialized()
        items = [self._public_table(row) for row in store.rows(SIGNAL_MODULE)
                 if row.get("确认状态") == "已确认"]
        return {"ok": True, "核对标准": CHECK_STANDARD, "total": len(items), "items": items}

    # ------------------------------------------------------------------
    # 跨模块联动（V005）：指标定义模块读取信号表并核对基线范围
    # ------------------------------------------------------------------
    def cross_check_metric(self, metric: str, metric_version: str | None = None) -> dict[str, Any]:
        self._ensure_initialized()
        metric_def = store.find_by_field("metric_def", "指标编号", metric)
        tables = [row for row in store.rows(SIGNAL_MODULE) if row.get("指标编号") == metric]
        results: list[dict[str, Any]] = []
        all_pass = bool(tables)
        for table in tables:
            rule_version = str(table.get("规则版本"))
            # 只核对该信号表实际引用的缓存窗口，避免规则多次更新后注册表中的
            # 其它版本记录干扰「复用窗口与规则版本同步登记」的判定
            referenced_keys = {signal.get("缓存键") for signal in table.get("signals", [])}
            cache_entries = [entry for entry in store.rows(CACHE_MODULE)
                             if entry.get("指标编号") == metric and entry.get("缓存键") in referenced_keys]
            version_sync_ok = bool(cache_entries) and all(
                rule_version in (entry.get("规则版本登记") or [])
                for entry in cache_entries
            )
            baseline_ok = all(
                int(signal["基线止"]) - int(signal["基线起"]) == int(table["基线窗口"])
                and signal.get("基线范围")
                for signal in table.get("signals", [])
            )
            standard_ok = all(signal.get("核对标准") == CHECK_STANDARD
                              for signal in table.get("signals", []))
            row_pass = version_sync_ok and baseline_ok and standard_ok
            all_pass = all_pass and row_pass
            results.append({
                "表编号": table.get("表编号"),
                "规则编号": table.get("规则编号"),
                "规则版本": rule_version,
                "指标编号": metric,
                "指标定义口径版本": (metric_def or {}).get("口径版本"),
                "缓存版本同步": version_sync_ok,
                "基线范围核对": baseline_ok,
                "V005标准留档": standard_ok,
                "核对结论": "通过" if row_pass else "不通过",
            })
        self._audit(0, metric_version or "-", "跨模块联动",
                    f"指标定义模块核对指标 {metric} 的信号表与基线范围，结论={'通过' if all_pass else '不通过'}")
        return {
            "ok": True,
            "核对标准": CHECK_STANDARD,
            "指标编号": metric,
            "指标定义存在": metric_def is not None,
            "信号表数量": len(tables),
            "全部通过": all_pass,
            "items": results,
        }

    # ------------------------------------------------------------------
    # 规则更新：窗口/阈值/上限调整与规则版本、缓存登记同步
    # ------------------------------------------------------------------
    def change_rule(self, rule_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_initialized()
        rule = self._rule(rule_id)
        if rule is None:
            return {"ok": False, "message": f"检测规则 {rule_id} 不存在或已归档"}
        before = {"基线窗口": rule.get("基线窗口"), "波动阈值": rule.get("波动阈值"),
                  "规则版本": rule.get("规则版本")}
        if payload.get("基线窗口") not in (None, ""):
            rule["基线窗口"] = _to_int(payload.get("基线窗口"), DEFAULT_WINDOW)
        if payload.get("波动阈值") not in (None, ""):
            rule["波动阈值"] = _to_float(payload.get("波动阈值"), DEFAULT_THRESHOLD)
        if payload.get("波动上限策略"):
            rule["波动上限策略"] = str(payload.get("波动上限策略"))
        new_version = str(payload.get("规则版本") or "").strip()
        if new_version and new_version != str(rule.get("规则版本")):
            rule["规则版本"] = new_version
            # 复用窗口与规则版本同步登记，否则规则更新后仍会读到旧基线
            metric = str(rule.get("监控指标"))
            for entry in store.rows(CACHE_MODULE):
                if entry.get("指标编号") == metric and new_version not in entry.get("规则版本登记", []):
                    entry.setdefault("规则版本登记", []).append(new_version)
                    entry["更新时间"] = _now()
        rule["_window"] = _to_int(rule.get("基线窗口"), DEFAULT_WINDOW)
        rule["_threshold"] = _to_float(rule.get("波动阈值"), DEFAULT_THRESHOLD)
        store.bump_version("anomaly_detect")
        self._audit(rule_id, str(rule.get("规则版本")), "规则更新",
                    f"规则配置变更 {before} -> {rule.get('规则版本')}，缓存登记已同步")
        return {"ok": True, "message": "规则已更新，缓存注册表已同步登记新版本",
                "entry": rule, "before": before}

    def list_audit(self, rule_id: int | None = None) -> list[dict[str, Any]]:
        self._ensure_initialized()
        rows = store.rows(AUDIT_MODULE)
        if rule_id is not None:
            rows = [row for row in rows if row.get("规则id") == rule_id]
        return rows

    def reset_demo_environment(self) -> dict[str, Any]:
        """重置信号表演练环境：恢复种子检测规则，清空缓存/信号表/暂存/归档/留档。

        历史监控数据保留（不存在时重新播种）。供 E2E 与演示使用，让每次运行都从
        确定状态开始，避免内存仓库跨用例累积缓存导致「首次构建」语义失真。
        """
        from app.seed import SEED_ROWS

        store.replace_rows("anomaly_detect", SEED_ROWS.get("anomaly_detect", []))
        for module in (SIGNAL_MODULE, STAGING_MODULE, ARCHIVED_MODULE,
                       CACHE_MODULE, AUDIT_MODULE):
            store.replace_rows(module, [])
        if not store.rows(HISTORY_MODULE):
            self._seed_history()
        self._normalize_rules()
        self._audit(0, "-", "演练环境重置", "恢复种子规则并清空信号表与基线缓存")
        return {"ok": True, "message": "信号表演练环境已重置为种子状态",
                "规则数": len(store.rows("anomaly_detect"))}

    @staticmethod
    def _public_table(table: dict[str, Any] | None) -> dict[str, Any] | None:
        if table is None:
            return None
        return {key: value for key, value in table.items() if not key.startswith("_")}


signal_service = AnomalySignalService()
