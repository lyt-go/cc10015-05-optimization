"""异常检测信号表测试：窗口复用、V005 判定、事务、续写与跨模块联动。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services.anomaly_detect import AnomalyDetectService
from app.services.anomaly_signals import (
    ARCHIVED_MODULE,
    AUDIT_MODULE,
    CACHE_MODULE,
    CHECK_STANDARD,
    HISTORY_MODULE,
    MODE_FULL_REBUILD,
    MODE_REUSE_PATCH,
    SIGNAL_MODULE,
    STAGING_MODULE,
    AnomalySignalService,
)


def _clear_all():
    for key in list(store._tables.keys()):
        store._tables[key] = []


def _make_rule(rule_id=1, metric="点击率", window=7, threshold=0.3, version="v1"):
    AnomalyDetectService().create_entry({
        "规则编号": f"AD-{rule_id:03d}",
        "监控指标": metric,
        "检测算法": "moving_average",
        "基线窗口": str(window),
        "波动阈值": str(threshold),
        "告警级别": "高",
        "静默时段": "0",
        "规则版本": version,
    })


def test_signal_table_basic_v005():
    """信号表保留基线范围与规则版本，V005 三项核对随信号留档。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule()
    result = svc.build_signal_table(1)
    assert result["ok"] is True
    table = result["table"]
    assert table["核对标准"] == CHECK_STANDARD
    assert table["规则版本"] == "v1"
    signals = table["signals"]
    assert signals, "应生成信号明细"
    for signal in signals:
        assert signal["规则版本"] == "v1"
        assert signal["核对标准"] == CHECK_STANDARD
        assert signal["基线范围"] == f"[{signal['基线起']},{signal['基线止']})"
        assert signal["基线止"] - signal["基线起"] == 7
        for check in ("范围核对", "阈值核对", "上限核对"):
            assert "通过" in signal[check] and "标准" in signal[check]
    # 历史里预置了尖峰，至少应核对出一个异常信号
    assert any(signal["是否异常"] for signal in signals)
    # 异常信号必须三项核对同时通过
    for signal in signals:
        if signal["是否异常"]:
            assert signal["范围核对"]["通过"]
            assert signal["阈值核对"]["通过"]
            assert signal["上限核对"]["通过"]


def test_window_reuse_on_continuous_runs():
    """连续检测同一指标时命中缓存，只更新变化窗口，并能说明复用范围与重算区间。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule()
    first = svc.build_signal_table(1)["table"]
    assert first["复用窗口数"] == 0
    assert first["重算窗口数"] == len(first["signals"])

    second = svc.build_signal_table(1)["table"]
    assert second["复用窗口数"] == len(second["signals"])
    assert second["重算窗口数"] == 0
    assert second["复用范围"] == [signal["时间点"] for signal in second["signals"]]
    # 缓存注册表里每个窗口都登记了规则版本，复用计数递增
    cache_rows = svc.list_cache_registry()
    assert cache_rows and all("v1" in row["规则版本登记"] for row in cache_rows)
    assert all(int(row["复用次数"]) >= 1 for row in cache_rows)


def test_cache_and_rule_version_registered_together():
    """复用窗口与规则版本必须同步登记，规则更新后不会再读到旧基线。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule()
    svc.build_signal_table(1)
    change = svc.change_rule(1, {"规则版本": "v2"})
    assert change["ok"] is True
    cache_rows = svc.list_cache_registry()
    assert all("v2" in row["规则版本登记"] for row in cache_rows)

    # 用新版本重新构建：缓存登记包含 v2 才允许复用，信号全部带新版本
    table = svc.build_signal_table(1)["table"]
    assert all(signal["规则版本"] == "v2" for signal in table["signals"])
    assert table["规则版本"] == "v2"
    assert table["复用窗口数"] == len(table["signals"])


