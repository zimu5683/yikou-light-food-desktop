/**
 * 任务面板：模式切换（下划线 tab）+ 订单处理 / 闪时送下单 表单 + 主操作条 + 更多菜单。
 * 校验结果由桥接层返回（start_order/start_sss 的 fields），前端渲染字段错误态。
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { MoreHorizontal } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { CloudForm } from '@/components/CloudForm'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Switch } from '@/components/ui/switch'
import { DateField, Field, GhostButton, Stepper, TextInput } from '@/components/fields'
import { useApp, type FieldErrors, type TaskMode } from '@/hooks/appContext'
import {
  api,
  classifyOutcome,
  isApiReady,
  type OrderFormPayload,
  type SssFormPayload,
  type SssUncertainDecision,
  type SssUncertainRecordView,
  type SssUncertainResolveResult,
  type SssUncertainReview,
  type SssUncertainState,
} from '@/lib/bridge'
import { cn } from '@/lib/utils'
import { canResolve as canResolveDecision, singleFlight } from '@/lib/interaction'
import { modeError } from '@/lib/format'

export function TaskPanel() {
  const { mode, setMode, workerAlive, config } = useApp()
  const formKey = config ? 'ready' : 'loading'
  return (
    <section className="flex min-h-0 flex-1 flex-col border-border lg:border-r">
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-5 pb-3 pt-4">
        <h1 className="font-serif text-lg font-semibold tracking-[1px]">任务配置</h1>
        <p className="mb-3.5 mt-0.5 text-xs text-muted-foreground">
          选择任务类型，准备好资料后启动。
        </p>

        <div role="tablist" className="mb-4 flex gap-[18px] border-b">
          <ModeTab active={mode === 'order'} onClick={() => setMode('order')}>
            订单处理
          </ModeTab>
          <ModeTab active={mode === 'cloud'} onClick={() => setMode('cloud')}>
            云文档同步
          </ModeTab>
          <ModeTab active={mode === 'sss'} onClick={() => setMode('sss')}>
            闪时送下单
          </ModeTab>
        </div>

        {/* 两个表单常驻渲染（仅切换可见性）：卸载会清空各字段的 useState，
            导致切页签后已输入内容丢失并被旧 config 重新填充。 */}
        <div className={cn(mode === 'order' ? 'block' : 'hidden')}>
          <OrderForm key={formKey} />
        </div>
        <div className={cn(mode === 'cloud' ? 'block' : 'hidden')}>
          <CloudForm key={formKey} />
        </div>
        <div className={cn(mode === 'sss' ? 'block' : 'hidden')}>
          <SssForm key={formKey} />
        </div>
      </div>
      {workerAlive && (
        <p className="border-t px-5 py-1.5 text-[11px] text-muted-foreground">
          任务运行中，开始与表单暂不可用。
        </p>
      )}
    </section>
  )
}

function ModeTab({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        'relative pb-2 pt-1.5 text-[13.5px] font-medium transition-colors',
        active ? 'font-semibold text-foreground' : 'text-muted-foreground hover:text-foreground',
        active &&
          "after:absolute after:inset-x-0 after:-bottom-px after:h-0.5 after:bg-primary after:content-['']",
      )}
    >
      {children}
    </button>
  )
}

/* ------------------------------------------------------------------ */
/* 订单处理                                                             */
/* ------------------------------------------------------------------ */

