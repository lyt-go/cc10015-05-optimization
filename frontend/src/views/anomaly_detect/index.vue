<template>
  <section class="page" data-module="anomaly_detect">
    <header class="page-head">
      <div>
        <h2>异常检测管理</h2>
        <p class="page-desc">维护检测规则与 V005 异常检测信号表：按规则复用可重叠基线窗口，每条信号保留基线范围与规则版本。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记检测规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出异常检测清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无异常检测数据，可先登记检测规则</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记检测规则</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <!-- =============== V005 异常检测信号表工作台 =============== -->
    <section class="signal-workbench" data-testid="signal-workbench">
      <header class="page-head">
        <div>
          <h3>异常检测信号表（核对标准 V005）</h3>
          <p class="page-desc">
            历史监控数据与基线窗口按需读取，连续检测只更新变化窗口；侧栏与详情页共用同一份信号表数据。
          </p>
        </div>
        <div class="page-actions">
          <button class="btn" data-testid="refresh-signals" type="button" @click="refreshWorkbench">刷新信号表</button>
          <button class="btn" data-testid="consumer-pull" type="button" @click="pullConsumer">消费端全量读取</button>
        </div>
      </header>

      <p v-if="signalError" class="error-text" data-testid="signal-error">{{ signalError }}</p>
      <p v-if="signalMessage" class="signal-msg" data-testid="signal-message">{{ signalMessage }}</p>

      <div class="signal-layout">
        <!-- 列表侧栏：全量信号表概览，与详情页同源 -->
        <aside class="signal-side" data-testid="signal-sidebar">
          <h4>信号表侧栏（{{ rows.length }} 条规则）</h4>
          <ul v-if="rows.length" class="side-list">
            <li
              v-for="rule in rows"
              :key="String(rule.id)"
              class="side-item"
              :class="{ active: Number(rule.id) === selectedRuleId }"
              :data-testid="`side-item-${rule.id}`"
              @click="selectRule(Number(rule.id))"
            >
              <strong>{{ rule.规则编号 }}</strong>
              <span>{{ rule.监控指标 }} · {{ rule.规则版本 }}</span>
              <template v-if="tableOf(Number(rule.id))">
                <span class="side-meta">
                  复用 {{ tableOf(Number(rule.id))!.复用窗口数 }} /
                  重算 {{ tableOf(Number(rule.id))!.重算窗口数 + (tableOf(Number(rule.id))!.补算窗口数 ?? 0) }} ·
                  异常 {{ countAnomaly(tableOf(Number(rule.id))!) }}
                </span>
                <span class="side-status" :data-status="tableOf(Number(rule.id))!.构建状态">
                  {{ tableOf(Number(rule.id))!.构建状态 }}
                </span>
              </template>
              <span v-else class="side-status side-empty">未构建</span>
            </li>
          </ul>
          <p v-else class="empty-state">暂无检测规则</p>

          <h4>基线窗口缓存注册表</h4>
          <ul class="side-list cache-list" data-testid="cache-registry">
            <li v-for="cache in cacheRows.slice(0, 8)" :key="cache.缓存键" class="cache-item">
              <span>{{ cache.指标编号 }} · 窗口 [{{ cache.窗口起 }},{{ cache.窗口止 }})</span>
              <span class="side-meta">
                大小 {{ cache.窗口大小 }} · 版本 {{ cache.规则版本登记.join('/') }} ·
                复用 {{ cache.复用次数 }}
              </span>
            </li>
          </ul>
          <p v-if="!cacheRows.length" class="empty-state">暂无缓存，构建后登记</p>
        </aside>

        <!-- 详情页：选中规则的信号表显示窗 -->
        <div class="signal-detail" data-testid="signal-detail">
          <template v-if="selectedRule">
            <div class="detail-head">
              <div>
                <h4>
                  <template v-if="selectedTable">{{ selectedTable.表编号 }} · {{ selectedTable.指标编号 }}</template>
                  <template v-else>规则 {{ selectedRule.规则编号 }} · {{ selectedRule.监控指标 }}（待构建信号表）</template>
                </h4>
                <p class="page-desc">
                  规则版本 <strong>{{ ruleForm.规则版本 }}</strong> ·
                  核对标准 <strong>V005</strong>
                  <template v-if="selectedTable"> · 状态 <strong>{{ selectedTable.构建状态 }}</strong></template>
                  <template v-if="selectedTable?.确认状态"> · {{ selectedTable.确认状态 }}</template>
                </p>
              </div>
              <div class="page-actions" v-if="selectedTable">
                <button class="btn" data-testid="btn-display" type="button" @click="openDisplay">信号表显示窗</button>
                <button class="btn" data-testid="btn-confirm" type="button" @click="confirmTable">结果确认</button>
                <button class="btn" data-testid="btn-cross-check" type="button" @click="runCrossCheck">V005跨模块核对</button>
              </div>
            </div>

            <template v-if="selectedTable">
            <div class="stat-row">
              <article class="stat-card">
                <span class="stat-label">复用窗口数</span>
                <strong class="stat-value" data-testid="kpi-reuse">{{ selectedTable.复用窗口数 }}</strong>
              </article>
              <article class="stat-card">
                <span class="stat-label">边界补算</span>
                <strong class="stat-value" data-testid="kpi-patch">{{ selectedTable.补算窗口数 ?? 0 }}</strong>
              </article>
              <article class="stat-card">
                <span class="stat-label">重算窗口数</span>
                <strong class="stat-value" data-testid="kpi-recompute">{{ selectedTable.重算窗口数 }}</strong>
              </article>
              <article class="stat-card">
                <span class="stat-label">异常信号</span>
                <strong class="stat-value" data-testid="kpi-anomaly">{{ countAnomaly(selectedTable) }}</strong>
              </article>
            </div>

            <div class="range-panel" data-testid="range-panel">
              <p>
                <strong>复用范围：</strong>
                {{ formatRange(selectedTable.复用范围) || '—' }}
              </p>
              <p>
                <strong>本次重算覆盖区间：</strong>
                {{ formatRange(selectedTable.重算覆盖区间) || '—' }}
              </p>
              <p v-if="selectedTable.版本冲突">
                <strong>版本冲突策略：</strong>{{ strategyLabel(selectedTable.构建策略) }}
              </p>
              <details v-if="selectedTable.决策留档?.权衡">
                <summary>V005 调用链与两种做法权衡</summary>
                <ol class="call-chain">
                  <li v-for="(step, idx) in selectedTable.决策留档.调用链" :key="idx">{{ step }}</li>
                </ol>
                <p class="tradeoff" v-for="(text, key) in selectedTable.决策留档.权衡" :key="key">
                  <strong>{{ strategyLabel(key) }}：</strong>{{ text }}
                </p>
              </details>
            </div>
            </template>

            <div class="rule-config" data-testid="rule-config">
              <h4>规则配置调整（窗口 / 阈值 / 上限 / 版本）</h4>
              <form class="analysis-form" @submit.prevent="saveRuleConfig">
                <label><span>基线窗口</span>
                  <input v-model="ruleForm.基线窗口" data-testid="config-window" type="number" min="2" />
                </label>
                <label><span>波动阈值</span>
                  <input v-model="ruleForm.波动阈值" data-testid="config-threshold" type="number" step="0.01" />
                </label>
                <label><span>波动上限策略</span>
                  <input v-model="ruleForm.波动上限策略" data-testid="config-upper" />
                </label>
                <label><span>规则版本</span>
                  <input v-model="ruleForm.规则版本" data-testid="config-version" />
                </label>
                <button class="btn primary" data-testid="config-save" type="submit">保存并同步缓存登记</button>
              </form>
            </div>

            <div class="build-actions">
              <button class="btn primary" data-testid="build-auto" type="button" @click="build('auto')">构建（自动判定）</button>
              <button class="btn" data-testid="build-reuse" type="button" @click="build('reuse_patch')">复用重叠窗口+补算边界</button>
              <button class="btn" data-testid="build-full" type="button" @click="build('full_rebuild')">按新窗口全量重建</button>
              <button class="btn" data-testid="build-interrupt" type="button" @click="buildInterrupt">模拟中断（续写演示）</button>
              <button class="btn" data-testid="build-resume" type="button" @click="resume">按缺失项续写</button>
            </div>

            <template v-if="selectedTable">
            <table class="data-table signal-table" data-testid="signal-table">
              <thead>
                <tr>
                  <th>信号编号</th>
                  <th>监控时间</th>
                  <th>监控值</th>
                  <th>基线范围</th>
                  <th>基线均值</th>
                  <th>波动上限</th>
                  <th>偏差比</th>
                  <th>范围核对</th>
                  <th>阈值核对</th>
                  <th>上限核对</th>
                  <th>窗口来源</th>
                  <th>规则版本</th>
                  <th>判定</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="signal in displaySignals" :key="signal.信号编号" :data-anomaly="String(signal.是否异常)">
                  <td>{{ signal.信号编号 }}</td>
                  <td>{{ signal.监控时间 }}</td>
                  <td>{{ signal.监控值 }}</td>
                  <td>{{ signal.基线范围 }}</td>
                  <td>{{ signal.基线均值 }}</td>
                  <td>{{ signal.波动上限 }}</td>
                  <td>{{ signal.偏差比 }}</td>
                  <td :data-pass="String(signal.范围核对.通过)">{{ signal.范围核对.通过 ? '通过' : '不通过' }}</td>
                  <td :data-pass="String(signal.阈值核对.通过)">{{ signal.阈值核对.通过 ? '通过' : '不通过' }}</td>
                  <td :data-pass="String(signal.上限核对.通过)">{{ signal.上限核对.通过 ? '通过' : '不通过' }}</td>
                  <td>{{ signal.窗口来源 }}</td>
                  <td>{{ signal.规则版本 }}</td>
                  <td>
                    <span class="verdict" :class="signal.是否异常 ? 'verdict-bad' : 'verdict-ok'">
                      {{ signal.是否异常 ? '异常' : '正常' }}
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-if="!displaySignals.length" class="empty-state">该规则暂无信号，点击上方构建按钮生成 V005 信号表</p>
            <div v-else class="pager" data-testid="signal-pager">
              <button class="btn" type="button" :disabled="signalPage === 1" @click="signalPage -= 1">上一页</button>
              <span>第 {{ signalPage }} / {{ signalTotalPages }} 页（共 {{ totalSignals }} 条信号）</span>
              <button class="btn" type="button" :disabled="signalPage >= signalTotalPages" @click="signalPage += 1">下一页</button>
            </div>

            <section v-if="selectedTable && crossReport" class="cross-panel" data-testid="cross-report">
              <h4>跨模块联动核对（指标定义 V005 模块）</h4>
              <p>
                指标编号 {{ crossReport.指标编号 }} · 信号表 {{ crossReport.信号表数量 }} 张 ·
                结论 <strong :class="crossReport.全部通过 ? 'verdict-ok' : 'verdict-bad'">
                  {{ crossReport.全部通过 ? '全部通过' : '存在不通过' }}
                </strong>
              </p>
              <table class="data-table">
                <thead>
                  <tr><th>表编号</th><th>规则版本</th><th>缓存版本同步</th><th>基线范围核对</th><th>V005留档</th><th>结论</th></tr>
                </thead>
                <tbody>
                  <tr v-for="line in crossReport.items" :key="line.表编号">
                    <td>{{ line.表编号 }}</td>
                    <td>{{ line.规则版本 }}</td>
                    <td>{{ line.缓存版本同步 ? '同步' : '失配' }}</td>
                    <td>{{ line.基线范围核对 ? '一致' : '不一致' }}</td>
                    <td>{{ line.V005标准留档 ? '已留档' : '缺失' }}</td>
                    <td>{{ line.核对结论 }}</td>
                  </tr>
                </tbody>
              </table>
            </section>

            <section v-if="selectedTable" class="audit-panel" data-testid="audit-panel">
              <h4>V005 留档核对记录</h4>
              <ul class="audit-list">
                <li v-for="audit in auditRows" :key="audit.id">
                  <span class="audit-time">{{ audit.时间 }}</span>
                  <span class="audit-stage">{{ audit.环节 }}</span>
                  <span>{{ audit.说明 }}</span>
                </li>
              </ul>
              <p v-if="!auditRows.length" class="empty-state">暂无留档记录</p>
            </section>
            </template>
          </template>
          <p v-else class="empty-state">请在左侧选择一条检测规则</p>
        </div>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条异常检测记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
