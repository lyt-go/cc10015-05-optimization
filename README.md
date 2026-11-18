# 多维分析实验平台

面向实验设计、指标计算、样本分析、效果评估与结论发布的数据分析实验平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实验设计 | `experiment` | 实验方案 | 方案编号、实验名称、对照组说明 |
| 样本分群 | `sample_group` | 样本分群 | 分群编号、分群名称、筛选条件 |
| 指标定义 | `metric_def` | 指标定义 | 指标编号、指标名称、计算公式 |
| AB结果分析 | `ab_result` | 分析报告 | 报告编号、实验编号、分析指标 |
| 分群对比 | `segment_compare` | 对比任务 | 任务编号、分群A、分群B |
| 多维下钻 | `drill_down` | 下钻维度 | 维度编号、维度名称、下钻层级 |
| 数据筛选 | `data_filter` | 筛选规则 | 规则编号、数据来源、筛选字段 |
| 归因分析 | `attr_analysis` | 归因任务 | 任务编号、目标指标、候选因子 |
| 异常检测 | `anomaly_detect` | 检测规则 | 规则编号、监控指标、检测算法 |
| 预测模型 | `forecast_model` | 模型配置 | 模型编号、预测对象、历史窗口 |
| 数据导出 | `data_export` | 导出任务 | 任务编号、数据来源、导出格式 |
| 同期群追踪 | `cohort_track` | 同期群定义 | 群组编号、群组名称、首次行为 |
| 可视化配置 | `chart_config` | 图表配置 | 图表编号、图表类型、数据来源 |
| 洞察报告 | `insight_report` | 洞察报告 | 报告编号、分析主题、数据来源 |
| 数据新鲜度 | `data_freshness` | 新鲜度监控 | 监控编号、数据表名、最新分区 |
| 口径注册 | `metric_alias` | 口径版本 | 版本编号、指标编号、变更说明 |
| 实验因子 | `expr_factor` | 因子配置 | 因子编号、因子名称、因子层级 |
| 评估看板 | `evaluate_board` | 看板配置 | 看板编号、看板名称、关联实验 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 异常检测信号表（V005）

异常检测模块在规则管理之外，额外提供一套可追溯的**异常检测信号表工作台**
（`backend/app/services/anomaly_signals.py`），解决每条规则重复整段扫描历史、
重算重叠基线导致长窗口任务耗时持续升高的问题。

- **历史监控数据与基线窗口按需读取**：历史序列独立成表 `anomaly_history`，
  基线结果按「指标 + 窗口范围」登记到缓存注册表 `anomaly_baseline_cache`，
  同指标的多条规则共享同一份历史与可重叠窗口。
- **窗口复用 + 规则版本同步登记**：缓存命中要求指标、窗口范围与规则版本三者一致；
  规则更新版本时同步登记到缓存，避免规则更新后读到旧基线。连续检测只更新变化窗口，
  信号表会标明复用范围与本次重算覆盖区间。
- **V005 判定标准**：每条信号都保留基线范围（起止下标/时间戳）与规则版本，并同时
  核对范围（样本数=基线窗口）、阈值（偏差比>波动阈值）、上限（监控值>均值+阈值×标准差），
  三项全过才记异常，核对明细随信号留档。
- **事务化构建与断点续写**：先写暂存表，成功后整体替换活动表，旧表进归档；
  中断后按缺失项续写，不覆盖上一完整信号表。显示窗发现残留「构建中」事务即回滚并留档。
- **版本冲突的两种做法**：切换基线窗口与读缓存同时发生时，可「复用重叠窗口并补算边界」
  （信号表显示复用范围与补算边界）或「按新窗口全量重建」（切到全量结果并列重算范围），
  决策含 V005 调用链与两种做法权衡。
- **跨模块联动**：指标定义模块可通过核对接口读取信号表的指标编号、规则版本，
  并核对复用窗口的基线范围与版本同步情况。
- **确认与消费**：V005 核对通过并确认后，消费端才能取得全量已确认信号表。

主要接口（均在 `/api/anomaly_detect` 下）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/history/series` | 读取历史监控数据（可按指标过滤） |
| GET | `/baseline/cache` | 基线窗口缓存注册表（含规则版本登记、复用次数） |
| POST | `/{id}/signals/build` | 构建信号表，`mode=auto/reuse_patch/full_rebuild` |
| POST | `/{id}/signals/resume` | 中断后按缺失项续写 |
| GET | `/{id}/signals/display` | 信号表显示窗（残留事务回滚 + V005 留档） |
| POST | `/{id}/signals/confirm` | 结果确认并开放消费端 |
| GET | `/signals/overview` | 全量信号表概览（侧栏与详情同源） |
| GET | `/signals/consumer` | 消费端取得全量已确认 V005 信号表 |
| GET | `/signals/cross-check` | 指标定义模块跨模块核对 |
| GET | `/signals/audit` | V005 留档核对记录 |
| PATCH | `/{id}/rule-config` | 调整窗口/阈值/上限/规则版本并同步缓存 |
| POST | `/signals/reset` | 演练环境重置（恢复种子规则、清空信号表与缓存） |

后端测试见 `backend/tests/test_anomaly_signals.py`，前端端到端见
`frontend/tests/anomaly-signals.spec.ts`。E2E 启动前会调用
`POST /api/test/reset-environment` 把内存仓库恢复到种子状态，因此可重复运行。

领域：数据工程
子方向：数据分析与数据应用
