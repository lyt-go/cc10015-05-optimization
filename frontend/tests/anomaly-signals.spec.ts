import { expect, test, type APIRequestContext } from '@playwright/test'

/**
 * V005 异常检测信号表工作台 E2E：
 * 窗口复用、规则版本同步、版本冲突两种做法、中断续写、显示窗回滚、确认消费与跨模块核对。
 *
 * 后端为内存仓库，每个用例先调用 /signals/reset 恢复种子状态，保证「首次构建必为重算、
 * 二次构建全部复用」等语义可重复；套件以单 worker 串行运行，避免共享状态互相干扰。
 */
const ENDPOINT = '/anomaly_detect'
const RESET_API = '/api/anomaly_detect/signals/reset'

async function resetEnvironment(request: APIRequestContext) {
  const response = await request.post(RESET_API)
  expect(response.ok()).toBeTruthy()
}

test.describe('异常检测信号表（V005）工作台', () => {
  test.describe.configure({ mode: 'serial' })
  let browserErrors: string[]

  test.beforeEach(async ({ page, request }) => {
    browserErrors = []
    page.on('pageerror', (e) => browserErrors.push(e.message))
    page.on('response', (r) => {
      if (r.url().includes('/api/') && r.status() >= 500) browserErrors.push(`${r.status()} ${r.url()}`)
    })
    await resetEnvironment(request)
    await page.goto(ENDPOINT)
    await expect(page.getByTestId('signal-workbench')).toBeVisible()
    // 侧栏列出全部 3 条种子检测规则，默认选中第一条
    await expect(page.getByTestId('side-item-1')).toBeVisible()
    await page.getByTestId('side-item-1').click()
  })

  test.afterEach(() => {
    // 构建中断接口按约定返回 400，属于业务语义而非服务端错误
    expect(browserErrors).toEqual([])
  })

  test.afterAll(async ({ request }) => {
    // 恢复种子三规则，避免影响 functionality.spec.ts 对初始行数的假设
    await resetEnvironment(request)
  })

  test('侧栏与详情同源：规则配置、V005 信号字段与基线范围', async ({ page }) => {
    await expect(page.getByTestId('signal-detail')).toBeVisible()
    await expect(page.getByTestId('build-auto')).toBeVisible()

    await page.getByTestId('build-auto').click()
    await expect(page.getByTestId('signal-table')).toBeVisible({ timeout: 15000 })

    const firstRow = page.getByTestId('signal-table').locator('tbody tr').first()
    await expect(firstRow).toBeVisible()
    // 每条信号保留基线范围与规则版本，并带 V005 三项核对
    await expect(firstRow.locator('td').nth(3)).toContainText('[')
    await expect(firstRow.locator('td').nth(11)).toContainText('v')
    for (const idx of [7, 8, 9]) {
      await expect(firstRow.locator('td').nth(idx)).toHaveText(/通过|不通过/)
    }
    // 侧栏与详情读取同一组数据：侧栏状态与详情 KPI 一致
    await expect(page.getByTestId('side-item-1')).toContainText('已完成')
    await expect(page.getByTestId('kpi-reuse')).toBeVisible()
  })

  test('连续检测只更新变化窗口：首次重算、第二次全部命中复用', async ({ page }) => {
    await page.getByTestId('build-auto').click()
    await expect(page.getByTestId('signal-table')).toBeVisible({ timeout: 15000 })
    expect(Number((await page.getByTestId('kpi-reuse').innerText()).trim())).toBe(0)
    const firstTotal = Number((await page.getByTestId('kpi-recompute').innerText()).trim())
    expect(firstTotal).toBeGreaterThan(0)

    await page.getByTestId('build-auto').click()
    await expect(page.getByTestId('signal-message')).toContainText(/复用 \d+ 个窗口/, { timeout: 15000 })
    expect(Number((await page.getByTestId('kpi-reuse').innerText()).trim())).toBeGreaterThan(0)
    expect(Number((await page.getByTestId('kpi-recompute').innerText()).trim())).toBe(0)
    // 复用范围与重算覆盖区间在详情页可见
    await expect(page.getByTestId('range-panel')).toContainText('复用范围')
    // 缓存注册表同步登记规则版本
    await expect(page.getByTestId('cache-registry')).toContainText('v1')
  })

  test('规则更新同步登记缓存，复用补算与全量重建两种做法', async ({ page }) => {
    await page.getByTestId('build-auto').click()
    await expect(page.getByTestId('signal-table')).toBeVisible({ timeout: 15000 })

    // 切换基线窗口 + 新版本，保存后缓存登记同步
    await page.getByTestId('config-window').fill('9')
    await page.getByTestId('config-version').fill('v2')
    await page.getByTestId('config-save').click()
    await expect(page.getByTestId('signal-message')).toContainText('缓存注册表已同步', { timeout: 10000 })
    await expect(page.getByTestId('cache-registry')).toContainText('v2')

    // 做法一：复用重叠窗口并补算边界——信号表显示复用范围并补算受影响边界
    await page.getByTestId('build-reuse').click()
    await expect(page.getByTestId('signal-table')).toBeVisible({ timeout: 15000 })
    await expect(page.getByTestId('range-panel')).toContainText('版本冲突策略')
    await expect(page.getByTestId('range-panel')).toContainText('复用重叠窗口并补算边界')
    expect(Number((await page.getByTestId('kpi-patch').innerText()).trim())).toBeGreaterThan(0)

    // 做法二：按新窗口全量重建——信号表切到全量结果并列出重算范围
    await page.getByTestId('config-window').fill('6')
    await page.getByTestId('config-version').fill('v3')
    await page.getByTestId('config-save').click()
    await page.getByTestId('build-full').click()
    await expect(page.getByTestId('signal-message')).toContainText('重算', { timeout: 15000 })
    expect(Number((await page.getByTestId('kpi-reuse').innerText()).trim())).toBe(0)
    expect(Number((await page.getByTestId('kpi-recompute').innerText()).trim())).toBeGreaterThan(0)
    await expect(page.getByTestId('range-panel')).toContainText('本次重算覆盖区间')
  })

  test('中断后按缺失项续写，不覆盖上一完整信号表', async ({ page }) => {
    await page.getByTestId('build-auto').click()
    await expect(page.getByTestId('signal-table')).toBeVisible({ timeout: 15000 })

    await page.getByTestId('config-window').fill('8')
    await page.getByTestId('config-version').fill('v4')
    await page.getByTestId('config-save').click()

    await page.getByTestId('build-interrupt').click()
    await expect(page.getByTestId('signal-message')).toContainText('中断', { timeout: 15000 })

    await page.getByTestId('build-resume').click()
    await expect(page.getByTestId('signal-message')).toContainText('续写完成', { timeout: 15000 })
    await expect(page.getByTestId('signal-table')).toBeVisible()
    await expect(page.getByTestId('side-item-1')).toContainText('已完成')
  })

  test('结果确认后消费端取得全量 V005 信号表，并可跨模块核对', async ({ page }) => {
    await page.getByTestId('build-auto').click()
    await expect(page.getByTestId('signal-table')).toBeVisible({ timeout: 15000 })

    await page.getByTestId('btn-confirm').click()
    await expect(page.getByTestId('signal-message')).toContainText('消费端', { timeout: 10000 })

    await page.getByTestId('consumer-pull').click()
    await expect(page.getByTestId('signal-message')).toContainText('V005', { timeout: 10000 })

    await page.getByTestId('btn-cross-check').click()
    await expect(page.getByTestId('cross-report')).toBeVisible({ timeout: 10000 })
    await expect(page.getByTestId('cross-report')).toContainText('跨模块联动核对')
    await expect(page.getByTestId('cross-report')).toContainText('通过')
  })

  test('原有 CRUD 链路与新工作台共存', async ({ page }) => {
    const bodyRows = page.locator('tbody tr[data-testid^="row-"]')
    await expect(bodyRows).toHaveCount(3)

    await page.getByTestId('filter-规则编号').fill('ANOM-0001')
    await page.getByTestId('query').click()
    await expect(bodyRows).toHaveCount(1)
    await page.getByTestId('reset').click()
    await expect(bodyRows).toHaveCount(3)

    const created = ['E2E-ANOM-1', '点击率', 'moving_average', '7', '0.3', '高', '0', 'v9']
    await page.getByTestId('open-create').click()
    const fields = ['规则编号', '监控指标', '检测算法', '基线窗口', '波动阈值', '告警级别', '静默时段', '规则版本']
    for (const [i, field] of fields.entries()) {
      await page.getByTestId(`create-${field}`).fill(created[i])
    }
    await page.getByTestId('submit-create').click()
    await expect(bodyRows).toHaveCount(4)
    const createdRow = page.locator('tbody tr').filter({ hasText: 'E2E-ANOM-1' })
    for (const action of ['提交校验', '标记生效', '停用规则']) {
      await createdRow.getByRole('button', { name: action, exact: true }).click()
    }
    await expect(createdRow).toHaveAttribute('data-status', '已停用')
    // 新规则也出现在信号表侧栏
    await expect(page.getByTestId('side-item-4')).toBeVisible()
  })
})
