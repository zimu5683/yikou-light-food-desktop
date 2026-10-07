/**
 * 云文档同步页签：把本地排单表的内容增量写入 WPS 云端排单表。
 *
 * 设计（详见 design/WPS-CLOUD-SYNC-PLAN.md）：
 * - 默认开启「测试模式」，只写测试文件，绝不碰正式排单表；
 * - 写入前必须先点「预览」，看清"会改谁、改成几"再确认上传；
 *   「预览」发放 10 分钟一次性令牌，上传必须带上它；预览之后任何变化
 *   （本地文件、目标表、排序开关、云端计划）都会让令牌失效并要求重新预览；
 * - 总餐次是**增量累加**：云端现值 + (本地本批餐次 − 账本里本批已同步的本地餐次)，
 *   因此同一批重复上传不会翻倍；协作者写的日期格 0 只读不写；
 * - 「地址排序」按子表维护列B 的顺序：新增客户先插到第 3、4 行之间再整表重排；
 * - 任何失败都只记日志，不影响本地排单任务。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { Field, TextInput } from '@/components/fields'
import { useApp } from '@/hooks/appContext'
import {
  api,
  classifyOutcome,
  conflictNotice,
  isApiReady,
  operationIsActive,
  type WpsCopyCheck,
  type WpsExecutionSummary,
  type WpsPlannedSummary,
  type WpsPreviewResult,
  type WpsRecoveryDecision,
  type WpsRecoveryOperation,
  type WpsRecoveryStatus,
  type WpsStatus,
  type WpsTableDetail,
} from '@/lib/bridge'
import { cn } from '@/lib/utils'
import { splitAddressLines } from '@/lib/format'
import { canUpload, singleFlight } from '@/lib/interaction'

/** 地址排序涉及的 6 张子表（顺序与后端 DEFAULT_ADDRESS_ORDER 一致）。 */
const ADDRESS_SHEETS = [
  '东湖中餐',
  '衣锦中餐',
  '医学院中餐',
  '东湖晚餐',
  '衣锦晚餐',
  '医学院晚餐',
] as const

const ADDRESS_PLACEHOLDER = '一行一个地址，从上到下就是排列顺序；留空 = 按地址升序排列（医学院用这种）'

/** 令牌类错误：本地预览已失效，必须重新预览（不允许直接重试上传）。 */
const STALE_PREVIEW_CODES = new Set([
  'preview_changed',
  'preview_expired',
  'preview_consumed',
  'preview_not_found',
  'preview_invalidated',
  'missing_preview',
])

/** 多行文本 → 顺序数组：按行拆分、去首尾空白、丢掉空行。 */
/** 地址清单输入框：样式与 TextInput 一致（底色 secondary，聚焦转卡片色）。 */
const addressTextareaClass = cn(
  'mt-1 w-full resize-y rounded-[4px] border border-transparent bg-secondary px-2.5 py-1.5',
  'font-mono text-xs leading-relaxed transition-colors outline-none',
  'placeholder:text-ink-faint focus-visible:border-ring focus-visible:bg-card',
  'focus-visible:ring-[3px] focus-visible:ring-ring/50',
)

