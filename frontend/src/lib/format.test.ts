/**
 * `lib/format.ts` 的回归锁 —— 这些函数原先散在 `.tsx` 组件里，**从未被测试覆盖**。
 *
 * 它们的特点是：**写错了不会报错**。地址顺序会静静切错、日志会显示乱、日期会差一天，
 * 而界面上不一定看得出来。抽出到 `.ts` 之后就能直接用 `node --test` 覆盖，**不需要
 * 引入 jsdom 或任何新依赖**。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  formatISO,
  formatLogMsg,
  isOrderSummary,
  modeError,
  sameDay,
  splitAddressLines,
  splitOrderSummary,
  startOfMonth,
} from './format.ts'

// ----------------------------------------------------------------------
// splitAddressLines：地址排序清单输入框
// ----------------------------------------------------------------------
test('splitAddressLines trims each line and drops blank ones', () => {
  assert.deepEqual(splitAddressLines('  小  \n大西\n\n  b2  \n'), ['小', '大西', 'b2'])
})

test('splitAddressLines tolerates Windows line endings', () => {
  // 记事本/Excel 里复制出来的清单常带 \r\n；'\r' 会被 trim 掉，不能残留。
  assert.deepEqual(splitAddressLines('小\r\n大西\r\n'), ['小', '大西'])
})

test('splitAddressLines handles empty and whitespace-only input', () => {
  assert.deepEqual(splitAddressLines(''), [])
  assert.deepEqual(splitAddressLines('\n\n\n'), [])
  assert.deepEqual(splitAddressLines('   \n\t\n  '), [])
})

test('splitAddressLines keeps a single line and preserves order', () => {
  assert.deepEqual(splitAddressLines('外卖柜'), ['外卖柜'])
  assert.deepEqual(splitAddressLines('c\nb\na'), ['c', 'b', 'a'], '不能排序，顺序就是用户定义的顺序')
})

// ----------------------------------------------------------------------
// isOrderSummary / formatLogMsg / splitOrderSummary：日志渲染
// ----------------------------------------------------------------------
test('isOrderSummary recognizes the order-summary rows', () => {
  assert.equal(isOrderSummary('W123｜张三｜小｜已下单'), true)
  assert.equal(isOrderSummary('W123|张三|小'), true, '半角竖线也算')
  assert.equal(isOrderSummary('W1 ｜张三'), true, 'W 号与分隔符之间允许空白')
})

test('isOrderSummary rejects ordinary log lines', () => {
  for (const msg of ['普通日志', 'w123｜小写不算', 'W123 没有分隔符', '', '订单 W123｜在中间']) {
    assert.equal(isOrderSummary(msg), false, msg)
  }
})

test('formatLogMsg turns a summary into one field per line', () => {
  assert.equal(formatLogMsg('W123｜张三｜小｜已下单'), 'W123\n张三\n小\n已下单')
})

test('formatLogMsg trims each field', () => {
  assert.equal(formatLogMsg('W1 ｜  张三  ｜ 小 '), 'W1\n张三\n小')
})

test('formatLogMsg leaves ordinary lines untouched', () => {
  // 普通日志里的换行/空格都要原样保留，不能被顺手改掉。
  const msg = '正在处理   订单\n第二行'
  assert.equal(formatLogMsg(msg), msg)
})

test('splitOrderSummary mirrors formatLogMsg field for field', () => {
  const msg = 'W1｜张三｜小'
  assert.deepEqual(splitOrderSummary(msg), formatLogMsg(msg).split('\n'))
})

// ----------------------------------------------------------------------
// 日期三件套
// ----------------------------------------------------------------------
test('startOfMonth returns the first day of the same month', () => {
  const got = startOfMonth(new Date(2026, 8, 16, 23, 59))
  assert.equal(got.getFullYear(), 2026)
  assert.equal(got.getMonth(), 8)
  assert.equal(got.getDate(), 1)
  assert.equal(got.getHours(), 0, '时分秒要归零，否则同日比较会出错')
})

test('startOfMonth handles January and December', () => {
  assert.equal(startOfMonth(new Date(2026, 0, 31)).getMonth(), 0)
  assert.equal(startOfMonth(new Date(2026, 11, 31)).getMonth(), 11)
})

test('sameDay compares local calendar days, not instants', () => {
  assert.equal(sameDay(new Date(2026, 8, 16, 0, 0), new Date(2026, 8, 16, 23, 59)), true)
  assert.equal(sameDay(new Date(2026, 8, 16), new Date(2026, 8, 17)), false)
  assert.equal(sameDay(new Date(2026, 8, 16), new Date(2026, 9, 16)), false)
  assert.equal(sameDay(new Date(2026, 8, 16), new Date(2027, 8, 16)), false)
})

test('formatISO zero-pads month and day', () => {
  assert.equal(formatISO(new Date(2026, 0, 5)), '2026-01-05')
  assert.equal(formatISO(new Date(2026, 11, 31)), '2026-12-31')
})

test('formatISO uses local date, not UTC', () => {
  // 关键性质：不能用 toISOString() —— 它按 UTC 换算。到底是「前一天」还是同一天，
  // 取决于时区偏移的**方向**（UTC+8 的 00:30 会退到前一天；UTC-4 的 00:30 不会），
  // 所以这里**算出**差异再断言，而不是假定一定有差异。
  const pad = (n: number) => String(n).padStart(2, '0')
  const localParts = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`

  // 1) 全天任意时刻都落在同一个本地日期
  for (const hour of [0, 1, 12, 23]) {
    const d = new Date(2026, 8, 16, hour, 30, 0)
    assert.equal(formatISO(d), '2026-09-16', `${hour} 点`)
  }

  // 2) 一律等于「本地分量直接拼」
  for (const d of [new Date(2026, 0, 1), new Date(2026, 11, 31, 23, 59), new Date(2026, 5, 15)]) {
    assert.equal(formatISO(d), localParts(d))
  }

  // 3) 用一个确定的 UTC 时刻，验证取的确实是本地日期而不是 UTC 日期
  const instant = new Date(Date.UTC(2026, 8, 16, 0, 0))
  assert.equal(formatISO(instant), localParts(instant))
  const utcDate = instant.toISOString().slice(0, 10)
  if (localParts(instant) !== utcDate) {
    // 本时区确实跨日 → 证明 formatISO 没有误用 UTC
    assert.notEqual(formatISO(instant), utcDate)
  }
})

test('formatISO round-trips with startOfMonth and sameDay', () => {
  const d = new Date(2026, 8, 16)
  assert.equal(formatISO(startOfMonth(d)), '2026-09-01')
  assert.equal(sameDay(startOfMonth(d), new Date(2026, 8, 1, 12)), true)
})

// ----------------------------------------------------------------------
// modeError：表单错误取值
// ----------------------------------------------------------------------
test('modeError reads the message of the given field', () => {
  assert.equal(modeError({ url: { message: '请输入管理网址' } }, 'url'), '请输入管理网址')
})

test('modeError returns undefined for missing fields or no errors', () => {
  assert.equal(modeError(null, 'url'), undefined)
  assert.equal(modeError({}, 'url'), undefined)
  assert.equal(modeError({ url: { message: 'x' } }, 'phone'), undefined)
  assert.equal(modeError({ url: undefined }, 'url'), undefined)
})