def test_interrupt_resume_keeps_last_complete_table():
    """窗口重建中断后按缺失项续写，不覆盖上一完整信号表。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule()
    complete = svc.build_signal_table(1)["table"]

    # 规则更新后触发全量重建并模拟中断
    svc.change_rule(1, {"规则版本": "v3", "基线窗口": 8})
    interrupted = svc.build_signal_table(1, mode=MODE_FULL_REBUILD, fail_after=5)
    assert interrupted["ok"] is False
    assert interrupted["status"] == "已中断"

    # 活动表仍是上一完整信号表，暂存表保留中断现场
    active = store.find(SIGNAL_MODULE, 1)
    assert active["规则版本"] == "v1"
    assert len(active["signals"]) == len(complete["signals"])
    staging = [row for row in store.rows(STAGING_MODULE) if row.get("规则id") == 1]
    assert staging and staging[0]["构建状态"] == "已中断"
    done = {signal["时间点"] for signal in staging[0]["signals"]}
    assert len(done) == 5

    # 按缺失项续写后活动表才被替换
    resumed = svc.resume_build(1)
    assert resumed["ok"] is True
    new_active = store.find(SIGNAL_MODULE, 1)
    assert new_active["规则版本"] == "v3"
    assert new_active["构建状态"] == "已完成"
    assert len(new_active["signals"]) > 5
    assert not [row for row in store.rows(STAGING_MODULE) if row.get("规则id") == 1]
    # 上一完整信号表进归档而不是被覆盖
    archived = [row for row in store.rows(ARCHIVED_MODULE) if row.get("规则id") == 1]
    assert archived and archived[-1]["规则版本"] == "v1"


def test_display_window_rolls_back_residual_transaction():
    """信号表显示窗发现残留事务即回滚，且按 V005 留档。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule()
    svc.build_signal_table(1)
    svc.change_rule(1, {"规则版本": "v4", "基线窗口": 9})
    svc.build_signal_table(1, mode=MODE_FULL_REBUILD, fail_after=3)
    # 模拟进程崩溃：暂存事务停在「构建中」成为残留，而不是可续写的已中断
    staging = [row for row in store.rows(STAGING_MODULE) if row.get("规则id") == 1]
    staging[0]["构建状态"] = "构建中"

    view = svc.display_signal_table(1)
    assert view["ok"] is True
    assert view["rollback"], "残留构建中事务应被回滚"
    assert view["active"] is not None
    assert view["active"]["规则版本"] == "v1"
    assert not [row for row in store.rows(STAGING_MODULE) if row.get("规则id") == 1]
    audit_rows = [row for row in svc.list_audit(1) if row["环节"] == "信号表显示窗"]
    assert audit_rows and all(row["核对标准"] == CHECK_STANDARD for row in audit_rows)


def test_display_window_keeps_interrupted_for_resume():
    """已中断的暂存事务保留给续写接口，显示窗只回滚真正的残留事务。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule()
    svc.build_signal_table(1)
    svc.change_rule(1, {"规则版本": "v4b", "基线窗口": 8})
    svc.build_signal_table(1, mode=MODE_FULL_REBUILD, fail_after=4)

    view = svc.display_signal_table(1)
    assert view["rollback"] == []
    assert view["interrupted"] is not None
    assert view["interrupted"]["构建状态"] == "已中断"
    # 中断现场保留后仍可续写
    resumed = svc.resume_build(1)
    assert resumed["ok"] is True
    assert store.find(SIGNAL_MODULE, 1)["规则版本"] == "v4b"


def test_conflict_reuse_patch_vs_full_rebuild():
    """切换基线窗口与读缓存同时发生：复用补算与全量重建两种做法都可留档。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule(window=7)
    svc.build_signal_table(1)
    svc.change_rule(1, {"规则版本": "v5", "基线窗口": 9})

    patched = svc.build_signal_table(1, mode=MODE_REUSE_PATCH)["table"]
    assert patched["版本冲突"] is True
    assert patched["构建策略"] == MODE_REUSE_PATCH
    assert patched["复用范围"], "复用补算应说明窗口复用范围"
    assert patched["补算窗口数"] > 0 and patched["重算覆盖区间"], "应补算受影响边界"
    assert all(signal["基线止"] - signal["基线起"] == 9 for signal in patched["signals"])
    # 决策留档包含 V005 调用链与两种做法的权衡
    decision = patched["决策留档"]
    assert decision["调用链"] and "V005" in "".join(decision["调用链"])
    assert set(decision["权衡"]) == {MODE_REUSE_PATCH, MODE_FULL_REBUILD}

    # 再切一次窗口并强制全量重建：信号表切到全量结果并列出重算范围
    svc.change_rule(1, {"规则版本": "v6", "基线窗口": 6})
    rebuilt = svc.build_signal_table(1, mode=MODE_FULL_REBUILD)["table"]
    assert rebuilt["构建策略"] == MODE_FULL_REBUILD
    assert rebuilt["复用窗口数"] == 0
    assert rebuilt["重算窗口数"] == len(rebuilt["signals"])
    assert rebuilt["重算覆盖区间"] == [signal["时间点"] for signal in rebuilt["signals"]]
    assert all(signal["窗口来源"] == "全量重算" for signal in rebuilt["signals"])


