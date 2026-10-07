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
  /** 名单来源：wps = 下单前从 WPS 云端读当天标 1 的人；excel = 读《闪时送.xlsx》。 */
  sss_order_source: 'wps' | 'excel'
  sss_product_name: string
  sss_common_address: string
  sss_use_fixed_address: boolean
  sss_fixed_lnt: number
  sss_fixed_lat: number
  sss_fixed_area_code: string
  sss_fixed_address_detail: string
  sss_dry_run: boolean
  sss_preflight: boolean
  /** 平台支持客户端幂等字段时由配置指定，默认空 = 至少一次提交+对账确认。 */
  sss_idempotency_field?: string
  api_mode: boolean
  /** WPS 云文档同步：把本地排单表增量写入云端排单表。 */
  wps_enabled: boolean
  wps_test_mode: boolean
  wps_test_file_id: string
  wps_test_drive_id: string
  wps_drive_id: string
  wps_cli_path: string
  wps_tables: Record<string, { file_id: string; drive_id?: string }>
  wps_test_tables: Record<string, string>
  wps_target_hour_start: number
  wps_target_hour_end: number
  wps_marker_enabled: boolean
}

/** 云文档同步状态（bridge.wps_status 返回）。 */
export interface WpsTableState {
  sheet: string
  file_id: string
  effective_file_id: string
  last_sync: string
  last_people: number
}

export interface WpsStatus {
  ok: boolean
  reason?: string
  enabled: boolean
  test_mode: boolean
  cli_path: string
  cli_found: boolean
  authenticated: boolean
  target_date: string
  weekday_number: number
  excel_path: string
  marker_enabled: boolean
  /** 云表按地址顺序重排：总开关。 */
  sort_enabled: boolean
  /** 每张子表的地址顺序清单；空数组 = 该表按地址升序。 */
  address_order: Record<string, string[]>
  /** 出厂默认顺序（界面「恢复默认」用，避免前后端各写一份）。 */
  address_order_defaults: Record<string, string[]>
  test_file_id?: string
  /** 测试模式下每张正式表对应的测试副本。 */
  test_tables?: Record<string, string>
  /** 当前写入目标是否全部是测试副本（不与正式表重合）。 */
  writing_test_copies?: boolean
  /** 正式排单表 ID 备份（暂停使用，可用于切回）。 */
  production_tables?: Record<string, string>
  state_path?: string
  tables: WpsTableState[]
}

export interface WpsPlanSummary {
  to_update: number
  to_append: number
  unchanged: number
  warned: number
}

export interface WpsCopyCheckItem {
  sheet: string
  file_id: string
  production_id: string
  status: 'aligned' | 'drifted' | 'same_as_production' | 'unreadable' | 'production_unreadable'
  rows?: number
  production_rows?: number
  missing?: string[]
  extra?: string[]
  reason?: string
}

export interface WpsCopyCheck {
  ok: boolean
  reason?: string
  drifted?: string[]
  all_aligned?: boolean
  tables?: WpsCopyCheckItem[]
}

export interface WpsResult {
  ok: boolean
  reason?: string
  target_date?: string
  text?: string
  summary?: WpsPlanSummary
  test_mode?: boolean
  result?: { written: number; failed: number; sheets: Array<{ sheet: string; status: string; reason?: string }> }
}

// ---------- 计划口径 vs 执行口径（禁止混用） ----------

/** 计划**要改**多少行；`kind` 恒为 `plan`。 */
export interface WpsPlannedSummary {
  kind: 'plan'
  to_update: number
  to_append: number
  unchanged: number
  skipped: number
  warned: number
  blocked: number
  rows: {
    to_update: number
    to_append: number
    unchanged: number
    skipped: number
    warned: number
    blocked: number
  }
  note: string
}

export interface WpsExecutionSheetCounts {
  total: number
  verified: number
  noop: number
  failed: number
  uncertain: number
  skipped: number
  blocked: number
  other: number
}

/**
 * 执行**实际证明**了多少行；`kind` 恒为 `execution`。
 *
 * ``rows`` 里为 `null` 表示"无法证明"，**不能**用计划数或成功表数顶替。
 */
