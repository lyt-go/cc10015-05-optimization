import { request as playwrightRequest, type FullConfig } from '@playwright/test'

/**
 * 全局前置：后端为内存仓库，整套 E2E（含原始 CRUD 全链路）都假设从种子三行数据开始。
 * 每次执行测试前调用演练环境重置接口，保证同一后端可重复运行、用例间不累积状态。
 */
async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects?.[0]?.use?.baseURL ?? 'http://127.0.0.1:5173'
  const apiBase = String(baseURL).replace(/\/$/, '')
  const context = await playwrightRequest.newContext({ baseURL: apiBase })
  try {
    const response = await context.post('/api/test/reset-environment')
    if (!response.ok()) {
      throw new Error(`环境重置失败：${response.status()} ${await response.text()}`)
    }
  } finally {
    await context.dispose()
  }
}

export default globalSetup