def test_cross_module_metric_check_v005():
    """跨模块联动：指标定义模块读取信号表的指标编号、规则版本并核对基线范围。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule(metric="点击率")
    svc.build_signal_table(1)

    # 指标定义模块登记同一指标编号
    store.rows("metric_def").append({
        "id": 1, "status": "已生效", "pending": False, "abnormal": False,
        "指标编号": "点击率", "指标名称": "点击率", "计算公式": "clicks/impr",
        "统计粒度": "日", "时间窗口": "7", "基准取值": "0.12", "精度位数": "4",
        "口径版本": "v1",
    })
    report = svc.cross_check_metric("点击率", "v1")
    assert report["ok"] is True
    assert report["核对标准"] == CHECK_STANDARD
    assert report["信号表数量"] == 1
    assert report["全部通过"] is True
    item = report["items"][0]
    assert item["指标编号"] == "点击率"
    assert item["规则版本"] == "v1"
    assert item["缓存版本同步"] is True
    assert item["基线范围核对"] is True
    assert item["V005标准留档"] is True

    # 未构建信号表的指标核对不通过
    missing = svc.cross_check_metric("不存在的指标")
    assert missing["全部通过"] is False


def test_confirm_then_consumer_pull_full_table():
    """V005 核对通过并确认后，消费端才能取得全量信号表。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule()
    svc.build_signal_table(1)

    assert svc.consumer_pull()["total"] == 0
    confirm = svc.confirm_signal_table(1)
    assert confirm["ok"] is True
    pulled = svc.consumer_pull()
    assert pulled["total"] == 1
    assert pulled["核对标准"] == CHECK_STANDARD
    table = pulled["items"][0]
    assert table["确认状态"] == "已确认"
    assert table["消费端可取"] is True
    assert len(table["signals"]) == len(svc.list_signal_tables()["items"][0]["signals"])


def test_overview_and_detail_share_same_data():
    """列表侧栏（overview）与详情页（display）读取同一组活动信号表数据。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule(rule_id=1, metric="点击率")
    _make_rule(rule_id=2, metric="支付转化率", window=5, threshold=0.25)
    svc.build_signal_table(1)
    svc.build_signal_table(2)

    overview = svc.list_signal_tables()
    assert overview["total"] == 2
    for summary in overview["items"]:
        detail = svc.display_signal_table(summary["规则id"])
        assert detail["active"]["表编号"] == summary["表编号"]
        assert detail["active"]["规则版本"] == summary["规则版本"]
        assert len(detail["active"]["signals"]) == len(summary["signals"])
        assert detail["active"]["signals"][0]["信号编号"] == summary["signals"][0]["信号编号"]


def test_history_and_cache_read_endpoints():
    """历史监控数据与基线窗口数据按需读取，不再整段重扫。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule(metric="点击率")
    history = svc.list_history("点击率")
    assert history and all(row["指标编号"] == "点击率" for row in history)
    assert all("监控值" in row and "时间点" in row for row in history)

    svc.build_signal_table(1)
    cache_rows = svc.list_cache_registry()
    assert cache_rows
    for row in cache_rows:
        assert row["窗口大小"] == 7
        assert row["基线均值"] is not None and row["基线标准差"] is not None
        assert row["起始时间"] and row["结束时间"]


