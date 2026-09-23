/**
 * Hermes Monitor — desktop half.
 *
 * Two footer items (STATUSBAR_AREAS.right):
 *   1. Worker  — animated spinner + count of running kanban cards, colored by
 *                the worst worker state (loop > stalled > active).
 *   2. Quote   — per-provider mini usage bars (+ reset time, or DeepSeek
 *                balance), colored by usage threshold.
 *
 * Both poll their own backend namespace via `ctx.rest` (namespace-relative
 * paths only — see the plugin contract). Periodic polling never bypasses the
 * backend cache; opening the menu on click re-fetches with `?fresh=1`.
 *
 * Polling uses React Query `refetchInterval` (the app's shared QueryClient),
 * so the timers are torn down automatically when the item unmounts and the
 * plugin is disabled/hot-reloaded.
 *
 * Plain ESM, loaded uncompiled — UI is `jsx()` calls, not JSX syntax. Only
 * `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime` resolve.
 */

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  GlyphSpinner,
  STATUSBAR_AREAS,
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
  icons,
  useQuery
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useCallback, useEffect, useRef, useState } from 'react'

const ID = 'hermes-monitor'

// Polling cadences (ms). Never below 10s.
const WORKER_INTERVAL_MS = 10_000
const QUOTA_INTERVAL_MS = 300_000

// Traffic-light colors for the semaforo states. Mid-saturation hues that stay
// legible on both the light and dark themes (bars + status dots).
const GREEN = '#22c55e'
const ORANGE = '#f59e0b'
const RED = '#ef4444'

const STATE_COLOR = { active: GREEN, stalled: ORANGE, loop: RED }

// Shared chrome styling for interactive statusbar items (matches core).
const CHIP_CLASS =
  'inline-flex h-full items-center gap-1 whitespace-nowrap rounded-none px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground'

// -- pure helpers -------------------------------------------------------------

function clampPct(value) {
  if (!Number.isFinite(value)) return 0
  return Math.max(0, Math.min(100, value))
}

function quotaColor(pct) {
  if (pct > 90) return RED
  if (pct >= 75) return ORANGE
  return GREEN
}

function pad2(n) {
  return String(n).padStart(2, '0')
}

/** epoch seconds -> local "HH:MM"; null/undefined/garbage -> "n/d". */
function formatResetTime(epoch) {
  if (!Number.isFinite(epoch)) return 'n/d'
  const d = new Date(epoch * 1000)
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.floor(Number(totalSeconds) || 0))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${sec}s`
  return `${sec}s`
}

/** seconds-since-last-activity -> "N s fa" / "N min fa" / "N h fa". */
function formatLastActivity(sec) {
  if (!Number.isFinite(sec)) return 'n/d'
  const s = Math.max(0, Math.floor(sec))
  if (s < 60) return `${s} s fa`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} min fa`
  return `${Math.floor(m / 60)} h fa`
}

function formatBalance(balance) {
  if (!balance || typeof balance.amount !== 'number') return 'n/d'
  const code = String(balance.currency || '').toUpperCase()
  const symbol = code === 'USD' ? '$' : code === 'EUR' ? '€' : code === 'CNY' ? '¥' : code ? `${code} ` : ''
  return `${symbol}${balance.amount.toFixed(2)}`
}

function worstWorkerState(workers) {
  if (workers.some(w => w.state === 'loop')) return 'loop'
  if (workers.some(w => w.state === 'stalled')) return 'stalled'
  return 'active'
}

// -- data hook ----------------------------------------------------------------

/**
 * Polls `path` at `intervalMs` through React Query (cached backend path), and
 * exposes `refreshFresh()` to re-fetch with `?fresh=1` while the menu is open.
 * `data` prefers the fresh snapshot while the menu is open, then falls back to
 * the polled value once closed.
 *
 * A failed request — poll or fresh — never leaves stale numbers on screen:
 * React Query keeps the last successful `data` across a failed refetch, so the
 * failure is turned into a neutral `null` snapshot plus `error: true` and the
 * chips render their "n/d" / inactive state. Rejections stay contained here.
 */