interface Check { 通过: boolean; 标准: string }
interface Signal {
  信号编号: string
  监控时间: string
  监控值: number
  基线范围: string
  基线均值: number
  波动上限: number
  偏差比: number
  范围核对: Check
  阈值核对: Check
  上限核对: Check
  窗口来源: string
  规则版本: string
  是否异常: boolean
  时间点: number
}
interface SignalTable {
  表编号: string
  规则id: number
  规则编号: string
  指标编号: string
  规则版本: string
  核对标准: string
  构建状态: string
  构建策略: string
  版本冲突: boolean
  确认状态?: string
  复用窗口数: number
  补算窗口数?: number
  重算窗口数: number
  复用范围: number[]
  重算覆盖区间: number[]
  signals: Signal[]
  决策留档?: { 调用链: string[]; 权衡: Record<string, string> }
}
interface CacheRow {
  缓存键: string
  指标编号: string
  窗口起: number
  窗口止: number
  窗口大小: number
  规则版本登记: string[]
  复用次数: number
}
interface CrossCheckLine {
  表编号: string
  规则编号: string
  规则版本: string
  指标编号: string
  缓存版本同步: boolean
  基线范围核对: boolean
  V005标准留档: boolean
  核对结论: string
}
interface CrossCheckReport {
  ok: boolean
  核对标准: string
  指标编号: string
  信号表数量: number
  全部通过: boolean
  items: CrossCheckLine[]
}
interface AuditRow {
  id: number
  时间: string
  规则id: number
  规则版本: string
  环节: string
  核对标准: string
  说明: string
}