export interface WpsExecutionSummary {
  kind: 'execution'
  contract_version: number
  status: string
  executed: boolean
  counts_source: string
  sheets: WpsExecutionSheetCounts
  rows: {
    verified: number | null
    failed: number | null
    uncertain: number | null
    skipped: number | null
    planned: number
  }
  rows_unknown: boolean
  proven_no_write: boolean
  written_sheets: number
  failed_sheets: number
  note: string
  next_action: string
}

/** 一张云表的逐格状态（预览与上传共用同一结构）。 */
export interface WpsTableDetail {
  sheet: string
  file_id: string
  target_date: string
  target_col: number
  target_header: string
  blocked_reason: string
  previous_batch: string
  warnings: string[]
  sort_enabled: boolean
  unknown_addresses: string[]
  counts: {
    to_update: number
    to_append: number
    unchanged: number
    skipped: number
    warned: number
    blocked: number
  }
  changes: Array<{
    kind: 'existing' | 'new'
    name: string
    phone: string
    row: number
    slot: number
    total_before: number
    total_after: number
    target_ok: boolean
    target_blocked: boolean
    target_occupied: string
    needs_write: boolean
    local_rows: number[]
    local_meals: number
    ledger_prev: number | null
  }>
}

/** `wps_preview()` 的返回值：必须把 `preview_id` 原样传给 `wps_upload`。 */
export interface WpsPreviewResult extends WpsResult {
  code?: string
  status?: string
  next_action?: string
  /** 预览令牌（10 分钟、一次性）。 */
  preview_id?: string
  expires_at?: string
  expires_in?: number
  ttl_seconds?: number
  state?: 'valid' | 'expired' | 'consumed' | 'invalidated'
  local_sha256?: string
  fingerprint?: { local_sha256: string; context: string; plan: string }
  planned_summary?: WpsPlannedSummary
  execution_summary?: WpsExecutionSummary
  tables?: WpsTableDetail[]
  blocked?: Array<{ sheet: string; reason: string }>
  warnings?: string[]
  /** 上下文变化时列出变化的键（排序开关、目标表、日期…）。 */
  changed?: string[]
  operation_id?: string
  conflicting_operation?: OperationInfo | null
}

/** `wps_upload(preview_id)` 的返回值。 */
export interface WpsUploadResult extends WpsPreviewResult {
  journal_path?: string
  failed_sheets?: string[]
}

// ---------- WPS 恢复状态与人工处置 ----------

export type WpsSheetStatus =
  | 'planned'
  | 'writing'
  | 'ledger_pending'
  | 'uncertain'
  | 'verified'
  | 'failed_no_write'
  | 'not_started'
  | 'retired_guarded'

export interface WpsRecoverySheet {
  sheet: string
  status: WpsSheetStatus | string
  display_status: string
  reason: string
  problems: string[]
  next_action: string
  allowed_actions: WpsRecoveryDecision[]
  target_date: string
  target_ref: string
  people: Array<{
    name: string
    phone: string
    slot: number
    local_meals: number
    total_before: number
    total_after: number
  }>
  people_count: number
  cloud_checked: boolean
  evidence: string
  retire_note: string
  retired_at: string
  prior_status: string
}

export interface WpsRecoveryOperation {
  operation_id: string
  operation_ref: string
  status: string
  next_action: string
  created_at: string
  updated_at: string
  target_date: string
  pending: boolean
  retired_guarded: boolean
  retire_note: string
  sheets: WpsRecoverySheet[]
}

/** `wps_recovery_status()`：只读本地日志，不联网、不写任何文件。 */
export interface WpsRecoveryStatus {
  ok: boolean
  contract_version: number
  source: 'local_journal'
  read_only: true
  queried_cloud: false
  error_code?: string
  reason?: string
  next_action?: string
  counts: Record<string, number>
  pending_count?: number
  guarded_count?: number
  operations: WpsRecoveryOperation[]
  pending_operations?: WpsRecoveryOperation[]
  journal_path?: string
}

export type WpsRecoveryDecision = 'retire_guarded' | 'cloud_verified' | 'cloud_untouched' | 'keep'

export interface WpsRecoveryResolveResult {
  ok: boolean
  status: string
  code?: string
  reason?: string
  reason_code?: string
  next_action?: string
  contract_version?: number
  read_only?: boolean
  /** 恒为 false：处置入口永不写云端。 */
  cloud_write: boolean
  operation_ref?: string
  changed: boolean
  note?: string
  resolved_at?: string
  operations: Array<{ journal_id?: string; sheet?: string; status: string; reason?: string }>
  allowed_decisions?: WpsRecoveryDecision[]
}