function useMonitor(ctx, path, intervalMs) {
  const query = useQuery({
    queryKey: [ID, path],
    queryFn: () => ctx.rest(path),
    refetchInterval: intervalMs,
    retry: false
  })

  const [fresh, setFresh] = useState(null)
  const [freshFailed, setFreshFailed] = useState(false)
  const openRef = useRef(false)

  // A successful poll supersedes a previous fresh-fetch failure, so the chip
  // returns to live data on its own.
  useEffect(() => {
    if (!query.isError) setFreshFailed(false)
  }, [query.isError, query.dataUpdatedAt])

  const refreshFresh = useCallback(() => {
    openRef.current = true
    let request
    try {
      request = ctx.rest(`${path}?fresh=1`)
    } catch {
      // Bridge threw synchronously: same neutral outcome as a rejection.
      setFresh(null)
      setFreshFailed(true)
      return
    }
    Promise.resolve(request)
      .then(data => {
        if (!openRef.current) return
        setFresh(data)
        setFreshFailed(false)
      })
      .catch(() => {
        if (!openRef.current) return
        setFresh(null)
        setFreshFailed(true)
      })
  }, [ctx, path])

  const close = useCallback(() => {
    openRef.current = false
    setFresh(null)
    setFreshFailed(false)
  }, [])

  // `isRefetchError` covers the v5 background-refetch failure that keeps the
  // previous `data` around with `status: 'error'`.
  const failed = query.isError === true || query.isRefetchError === true || freshFailed
  const data = failed ? null : fresh ?? query.data

  return { data, error: failed, refreshFresh, close }
}

// -- shared chrome helpers ----------------------------------------------------

function withTooltip(trigger, label) {
  return jsxs(TooltipProvider, {
    delayDuration: 0,
    children: [
      jsxs(Tooltip, {
        children: [
          jsx(TooltipTrigger, { asChild: true, children: trigger }),
          jsx(TooltipContent, { children: label })
        ]
      })
    ]
  })
}

// -- Worker item --------------------------------------------------------------

function renderWorkerRow(w) {
  return jsx(DropdownMenuItem, {
    onSelect: event => event.preventDefault(),
    children: jsxs('div', {
      className: 'flex w-full flex-col gap-0.5',
      children: [
        jsxs('div', {
          className: 'flex w-full items-center gap-1.5',
          children: [
            jsx('span', {
              className: 'size-1.5 shrink-0 rounded-full',
              style: { backgroundColor: STATE_COLOR[w.state] ?? GREEN }
            }),
            jsx('span', {
              className: 'min-w-0 flex-1 truncate font-medium',
              children: w.title || w.card_id || '(senza titolo)'
            }),
            jsx('span', {
              className: 'shrink-0 tabular-nums text-(--ui-text-quaternary)',
              children: formatDuration(w.duration_s)
            })
          ]
        }),
        jsxs('div', {
          className: 'flex w-full items-center gap-1 text-[0.625rem] text-(--ui-text-quaternary)',
          children: [
            jsx('span', { className: 'shrink-0 font-mono', children: w.card_id || '—' }),
            jsx('span', { children: '·' }),
            jsx('span', { className: 'min-w-0 flex-1 truncate', children: w.assignee || '—' }),
            jsx('span', { className: 'shrink-0', children: `ultima attività ${formatLastActivity(w.last_activity_s)}` })
          ]
        })
      ]
    })
  }, w.card_id)
}

