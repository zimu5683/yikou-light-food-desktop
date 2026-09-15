/**
 * 云文档同步页签：把本地排单表的内容增量写入 WPS 云端排单表。
 *
 * 设计（详见 design/WPS-CLOUD-SYNC-PLAN.md）：
 * - 默认开启「测试模式」，只写测试文件，绝不碰正式排单表；
 * - 写入前必须先点「预览」，看清"会改谁、改成几"再确认上传；
 * - 总餐次写的是本地餐次的**绝对值**，因此重复上传不会翻倍；
 * - 「地址排序」按子表维护列B 的顺序：新增客户先插到第 3、4 行之间再整表重排；
 * - 任何失败都只记日志，不影响本地排单任务。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { Field, TextInput } from '@/components/fields'
import { useApp } from '@/hooks/appContext'
import { api, isApiReady, type WpsCopyCheck, type WpsResult, type WpsStatus } from '@/lib/bridge'
import { cn } from '@/lib/utils'

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

/** 多行文本 → 顺序数组：按行拆分、去首尾空白、丢掉空行。 */
function splitAddressLines(raw: string): string[] {
  return raw
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
}

/** 地址清单输入框：样式与 TextInput 一致（底色 secondary，聚焦转卡片色）。 */
const addressTextareaClass = cn(
  'mt-1 w-full resize-y rounded-[4px] border border-transparent bg-secondary px-2.5 py-1.5',
  'font-mono text-xs leading-relaxed transition-colors outline-none',
  'placeholder:text-ink-faint focus-visible:border-ring focus-visible:bg-card',
  'focus-visible:ring-[3px] focus-visible:ring-ring/50',
)

export function CloudForm() {
  const { config } = useApp()
  const [status, setStatus] = useState<WpsStatus | null>(null)
  const [preview, setPreview] = useState<WpsResult | null>(null)
  const [busy, setBusy] = useState<'' | 'preview' | 'upload' | 'auth' | 'refresh' | 'check'>('')
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

  const refresh = useCallback(async () => {
    if (!isApiReady()) return
    setBusy((b) => (b === '' ? 'refresh' : b))
    try {
      const next = await api().wps_status()
      setStatus(next)
      setSortEnabled(next.sort_enabled)
      if (!orderDirty.current) setAddressOrder(next.address_order ?? {})
    } catch {
      /* 状态查询失败不打扰用户 */
    } finally {
      setBusy((b) => (b === 'refresh' ? '' : b))
    }
  }, [])

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
    if (!isApiReady() || busy) return
    setBusy('upload')
    setMessage('')
    try {
      const result = await api().wps_upload()
      setPreview(result)
      setMessage(
        result.ok
          ? `上传完成（目标日期 ${result.target_date}）`
          : result.reason ?? '上传失败，详见日志',
      )
      await refresh()
    } catch (error) {
      setMessage(`上传失败：${String(error)}`)
    } finally {
      setBusy('')
    }
  }, [busy, refresh])

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

  const summary = preview?.summary
  const pending = summary ? summary.to_update + summary.to_append : 0
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
          总餐次取本地表的绝对值，重复上传不会翻倍。
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

      <div className="mb-3.5 flex flex-wrap gap-2">
        <Button variant="outline" size="sm" onClick={refresh} disabled={busy !== ''}>
          {busy === 'refresh' ? '刷新中…' : '刷新状态'}
        </Button>
        <Button variant="outline" size="sm" onClick={onAuthorize} disabled={busy !== ''}>
          {busy === 'auth' ? '授权中…' : '去授权'}
        </Button>
        <Button variant="outline" size="sm" onClick={onCheckCopies} disabled={busy !== ''}>
          {busy === 'check' ? '核对中…' : '检查副本一致性'}
        </Button>
      </div>

      <div className="flex flex-wrap gap-2">
        <Button variant="outline" size="sm" onClick={onPreview} disabled={busy !== '' || !enabled}>
          {busy === 'preview' ? '预览中…' : '预览（只读）'}
        </Button>
        <Button
          size="sm"
          onClick={onUpload}
          disabled={busy !== '' || !enabled || !preview?.ok}
          title={preview?.ok ? undefined : '请先预览'}
        >
          {busy === 'upload' ? '上传中…' : '确认上传'}
        </Button>
      </div>

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

      {summary ? (
        <p className="mt-2 text-xs text-muted-foreground">
          本次：需更新 <b>{summary.to_update}</b> 人，新增 <b>{summary.to_append}</b> 人，
          已完成 <b>{summary.unchanged}</b> 人
          {summary.warned > 0 ? `，注意 ${summary.warned} 项` : ''}
          {pending === 0 ? '（云端已经是这个样子，上传不会产生改动）' : ''}
        </p>
      ) : null}

      {preview?.text ? (
        <pre className="mt-2 max-h-64 overflow-auto rounded-md border bg-muted/40 p-2.5 text-[11px] leading-relaxed">
          {preview.text}
        </pre>
      ) : null}
    </div>
  )
}
