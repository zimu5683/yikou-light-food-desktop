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
  | 'partial'
  | 'stopped'
  | 'error'
  | 'updating'

export interface AppConfigState {
  target_url: string
  phone_number: string
  excel_path: string
  order_date: string
  order_count: number | null
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
  sss_dry_run: boolean
  /** 平台支持客户端幂等字段时由配置指定，默认空 = 至少一次提交+对账确认。 */
  sss_idempotency_field?: string
  api_mode: boolean
}

export interface AppState {
  version: string
  status: StatusState
  frozen: boolean
  /** Python 进程标识：用于识别重启后 sequence 归零，避免复用旧 cursor。 */
  event_producer_id?: string
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

type BridgeEventBase =
  | { event: 'log'; payload: LogEntry }
  | { event: 'status'; payload: { state: StatusState } }
  | {
      event: 'task:done'
      payload: {
        message: string
        stopped: boolean
        partial: boolean
        result: Record<string, number | boolean | null>
      }
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
  | {
      event: 'events:dropped'
      payload: {
        dropped_count: number
        critical_dropped_count?: number
        first_sequence?: number
        last_sequence?: number
        total_dropped?: number
        total_critical_dropped?: number
        message: string
      }
    }

export interface BridgeEventMeta {
  event_id: string
  sequence: number
  created_at: number
  droppable: boolean
  /** Python 端合成的“事件被丢弃”告警，不对应真实 sequence。 */
  synthetic?: boolean
}

export type BridgeEvent = BridgeEventBase & BridgeEventMeta

export interface DrainEventsResult {
  events: BridgeEvent[]
  producer_id: string
  latest_sequence: number
  acked_sequence: number
  dropped_count: number
  critical_dropped_count?: number
  first_available_sequence: number
}

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

/** 订单表单防抖即时保存的载荷（不触发任务、不带密码）。 */
export interface OrderConfigPayload {
  url?: string
  phone?: string
  excel?: string
  date?: string
  count?: number | null
  api_mode?: boolean
}

/** 闪时送表单防抖即时保存的载荷（不触发任务、不带密码）。 */
export interface SssConfigPayload {
  url?: string
  account?: string
  excel?: string
  product_name?: string
  common_address?: string
  use_fixed_address?: boolean
  fixed_lnt?: string | number
  fixed_lat?: string | number
  fixed_area_code?: string
  fixed_address_detail?: string
  dry_run?: boolean
  api_mode?: boolean
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
  dry_run: boolean
  api_mode: boolean
}

export interface FieldErrors {
  ok: boolean
  reason?: string
  message?: string
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
  drain_events(lastSequence?: number, ackSequence?: number, producerId?: string): Promise<DrainEventsResult>
  begin_window_drag(x: number, y: number): Promise<{ ok: boolean; handled: boolean }>
  echo_test(message: string, payload?: Record<string, unknown>): Promise<{ echo: string; payload_keys: string[] | null }>
  window_action(action: 'minimize' | 'toggle_maximize' | 'close'): Promise<{ action?: string }>
  request_close(): Promise<{ action: string }>
  set_split_ratio(ratio: number): Promise<{ ok: boolean; ratio: number }>
  save_order_config(payload: OrderConfigPayload): Promise<{ ok: boolean; reason?: string; saved?: { order_date: string; order_count: number | null } }>
  save_sss_config(payload: SssConfigPayload): Promise<{ ok: boolean; reason?: string }>
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

// ---------- cursor / 重放 / 去重 ----------
//
// Python 保留最近事件并按 sequence 返回；前端只在事件成功 dispatch 后推进
// cursor，并持久化到 localStorage。页面刷新/短暂断开后可从断点重放；同一
// event_id 不会重复应用。Python 进程重启会换 producer_id，此时 cursor 归零。
const CURSOR_STORAGE_KEY = 'yikou.bridge.cursor.v1'
const DEDUPE_LIMIT = 5000

interface StoredCursor {
  producerId: string
  sequence: number
  ackSequence: number
}

let eventProducerId = ''
let eventCursor = 0
let eventAckCursor = 0
let droppedCountNotified = 0
const seenEventIds = new Set<string>()
const seenEventOrder: string[] = []

function readStoredCursor(): void {
  try {
    const raw = window.localStorage.getItem(CURSOR_STORAGE_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw) as Partial<StoredCursor>
    if (typeof parsed.producerId === 'string') eventProducerId = parsed.producerId
    if (typeof parsed.sequence === 'number') eventCursor = Math.max(0, parsed.sequence)
    if (typeof parsed.ackSequence === 'number') eventAckCursor = Math.max(0, parsed.ackSequence)
  } catch {
    // localStorage 不可用（隐私模式/文件协议）时退化为本次页面内 cursor。
  }
}

function persistCursor(): void {
  try {
    const value: StoredCursor = {
      producerId: eventProducerId,
      sequence: eventCursor,
      ackSequence: eventAckCursor,
    }
    window.localStorage.setItem(CURSOR_STORAGE_KEY, JSON.stringify(value))
  } catch {
    // 忽略存储失败；下一次轮询仍按内存 cursor 继续。
  }
}

function adoptProducer(producerId?: string): void {
  if (!producerId || producerId === eventProducerId) return
  // 新的 Python 进程：旧 sequence 无意义，必须从头消费保留窗口。
  eventProducerId = producerId
  eventCursor = 0
  eventAckCursor = 0
  droppedCountNotified = 0
  seenEventIds.clear()
  seenEventOrder.length = 0
  persistCursor()
}

function rememberEventId(eventId: string): boolean {
  if (seenEventIds.has(eventId)) return false
  seenEventIds.add(eventId)
  seenEventOrder.push(eventId)
  if (seenEventOrder.length > DEDUPE_LIMIT) {
    const oldest = seenEventOrder.shift()
    if (oldest) seenEventIds.delete(oldest)
  }
  return true
}

readStoredCursor()

/** 拉取并应用一批桥接事件；由 useApp 的定时轮询调用。 */
export async function pullBridgeEvents(): Promise<void> {
  if (!isApiReady()) return
  const result = await api().drain_events(eventCursor, eventAckCursor, eventProducerId)
  if (!result) return
  adoptProducer(result.producer_id)

  for (const event of result.events) {
    if (event.sequence <= eventCursor && !event.synthetic) continue
    if (seenEventIds.has(event.event_id)) {
      // 之前已成功应用但 cursor 尚未推进（例如持久化前页面抖动）：补推进即可。
      if (event.sequence > eventCursor) eventCursor = event.sequence
      continue
    }
    try {
      dispatch(event)
    } catch (error) {
      // 监听器异常时绝不能推进 cursor：保留该事件，下一次轮询重放。
      console.error('bridge event listener failed; event will be replayed', error)
      break
    }
    rememberEventId(event.event_id)
    if (event.sequence > eventCursor) eventCursor = event.sequence
  }

  if (result.acked_sequence > eventAckCursor) eventAckCursor = result.acked_sequence
  eventAckCursor = Math.max(eventAckCursor, eventCursor)
  if (result.dropped_count > droppedCountNotified) {
    droppedCountNotified = result.dropped_count
  }
  persistCursor()
}

export function bridgeCursor(): StoredCursor {
  return { producerId: eventProducerId, sequence: eventCursor, ackSequence: eventAckCursor }
}

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
        return new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('bridge timeout')), timeoutMs)
          promise.then(
            (value) => {
              clearTimeout(timer)
              resolve(value)
            },
            (error) => {
              clearTimeout(timer)
              reject(error)
            },
          )
        })
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
  adoptProducer(state.event_producer_id)
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
      order_count: null,
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
      sss_dry_run: true,
      sss_idempotency_field: '',
      api_mode: true,
    },
    passwords: { order: '', sss: '' },
  }
}