function WorkerChip({ ctx }) {
  const { data, error, refreshFresh, close } = useMonitor(ctx, '/workers', WORKER_INTERVAL_MS)

  const workers = Array.isArray(data?.workers) ? data.workers : []
  const count = Number.isFinite(data?.count) ? data.count : workers.length
  const color = count > 0 ? STATE_COLOR[worstWorkerState(workers)] : null
  // The Kanban link comes from the first worker only; if it has no URL we omit
  // the entry entirely rather than borrowing another card's URL.
  const firstWorker = workers[0]
  const kanbanUrl =
    firstWorker && typeof firstWorker.kanban_url === 'string' && firstWorker.kanban_url
      ? firstWorker.kanban_url
      : null

  const statusLabel = error
    ? 'Worker: dati non disponibili'
    : count > 0
      ? `Worker: ${count} ${count === 1 ? 'attivo' : 'attivi'}`
      : 'Nessun worker attivo'

  const handleOpenChange = open => (open ? refreshFresh() : close())

  const trigger = jsx(DropdownMenuTrigger, {
    asChild: true,
    children: jsxs('button', {
      type: 'button',
      className: CHIP_CLASS,
      'aria-label': statusLabel,
      children:
        count > 0
          ? [
              jsx('span', {
                className: 'inline-flex items-center',
                style: { color },
                children: jsx(GlyphSpinner, { className: 'text-[0.8125rem]', ariaLabel: 'Worker attivi' })
              }),
              jsx('span', { className: 'tabular-nums text-(--ui-text-secondary)', children: String(count) })
            ]
          : [
              jsx(icons.Users, { className: 'size-3.5 text-(--ui-text-quaternary)' }),
              jsx('span', { className: 'tabular-nums text-(--ui-text-quaternary)', children: '0' })
            ]
    })
  })

  const menuChildren = [
    jsx('div', {
      className: 'px-2 pt-1 pb-0.5 text-[0.625rem] font-medium uppercase tracking-wide text-(--ui-text-quaternary)',
      children: error
        ? 'Worker n/d'
        : count > 0
          ? `${count} worker ${count === 1 ? 'attivo' : 'attivi'}`
          : 'Nessun worker attivo'
    }, 'header')
  ]

  if (count > 0) {
    for (const w of workers) menuChildren.push(renderWorkerRow(w))
  }

  if (kanbanUrl) {
    menuChildren.push(jsx(DropdownMenuSeparator, {}, 'sep'))
    menuChildren.push(
      jsxs(DropdownMenuItem, {
        onSelect: () => {
          try {
            Promise.resolve(ctx.os.openExternal(kanbanUrl)).catch(() => {})
          } catch {
            // Bridge unavailable: nothing to open, never break the menu.
          }
        },
        children: [jsx(icons.ExternalLink, { className: 'size-3.5' }), jsx('span', { children: 'Apri Kanban' })]
      }, 'kanban')
    )
  }

  return jsxs(DropdownMenu, {
    onOpenChange: handleOpenChange,
    children: [
      withTooltip(trigger, statusLabel),
      jsx(DropdownMenuContent, {
        align: 'end',
        side: 'top',
        sideOffset: 8,
        children: menuChildren
      })
    ]
  })
}

// -- Quote item ---------------------------------------------------------------

/** Footer read-out for one window: label, mini-bar, % used, local reset hour. */
function renderWindowBar(win) {
  const hasPct = Number.isFinite(win.used_percent)
  const pct = clampPct(win.used_percent)
  return jsxs('span', {
    className: 'inline-flex items-center gap-1',
    children: [
      jsx('span', {
        className: 'text-[0.625rem] text-(--ui-text-quaternary)',
        children: win.label || '—'
      }),
      jsx('span', {
        className: 'relative block h-1.5 w-5 shrink-0 overflow-hidden rounded-sm bg-(--ui-stroke-secondary)',
        children: hasPct
          ? jsx('span', {
              className: 'absolute inset-y-0 left-0 rounded-sm',
              style: { width: `${pct}%`, backgroundColor: quotaColor(pct) }
            })
          : null
      }),
      jsx('span', {
        className: 'tabular-nums text-[0.625rem] text-(--ui-text-secondary)',
        children: hasPct ? `${Math.round(pct)}%` : 'n/d'
      }),
      jsx('span', {
        className: 'text-[0.625rem] text-(--ui-text-quaternary)',
        children: win.reset_at != null ? `reset ${formatResetTime(win.reset_at)}` : 'reset n/d'
      })
    ]
  }, win.label)
}

function renderProviderChip(p) {
  const nok = p.status !== 'ok'
  const windows = Array.isArray(p.windows) ? p.windows : []
  const value = nok
    ? jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: 'n/d' })
    : p.id === 'deepseek'
      ? jsx('span', {
          className: 'font-medium tabular-nums text-(--ui-text-secondary)',
          children: formatBalance(p.balance)
        })
      : windows.length > 0
        ? jsxs('span', {
            className: 'inline-flex items-center gap-1.5',
            children: windows.map(renderWindowBar)
          })
        : jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: 'n/d' })

  return jsxs('span', {
    className: 'inline-flex items-center gap-1',
    children: [
      jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: p.label || p.id }),
      value
    ]
  }, p.id)
}