export function CloudForm() {
  const { config, operation, refreshOperation } = useApp()
  const [status, setStatus] = useState<WpsStatus | null>(null)
  const [preview, setPreview] = useState<WpsPreviewResult | null>(null)
  const [busy, setBusy] = useState<'' | 'preview' | 'upload' | 'auth' | 'refresh' | 'check'>('')
  const [recovery, setRecovery] = useState<WpsRecoveryStatus | null>(null)
  // 上传用单飞包装：双击/连点只会发出一次 wps_upload（后端另有权威互斥）。
  const uploadOnce = useRef(
    singleFlight(async (previewId: string) => api().wps_upload(previewId)),
  ).current
  const [resolveTarget, setResolveTarget] = useState<WpsRecoveryOperation | null>(null)
  const [copyCheck, setCopyCheck] = useState<WpsCopyCheck | null>(null)
  const [message, setMessage] = useState('')
  const [enabled, setEnabled] = useState(config?.wps_enabled ?? false)
  const [testMode, setTestMode] = useState(config?.wps_test_mode ?? true)
  const [cliPath, setCliPath] = useState(config?.wps_cli_path ?? '')
  const [marker, setMarker] = useState(config?.wps_marker_enabled ?? true)
  // 地址排序：真实值来自 wps_status（bridge_ready 的 config 里不带这两个字段），
  // 排序开关后端默认开启，先按默认值渲染、拉到状态后被覆盖。
  const [sortEnabled, setSortEnabled] = useState(true)
  const [addressOrder, setAddressOrder] = useState<Record<string, string[]>>({})
  const [orderOpen, setOrderOpen] = useState(false)
  // 编辑中的原始文本：直接回写 addressOrder 会让行尾回车被立刻吞掉，无法换行。
  const [orderDraft, setOrderDraft] = useState<Record<string, string>>({})
  // 本地清单有未保存改动时，不要被刷新回来的远端值覆盖。
  const orderDirty = useRef(false)
  const loaded = useRef(false)

  const refreshRecovery = useCallback(async () => {
    if (!isApiReady()) return
    try {
      setRecovery(await api().wps_recovery_status())
    } catch {
      /* 恢复状态查询失败不打扰用户；真正的处置入口会自己报错 */
    }
  }, [])

  const refresh = useCallback(async () => {
    if (!isApiReady()) return
    setBusy((b) => (b === '' ? 'refresh' : b))
    try {
      const next = await api().wps_status()
      setStatus(next)
      setSortEnabled(next.sort_enabled)
      if (!orderDirty.current) setAddressOrder(next.address_order ?? {})
      await refreshRecovery()
    } catch {
      /* 状态查询失败不打扰用户 */
    } finally {
      setBusy((b) => (b === 'refresh' ? '' : b))
    }
  }, [refreshRecovery])

  useEffect(() => {
    if (loaded.current || !config) return
    loaded.current = true
    setEnabled(config.wps_enabled)
    setTestMode(config.wps_test_mode)
    setCliPath(config.wps_cli_path)
    setMarker(config.wps_marker_enabled)
    void refresh()
  }, [config, refresh])

  /** 保存云同步配置（返回是否成功；现有调用方忽略返回值）。 */
  function save(partial: Record<string, unknown>): Promise<boolean> {
    if (!isApiReady()) return Promise.resolve(false)
    return api()
      .save_wps_config({
        enabled,
        test_mode: testMode,
        cli_path: cliPath,
        drive_id: config?.wps_drive_id ?? '',
        test_file_id: config?.wps_test_file_id ?? '',
        test_drive_id: config?.wps_test_drive_id ?? '',
        marker_enabled: marker,
        sort_enabled: sortEnabled,
        address_order: addressOrder,
        tables: config?.wps_tables ?? {},
        test_tables: config?.wps_test_tables ?? {},
        ...partial,
      })
      .then((result) => {
        orderDirty.current = false
        void refresh()
        return result?.ok !== false
      })
      .catch(() => false)
  }

  /** 保存地址排序：把本地清单（含未保存改动）整份写回配置。 */
  async function onSaveOrder() {
    setMessage('')
    const ok = await save({ sort_enabled: sortEnabled, address_order: addressOrder })
    setMessage(ok ? '地址顺序已保存' : '地址顺序保存失败，详见日志')
  }

  /** 恢复出厂默认顺序：只改本地 state，点「保存顺序」才落盘。 */
  function onRestoreOrderDefaults() {
    const next: Record<string, string[]> = {}
    for (const sheet of ADDRESS_SHEETS) {
      // status 里没返回默认值（旧后端）时退回当前值（= 不变化）；
      // 注意不能用 `|| []` 兜底：医学院的默认值本身就是空数组。
      const fallback = status?.address_order_defaults?.[sheet] ?? addressOrder[sheet] ?? []
      next[sheet] = [...fallback]
    }
    setAddressOrder(next)
    setOrderDraft({})
    orderDirty.current = true
    setMessage('已恢复为出厂默认顺序，点「保存顺序」后写入配置')
  }

  const onPreview = useCallback(async () => {
    if (!isApiReady() || busy) return
    setBusy('preview')
    // 作废旧令牌：预览入口本身会发新令牌，绝不能让人拿旧 id 去上传。
    setPreview(null)
    setMessage('')
    try {
      const result = await api().wps_preview()
      setPreview(result)
      setMessage(result.ok ? '' : result.reason ?? '预览失败')
    } catch (error) {
      setMessage(`预览失败：${String(error)}`)
    } finally {
      setBusy('')
    }
  }, [busy])

  const onUpload = useCallback(async () => {
    if (!isApiReady() || busy || !preview?.preview_id) return
    setBusy('upload')
    setMessage('')
    try {
      // 单飞：整个上传期间按钮保持 disabled（busy 非空），不会重复提交。
      const result = await uploadOnce(preview.preview_id)
      setPreview(result)
      const view = classifyOutcome({
        status: result.status,
        code: result.code,
        ok: result.ok,
        executionSummary: result.execution_summary,
      })
      if (STALE_PREVIEW_CODES.has(result.code ?? '')) {
        setPreview(null) // 令牌已失效，清掉以免用户拿它重试
        setMessage(`【${view.label}】${result.reason ?? '预览已失效'}`
                   + (result.next_action ? ` —— 下一步：${result.next_action}` : ''))
      } else if (result.ok) {
        const rows = result.execution_summary?.rows
        setMessage(
          `上传完成（目标日期 ${result.target_date}）：实际写入 ${rows?.verified ?? '未知'} 行`,
        )
      } else {
        setMessage(
          `【${view.label}】${result.reason ?? '上传失败，详见日志'}` +
            (result.next_action ? ` —— 下一步：${result.next_action}` : ''),
        )
      }
      await Promise.all([refresh(), refreshRecovery(), refreshOperation()])
    } catch (error) {
      setMessage(`上传失败：${String(error)}`)
    } finally {
      setBusy('')
    }
  }, [busy, preview, refresh, refreshRecovery, refreshOperation, uploadOnce])

  const onCheckCopies = useCallback(async () => {
    if (!isApiReady() || busy) return
    setBusy('check')
    setCopyCheck(null)
    try {
      setCopyCheck(await api().wps_check_copies())
    } catch (error) {
      setCopyCheck({ ok: false, reason: String(error) })
    } finally {
      setBusy('')
    }
  }, [busy])

  const onAuthorize = useCallback(async () => {
    if (!isApiReady() || busy) return
    setBusy('auth')
    try {
      const result = await api().wps_authorize()
      setMessage(result.ok ? result.hint ?? '已启动授权' : result.reason ?? '授权启动失败')
    } finally {
      setBusy('')
    }
  }, [busy])

  const planned = preview?.planned_summary
  const executed = preview?.execution_summary
  // 后端统一操作状态：有任何操作在跑就禁用本页的写入类入口。
  // 这只是体验层——后端在同一个协调器上做权威判定，冲突一律立即拒绝。
  const operationBlocked = operationIsActive(operation)
  // 按钮可用条件（含"上一次结果未知就不许直接重传"）由纯函数给出，
  // 与后端校验一一对应；后端仍然是权威判定。
  const uploadGate = canUpload({
    enabled: Boolean(enabled),
    operationActive: operationBlocked,
    previewId: preview?.preview_id,
    previewOk: preview?.ok,
    outcomeKind: preview
      ? classifyOutcome({
          status: preview.status, code: preview.code, ok: preview.ok,
          executionSummary: preview.execution_summary,
        }).kind
      : undefined,
  })
  const outcomeView = preview
    ? classifyOutcome({
        status: preview.status,
        code: preview.code,
        ok: preview.ok,
        executionSummary: preview.execution_summary,
      })
    : null
  // 结果未知时**不允许**直接重试上传：必须先去做恢复处置（否则可能重复累加）；
  // 该判定已并入 uploadGate（canUpload 的 outcomeKind 参数）。

  const pending = planned ? planned.rows.to_update + planned.rows.to_append : 0
  /** 地址清单非空（= 已手工指定顺序）的子表数量；空清单表示按地址升序。 */
  const addressConfigured = ADDRESS_SHEETS.filter(
    (sheet) => (addressOrder[sheet] ?? []).length > 0,
  ).length
  const authText = !status
    ? '状态未知'
    : !status.cli_found
      ? '未找到组件'
      : status.authenticated
        ? '已授权'
        : '未授权'

  return (
    <div>
      <div className="mb-3.5 rounded-md border bg-muted/40 px-3 py-2.5 text-xs leading-relaxed">
        <p className="font-medium text-foreground">把本地排单表同步到 WPS 云端</p>
        <p className="mt-1 text-muted-foreground">
          只写入"日期格 1"和"总餐次"，不改动云端其它内容（公式、排序、颜色都保留）。
          总餐次是<b>增量累加</b>（云端现值 + 本次增量），同一批重复上传不会翻倍；
          协作者写的日期格 0 只读不写。
        </p>
      </div>

      {status?.writing_test_copies ? (
        <div className="mb-3.5 rounded-md border border-emerald-500/50 bg-emerald-500/5 px-3 py-2 text-[11px] leading-relaxed">
          <b>当前写入目标是 6 个测试副本</b>，不会碰你的正式排单表。
          {status.production_tables && Object.keys(status.production_tables).length > 0
            ? '正式表 ID 已备份，需要时可在「更多」里切回。'
            : ''}
        </div>
      ) : null}

      <div className="mb-3.5 flex items-center justify-between gap-3 rounded-md border px-3 py-2.5">
        <div>
          <p className="text-[13px] font-medium">启用云文档同步</p>
          <p className="text-[11px] text-muted-foreground">关闭时本页所有写入操作都会被拒绝</p>
        </div>
        <Switch
          checked={enabled}
          onCheckedChange={(v) => {
            setEnabled(v)
            save({ enabled: v })
          }}
        />
      </div>

      <div
        className={cn(
          'mb-3.5 flex items-center justify-between gap-3 rounded-md border px-3 py-2.5',
          testMode && 'border-amber-500/60 bg-amber-500/5',
        )}
      >
        <div>
          <p className="text-[13px] font-medium">
            测试模式{testMode ? '（已开启）' : ''}
          </p>
          <p className="text-[11px] text-muted-foreground">
            {testMode
              ? '不写协作者通讯记号，适合反复试跑'
              : '写入目标不受此开关影响（当前始终是测试副本），仅通讯记号会一起写'}
          </p>
        </div>
        <Switch
          checked={testMode}
          onCheckedChange={(v) => {
            setTestMode(v)
            save({ test_mode: v })
          }}
        />
      </div>

      <Field label="云同步组件" helper="留空则自动查找内置组件">
        <TextInput
          value={cliPath}
          onChange={(e) => setCliPath(e.target.value)}
          onBlur={() => save({ cli_path: cliPath })}
          placeholder="自动查找"
        />
      </Field>

      <div className="mb-3.5 rounded-md border px-3 py-2.5">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-[13px] font-medium">地址排序{sortEnabled ? '（已开启）' : ''}</p>
            <p className="text-[11px] text-muted-foreground">
              开启后：新增客户时先把订单插到第 3、4 行之间，上好底色，再按下面的顺序把整张表重排
            </p>
          </div>
          <Switch
            checked={sortEnabled}
            onCheckedChange={(v) => {
              setSortEnabled(v)
              save({ sort_enabled: v })
            }}
          />
        </div>

        <button
          type="button"
          className="mt-2 flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
          onClick={() => setOrderOpen((open) => !open)}
        >
          {orderOpen ? (
            <ChevronDown className="size-3.5" />
          ) : (
            <ChevronRight className="size-3.5" />
          )}
          {orderOpen
            ? '收起地址顺序'
            : `展开地址顺序（已指定 ${addressConfigured}/6 张子表）`}
        </button>

        {orderOpen ? (
          <div className="mt-2.5">
            {ADDRESS_SHEETS.map((sheet) => {
              const items = addressOrder[sheet] ?? []
              return (
                <div key={sheet} className="mb-2.5 last:mb-0">
                  <p className="text-[11px] font-medium">
                    {sheet}
                    <span className="ml-1 font-normal text-muted-foreground">
                      （{items.length > 0 ? `${items.length} 个地址` : '留空 = 按地址升序'}）
                    </span>
                  </p>
                  <textarea
                    rows={4}
                    spellCheck={false}
                    placeholder={ADDRESS_PLACEHOLDER}
                    className={addressTextareaClass}
                    value={orderDraft[sheet] ?? items.join('\n')}
                    onChange={(e) => {
                      const raw = e.target.value
                      // 编辑期间保留原始文本（含空行），规整后的结果同步进 state。
                      setOrderDraft((prev) => ({ ...prev, [sheet]: raw }))
                      setAddressOrder((prev) => ({ ...prev, [sheet]: splitAddressLines(raw) }))
                      orderDirty.current = true
                    }}
                    onBlur={() =>
                      // 失焦时丢掉草稿，让输入框回到"已规整"的内容（空行被清掉）。
                      setOrderDraft((prev) => {
                        if (!(sheet in prev)) return prev
                        const next = { ...prev }
                        delete next[sheet]
                        return next
                      })
                    }
                  />
                </div>
              )
            })}
            <div className="mt-2.5 flex flex-wrap gap-2">
              <Button variant="outline" size="sm" onClick={onRestoreOrderDefaults}>
                恢复默认
              </Button>
              <Button size="sm" onClick={onSaveOrder}>
                保存顺序
              </Button>
            </div>
            <p className="mt-1.5 text-[11px] text-muted-foreground">
              列表里没有的地址一律排到最后；「恢复默认」只改这里的内容，点「保存顺序」才写入配置。
            </p>
          </div>
        ) : null}
      </div>

      {status?.tables && status.tables.length > 0 ? (
        <div className="mb-3.5 rounded-md border px-3 py-2.5 text-[11px] text-muted-foreground">
          <p className="mb-1 font-medium text-foreground">写入对应关系</p>
          {status.tables.map((tbl) => (
            <p key={tbl.sheet} className="truncate">
              {tbl.sheet} → {tbl.file_id.slice(0, 10)}…
              {tbl.last_sync ? `（上次同步 ${tbl.last_sync.replace('T', ' ')}）` : ''}
            </p>
          ))}
        </div>
      ) : null}

      <div className="mb-3.5 rounded-md border px-3 py-2.5 text-xs">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <span>
            状态：
            <b className={cn(status?.authenticated ? 'text-emerald-600' : 'text-amber-600')}>
              {authText}
            </b>
          </span>
          <span>
            目标日期：<b>{status?.target_date ?? '—'}</b>
          </span>
          <span>
            通讯记号：<b>{status?.weekday_number ?? '—'}</b>
          </span>
        </div>
        {status?.excel_path ? (
          <p className="mt-1 truncate text-muted-foreground" title={status.excel_path}>
            排单表：{status.excel_path}
          </p>
        ) : (
          <p className="mt-1 text-amber-600">尚未选择排单表，请先到「订单处理」里选好</p>
        )}
        {status && !status.authenticated && status.cli_found ? (
          <p className="mt-1 text-muted-foreground">
            首次使用需要在浏览器里确认一次授权，之后约一年内无需重复授权。
          </p>
        ) : null}
      </div>

      {!testMode ? (
        <div className="mb-3.5 rounded-md border border-destructive/50 bg-destructive/5 px-3 py-2 text-[11px] leading-relaxed text-destructive">
          ⚠ 正式模式：上传会直接修改你的云端排单表。建议先保持测试模式跑通，
          或至少先点「预览」确认要改的内容。
        </div>
      ) : null}

      {operationBlocked ? (
        <div className="mb-3.5 rounded-md border border-amber-500/50 bg-amber-500/5 px-3 py-2 text-[11px] leading-relaxed">
          <b>{conflictNotice(operation)}</b>
          <span className="ml-1">
            —— 云同步的写入类操作会被后端拒绝（冲突立即返回，不排队）。等它结束后再试。
          </span>
        </div>
      ) : null}

      <div className="mb-3.5 flex flex-wrap gap-2">
        <Button variant="outline" size="sm" onClick={refresh} disabled={busy !== ''}>
          {busy === 'refresh' ? '刷新中…' : '刷新状态'}
        </Button>
        <Button variant="outline" size="sm" onClick={onAuthorize}
                disabled={busy !== '' || operationBlocked}>
          {busy === 'auth' ? '授权中…' : '去授权'}
        </Button>
        <Button variant="outline" size="sm" onClick={onCheckCopies}
                disabled={busy !== '' || operationBlocked}>
          {busy === 'check' ? '核对中…' : '检查副本一致性'}
        </Button>
      </div>

      <div className="flex flex-wrap gap-2">
        <Button variant="outline" size="sm" onClick={onPreview}
                disabled={busy !== '' || !enabled || operationBlocked}>
          {busy === 'preview' ? '预览中…' : '预览（只读）'}
        </Button>
        <Button
          size="sm"
          onClick={onUpload}
          disabled={busy !== '' || !preview?.ok || !uploadGate.allowed}
          title={uploadGate.allowed ? undefined : uploadGate.reason}
        >
          {busy === 'upload' ? '上传中…（请勿重复点击）' : '确认上传'}
        </Button>
      </div>

      {preview?.preview_id ? (
        <p className="mt-2 text-[11px] text-muted-foreground">
          预览令牌 <code>{preview.preview_id.slice(0, 12)}…</code>
          {typeof preview.expires_in === 'number' ? `（${preview.expires_in} 秒内有效）` : ''}
          ：预览之后若改了本地表、目标表或排序开关，令牌会自动失效，必须重新预览。
        </p>
      ) : null}

      <RecoveryCard
        status={recovery}
        busy={busy !== ''}
        onResolve={(operation) => setResolveTarget(operation)}
        onRefresh={refreshRecovery}
      />

      {copyCheck ? (
        <div className="mt-3 rounded-md border px-3 py-2 text-[11px] leading-relaxed">
          {!copyCheck.ok ? (
            <p className="text-destructive">核对失败：{copyCheck.reason}</p>
          ) : copyCheck.all_aligned ? (
            <p className="text-emerald-600">
              ✅ 6 张副本与正式表一致（行数与姓名序列都相同），测试结果可代表线上情况
            </p>
          ) : (
            <>
              <p className="text-amber-600">
                ⚠ 有副本已过时：{(copyCheck.drifted ?? []).join('、')}
                —— 正式表被改过，副本还是旧快照，建议重新同步副本
              </p>
              <div className="mt-1 text-muted-foreground">
                {(copyCheck.tables ?? []).map((tbl) => (
                  <p key={tbl.sheet}>
                    {tbl.sheet}：正式 {tbl.production_rows ?? '—'} 人 / 副本 {tbl.rows ?? '—'} 人
                    {tbl.missing && tbl.missing.length > 0
                      ? `，副本缺 ${tbl.missing.join('、')}`
                      : ''}
                    {tbl.extra && tbl.extra.length > 0
                      ? `，副本多 ${tbl.extra.join('、')}`
                      : ''}
                  </p>
                ))}
              </div>
            </>
          )}
        </div>
      ) : null}

      {message ? (
        <p
          className={cn(
            'mt-3 text-xs',
            message.includes('失败') || message.includes('错误')
              ? 'text-destructive'
              : 'text-muted-foreground',
          )}
        >
          {message}
        </p>
      ) : null}

      {outcomeView && preview && !preview.ok ? (
        <p
          className={cn(
            'mt-3 rounded-md border px-3 py-2 text-xs',
            outcomeView.tone === 'danger'
              ? 'border-destructive/50 bg-destructive/5 text-destructive'
              : outcomeView.tone === 'warn'
                ? 'border-amber-500/50 bg-amber-500/5'
                : 'text-muted-foreground',
          )}
        >
          结果：<b>{outcomeView.label}</b>
          {preview.next_action ? ` —— 下一步：${preview.next_action}` : ''}
        </p>
      ) : null}

      {planned ? <PlannedSummaryPanel planned={planned} pending={pending} /> : null}
      {executed ? <ExecutionSummaryPanel executed={executed} /> : null}
      {preview?.tables && preview.tables.length > 0 ? (
        <TableStatusPanel tables={preview.tables} />
      ) : null}

      {preview?.text ? (
        <pre className="mt-2 max-h-64 overflow-auto rounded-md border bg-muted/40 p-2.5 text-[11px] leading-relaxed">
          {preview.text}
        </pre>
      ) : null}

      {resolveTarget ? (
        <RecoveryResolveDialog
          operation={resolveTarget}
          onClose={() => setResolveTarget(null)}
          onDone={async (text) => {
            setResolveTarget(null)
            setMessage(text)
            await refreshRecovery()
          }}
        />
      ) : null}
    </div>
  )
}