const ENDPOINT = '/api/anomaly_detect'
const columns = ['规则编号', '监控指标', '检测算法', '基线窗口', '波动阈值', '告警级别', '静默时段', '规则版本']
const actions = ['提交校验', '标记生效', '停用规则']
const statuses = ['草稿', '待校验', '已生效', '已停用']
const metricNames = ['检测规则总数', '待校验规则', '已生效规则']
const SIGNAL_PAGE_SIZE = 12

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

// ---- 信号表工作台状态：侧栏与详情共用 signalTables 这同一组数据 ----
const signalTables = ref<SignalTable[]>([])
const cacheRows = ref<CacheRow[]>([])
const auditRows = ref<AuditRow[]>([])
const selectedRuleId = ref<number | null>(null)
const interruptedTable = ref<SignalTable | null>(null)
const crossReport = ref<CrossCheckReport | null>(null)
const signalError = ref('')
const signalMessage = ref('')
const signalPage = ref(1)
const ruleForm = reactive({ 基线窗口: '7', 波动阈值: '0.3', 波动上限策略: 'mean + 波动阈值 × std', 规则版本: 'v1' })

const selectedRule = computed<Row | null>(
  () => rows.value.find((item) => Number(item.id) === selectedRuleId.value) ?? null,
)
const selectedTable = computed<SignalTable | null>(
  () => signalTables.value.find((item) => item.规则id === selectedRuleId.value) ?? null,
)
const displaySignals = computed<Signal[]>(() => {
  const source = interruptedTable.value ?? selectedTable.value
  if (!source) return []
  const pages = Math.max(1, Math.ceil(source.signals.length / SIGNAL_PAGE_SIZE))
  if (signalPage.value > pages) signalPage.value = pages
  const start = (signalPage.value - 1) * SIGNAL_PAGE_SIZE
  return source.signals.slice(start, start + SIGNAL_PAGE_SIZE)
})
const totalSignals = computed<number>(() => {
  const source = interruptedTable.value ?? selectedTable.value
  return source ? source.signals.length : 0
})
const signalTotalPages = computed<number>(() => Math.max(1, Math.ceil(totalSignals.value / SIGNAL_PAGE_SIZE)))