function OrderForm() {
  const { config, passwords, startOrder, workerAlive } = useApp()
  const [url, setUrl] = useState(config?.target_url ?? '')
  const [phone, setPhone] = useState(config?.phone_number ?? '')
  const [password, setPassword] = useState(passwords.order ?? '')
  const [excel, setExcel] = useState(config?.excel_path ?? '')
  const [date, setDate] = useState(config?.order_date ?? '')
  const [count, setCount] = useState<number | null>(config?.order_count ?? null)
  const [remember, setRemember] = useState(true)
  const [apiMode, setApiMode] = useState(config?.api_mode ?? true)
  const [fields, setFields] = useState<FieldErrors | null>(null)
  const [busy, setBusy] = useState(false)

  // 字段停止变化后自动落盘（切页签/退出重进都从后端还原，配置不丢失）。
  const scheduleSave = useDebouncedSave(() => {
    if (!isApiReady()) return
    api()
      .save_order_config({ url, phone, excel, date, count, api_mode: apiMode })
      .catch(() => {})
  })
  const firstSave = useRef(true)
  useEffect(() => {
    if (firstSave.current) {
      firstSave.current = false
      return
    }
    scheduleSave()
  }, [url, phone, excel, date, count, apiMode, scheduleSave])

  const excelError = modeError(fields, 'excel')
  const excelOk = !excelError && excel && !fields ? '文件已准备' : undefined

  async function onStart() {
    if (busy) return
    setBusy(true)
    setFields(null)
    try {
      const payload: OrderFormPayload = { url, phone, password, excel, date, count: count === null ? '' : String(count), remember, api_mode: apiMode }
      const errors = await startOrder(payload)
      if (errors) setFields(errors)
    } finally {
      setBusy(false)
    }
  }

  async function chooseFile() {
    if (!isApiReady()) return
    const result = await api().choose_excel('order')
    if (result.path) setExcel(result.path)
    if (result.error) setFields((prev) => ({ ...prev, excel: { message: result.error } }))
  }

  async function newTemplate() {
    if (!isApiReady()) return
    const result = await api().new_template('order')
    if (result.path) setExcel(result.path)
    if (result.error) setFields((prev) => ({ ...prev, excel: { message: result.error } }))
  }

  return (
    <div>
      <Field label="管理网址" htmlFor="order-url" error={modeError(fields, 'url')} helper="用于登录管理后台">
        <TextInput
          id="order-url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://example.com/admin"
        />
      </Field>

      <Field label="手机号 / 账号" htmlFor="order-phone" error={modeError(fields, 'phone')} helper="用于登录管理后台">
        <TextInput
          id="order-phone"
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
        />
      </Field>

      <Field label="登录密码" htmlFor="order-password" error={modeError(fields, 'password')} helper="密码仅保存在系统凭据管理器中">
        <TextInput
          id="order-password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </Field>

      <Field label="Excel 文件" htmlFor="order-excel" error={excelError} okMessage={excelOk} helper="支持 .xlsx / .xlsm">
        <div className="flex gap-1.5">
          <TextInput
            id="order-excel"
            value={excel}
            onChange={(e) => setExcel(e.target.value)}
            state={excel && !excelError ? 'valid' : undefined}
            className="min-w-0 flex-1"
            placeholder="选择排单 .xlsx 文件"
          />
          <GhostButton onClick={chooseFile}>选择文件</GhostButton>
          <GhostButton onClick={newTemplate}>新建模板</GhostButton>
        </div>
      </Field>

      <Field label="目标日期" error={modeError(fields, 'date')} helper="留空默认今天；只允许选择今天或过去日期">
        <DateField value={date} onChange={setDate} invalid={Boolean(modeError(fields, 'date'))} />
      </Field>

      <Field label="待处理订单数" error={modeError(fields, 'count')}>
        <Stepper value={count} onChange={setCount} invalid={Boolean(modeError(fields, 'count'))} />
        <p className="mt-1 text-[11px] text-muted-foreground">留空=全部订单</p>
      </Field>

      <div className="mb-4 mt-1 flex items-center gap-2 text-[12.5px] text-muted-foreground">
        <Switch checked={remember} onCheckedChange={setRemember} aria-label="保存到系统凭据管理器" />
        <span>保存到系统凭据管理器</span>
      </div>

      <div className="mb-4 mt-1 flex items-center gap-2 text-[12.5px] text-muted-foreground">
        <Switch checked={apiMode} onCheckedChange={setApiMode} aria-label="纯接口模式（不启动浏览器）" />
        <span>纯接口模式（不启动浏览器）</span>
      </div>

      <BottomDock>
        <ActionBar
          startLabel="开始处理"
          onStart={onStart}
          startBusy={busy}
          startDisabled={workerAlive}
        />
        <ToolsMenu mode="order" />
      </BottomDock>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 闪时送下单                                                           */
/* ------------------------------------------------------------------ */

function SssForm() {
  const { config, passwords, startSss, workerAlive, operation, refreshOperation } = useApp()
  const [url, setUrl] = useState(config?.sss_url ?? '')
  const [account, setAccount] = useState(config?.sss_account ?? '')
  const [password, setPassword] = useState(passwords.sss ?? '')
  const [excel, setExcel] = useState(config?.sss_excel_path ?? '')
  const [orderSource, setOrderSource] = useState<'wps' | 'excel'>(config?.sss_order_source ?? 'wps')
  const [productName, setProductName] = useState(config?.sss_product_name ?? '轻食')
  // 固定地址配置当前不在界面中编辑，直接由 config 派生，避免未使用 setter。
  const commonAddress = config?.sss_common_address ?? ''
  const useFixedAddress = config?.sss_use_fixed_address ?? true
  const fixedLnt = String(config?.sss_fixed_lnt ?? '119.728224')
  const fixedLat = String(config?.sss_fixed_lat ?? '30.256632')
  const fixedAreaCode = config?.sss_fixed_area_code ?? '330110'
  const fixedAddressDetail = config?.sss_fixed_address_detail ?? '浙江农林大学东湖校区'
  const [remember, setRemember] = useState(true)
  const [dryRun, setDryRun] = useState(config?.sss_dry_run ?? true)
  const [preflight, setPreflight] = useState(config?.sss_preflight ?? false)
  const [apiMode, setApiMode] = useState(config?.api_mode ?? true)
  const [fields, setFields] = useState<FieldErrors | null>(null)
  const [busy, setBusy] = useState(false)
  const [dayBusy, setDayBusy] = useState(false)

  // 字段停止变化后自动落盘（切页签/退出重进都从后端还原，配置不丢失）。
  const scheduleSave = useDebouncedSave(() => {
    if (!isApiReady()) return
    api()
      .save_sss_config({
        url,
        account,
        excel,
        order_source: orderSource,
        product_name: productName,
        common_address: commonAddress,
        use_fixed_address: useFixedAddress,
        fixed_lnt: fixedLnt,
        fixed_lat: fixedLat,
        fixed_area_code: fixedAreaCode,
        fixed_address_detail: fixedAddressDetail,
        dry_run: dryRun,
        preflight,
        api_mode: apiMode,
      })
      .catch(() => {})
  })
  const firstSave = useRef(true)
  useEffect(() => {
    if (firstSave.current) {
      firstSave.current = false
      return
    }
    scheduleSave()
  }, [
    url, account, excel, productName, commonAddress, useFixedAddress,
    fixedLnt, fixedLat, fixedAreaCode, fixedAddressDetail, dryRun, preflight, apiMode,
    orderSource,
    scheduleSave,
  ])

  const excelError = modeError(fields, 'excel')
  const excelOk = !excelError && excel && !fields ? '文件已准备' : undefined

  async function onStart() {
    if (busy) return
    setBusy(true)
    setFields(null)
    try {
      const payload: SssFormPayload = {
        url,
        account,
        password,
        excel,
        order_source: orderSource,
        product_name: productName,
        common_address: commonAddress,
        use_fixed_address: useFixedAddress,
        fixed_lnt: fixedLnt,
        fixed_lat: fixedLat,
        fixed_area_code: fixedAreaCode,
        fixed_address_detail: fixedAddressDetail,
        remember,
        dry_run: dryRun,
        preflight,
        api_mode: apiMode,
      }
      const errors = await startSss(payload)
      if (errors) setFields(errors)
    } finally {
      setBusy(false)
    }
  }

  async function chooseFile() {
    if (!isApiReady()) return
    const result = await api().choose_excel('sss')
    if (result.path) setExcel(result.path)
    if (result.error) setFields((prev) => ({ ...prev, excel: { message: result.error } }))
  }

  async function newTemplate() {
    if (!isApiReady()) return
    const result = await api().new_template('sss')
    if (result.path) setExcel(result.path)
    if (result.error) setFields((prev) => ({ ...prev, excel: { message: result.error } }))
  }

  /** 读取云端当天名单（东湖午餐/东湖晚餐）并留档，不下单。 */
  async function readDayOrders() {
    if (dayBusy) return
    setDayBusy(true)
    try {
      const result = await api().sss_day_orders()
      if (!result.ok) {
        toast.error(result.reason ?? '读取云端当天名单失败', { duration: 8000 })
        return
      }
      const parts = Object.entries(result.meals ?? {}).map(([name, info]) =>
        info.skipped
          ? `${name}不下单（${info.reason || '没有当天列'}）`
          : `${name} ${info.orders} 人（标 1 共 ${info.marked}，大西/小 ${info.skipped_address} 人不送）`,
      )
      toast.success(
        `云端当天名单 ${result.target_date ?? ''} ${result.date_text ?? ''}：${parts.join('；') || '没有数据'}`,
        { duration: 8000 },
      )
      if (result.archive_error) {
        toast.error(`留档 Excel 写入失败：${result.archive_error}`, { duration: 8000 })
      }
    } catch (error) {
      toast.error(`读取失败：${String(error)}`)
    } finally {
      setDayBusy(false)
    }
  }

  return (
    <div>
      <Field label="闪时送网址" htmlFor="sss-url" error={modeError(fields, 'url')} helper="闪时送下单平台地址">
        <TextInput id="sss-url" value={url} onChange={(e) => setUrl(e.target.value)} />
      </Field>

      <Field label="闪时送账号" htmlFor="sss-account" error={modeError(fields, 'account')} helper="用于登录闪时送平台">
        <TextInput id="sss-account" value={account} onChange={(e) => setAccount(e.target.value)} />
      </Field>

      <Field label="登录密码" htmlFor="sss-password" error={modeError(fields, 'password')} helper="密码仅保存在系统凭据管理器中">
        <TextInput
          id="sss-password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </Field>

      <Field
        label="名单来源"
        helper={
          orderSource === 'wps'
            ? '每次下单前读取东湖午餐/东湖晚餐「当天列标 1」的人；地址是大西/小的不下单'
            : '读取《闪时送.xlsx》里的名单（人工准备），不做云端读取'
        }
      >
        <div className="flex gap-1.5" role="group" aria-label="名单来源">
          <SourceButton
            active={orderSource === 'wps'}
            onClick={() => setOrderSource('wps')}
          >
            云端当天名单
          </SourceButton>
          <SourceButton
            active={orderSource === 'excel'}
            onClick={() => setOrderSource('excel')}
          >
            本地 Excel
          </SourceButton>
        </div>
      </Field>

      <Field
        label="订单 Excel 文件"
        htmlFor="sss-excel"
        error={excelError}
        okMessage={excelOk}
        helper={
          orderSource === 'wps'
            ? '云端模式：作为当天名单的留档文件，可留空'
            : '午餐/晚餐两表，A=姓名 B=门牌号 C=电话'
        }
      >
        <div className="flex gap-1.5">
          <TextInput
            id="sss-excel"
            value={excel}
            onChange={(e) => setExcel(e.target.value)}
            state={excel && !excelError ? 'valid' : undefined}
            className="min-w-0 flex-1"
            placeholder="选择闪时送 .xlsx 文件"
          />
          <GhostButton onClick={chooseFile}>选择文件</GhostButton>
          <GhostButton onClick={newTemplate}>新建模板</GhostButton>
        </div>
      </Field>

      {orderSource === 'wps' && (
        <div className="mb-4 -mt-1 flex items-center gap-2">
          <GhostButton onClick={readDayOrders} disabled={dayBusy || workerAlive}>
            {dayBusy ? '读取中…' : '读取云端当天名单'}
          </GhostButton>
          <span className="text-[11px] text-muted-foreground">
            只读取并写留档，不下单
          </span>
        </div>
      )}

      <UncertainPanel
        password={password}
        workerAlive={workerAlive}
        operationBlocked={Boolean(operation?.active)}
        conflictText={
          operation?.active
            ? `当前正在执行：${operation.operation?.title ?? ''}`
            : ''
        }
        onOperationChanged={refreshOperation}
      />

      <Field label="商品名称" htmlFor="sss-product" helper="下单时商品“名称”的默认值">
        <TextInput
          id="sss-product"
          value={productName}
          onChange={(e) => setProductName(e.target.value)}
        />
      </Field>

      <div className="mb-4 mt-1 flex items-center gap-2 text-[12.5px] text-muted-foreground">
        <Switch checked={remember} onCheckedChange={setRemember} aria-label="保存到系统凭据管理器" />
        <span>保存到系统凭据管理器</span>
      </div>

      <div className="mb-4 mt-1 flex items-center gap-2 text-[12.5px] text-muted-foreground">
        <Switch checked={dryRun} onCheckedChange={setDryRun} aria-label="干跑：只预览报文，不创建订单" />
        <span>干跑：只预览报文，不创建订单</span>
      </div>

      <div className="mb-4 mt-1 flex items-center gap-2 text-[12.5px] text-muted-foreground">
        <Switch
          checked={preflight}
          onCheckedChange={setPreflight}
          aria-label="预检：登录并检查，不创建订单"
        />
        <span>预检：登录并检查余额/订单，不创建订单</span>
      </div>

      <div className="mb-4 mt-1 flex items-center gap-2 text-[12.5px] text-muted-foreground">
        <Switch checked={apiMode} onCheckedChange={setApiMode} aria-label="纯接口模式（不启动浏览器）" />
        <span>纯接口模式（不启动浏览器）</span>
      </div>

      <BottomDock>
        <ActionBar
          startLabel="开始下单"
          onStart={onStart}
          startBusy={busy}
          startDisabled={workerAlive}
        />
        <ToolsMenu mode="sss" />
      </BottomDock>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 共享：主操作条 + 工具菜单                                              */
/* ------------------------------------------------------------------ */

/** 名单来源分段按钮（云端当天名单 / 本地 Excel） */
function SourceButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        'h-[34px] flex-1 rounded-[4px] border px-3 text-xs transition-colors',
        active
          ? 'border-primary bg-secondary font-medium text-primary-strong'
          : 'border-border bg-card text-muted-foreground hover:border-primary hover:text-primary-strong',
      )}
    >
      {children}
    </button>
  )
}

