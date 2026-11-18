import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  // 每次执行前把内存后端恢复到种子状态，保证整套用例可重复运行
  globalSetup: './tests/global-setup.ts',
  timeout: 300_000,
  expect: { timeout: 10_000 },
  // 后端是内存仓库，所有用例共享同一份模块数据；并行 worker 会互相改写状态
  // （例如信号表重置与 CRUD 登记同时发生），因此全链路按单 worker 串行执行。
  workers: 1,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:5173',
    browserName: 'chromium',
    headless: true,
    actionTimeout: 10_000,
  },
  reporter: 'line',
})