function countAnomaly(table: SignalTable): number {
  return table.signals.filter((signal) => signal.是否异常).length
}

function tableOf(ruleId: number): SignalTable | undefined {
  return signalTables.value.find((item) => item.规则id === ruleId)
}

function formatRange(points: number[] | undefined): string {
  if (!points || !points.length) return ''
  const sorted = [...points].sort((a, b) => a - b)
  const spans: string[] = []
  let start = sorted[0]
  let prev = sorted[0]
  for (const point of sorted.slice(1)) {
    if (point === prev + 1) {
      prev = point
      continue
    }
    spans.push(start === prev ? `${start}` : `${start}-${prev}`)
    start = prev = point
  }
  spans.push(start === prev ? `${start}` : `${start}-${prev}`)
  return spans.join('，')
}

function strategyLabel(mode: string): string {
  if (mode === 'reuse_patch') return '复用重叠窗口并补算边界'
  if (mode === 'full_rebuild') return '按新窗口全量重建'
  return mode
}

async function getJson(path: string): Promise<unknown> {
  const response = await request(path)
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? `接口返回 ${response.status}`)
  }
  return response.json()
}

async function refreshWorkbench(): Promise<void> {
  signalError.value = ''
  try {
    const overview = await getJson(`${ENDPOINT}/signals/overview`) as { items: SignalTable[] }
    signalTables.value = overview.items ?? []
    const cache = await getJson(`${ENDPOINT}/baseline/cache`) as { items: CacheRow[] }
    cacheRows.value = cache.items ?? []
    if (selectedRuleId.value === null && rows.value.length) {
      selectFirstRule()
    }
    if (selectedRuleId.value !== null) {
      await syncDisplay(selectedRuleId.value)
    }
  } catch (error) {
    signalError.value = error instanceof Error ? error.message : '信号表读取失败'
  }
}