function renderWindowDetail(win) {
  const pct = clampPct(win.used_percent)
  return jsxs('div', {
    className: 'flex w-full items-center gap-2',
    children: [
      jsx('span', { className: 'w-16 shrink-0 text-(--ui-text-quaternary)', children: win.label }),
      jsx('span', {
        className: 'h-1.5 flex-1 overflow-hidden rounded-sm bg-(--ui-stroke-secondary)',
        children: jsx('span', {
          className: 'block h-full rounded-sm',
          style: { width: `${pct}%`, backgroundColor: quotaColor(pct) }
        })
      }),
      jsx('span', {
        className: 'w-10 shrink-0 text-right tabular-nums text-(--ui-text-secondary)',
        children: `${Math.round(win.used_percent)}%`
      }),
      jsx('span', {
        className: 'w-16 shrink-0 text-right text-(--ui-text-quaternary)',
        children: win.reset_at != null ? `reset ${formatResetTime(win.reset_at)}` : 'n/d'
      })
    ]
  }, win.label)
}

function renderProviderDetail(p) {
  const nok = p.status !== 'ok'
  const details =
    nok
      ? []
      : p.id === 'deepseek'
        ? [
            jsxs('div', {
              className: 'flex w-full items-center gap-2',
              children: [
                jsx('span', { className: 'text-(--ui-text-quaternary)', children: 'Saldo residuo' }),
                jsx('span', {
                  className: 'ml-auto font-medium tabular-nums text-(--ui-text-secondary)',
                  children: formatBalance(p.balance)
                })
              ]
            })
          ]
        : (Array.isArray(p.windows) ? p.windows : []).map(renderWindowDetail)

  return jsx(DropdownMenuItem, {
    onSelect: event => event.preventDefault(),
    children: jsxs('div', {
      className: 'flex w-full flex-col gap-1',
      children: [
        jsxs('div', {
          className: 'flex w-full items-center gap-1.5',
          children: [
            jsx('span', { className: 'font-medium text-(--ui-text-secondary)', children: p.label || p.id }),
            nok ? jsx('span', { className: 'text-(--ui-text-quaternary)', children: 'n/d' }) : null
          ]
        }),
        ...details
      ]
    })
  }, p.id)
}

function QuoteChip({ ctx }) {
  const { data, error, refreshFresh, close } = useMonitor(ctx, '/quotas', QUOTA_INTERVAL_MS)

  const providers = Array.isArray(data?.providers) ? data.providers : []

  const handleOpenChange = open => (open ? refreshFresh() : close())

  const chipNodes = []
  providers.forEach((p, i) => {
    if (i > 0) {
      chipNodes.push(
        jsx('span', { className: 'px-0.5 text-(--ui-text-quaternary)', children: '·' }, `chip-sep-${p.id}`)
      )
    }
    chipNodes.push(renderProviderChip(p))
  })

  const trigger = jsx(DropdownMenuTrigger, {
    asChild: true,
    children: jsx('button', {
      type: 'button',
      className: CHIP_CLASS,
      'aria-label': error ? 'Quote: dati non disponibili' : 'Quote piani',
      children: providers.length > 0
        ? chipNodes
        : jsx('span', { className: 'text-(--ui-text-quaternary)', children: 'Quote n/d' })
    })
  })

  const menuChildren = []
  providers.forEach((p, i) => {
    if (i > 0) menuChildren.push(jsx(DropdownMenuSeparator, {}, `sep-${p.id}`))
    menuChildren.push(renderProviderDetail(p))
  })
  if (menuChildren.length === 0) {
    menuChildren.push(
      jsx('div', {
        className: 'px-2 py-1 text-[0.625rem] text-(--ui-text-quaternary)',
        children: error ? 'Quote n/d — dati non disponibili' : 'Nessun dato quote disponibile'
      }, 'empty')
    )
  }

  return jsxs(DropdownMenu, {
    onOpenChange: handleOpenChange,
    children: [
      withTooltip(trigger, 'Quote piani'),
      jsx(DropdownMenuContent, {
        align: 'end',
        side: 'top',
        sideOffset: 8,
        children: menuChildren
      })
    ]
  })
}

// -- plugin contract ----------------------------------------------------------

export default {
  id: ID, // must match the folder name / manifest name
  name: 'Hermes Monitor',
  // Unified-package desktop halves ship opt-in: the plugin inventories in
  // Capabilities → Plugins and stays off until the user flips the switch.
  defaultEnabled: false,
  register(ctx) {
    ctx.register({
      id: 'worker',
      area: STATUSBAR_AREAS.right,
      order: 140,
      render: () => jsx(WorkerChip, { ctx })
    })

    ctx.register({
      id: 'quote',
      area: STATUSBAR_AREAS.right,
      order: 150,
      render: () => jsx(QuoteChip, { ctx })
    })
  }
}
