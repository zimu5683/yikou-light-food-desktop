/**
 * 纯展示/解析函数（无 React、无 DOM、无副作用）。
 *
 * **为什么单独成一个模块**：这些函数原先散在 `.tsx` 组件里。它们逻辑不复杂但容易写错，
 * 而写错了**不会报错**——只会把地址顺序切错、把日志显示乱、把日期算错。要覆盖它们
 * 只能写测试，但 Node 的 `node --test` **无法 import `.tsx`**（不会做 JSX 转译），
 * 所以只要函数留在组件文件里就永远测不到。
 *
 * 抽到这里之后：组件照常 import 使用，测试用 `node --test src/lib/*.test.ts` 直接覆盖，
 * **不需要引入 jsdom 等任何新依赖**。逻辑与原实现逐字一致。
 */

/** 地址排序清单输入框：按行拆分，去首尾空白，丢弃空行。 */
export function splitAddressLines(raw: string): string[] {
  return raw
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
}

/**
 * 日志里的「订单摘要」行。
 *
 * `automation._format_order_summary` 用全角「｜」连接字段（也兼容半角 `|`），
 * 形如 `W123｜张三｜小｜已下单`。这类行在界面上要换行展示。
 */
export function isOrderSummary(msg: string): boolean {
  return /^W\d+\s*[｜|]/.test(msg)
}

/** 把订单摘要行按分隔符拆成多行；普通日志原样返回。 */
export function formatLogMsg(msg: string): string {
  if (!isOrderSummary(msg)) return msg
  return msg
    .split(/[｜|]/)
    .map((part) => part.trim())
    .join('\n')
}

/** 订单摘要拆成各字段（供逐行渲染，首行加粗）。 */
export function splitOrderSummary(msg: string): string[] {
  return msg.split(/[｜|]/).map((part) => part.trim())
}

/** 取某月的 1 号（本地时区，用于日历翻页）。 */
export function startOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), 1)
}

/** 两个日期是否是同一天（按本地年月日比较，忽略时分秒）。 */
export function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  )
}

/**
 * 格式化成 `YYYY-MM-DD`（提交给后端的口径）。
 *
 * 刻意**不用** `toISOString()` —— 那个按 UTC 换算，在东八区的清晨会把日期算成前一天。
 */
export function formatISO(d: Date): string {
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

/** 从表单错误集合里取某个字段的提示文案。 */
export function modeError(
  fields: Record<string, { message: string } | undefined> | null,
  key: string,
): string | undefined {
  return fields?.[key]?.message
}