// ---------- 闪时送未决记录 ----------

export interface SssUncertainRecordView {
  journal_id: string
  identifier: string
  sheet: string
  batch_id: string
  delivery_date: string
  status: 'inflight' | 'unresolved' | string
  error: string
  created_at: string
  name: string
  /** 已脱敏（前 3 后 4）。 */
  phone: string
  delivery_time: string
  door_num: string
  address: string
  goods_name: string
  account: string
  platform: string
  source: string
}

/** `sss_uncertain_records()`：日志损坏时 `journal_unreadable=true`，绝不能当成"没有"。 */
export interface SssUncertainState {
  ok: boolean
  reason?: string
  error_code?: 'journal_unreadable' | string
  journal_unreadable: boolean
  records: SssUncertainRecordView[]
  counts: {
    active: number
    inflight: number
    unresolved: number
    resolved: number
    discarded: number
  }
  journal_path?: string
  fingerprint?: string
  delivery_date?: string
  account?: string
  next_action?: string
}

export type SssReviewClassification =
  | 'station_confirmed'
  | 'station_missing'
  | 'station_found_other_day'
  | 'scan_failed'

export interface SssUncertainReviewItem {
  journal_id: string
  classification: SssReviewClassification
  reason: string
  name: string
  phone: string
  delivery_time: string
  error: string
}

/** `start_sss_review()`：只读核对（不对远端订单产生写副作用）。 */
export interface SssUncertainReview {
  ok: boolean
  reason?: string
  next_action?: string
  journal_path?: string
  batch_key?: string
  delivery_date?: string
  account?: string
  created_at?: string
  expires_at?: string
  ttl_seconds?: number
  queried_cloud?: boolean
  read_only?: boolean
  cloud_write?: false
  results: SssUncertainReviewItem[]
  counts?: Record<string, number>
  confirmed?: number
  missing?: number
  other_day?: number
  scan_failed?: number
}

export type SssUncertainDecision = 'station_present' | 'station_absent' | 'keep'

/** `sss_uncertain_resolve()`：只写本地日志，**不会发送创建订单请求**。 */
export interface SssUncertainResolveResult {
  ok: boolean
  status: string
  code?: string
  reason?: string
  reason_code?: string
  next_action?: string
  cloud_write: boolean
  changed: boolean
  note?: string
  record_ids?: string[]
  operations: Array<{ journal_id: string; status: string }>
}

// ---------- 统一操作状态 ----------

export interface OperationInfo {
  operation_id: string
  mode: string
  title: string
  status: string
  active: boolean
  phase: string
  reason: string
  next_action: string
  summary: Record<string, unknown>
  started_at: string
  finished_at: string
}

export interface OperationStatusResult {
  ok: boolean
  active: boolean
  operation: OperationInfo | null
  last: OperationInfo | null
  reason?: string
  reason_code?: string
}