def test_v005_threshold_range_upper_limit_judgement():
    """判定不能只看规则：范围、阈值、上限任一不满足都不算异常。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule(window=7, threshold=0.3)
    table = svc.build_signal_table(1)["table"]
    for signal in table["signals"]:
        expected_anomaly = (
            signal["范围核对"]["通过"]
            and signal["阈值核对"]["通过"]
            and signal["上限核对"]["通过"]
        )
        assert signal["是否异常"] is expected_anomaly
        # 阈值与上限数值都来自 V005 标准留档
        assert signal["波动上限"] == round(signal["基线均值"]
                                           + signal["波动阈值"] * signal["基线标准差"], 6)


def test_audit_trail_records_v005():
    """构建、回滚、续写、确认、跨模块联动全部按 V005 留档。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule()
    svc.build_signal_table(1)
    svc.confirm_signal_table(1)
    svc.cross_check_metric("点击率")
    stages = {row["环节"] for row in store.rows(AUDIT_MODULE)}
    assert {"信号表构建", "结果确认", "跨模块联动"}.issubset(stages)
    assert all(row["核对标准"] == CHECK_STANDARD for row in store.rows(AUDIT_MODULE))


def test_history_table_separated_from_rules():
    """历史监控数据独立成表，多条规则共享同一份历史，不重复扫描存储。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule(rule_id=1, metric="点击率")
    _make_rule(rule_id=2, metric="点击率", window=5)
    svc.build_signal_table(1)
    svc.build_signal_table(2)
    metric_history = [row for row in store.rows(HISTORY_MODULE) if row["指标编号"] == "点击率"]
    # 两条规则共用同一份历史序列
    assert len({id(row) for row in metric_history}) == len(metric_history)
    # 不同窗口各自登记缓存，互不污染
    windows = {row["窗口大小"] for row in store.rows(CACHE_MODULE) if row["指标编号"] == "点击率"}
    assert windows == {5, 7}


def test_reset_demo_environment_restores_seed_state():
    """演练环境重置：恢复种子规则，清空缓存与信号表，保留历史监控数据。"""
    _clear_all()
    svc = AnomalySignalService()
    _make_rule()
    svc.build_signal_table(1)
    svc.change_rule(1, {"规则版本": "v9", "基线窗口": 12})
    assert store.rows(CACHE_MODULE)
    assert store.rows(SIGNAL_MODULE)

    result = svc.reset_demo_environment()
    assert result["ok"] is True
    assert result["规则数"] == 3
    # 缓存、信号表、暂存、归档、留档全部清空
    assert not store.rows(CACHE_MODULE)
    assert not store.rows(SIGNAL_MODULE)
    assert not store.rows(STAGING_MODULE)
    assert not store.rows(ARCHIVED_MODULE)
    # 历史监控数据保留
    assert store.rows(HISTORY_MODULE)
    # 规则恢复为种子窗口 7，且重新规范化为可运行配置
    rule = store.find("anomaly_detect", 1)
    assert str(rule["基线窗口"]) == "7"
    assert rule["_window"] == 7
    assert rule["_threshold"] == 0.3


def test_store_reset_all_restores_seed():
    """全环境重置把所有模块恢复到种子初始状态。"""
    _clear_all()
    assert store.rows("anomaly_detect") == []
    store.reset_all()
    # 种子模块全部恢复，异常检测为三条可运行规则
    assert len(store.module_names()) >= 18
    assert len(store.rows("anomaly_detect")) == 3
    assert store.rows("anomaly_detect")[0]["监控指标"] == "点击率"
    # 重置后版本号回到 1，信号表相关新表为空
    assert store.version("anomaly_detect") == 1
    assert store.rows(SIGNAL_MODULE) == []
    assert store.rows(CACHE_MODULE) == []
