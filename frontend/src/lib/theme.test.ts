/**
 * `lib/theme.ts` 的回归锁 —— 改动前它是 `src/lib/` 里唯一没有测试的模块。
 *
 * 为什么值得测：主题是否生效**只影响观感、不影响功能**，所以坏了很难被察觉
 * （界面照常跑，只是每次重启都回到浅色、或者深色下文字看不清）。而这段逻辑
 * 恰好有三处容易写错的边界：
 *
 * 1. `initialTheme` 必须**严格校验** localStorage 里的值 —— 只认 `"dark"` / `"light"`。
 *    若写成 `return saved` 之类的宽松写法，一个被写坏的值（`"DARK"`、`"purple"`）
 *    就会被当成合法主题返回。
 * 2. localStorage 在**隐私模式 / 存储被禁用**时会抛异常，两处都必须吞掉：
 *    读失败退回浅色，写失败不能影响切换本身。
 * 3. `applyTheme` 必须**双向**切换 `.dark`（切回浅色要移除），而不是只加不减。
 *
 * 本文件不需要 DOM 环境，也不需要新增依赖：直接给 `globalThis` 打桩即可，
 * 与既有的 `bridge.events*.test.ts` 同一套做法。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

type Stubs = {
  store: Map<string, string>
  classes: Set<string>
  throwOnGet: boolean
  throwOnSet: boolean
}

function installStubs(): Stubs {
  const state: Stubs = {
    store: new Map(),
    classes: new Set(),
    throwOnGet: false,
    throwOnSet: false,
  }
  globalThis.localStorage = {
    getItem(key: string) {
      if (state.throwOnGet) throw new Error('storage disabled')
      return state.store.get(key) ?? null
    },
    setItem(key: string, value: string) {
      if (state.throwOnSet) throw new Error('quota exceeded')
      state.store.set(key, value)
    },
  } as unknown as Storage
  globalThis.document = {
    documentElement: {
      classList: {
        toggle(name: string, force?: boolean) {
          if (force) state.classes.add(name)
          else state.classes.delete(name)
          return Boolean(force)
        },
      },
    },
  } as unknown as Document
  return state
}

// 模块本身不持有状态（每次调用才读 localStorage / document），因此可以只 import 一次。
const { applyTheme, initialTheme } = await import('./theme.ts')

test('initialTheme defaults to light when storage is empty', () => {
  installStubs()
  assert.equal(initialTheme(), 'light')
})

test('initialTheme returns the saved theme when it is valid', () => {
  const state = installStubs()
  for (const theme of ['dark', 'light'] as const) {
    state.store.set('yikou-theme', theme)
    assert.equal(initialTheme(), theme)
  }
})

test('initialTheme rejects anything that is not exactly dark or light', () => {
  // 变异测试备注：把 `saved === "dark" || saved === "light"` 弱化成只认 `"dark"`，
  // 本套测试**抓不到** —— 因为存的是 "light" 时会落到末尾的 `return "light"`，
  // 结果完全相同，属**等价变异**。这里记录下来，避免后人误判成缺口。
  const state = installStubs()
  // 只认这两个字面量：大小写不同、拼错、空串都必须退回浅色。
  for (const bad of ['DARK', 'Dark', 'purple', '', ' ', 'dark ', 'true']) {
    state.store.set('yikou-theme', bad)
    assert.equal(initialTheme(), 'light', `值 ${JSON.stringify(bad)} 不该被当成合法主题`)
  }
})

test('initialTheme survives a throwing localStorage (private mode)', () => {
  const state = installStubs()
  state.store.set('yikou-theme', 'dark')
  state.throwOnGet = true
  assert.equal(initialTheme(), 'light', '读失败必须退回浅色，而不是抛出去')
})

test('applyTheme adds the dark class and persists the choice', () => {
  const state = installStubs()
  applyTheme('dark')
  assert.ok(state.classes.has('dark'), '<html> 上应当挂上 .dark')
  assert.equal(state.store.get('yikou-theme'), 'dark')
})

test('applyTheme removes the dark class when switching back to light', () => {
  const state = installStubs()
  applyTheme('dark')
  applyTheme('light')
  assert.ok(!state.classes.has('dark'), '切回浅色必须移除 .dark（不能只加不减）')
  assert.equal(state.store.get('yikou-theme'), 'light')
})

test('applyTheme still switches the class when persistence fails', () => {
  const state = installStubs()
  state.throwOnSet = true
  // 存储写不进去只是「记不住」，不该阻止本次切换、更不该把异常抛给调用方。
  assert.doesNotThrow(() => applyTheme('dark'))
  assert.ok(state.classes.has('dark'))
})

test('applyTheme is idempotent for the same theme', () => {
  const state = installStubs()
  applyTheme('dark')
  applyTheme('dark')
  assert.ok(state.classes.has('dark'))
  assert.equal(initialTheme(), 'dark')
})