/** 云端当天名单（bridge.sss_day_orders 返回）。 */
export interface SssDayOrders {
  ok: boolean
  reason?: string
  target_date?: string
  date_text?: string
  total?: number
  archive_error?: string
  meals?: Record<
    string,
    {
      table: string
      marked: number
      skipped_address: number
      orders: number
      date_text: string
      skipped: boolean
      reason: string
      warnings: string[]
    }
  >
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

export interface PendingAddressItem {
  raw_address: string
  order_numbers: string[]
  campus: string
  confidence: string
  reason: string
  suggested_point: string
  candidates?: Record<string, number>
}

export interface AddressInputRequest {
  id: string
  title: string
  message: string
  items: PendingAddressItem[]
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
  | { event: 'update:available'; payload: UpdateAvailable }
  | { event: 'update:latest'; payload: { manual: boolean; current: string } }
  | { event: 'update:error'; payload: { message: string } }
  | { event: 'update:progress'; payload: { downloaded: number; total: number | null } }
  | { event: 'update:stage'; payload: { stage: string } }
  | { event: 'update:install_error'; payload: { message: string } }
  | { event: 'update:installed'; payload: { message: string } }
  | { event: 'decision'; payload: DecisionRequest }
  | { event: 'captcha'; payload: CaptchaRequest }
  | { event: 'address_input'; payload: AddressInputRequest }
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
  order_source?: 'wps' | 'excel'
  product_name?: string
  common_address?: string
  use_fixed_address?: boolean
  fixed_lnt?: string | number
  fixed_lat?: string | number
  fixed_area_code?: string
  fixed_address_detail?: string
  dry_run?: boolean
  preflight?: boolean
  api_mode?: boolean
}

export interface SssFormPayload {
  url: string
  account: string
  password: string
  excel: string
  order_source: 'wps' | 'excel'
  product_name: string
  common_address: string
  use_fixed_address: boolean
  fixed_lnt: string
  fixed_lat: string
  fixed_area_code: string
  fixed_address_detail: string
  remember: boolean
  dry_run: boolean
  preflight: boolean
  api_mode: boolean
}

export interface FieldErrors {
  ok: boolean
  reason?: string
  message?: string
  fields?: Record<string, { message: string }>
}

/** 云文档同步配置（只包含这个页签会改的字段）。 */
export interface WpsConfigPayload {
  enabled: boolean
  test_mode: boolean
  cli_path: string
  drive_id: string
  test_file_id: string
  test_drive_id: string
  marker_enabled: boolean
  /** 排序总开关；不带该字段时后端保持原值。 */
  sort_enabled?: boolean
  /** 每张子表的地址顺序（传数组）；空数组 = 该表按地址升序。 */
  address_order?: Record<string, string[]>
  tables: Record<string, { file_id: string; drive_id?: string }>
  test_tables: Record<string, string>
}

// ---------- window 声明 ----------

interface PywebviewApi {
  bridge_ready(): Promise<AppState>
  start_order(payload: OrderFormPayload): Promise<FieldErrors>
  start_sss(payload: SssFormPayload): Promise<FieldErrors>
  sss_day_orders(): Promise<SssDayOrders>
  stop_task(): Promise<{ ok: boolean }>
  worker_alive(): Promise<boolean>
  resolve_decision(id: string, choice: string): Promise<{ ok: boolean }>
  resolve_captcha(id: string, code: string): Promise<{ ok: boolean }>
  resolve_address_input(id: string, entries: Record<string, string>): Promise<{ ok: boolean }>
  choose_excel(mode: 'order' | 'sss'): Promise<{ path: string; error: string }>
  new_template(mode: 'order' | 'sss'): Promise<{ path: string; error: string }>
  check_browser(): Promise<{ ok: boolean }>
  wps_status(): Promise<WpsStatus>
  wps_preview(): Promise<WpsPreviewResult>
  /** 必须传 `wps_preview()` 返回的 `preview_id`；无参调用会被安全拒绝。 */
  wps_upload(previewId?: string): Promise<WpsUploadResult>
  wps_authorize(): Promise<{ ok: boolean; reason?: string; hint?: string }>
  wps_check_copies(): Promise<WpsCopyCheck>
  /** 只读恢复状态：不联网、不写任何文件。 */
  wps_recovery_status(): Promise<WpsRecoveryStatus>
  /** 人工处置：带确认与备注；**永不写云端**。 */
  wps_recovery_resolve(payload: {
    operation_id: string
    decision: WpsRecoveryDecision
    confirm: string
    note: string
    confirm_structure_checked?: boolean
  }): Promise<WpsRecoveryResolveResult>
  /** 未决记录列表（脱敏）；日志损坏时 `journal_unreadable=true`。 */
  sss_uncertain_records(): Promise<SssUncertainState>
  /** 只读核对：登录 + 查订单 + 存快照；不发送创建订单请求。 */
  start_sss_review(payload?: { password?: string }): Promise<SssUncertainReview>
  /** 人工处置未决记录：不发送创建订单请求。 */
  sss_uncertain_resolve(payload: {
    decision: SssUncertainDecision
    confirm: string
    note: string
    record_ids: string[]
  }): Promise<SssUncertainResolveResult>
  /** 统一操作状态：前端据此禁用按钮并显示"谁在跑、跑到哪一步"。 */
  operation_status(operationId?: string): Promise<OperationStatusResult>
  save_wps_config(payload: WpsConfigPayload): Promise<{ ok: boolean; reason?: string }>
  /** 删除本机密钥环里的密码；`ok=false` 时**没有**删掉，必须如实提示。 */
  clear_password(mode: 'order' | 'sss'): Promise<{
    ok: boolean
    removed?: boolean
    reason?: string
    next_action?: string
  }>
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
      sss_order_source: 'wps',
      sss_product_name: '轻食',
      sss_common_address: '嗯哼',
      sss_use_fixed_address: true,
      sss_fixed_lnt: 119.728224,
      sss_fixed_lat: 30.256632,
      sss_fixed_area_code: '330110',
      sss_fixed_address_detail: '浙江农林大学东湖校区',
      sss_dry_run: true,
      sss_preflight: false,
      sss_idempotency_field: '',
      api_mode: true,
      wps_enabled: false,
      wps_test_mode: true,
      wps_test_file_id: '',
      wps_test_drive_id: '',
      wps_drive_id: '',
      wps_cli_path: '',
      wps_tables: {},
      wps_test_tables: {},
      wps_target_hour_start: 20,
      wps_target_hour_end: 10,
      wps_marker_enabled: true,
    },
    passwords: { order: '', sss: '' },
  }
}