// ----------------------------------------------------------------------
// 计划口径 / 执行口径：**分开展示**，禁止用"计划改 100 行"代表"已写入 100 行"
// ----------------------------------------------------------------------

function PlannedSummaryPanel({ planned, pending }: { planned: WpsPlannedSummary; pending: number }) {
  return (
    <div className="mt-3 rounded-md border px-3 py-2 text-xs">
      <p className="font-medium">计划要改动的行数（不是执行结果）</p>
      <p className="mt-1 text-muted-foreground">
        需更新 <b>{planned.rows.to_update}</b> 行，新增 <b>{planned.rows.to_append}</b> 行，
        无需改动 <b>{planned.rows.unchanged}</b> 行
        {planned.rows.skipped > 0 ? `，跳过 ${planned.rows.skipped} 行（日期格被协作者占用）` : ''}
        {planned.rows.blocked > 0 ? `，${planned.rows.blocked} 张表被拒绝（批次日期不符）` : ''}
      </p>
      {pending === 0 ? (
        <p className="mt-1 text-muted-foreground">（云端已经是这个样子，上传不会产生改动）</p>
      ) : null}
    </div>
  )
}

function ExecutionSummaryPanel({ executed }: { executed: WpsExecutionSummary }) {
  const rows = executed.rows
  const show = (value: number | null) => (value === null ? '未知' : String(value))
  return (
    <div className="mt-2 rounded-md border px-3 py-2 text-xs">
      <p className="font-medium">
        实际执行结果
        {executed.proven_no_write ? '（已证明未写入任何内容）' : ''}
      </p>
      <p className="mt-1 text-muted-foreground">
        成功表 <b>{executed.sheets.verified}</b>，无需改动 <b>{executed.sheets.noop}</b>，
        失败 <b>{executed.sheets.failed}</b>
        {executed.sheets.uncertain > 0 ? `，结果不确定 ${executed.sheets.uncertain}` : ''}
        {executed.sheets.blocked > 0 ? `，被拒绝 ${executed.sheets.blocked}` : ''}
        {executed.sheets.skipped > 0 ? `，跳过 ${executed.sheets.skipped}` : ''}
      </p>
      <p className="mt-1 text-muted-foreground">
        实际写入（逐格回读校验通过）<b>{show(rows.verified)}</b> 行
        {executed.rows_unknown ? '（无法证明的行数按"未知"显示，不代表 0）' : ''}
      </p>
      {executed.next_action && executed.next_action !== 'none' ? (
        <p className="mt-1 text-amber-600">下一步：{executed.next_action}</p>
      ) : null}
    </div>
  )
}