function selectFirstRule(): void {
  const first = rows.value[0]
  if (!first) return
  selectedRuleId.value = Number(first.id)
  interruptedTable.value = null
  crossReport.value = null
  signalPage.value = 1
  ruleForm.基线窗口 = String(first.基线窗口 ?? '7')
  ruleForm.波动阈值 = String(first.波动阈值 ?? '0.3')
  ruleForm.规则版本 = String(first.规则版本 ?? 'v1')
}

async function syncDisplay(ruleId: number): Promise<void> {
  try {
    const view = await getJson(`${ENDPOINT}/${ruleId}/signals/display`) as {
      active: SignalTable | null
      interrupted: SignalTable | null
      rollback: string[]
    }
    if (view.rollback?.length) {
      signalMessage.value = `显示窗回滚残留事务：${view.rollback.join('、')}，活动表保持上一完整版本`
    }
    interruptedTable.value = view.interrupted
    await loadAudit(ruleId)
  } catch (error) {
    signalError.value = error instanceof Error ? error.message : '显示窗读取失败'
  }
}

async function selectRule(ruleId: number): Promise<void> {
  selectedRuleId.value = ruleId
  interruptedTable.value = null
  crossReport.value = null
  signalPage.value = 1
  const rule = rows.value.find((item) => Number(item.id) === ruleId)
  if (rule) {
    ruleForm.基线窗口 = String(rule.基线窗口 ?? '7')
    ruleForm.波动阈值 = String(rule.波动阈值 ?? '0.3')
    ruleForm.规则版本 = String(rule.规则版本 ?? 'v1')
  }
  await refreshWorkbench()
}

async function openDisplay(): Promise<void> {
  if (selectedRuleId.value === null) return
  await syncDisplay(selectedRuleId.value)
}

