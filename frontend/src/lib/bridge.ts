/**
 * 桥接客户端：与 app/bridge.py 的 js_api / 事件协议一一对应。
 *
 * Python→JS：window.__bridge.dispatch({event, payload})，由本模块分发。
 * JS→Python：window.pywebview.api.<method>()，返回 Promise。
 */

// ---------- 协议类型 ----------

export type LogLevel = 'INFO' | 'OK' | 'WARN' | 'ERROR'

export interface LogEntry {
  ts: string
  level: LogLevel
  msg: string
}

export type StatusState =
  | 'ready'
  | 'running'
  | 'stopping'
  | 'success'
  | 'stopped'
  | 'error'
  | 'updating'

export interface AppConfigState {
  target_url: string
  phone_number: string
  excel_path: string
  order_date: string
  split_ratio: number
  sss_url: string
  sss_account: string
  sss_excel_path: string
  sss_product_name: string
  sss_common_address: string
  sss_use_fixed_address: boolean
  sss_fixed_lnt: number
  sss_fixed_lat: number
  sss_fixed_area_code: string
  sss_fixed_address_detail: string
  api_mode: boolean
}

export interface AppState {
  version: string
  status: StatusState
  frozen: boolean
  config: AppConfigState
  passwords: { order: string; sss: string }
}

export type DecisionKind = 'order_retry' | 'sss_retry' | 'save_retry' | 'close_confirm'

export interface DecisionChoice {
  value: string
  label: string
  style: 'primary' | 'neutral' | 'danger'
}

export interface DecisionRequest {
  id: string
  kind: DecisionKind
  title: string
  message: string
  choices: DecisionChoice[]
}

export interface CaptchaRequest {
  id: string
  image: string
}

export interface UpdateAvailable {
  tag: string
  current: string
  body: string
  can_auto_install: boolean
}

export type BridgeEvent =
  | { event: 'log'; payload: LogEntry }
  | { event: 'status'; payload: { state: StatusState } }
  | {
      event: 'task:done'
      payload: { message: string; stopped: boolean; result: Record<string, number> }
    }
  | { event: 'task:error'; payload: { message: string } }
  | { event: 'task:browser_missing'; payload: { message: string } }
  | { event: 'update:available'; payload: UpdateAvailable }
  | { event: 'update:latest'; payload: { manual: boolean; current: string } }
  | { event: 'update:error'; payload: { message: string } }
  | { event: 'update:progress'; payload: { downloaded: number; total: number | null } }
  | { event: 'update:stage'; payload: { stage: string } }
  | { event: 'update:install_error'; payload: { message: string } }
  | { event: 'update:installed'; payload: { message: string } }
  | { event: 'decision'; payload: DecisionRequest }
  | { event: 'captcha'; payload: CaptchaRequest }

// ---------- js_api 载荷 ----------

export interface OrderFormPayload {
  url: string
  phone: string
  password: string
  excel: string
  date: string
  count: string
  remember: boolean
  api_mode: boolean
}

export interface SssFormPayload {
  url: string
  account: string
  password: string
  excel: string
  product_name: string
  common_address: string
  use_fixed_address: boolean
  fixed_lnt: string
  fixed_lat: string
  fixed_area_code: string
  fixed_address_detail: string
  remember: boolean
  api_mode: boolean
}

export interface FieldErrors {
  ok: boolean
  fields?: Record<string, { message: string }>
}

// ---------- window 声明 ----------

interface PywebviewApi {
  bridge_ready(): Promise<AppState>
  start_order(payload: OrderFormPayload): Promise<FieldErrors>
  start_sss(payload: SssFormPayload): Promise<FieldErrors>
  stop_task(): Promise<{ ok: boolean }>
  worker_alive(): Promise<boolean>
  resolve_decision(id: string, choice: string): Promise<{ ok: boolean }>
  resolve_captcha(id: string, code: string): Promise<{ ok: boolean }>
  choose_excel(mode: 'order' | 'sss'): Promise<{ path: string; error: string }>
  new_template(mode: 'order' | 'sss'): Promise<{ path: string; error: string }>
  check_browser(): Promise<{ ok: boolean }>
  clear_password(mode: 'order' | 'sss'): Promise<{ ok: boolean }>
  check_updates(manual: boolean): Promise<{ ok: boolean; reason?: string }>
  install_update(): Promise<{ ok: boolean; reason?: string }>
  open_external(url: string): Promise<{ ok: boolean }>
  frontend_report(payload: Record<string, unknown> | string): Promise<{ ok: boolean }>
  drain_events(): Promise<{ events: BridgeEvent[] }>
  begin_window_drag(x: number, y: number): Promise<{ ok: boolean; handled: boolean }>
  echo_test(message: string, payload?: Record<string, unknown>): Promise<{ echo: string; payload_keys: string[] | null }>
  window_action(action: 'minimize' | 'toggle_maximize' | 'close'): Promise<{ action?: string }>
  request_close(): Promise<{ action: string }>
  set_split_ratio(ratio: number): Promise<{ ok: boolean; ratio: number }>
}

