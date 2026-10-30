import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

// Exercise the actual registration contract without Electron or React dependencies.
const source = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
  .replace(/import\s*\{[\s\S]*?\}\s*from\s*['"][^'"]+['"]/g, '')
const { default: plugin } = await import(`data:text/javascript;base64,${Buffer.from(
  "const STATUSBAR_AREAS = {right:'statusBar.right'}; const PALETTE_AREA = 'palette';\n" + source
).toString('base64')}`)

function fixture(value) {
  const saved = new Map(value === undefined ? [] : [['showQuotaChip', value]])
  const active = new Map()
  const ctx = {
    storage: { get: (k, fallback) => saved.has(k) ? saved.get(k) : fallback,
      set: (k, v) => saved.set(k, v) },
    register: c => { active.set(c.id, c); return () => active.delete(c.id) }
  }
  plugin.register(ctx)
  return { saved, active, ctx }
}

test('quota chip defaults on while worker chip is always registered', () => {
  const { active } = fixture()
  assert.ok(active.has('quote'))
  assert.ok(active.has('worker'))
})

test('showQuotaChip false omits quota contribution, preserving workers', () => {
  const { active } = fixture(false)
  assert.equal(active.has('quote'), false)
  assert.ok(active.has('worker'))
})

test('palette toggle persists, updates contributions live, and survives reload', () => {
  const { saved, active, ctx } = fixture()
  const toggle = active.get('toggle-quota-chip').data
  assert.equal(toggle.detail(), 'on')
  toggle.run()
  assert.equal(saved.get('showQuotaChip'), false)
  assert.equal(toggle.detail(), 'off')
  assert.equal(active.has('quote'), false)
  active.clear()
  plugin.register(ctx)
  assert.equal(active.has('quote'), false)
  active.get('toggle-quota-chip').data.run()
  assert.equal(saved.get('showQuotaChip'), true)
  assert.ok(active.has('quote'))
  assert.ok(active.has('worker'))
})