function TableStatusPanel({ tables }: { tables: WpsTableDetail[] }) {
  return (
    <div className="mt-2 rounded-md border px-3 py-2 text-[11px]">
      <p className="mb-1 font-medium">每张云表</p>
      {tables.map((table) => (
        <p key={table.sheet} className="text-muted-foreground">
          {table.sheet}：
          {table.blocked_reason ? (
            <span className="text-destructive"> 被拒绝（{table.blocked_reason}）</span>
          ) : (
            <>
              {' '}
              更新 {table.counts.to_update}、新增 {table.counts.to_append}、已完成{' '}
              {table.counts.unchanged}
              {table.counts.skipped > 0 ? `、跳过 ${table.counts.skipped}` : ''}
              {table.warnings.length > 0 ? `、告警 ${table.warnings.length}` : ''}
            </>
          )}
        </p>
      ))}
    </div>
  )
}

// ----------------------------------------------------------------------
// 恢复状态卡片：未完成的写入必须能被看见、被处置
// ----------------------------------------------------------------------

function RecoveryCard({
  status,
  busy,
  onResolve,
  onRefresh,
}: {
  status: WpsRecoveryStatus | null
  busy: boolean
  onResolve: (operation: WpsRecoveryOperation) => void
  onRefresh: () => void
}) {
  if (!status) return null
  if (!status.ok) {
    return (
      <div className="mt-3 rounded-md border border-destructive/50 bg-destructive/5 px-3 py-2 text-[11px] leading-relaxed text-destructive">
        <p className="font-medium">云同步恢复状态不可读</p>
        <p className="mt-1">{status.reason ?? status.error_code}</p>
        <p className="mt-1">
          在修好本地日志之前**请不要上传**（无法证明上一次写入到哪一步）。
        </p>
      </div>
    )
  }
  const operations = status.operations ?? []
  if (operations.length === 0) return null
  const pendingOps = operations.filter((op) => op.pending)
  const guardedOps = operations.filter((op) => !op.pending)
  return (
    <div
      className={cn(
        'mt-3 rounded-md border px-3 py-2 text-[11px] leading-relaxed',
        pendingOps.length > 0
          ? 'border-destructive/50 bg-destructive/5'
          : 'border-amber-500/50 bg-amber-500/5',
      )}
    >
      <p className="font-medium">
        有未完成的云同步任务（{pendingOps.length > 0 ? '待处理' : '已退出但仍有防重复闸门'}）
      </p>
      <p className="mt-1">
        {pendingOps.length > 0
          ? '上一次写入的结果没能确认，程序不会自动补写云端。请先只读核对云端，再选择处置方式。'
          : '这些旧任务已退出待处理队列，但同一日期 + 同一云表仍被防重复闸门阻断，必须靠实际云端核对才能解除。'}
      </p>
      {operations.map((op) => (
        <div key={op.operation_id} className="mt-2 border-t pt-2">
          <p>
            {op.operation_id} · {op.status} · 目标日期 {op.target_date || '—'}
            {op.retired_guarded ? ' · 已退出（保留闸门）' : ''}
          </p>
          {op.sheets.map((sheet) => (
            <p key={sheet.sheet} className="text-muted-foreground">
              {sheet.sheet}：{sheet.display_status}
              {sheet.people_count > 0 ? `（${sheet.people_count} 行）` : ''}
              {sheet.reason ? ` —— ${sheet.reason}` : ''}
            </p>
          ))}
          <div className="mt-1.5 flex flex-wrap gap-2">
            <Button variant="outline" size="sm" onClick={onRefresh} disabled={busy}>
              刷新状态
            </Button>
            <Button size="sm" onClick={() => onResolve(op)} disabled={busy}>
              处置这个任务
            </Button>
          </div>
        </div>
      ))}
      {guardedOps.length > 0 ? (
        <p className="mt-2 text-muted-foreground">
          提示：退出（保留闸门）不等于"允许重传"。要真正解除，必须用 cloud_verified /
          cloud_untouched 拿到云端只读证据。
        </p>
      ) : null}
    </div>
  )
}

