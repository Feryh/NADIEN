// Formatting of the sync-all job's per-step report (backend/app/collectors/jobs.py run_all).

export interface SyncStep {
  step: string
  status: 'ok' | 'failed' | 'skipped'
  duration_s: number
  report?: unknown
  error?: string
  detail?: string
}

export const STEP_LABELS: Record<string, string> = {
  backup: 'Backup database',
  observium: 'Observium utama',
  observium_csr: 'Observium CSR',
  configs: 'Config gitweb',
  discovery: 'Topologi LLDP/config',
  rrd: 'Histori RRD',
  rrd_csr: 'Histori RRD CSR',
}

type Fields = Record<string, unknown>

function fields(value: unknown): Fields {
  return value !== null && typeof value === 'object' ? (value as Fields) : {}
}

function count(value: unknown): number {
  return typeof value === 'number' ? value : 0
}

function num(value: unknown): string {
  return count(value).toLocaleString('id-ID')
}

export function syncAllSteps(report: unknown): SyncStep[] | null {
  const steps = fields(report).steps
  return Array.isArray(steps) ? (steps as SyncStep[]) : null
}

function okSummary(step: string, report: Fields): string {
  const counts = fields(report.counts)
  switch (step) {
    case 'backup':
      return String(report.name ?? report.detail ?? report.status ?? '')
    case 'observium':
    case 'observium_csr':
      return `${num(counts.devices_seen)} device · ${num(counts.ports_seen)} port`
    case 'configs':
      return `${num(counts.changed)} file berubah · ${num(counts.applied)} diterapkan`
    case 'discovery':
      return `${num(report.lldp_links)} LLDP · ${num(report.config_links)} config · ${num(report.created)} baru · ${num(report.updated)} diperbarui`
    case 'rrd':
    case 'rrd_csr': {
      const parts = [`${num(report.links_with_history)}/${num(report.links)} link`]
      if (count(report.links_backfilled)) parts.push(`${num(report.links_backfilled)} backfill`)
      parts.push(`${num(report.samples)} sampel`)
      if (count(report.error_count)) parts.push(`${num(report.error_count)} error`)
      return parts.join(' · ')
    }
    default:
      return ''
  }
}

export function stepSummary(step: SyncStep): string {
  if (step.status === 'failed') return step.error ?? 'gagal'
  if (step.status === 'skipped') return step.detail ?? 'dilewati'
  return okSummary(step.step, fields(step.report))
}

export function formatDuration(seconds: number): string {
  const total = Math.round(seconds)
  if (total < 60) return `${total}s`
  return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`
}

/** "observium: ports" (a sync-all step and its own phase) -> "Observium utama — mengambil port". */
export function phaseLabel(phase: string, phases: Record<string, string>): string {
  if (phases[phase]) return phases[phase]
  const split = phase.indexOf(': ')
  if (split < 0) return STEP_LABELS[phase] ?? phase
  const step = phase.slice(0, split)
  const sub = phase.slice(split + 2)
  return `${STEP_LABELS[step] ?? step} — ${phases[sub] ?? sub}`
}
