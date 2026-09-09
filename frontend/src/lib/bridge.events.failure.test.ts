import assert from 'node:assert/strict'
import test from 'node:test'

test('listener failure keeps cursor and replays the event', async () => {
  const storage = new Map<string, string>()
  let shouldFail = true
  const delivered: Array<Record<string, unknown>> = []
  const event = {
    event: 'task:error' as const,
    payload: { message: 'boom' },
    event_id: 'producer-1:1',
    sequence: 1,
    created_at: 1,
    timestamp: 1,
    droppable: false,
  }

  const fakeApi = {
    bridge_ready: async () => ({
      version: 'test',
      status: 'ready' as const,
      frozen: false,
      event_producer_id: 'producer-1',
      config: {
        target_url: '', phone_number: '', excel_path: '', order_date: '',
        order_count: null, split_ratio: 0.38, sss_url: '', sss_account: '',
        sss_excel_path: '', sss_product_name: '', sss_common_address: '',
        sss_use_fixed_address: true, sss_fixed_lnt: 0, sss_fixed_lat: 0,
        sss_fixed_area_code: '', sss_fixed_address_detail: '',
        sss_dry_run: true, api_mode: true,
      },
      passwords: { order: '', sss: '' },
    }),
    drain_events: async (lastSequence = 0) => ({
      events: lastSequence < 1 ? [event] : [],
      producer_id: 'producer-1',
      latest_sequence: 1,
      acked_sequence: lastSequence,
      dropped_count: 0,
      first_available_sequence: 1,
    }),
  }

  globalThis.window = {
    localStorage: {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value),
    },
    __bridge: { dispatch: () => {} },
    pywebview: { api: fakeApi },
  } as unknown as Window & typeof globalThis

  const bridge = await import('./bridge.ts')
  await bridge.connectBridge()
  bridge.onBridgeEvent((message) => {
    if (shouldFail) throw new Error('listener failed')
    delivered.push(message as unknown as Record<string, unknown>)
  })

  const originalConsoleError = console.error
  console.error = () => {}
  try {
    await bridge.pullBridgeEvents()
  } finally {
    console.error = originalConsoleError
  }
  assert.equal(bridge.bridgeCursor().sequence, 0, '失败事件不能推进 cursor')
  assert.equal(delivered.length, 0)

  shouldFail = false
  await bridge.pullBridgeEvents()
  assert.equal(bridge.bridgeCursor().sequence, 1)
  assert.equal(delivered.length, 1)
})