/** 表单底部停靠坞：更多菜单 + 主操作条整体吸底 */
function BottomDock({ children }: { children: ReactNode }) {
  return (
    <div className="sticky bottom-0 -mx-5 bg-background px-5 pb-3 pt-2">
      {children}
    </div>
  )
}

function ActionBar({
  startLabel,
  onStart,
  startBusy,
  startDisabled,
}: {
  startLabel: string
  onStart: () => void
  startBusy: boolean
  startDisabled: boolean
}) {
  const { stopTask, workerAlive } = useApp()
  const [confirming, setConfirming] = useState(false)

  return (
    <>
      <div className="flex gap-2 border-t pt-3.5">
        <Button
          className="btn-serif-primary h-[38px] flex-1 rounded-[6px] text-sm"
          disabled={startDisabled || startBusy}
          onClick={onStart}
        >
          {startBusy ? '校验中…' : startLabel}
        </Button>
        <Button
          variant="outline"
          className="h-[38px] w-24 rounded-[6px] border-destructive/45 bg-card text-[13px] text-destructive hover:bg-destructive/5 hover:text-destructive"
          disabled={!workerAlive}
          onClick={() => setConfirming(true)}
        >
          停止
        </Button>
      </div>
      <ConfirmStopDialog open={confirming} onOpenChange={setConfirming} onConfirm={stopTask} />
    </>
  )
}

