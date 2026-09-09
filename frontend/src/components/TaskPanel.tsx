/**
 * 任务面板：模式切换（下划线 tab）+ 订单处理 / 闪时送下单 表单 + 主操作条 + 更多菜单。
 * 校验结果由桥接层返回（start_order/start_sss 的 fields），前端渲染字段错误态。
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { MoreHorizontal } from 'lucide-react'
import { Button } from '@/components/ui/button'
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
import { api, isApiReady, type OrderFormPayload, type SssFormPayload } from '@/lib/bridge'
import { cn } from '@/lib/utils'

function modeError(fields: FieldErrors | null, key: string): string | undefined {
  return fields?.[key]?.message
}

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
          <ModeTab active={mode === 'sss'} onClick={() => setMode('sss')}>
            闪时送下单
          </ModeTab>
        </div>

        {/* 两个表单常驻渲染（仅切换可见性）：卸载会清空各字段的 useState，
            导致切页签后已输入内容丢失并被旧 config 重新填充。 */}
        <div className={cn(mode === 'order' ? 'block' : 'hidden')}>
          <OrderForm key={formKey} />
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
  const { config, passwords, startSss, workerAlive } = useApp()
  const [url, setUrl] = useState(config?.sss_url ?? '')
  const [account, setAccount] = useState(config?.sss_account ?? '')
  const [password, setPassword] = useState(passwords.sss ?? '')
  const [excel, setExcel] = useState(config?.sss_excel_path ?? '')
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
  const [apiMode, setApiMode] = useState(config?.api_mode ?? true)
  const [fields, setFields] = useState<FieldErrors | null>(null)
  const [busy, setBusy] = useState(false)

  // 字段停止变化后自动落盘（切页签/退出重进都从后端还原，配置不丢失）。
  const scheduleSave = useDebouncedSave(() => {
    if (!isApiReady()) return
    api()
      .save_sss_config({
        url,
        account,
        excel,
        product_name: productName,
        common_address: commonAddress,
        use_fixed_address: useFixedAddress,
        fixed_lnt: fixedLnt,
        fixed_lat: fixedLat,
        fixed_area_code: fixedAreaCode,
        fixed_address_detail: fixedAddressDetail,
        dry_run: dryRun,
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
    fixedLnt, fixedLat, fixedAreaCode, fixedAddressDetail, dryRun, apiMode,
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
        product_name: productName,
        common_address: commonAddress,
        use_fixed_address: useFixedAddress,
        fixed_lnt: fixedLnt,
        fixed_lat: fixedLat,
        fixed_area_code: fixedAreaCode,
        fixed_address_detail: fixedAddressDetail,
        remember,
        dry_run: dryRun,
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
        label="订单 Excel 文件"
        htmlFor="sss-excel"
        error={excelError}
        okMessage={excelOk}
        helper="午餐/晚餐两表，A=姓名 B=门牌号 C=电话"
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
        onConfirm={() => clearPassword(mode)}
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