const DECISION_LABELS: Record<WpsRecoveryDecision, string> = {
  cloud_verified: '云端已完整写入（会重新只读核对）',
  cloud_untouched: '云端完全没动过（会重新只读核对）',
  retire_guarded: '退出待处理，但保留防重复闸门',
  keep: '保持阻断（只记备注）',
}

function RecoveryResolveDialog({
  operation,
  onClose,
  onDone,
}: {
  operation: WpsRecoveryOperation
  onClose: () => void
  onDone: (message: string) => void
}) {
  const [decision, setDecision] = useState<WpsRecoveryDecision>('keep')
  const [note, setNote] = useState('')
  const [structureChecked, setStructureChecked] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const needsNote = decision !== 'keep'
  const canSubmit =
    !busy &&
    note.trim().length >= (needsNote ? 4 : 0) &&
    (decision !== 'retire_guarded' || structureChecked)

  async function submit() {
    if (!isApiReady() || !canSubmit) return
    setBusy(true)
    setError('')
    try {
      const result = await api().wps_recovery_resolve({
        operation_id: operation.operation_id,
        decision,
        confirm: decision,
        note: note.trim(),
        confirm_structure_checked: structureChecked,
      })
      if (result.ok) {
        onDone(
          result.reason ||
            `${DECISION_LABELS[decision]}：已记录（本地审计，未写云端）`,
        )
      } else {
        setError(result.reason ?? '处置被拒绝')
      }
    } catch (err) {
      setError(String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-lg rounded-md border bg-card p-4 text-xs shadow-lg">
        <p className="text-[13px] font-medium">处置未完成的云同步任务</p>
        <p className="mt-1 text-muted-foreground">
          {operation.operation_id} · 目标日期 {operation.target_date || '—'}
        </p>
        <p className="mt-1 text-muted-foreground">
          这个入口<b>不会写云端</b>：它只重新只读核对并把结论写进本地审计。
        </p>

        <div className="mt-3 space-y-1.5">
          {(Object.keys(DECISION_LABELS) as WpsRecoveryDecision[]).map((value) => (
            <label key={value} className="flex items-start gap-2">
              <input
                type="radio"
                className="mt-0.5"
                name="wps-recovery-decision"
                checked={decision === value}
                onChange={() => setDecision(value)}
              />
              <span>{DECISION_LABELS[value]}</span>
            </label>
          ))}
        </div>

        {decision === 'retire_guarded' ? (
          <label className="mt-2 flex items-start gap-2">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={structureChecked}
              onChange={(e) => setStructureChecked(e.target.checked)}
            />
            <span>我已人工核对过云端表结构（退出会保留同日期同表的防重复闸门）</span>
          </label>
        ) : null}

        <label className="mt-3 block">
          <span className="text-muted-foreground">
            人工备注{needsNote ? '（至少 4 个字符）' : '（可选）'}
          </span>
          <textarea
            rows={3}
            className={addressTextareaClass}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="写下你凭什么这样判定，例如：已逐格核对云端，金额与行数一致"
          />
        </label>

        {decision === 'cloud_verified' || decision === 'cloud_untouched' ? (
          <p className="mt-1 text-muted-foreground">
            这两个动作会重新只读云端逐格核对；核对不通过会保持阻断，不会因为勾选就放行。
          </p>
        ) : null}
        {error ? <p className="mt-2 text-destructive">{error}</p> : null}

        <div className="mt-3 flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button size="sm" onClick={submit} disabled={!canSubmit}>
            {busy ? '处置中…' : '确认处置'}
          </Button>
        </div>
      </div>
    </div>
  )
}