declare global {
  interface Window {
    pywebview?: { api: PywebviewApi }
    __bridge: { dispatch(message: BridgeEvent): void }
  }
}

// ---------- 客户端实现 ----------

type Listener = (event: BridgeEvent) => void

const listeners = new Set<Listener>()
const queued: BridgeEvent[] = []

/** Python 端就绪前的事件先入队，握手后统一回放。 */
let apiReady = false

function dispatch(message: BridgeEvent): void {
  if (!apiReady) {
    queued.push(message)
    return
  }
  for (const listener of listeners) listener(message)
}

window.__bridge = { dispatch }

export function onBridgeEvent(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function api(): PywebviewApi {
  const raw = window.pywebview?.api
  if (!raw) throw new Error('pywebview API 尚未就绪')
  return new Proxy(raw, {
    get(target, prop) {
      const value = Reflect.get(target, prop)
      if (typeof value !== 'function') return value
      return (...args: unknown[]) => {
        const promise = (value as (...a: unknown[]) => Promise<unknown>).apply(target, args)
        // evaluate_js 结果投递在 WebKitGTK 上可能被吞，超时兜底避免 UI 永久悬挂。
        // 文件对话框等会合法长阻塞的调用不设超时。
        const timeoutMs = prop === 'drain_events' ? 4000 : 0
        if (!timeoutMs) return promise
        return Promise.race([
          promise,
          new Promise((_, reject) =>
            setTimeout(() => reject(new Error('bridge timeout')), timeoutMs),
          ),
        ])
      }
    },
  }) as PywebviewApi
}

export function isApiReady(): boolean {
  return Boolean(window.pywebview?.api)
}

export interface ReadyResult {
  state: AppState
  /** 模拟浏览器开发环境（无 pywebview）时为 true。 */
  mocked: boolean
}

/**
 * 等待 pywebview 注入完成，完成握手并回放积压事件。
 * 开发态（纯浏览器）下返回一份静态 mock 状态，便于脱离 Python 调 UI。
 */
export async function connectBridge(): Promise<ReadyResult> {
  if (!window.pywebview) {
    await new Promise<void>((resolve) => {
      // pywebview 6 GTK 的注入可能晚于首帧；10s 内未注入才降级 mock。
      const timer = setTimeout(() => resolve(), 10000)
      window.addEventListener('pywebviewready', () => {
        clearTimeout(timer)
        resolve()
      })
    })
  }
  if (!window.pywebview?.api) {
    // 浏览器直开（无 Python 壳）：提供 mock 状态方便样式开发。
    apiReady = true
    return { state: mockState(), mocked: true }
  }
  const state = await api().bridge_ready()
  apiReady = true
  for (const message of queued.splice(0)) dispatch(message)
  return { state, mocked: false }
}

function mockState(): AppState {
  return {
    version: '3.0.0-dev',
    status: 'ready',
    frozen: false,
    config: {
      target_url: 'https://m.icall.me/admin/#/login',
      phone_number: '13968033834',
      excel_path: '/home/zimu/文档/排单.xlsx',
      order_date: '2026-09-05',
      split_ratio: 0.38,
      sss_url: 'https://sssplusnew.zhuopaikeji.com/takeout',
      sss_account: '18758187837',
      sss_excel_path: '/home/zimu/文档/闪时送.xlsx',
      sss_product_name: '轻食',
      sss_common_address: '嗯哼',
      sss_use_fixed_address: true,
      sss_fixed_lnt: 119.728224,
      sss_fixed_lat: 30.256632,
      sss_fixed_area_code: '330110',
      sss_fixed_address_detail: '浙江农林大学东湖校区',
      api_mode: true,
    },
    passwords: { order: '', sss: '' },
  }
}