// ---------- 结果分档：前端必须能区分成功/部分/未知/阻断/需恢复 ----------

export type OutcomeKind =
  | 'success'
  | 'partial'
  | 'unknown'
  | 'blocked'
  | 'needs_recovery'
  | 'rejected'

export interface OutcomeView {
  kind: OutcomeKind
  label: string
  tone: 'ok' | 'warn' | 'danger' | 'muted'
  /** 界面上要显示的"下一步"（后端给出时优先用它）。 */
  nextAction: string
}

const OUTCOME_LABELS: Record<OutcomeKind, { label: string; tone: OutcomeView['tone'] }> = {
  success: { label: '成功', tone: 'ok' },
  partial: { label: '部分完成', tone: 'warn' },
  unknown: { label: '结果未知（需人工核对）', tone: 'danger' },
  blocked: { label: '已被拒绝（未写入）', tone: 'warn' },
  needs_recovery: { label: '需要恢复处置', tone: 'danger' },
  rejected: { label: '未执行', tone: 'muted' },
}

/**
 * 把后端返回的状态分档。
 *
 * 顺序很重要：**未知**永远优先于"失败" —— 写入结果不确定时不能被显示成
 * 一次干净的失败，否则用户会直接重试，而重试可能造成重复累加/重复下单。
 */
export function classifyOutcome(input: {
  status?: string
  code?: string
  ok?: boolean
  executionSummary?: WpsExecutionSummary
  uncertainPending?: number
}): OutcomeView {
  const status = String(input.status ?? '').toLowerCase()
  const code = String(input.code ?? '')
  const summary = input.executionSummary
  let kind: OutcomeKind = 'success'

  if (code === 'operation_conflict') {
    kind = 'rejected'
  } else if (code === 'preview_changed' || code?.startsWith('preview_')
             || code === 'missing_preview') {
    kind = 'rejected'
  } else if (status === 'uncertain' || status === 'verify_unreadable'
             || status === 'blocked_by_uncertain' || status === 'uncertain_journal_unreadable') {
    kind = 'unknown'
  } else if (status === 'blocked' || status === 'stale_batch'
             || status === 'insufficient_balance' || status === 'balance_unknown'
             || status === 'rejected') {
    kind = 'blocked'
  } else if (status === 'failed' || status === 'error') {
    // 失败但"结果未知"时仍按未知处理。
    kind = summary && summary.rows_unknown && !summary.proven_no_write
      ? 'unknown' : 'partial'
  } else if (status === 'partial' || status === 'verify_failed') {
    kind = summary && summary.rows_unknown ? 'unknown' : 'partial'
  } else if (status === 'no_orders' || status === 'dry_run' || status === 'noop') {
    kind = 'success'
  } else if (!input.ok && status === '') {
    kind = 'rejected'
  }

  if ((input.uncertainPending ?? 0) > 0 && kind === 'success') {
    kind = 'unknown'
  }
  const meta = OUTCOME_LABELS[kind]
  return { kind, label: meta.label, tone: meta.tone, nextAction: '' }
}

/** 操作是否仍占用槽位（前端据此禁用按钮；安全判定仍在后端）。 */
export function operationIsActive(status: OperationStatusResult | null): boolean {
  return Boolean(status?.active)
}

/** 冲突时的提示文案（"谁在跑 + 下一步"）。 */
export function conflictNotice(status: OperationStatusResult | null): string {
  const current = status?.operation
  if (!current) return ''
  const phase = current.phase ? `（${current.phase}）` : ''
  return `当前正在执行：${current.title}${phase}`
}