function ToolsMenu({ mode }: { mode: TaskMode }) {
  const { checkBrowser, clearPassword, checkUpdates } = useApp()
  const [confirmClear, setConfirmClear] = useState(false)

  return (
    <>
      <nav className="mt-3 flex items-center text-xs text-muted-foreground">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              className="flex items-center gap-1 rounded px-2 py-1 hover:bg-secondary hover:text-foreground"
              aria-label="更多工具"
            >
              <MoreHorizontal className="size-4" />
              更多
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="rounded-md text-xs">
            <DropdownMenuItem onClick={checkBrowser}>检查浏览器</DropdownMenuItem>
            <DropdownMenuItem onClick={() => setConfirmClear(true)}>清除密码</DropdownMenuItem>
            <DropdownMenuItem onClick={() => checkUpdates(true)}>检查更新</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </nav>
      <ConfirmClearPassword
        open={confirmClear}
        onOpenChange={setConfirmClear}
        onConfirm={() => clearPassword(mode === 'sss' ? 'sss' : 'order')}
      />
    </>
  )
}

/* 停止确认（对齐旧版 askyesnocancel 语义：是=停止 / 否=继续 / 取消=返回） */
function ConfirmStopDialog({
  open,
  onOpenChange,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onConfirm: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-sm rounded-lg">
        <DialogHeader>
          <DialogTitle className="font-serif">暂停处理</DialogTitle>
          <DialogDescription>是否停止当前任务？停止后需等待浏览器操作结束。</DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2">
          <Button variant="ghost" className="h-8 text-xs" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            variant="outline"
            className="h-8 rounded-[6px] border-border bg-card text-xs text-foreground hover:bg-secondary"
            onClick={() => {
              onOpenChange(false)
            }}
          >
            继续处理
          </Button>
          <Button
            className="h-8 rounded-[6px] bg-destructive text-xs text-white hover:bg-destructive/90"
            onClick={() => {
              onOpenChange(false)
              onConfirm()
            }}
          >
            停止任务
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ConfirmClearPassword({
  open,
  onOpenChange,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onConfirm: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-sm rounded-lg">
        <DialogHeader>
          <DialogTitle className="font-serif">清除密码</DialogTitle>
          <DialogDescription>
            将从系统凭据管理器删除本机保存的密码，输入框也会清空。继续吗？
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2">
          <Button variant="ghost" className="h-8 text-xs" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            className="h-8 rounded-[6px] text-xs"
            onClick={() => {
              onOpenChange(false)
              onConfirm()
            }}
          >
            清除
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ------------------------------------------------------------------ */
/* 防抖自动保存：字段停止变化 delay ms 后把表单值持久化到后端配置。          */
/* 只在 isApiReady（真实桌面端）且已从 config 完成首次填充后触发，避免     */
/* 初始化瞬间把默认值写回配置，也避免浏览器 mock 态空跑。                  */
/* ------------------------------------------------------------------ */
function useDebouncedSave(save: () => void, delay = 500): () => void {
  const timer = useRef<number | undefined>(undefined)
  const saveRef = useRef(save)
  useEffect(() => {
    saveRef.current = save
  }, [save])
  const cancel = useCallback(() => {
    if (timer.current !== undefined) {
      clearTimeout(timer.current)
      timer.current = undefined
    }
  }, [])
  const trigger = useCallback(() => {
    cancel()
    timer.current = window.setTimeout(() => {
      timer.current = undefined
      saveRef.current()
    }, delay)
  }, [cancel, delay])
  useEffect(() => cancel, [cancel])
  return trigger
}


/* ------------------------------------------------------------------ */
/* 未决记录：只读核对 + 人工处置                                          */
/*                                                                    */
/* 闪时送接口没有客户端幂等键，"POST 已发出但结果未知"时重复提交会真的      */
/* 多下一单。所以：                                                     */
/*  * 只要还有 inflight/unresolved 记录，程序就拒绝再发下单请求；          */
/*  * 这一块是唯一的出路 —— 只读核对（不下单）+ 人工处置（不发送创建订单请求）。*/
/* ------------------------------------------------------------------ */

const CLASSIFICATION_LABELS: Record<string, string> = {
  station_confirmed: '站内已找到对应订单',
  station_missing: '站内确认没有这一单',
  station_found_other_day: '找到相似订单但日期不同',
  scan_failed: '读取失败，无法判定',
}

function UncertainPanel({
  password,
  workerAlive,
  operationBlocked,
  conflictText,
  onOperationChanged,
}: {
  password: string
  workerAlive: boolean
  operationBlocked: boolean
  conflictText: string
  onOperationChanged: () => Promise<void> | void
}) {
  const [state, setState] = useState<SssUncertainState | null>(null)
  const [review, setReview] = useState<SssUncertainReview | null>(null)
  const [busy, setBusy] = useState<'' | 'load' | 'review' | 'resolve'>('')
  const [message, setMessage] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [decision, setDecision] = useState<SssUncertainDecision>('keep')
  const [note, setNote] = useState('')
  // 处置用单飞包装：双击只发出一次（后端另有权威互斥与锁内校验）。
  const resolveOnce = useRef(
    singleFlight(async (payload: {
      decision: SssUncertainDecision
      confirm: string
      note: string
      record_ids: string[]
    }) => api().sss_uncertain_resolve(payload)),
  ).current

  const load = useCallback(async () => {
    if (!isApiReady()) return
    setBusy((b) => (b === '' ? 'load' : b))
    try {
      const next = await api().sss_uncertain_records()
      setState(next)
      if (!next.ok) setMessage(next.reason ?? '未决记录不可读')
    } catch (error) {
      setMessage(`读取未决记录失败：${String(error)}`)
    } finally {
      setBusy((b) => (b === 'load' ? '' : b))
    }
  }, [])

  useEffect(() => {
    // 延后一拍再拉：effect 体内同步 setState 会触发级联渲染（lint 规则）。
    const timer = setTimeout(() => void load(), 0)
    return () => clearTimeout(timer)
  }, [load])

  const onReview = useCallback(async () => {
    if (!isApiReady() || busy) return
    setBusy('review')
    setMessage('')
    try {
      const result = await api().start_sss_review({ password })
      setReview(result)
      setMessage(
        result.ok
          ? `只读核对完成：站内已确认 ${result.confirmed ?? 0}、站内没有 ${result.missing ?? 0}、` +
              `日期不符 ${result.other_day ?? 0}、读取失败 ${result.scan_failed ?? 0}`
          : `【${classifyOutcome({ status: (result as { status?: string }).status,
                                   code: (result as { code?: string }).code,
                                   ok: result.ok }).label}】` +
              `${result.reason ?? '只读核对失败'}` +
              (result.next_action ? ` —— 下一步：${result.next_action}` : ''),
      )
      await Promise.all([load(), onOperationChanged()])
    } catch (error) {
      setMessage(`只读核对失败：${String(error)}`)
    } finally {
      setBusy('')
    }
  }, [busy, load, onOperationChanged, password])

  const onResolve = useCallback(async () => {
    if (!isApiReady() || busy || selected.length === 0) return
    setBusy('resolve')
    setMessage('')
    try {
      const result: SssUncertainResolveResult = await resolveOnce({
        decision,
        confirm: decision,
        note: note.trim(),
        record_ids: selected,
      })
      setMessage(
        result.ok
          ? result.reason ?? '处置完成（未发送任何创建订单请求）'
          : `【${classifyOutcome({ status: result.status, ok: result.ok }).label}】`
            + `${result.reason ?? '处置被拒绝'}`
            + (result.next_action ? ` —— 下一步：${result.next_action}` : ''),
      )
      if (result.ok) {
        setSelected([])
        setNote('')
        await load()
      }
    } catch (error) {
      setMessage(`处置失败：${String(error)}`)
    } finally {
      setBusy('')
    }
  }, [busy, decision, load, note, resolveOnce, selected])

  const active = state?.counts.active ?? 0
  const records = state?.records ?? []
  const ids = records.map((record) => record.journal_id)
  const allSelected = ids.length > 0 && ids.every((id) => selected.includes(id))

  // 处置按钮的可用条件：station_absent 必须"已选记录全部是站内确认没有"。
  // 与后端校验一一对应的可用条件（纯函数，在 lib/interaction.ts 里有测试）：
  // "站内确认没有"必须证据新鲜且所选记录全部为 station_missing。
  const resolveGate = canResolveDecision({
    decision,
    selected,
    evidence: review?.ok
      ? { results: review.results ?? [], created_at: review.created_at,
          ttl_seconds: review.ttl_seconds }
      : null,
    note,
  })
  const canResolve = !busy && !operationBlocked && resolveGate.allowed
  const resolveHint = resolveGate.reason

  return (
    <div className="mb-4 rounded-md border px-3 py-2.5 text-[11px] leading-relaxed">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-[12.5px] font-medium">
            未决订单记录{active > 0 ? `（${active} 条待处理）` : ''}
          </p>
          <p className="mt-0.5 text-muted-foreground">
            上一次下单若没拿到确定响应就会留在这里。有未决记录时程序拒绝再发下单请求，
            需要先做只读核对或人工处置。
          </p>
        </div>
        <GhostButton onClick={load} disabled={busy !== ''}>
          {busy === 'load' ? '刷新中…' : '刷新'}
        </GhostButton>
      </div>

      {state && !state.ok ? (
        <p className="mt-2 text-destructive">
          {state.journal_unreadable
            ? `未决日志不可用（${state.reason}）：请先人工核对站内订单并修复该文件，`
            : `${state.reason} `}
          在此之前**不要**重新下单。
        </p>
      ) : null}

      {state?.ok && records.length === 0 ? (
        <p className="mt-2 text-muted-foreground">没有未决记录，可以正常下单。</p>
      ) : null}

      {state?.ok && records.length > 0 ? (
        <>
          <div className="mt-2 flex items-center gap-2">
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={allSelected}
                onChange={(e) => setSelected(e.target.checked ? [...ids] : [])}
              />
              <span>全选</span>
            </label>
            <GhostButton onClick={onReview}
                         disabled={busy !== '' || workerAlive || operationBlocked}
                         title={operationBlocked ? conflictText : undefined}>
              {busy === 'review' ? '核对中…' : '只读核对（不下单）'}
            </GhostButton>
          </div>

          <div className="mt-2 space-y-1">
            {records.map((record) => (
              <UncertainRow
                key={record.journal_id}
                record={record}
                checked={selected.includes(record.journal_id)}
                classification={
                  review?.results.find((item) => item.journal_id === record.journal_id)
                    ?.classification
                }
                onToggle={(checked) =>
                  setSelected((prev) =>
                    checked
                      ? [...prev, record.journal_id]
                      : prev.filter((id) => id !== record.journal_id),
                  )
                }
              />
            ))}
          </div>

          {review?.ok ? (
            <p className="mt-2 text-muted-foreground">
              只读核对快照：
              {(review.results ?? [])
                .map(
                  (item) =>
                    `${item.name}（${CLASSIFICATION_LABELS[item.classification] ?? item.classification}）`,
                )
                .join('；')}
            </p>
          ) : null}

          <div className="mt-2 flex flex-wrap items-center gap-2">
            {(
              [
                ['keep', '保持阻断（只记备注）'],
                ['station_present', '站内已有这些订单'],
                ['station_absent', '确认站内没有（解除阻断）'],
              ] as Array<[SssUncertainDecision, string]>
            ).map(([value, label]) => (
              <label key={value} className="flex items-center gap-1">
                <input
                  type="radio"
                  name="sss-uncertain-decision"
                  checked={decision === value}
                  onChange={() => setDecision(value)}
                />
                <span>{label}</span>
              </label>
            ))}
          </div>

          {resolveHint && decision !== 'keep' ? (
            <p className="mt-1 text-amber-600">{resolveHint}</p>
          ) : null}

          <textarea
            rows={2}
            className="mt-2 w-full resize-y rounded-[4px] border border-transparent bg-secondary px-2.5 py-1.5 font-mono text-[11px] leading-relaxed outline-none focus-visible:border-ring focus-visible:bg-card"
            placeholder={
              decision === 'keep' ? '备注（可选）' : '人工备注（至少 4 个字符）'
            }
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />

          <div className="mt-2 flex flex-wrap items-center gap-2">
            <GhostButton onClick={onResolve} disabled={!canResolve || operationBlocked}
                         title={operationBlocked ? conflictText : undefined}>
              {busy === 'resolve' ? '处置中…' : '确认处置'}
            </GhostButton>
            <span className="text-muted-foreground">
              处置过程<b>不会发送创建订单请求</b>；解除阻断后，
              只有你再点「开始下单」才会真正下单。
            </span>
          </div>
        </>
      ) : null}

      {message ? (
        <p
          className={cn(
            'mt-2',
            message.includes('失败') || message.includes('没有') || message.includes('不可')
              ? 'text-destructive'
              : 'text-muted-foreground',
          )}
        >
          {message}
        </p>
      ) : null}
    </div>
  )
}

function UncertainRow({
  record,
  checked,
  classification,
  onToggle,
}: {
  record: SssUncertainRecordView
  checked: boolean
  classification?: string
  onToggle: (checked: boolean) => void
}) {
  return (
    <label className="flex items-start gap-2 rounded border px-2 py-1">
      <input
        type="checkbox"
        className="mt-0.5"
        checked={checked}
        onChange={(e) => onToggle(e.target.checked)}
      />
      <span className="min-w-0 flex-1">
        <span className="font-medium">
          {record.name}（{record.phone}）
        </span>
        <span className="ml-1 text-muted-foreground">
          {record.delivery_time || record.delivery_date} · {record.sheet}
          {record.door_num ? ` · ${record.door_num}` : ''}
        </span>
        <br />
        <span className="text-muted-foreground">
          {record.status === 'inflight' ? '已发出，等待响应' : '结果未知，等待对账'}
          {record.created_at ? ` · ${record.created_at.replace('T', ' ')}` : ''}
          {record.error ? ` · ${record.error}` : ''}
        </span>
        {classification ? (
          <>
            <br />
            <span className="text-muted-foreground">
              核对结论：{CLASSIFICATION_LABELS[classification] ?? classification}
            </span>
          </>
        ) : null}
      </span>
    </label>
  )
}