async function build(mode: 'auto' | 'reuse_patch' | 'full_rebuild'): Promise<void> {
  if (selectedRuleId.value === null) return
  signalError.value = ''
  signalMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${selectedRuleId.value}/signals/build`, {
      method: 'POST',
      body: JSON.stringify({ mode }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail ?? '信号表构建失败')
    const table = payload.table as SignalTable
    signalMessage.value = `构建完成：策略「${strategyLabel(table.构建策略)}」，复用 ${table.复用窗口数} 个窗口，重算 ${table.重算窗口数} 个窗口`
    await refreshWorkbench()
  } catch (error) {
    signalError.value = error instanceof Error ? error.message : '信号表构建失败'
    await refreshWorkbench()
  }
}

async function buildInterrupt(): Promise<void> {
  if (selectedRuleId.value === null) return
  signalError.value = ''
  try {
    const response = await request(`${ENDPOINT}/${selectedRuleId.value}/signals/build`, {
      method: 'POST',
      body: JSON.stringify({ mode: 'full_rebuild', fail_after: 4 }),
    })
    const payload = await response.json()
    // 中断按约定返回 400，payload.detail 说明原因，暂存表保留缺失项
    signalMessage.value = `窗口重建已中断：${payload.detail ?? payload.message}，可续写且不覆盖上一完整信号表`
    await selectRule(selectedRuleId.value)
  } catch (error) {
    signalError.value = error instanceof Error ? error.message : '中断模拟失败'
  }
}

async function resume(): Promise<void> {
  if (selectedRuleId.value === null) return
  signalError.value = ''
  try {
    const response = await request(`${ENDPOINT}/${selectedRuleId.value}/signals/resume`, { method: 'POST' })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail ?? '续写失败')
    signalMessage.value = `续写完成：信号表 ${data.table.表编号} 已提交活动表`
    interruptedTable.value = null
    await refreshWorkbench()
  } catch (error) {
    signalError.value = error instanceof Error ? error.message : '续写失败'
  }
}

async function confirmTable(): Promise<void> {
  if (selectedRuleId.value === null) return
  signalError.value = ''
  try {
    const response = await request(`${ENDPOINT}/${selectedRuleId.value}/signals/confirm`, { method: 'POST' })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail ?? '确认失败')
    signalMessage.value = data.message
    await refreshWorkbench()
  } catch (error) {
    signalError.value = error instanceof Error ? error.message : '确认失败'
  }
}

async function pullConsumer(): Promise<void> {
  signalError.value = ''
  try {
    const data = await getJson(`${ENDPOINT}/signals/consumer`) as { total: number; 核对标准: string }
    signalMessage.value = `消费端取得全量 ${data.核对标准} 异常检测信号表 ${data.total} 张`
  } catch (error) {
    signalError.value = error instanceof Error ? error.message : '消费端读取失败'
  }
}

async function runCrossCheck(): Promise<void> {
  if (!selectedTable.value) return
  signalError.value = ''
  try {
    const params = new URLSearchParams({
      metric: selectedTable.value.指标编号,
      metric_version: selectedTable.value.规则版本,
    })
    crossReport.value = await getJson(`${ENDPOINT}/signals/cross-check?${params}`) as CrossCheckReport
    signalMessage.value = '指标定义 V005 模块已核对信号表的指标编号、规则版本与复用窗口基线范围'
  } catch (error) {
    signalError.value = error instanceof Error ? error.message : '跨模块核对失败'
  }
}

async function saveRuleConfig(): Promise<void> {
  if (selectedRuleId.value === null) return
  signalError.value = ''
  try {
    const response = await request(`${ENDPOINT}/${selectedRuleId.value}/rule-config`, {
      method: 'PATCH',
      body: JSON.stringify(ruleForm),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail ?? '规则更新失败')
    signalMessage.value = data.message
    await reload()
    await refreshWorkbench()
  } catch (error) {
    signalError.value = error instanceof Error ? error.message : '规则更新失败'
  }
}

async function loadAudit(ruleId: number): Promise<void> {
  try {
    const data = await getJson(`${ENDPOINT}/signals/audit?rule_id=${ruleId}`) as { items: AuditRow[] }
    auditRows.value = (data.items ?? []).slice(-12).reverse()
  } catch {
    auditRows.value = []
  }
}

function resetFilters(): void {
  filters.value = {}
  void reload()
}

async function exportRows(): Promise<void> {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'anomaly_detect.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate(): void {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate(): Promise<void> {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row): Promise<void> {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('异常检测动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '异常检测操作失败'
  }
}

async function reload(): Promise<void> {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('检测规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '异常检测列表读取失败'
  }
}

onMounted(async () => {
  await reload()
  await refreshWorkbench()
})
</script>

<style scoped>
.signal-workbench {
  margin-top: 24px;
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px;
}
.signal-layout {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 14px;
  align-items: start;
}
.signal-side {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px;
  position: sticky;
  top: 12px;
}
.signal-side h4 {
  margin: 10px 0 6px;
  font-size: 13px;
}
.side-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 320px;
  overflow-y: auto;
}
.side-item {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
}
.side-item.active {
  border-color: var(--brand);
  box-shadow: 0 0 0 1px var(--brand) inset;
}
.side-meta {
  color: var(--muted);
}
.side-status {
  align-self: flex-start;
  background: #eef2ff;
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 11px;
}
.side-empty {
  background: #f1f5f9;
  color: var(--muted);
}
.cache-item {
  font-size: 11px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--border);
}
.signal-detail {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
}
.detail-head {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}
.range-panel {
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  margin: 10px 0;
  font-size: 13px;
}
.range-panel p {
  margin: 4px 0;
}
.call-chain {
  margin: 6px 0;
  padding-left: 18px;
  font-size: 12px;
}
.tradeoff {
  font-size: 12px;
  color: #334155;
}
.rule-config {
  margin: 10px 0;
}
.build-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 10px 0;
}
.signal-table {
  font-size: 12px;
}
.signal-table td,
.signal-table th {
  padding: 6px 8px;
  white-space: nowrap;
}
.verdict {
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 12px;
}
.verdict-bad {
  background: #fee2e2;
  color: #b42318;
}
.verdict-ok {
  background: #dcfce7;
  color: #166534;
}
.cross-panel,
.audit-panel {
  margin-top: 14px;
}
.audit-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
}
.audit-list li {
  display: flex;
  gap: 8px;
  border-bottom: 1px dashed var(--border);
  padding: 4px 0;
}
.audit-time {
  color: var(--muted);
  white-space: nowrap;
}
.audit-stage {
  font-weight: 600;
  white-space: nowrap;
}
.signal-msg {
  color: #166534;
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 13px;
}
.pager {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--muted);
}
.pager .btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
@media (max-width: 1100px) {
  .signal-layout {
    grid-template-columns: 1fr;
  }
  .signal-side {
    position: static;
  }
}
</style>
