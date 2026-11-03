// Result of the service validation (Phase 5): four groups of engineering checks.

export type CheckStatus = 'pass' | 'warn' | 'fail' | 'info'

export interface ValidationCheck {
  group: string
  code: string
  status: CheckStatus
  title: string
  detail: string
  items: string[]
}

export interface ValidationResult {
  status: 'pass' | 'warn' | 'fail'
  counts: Record<CheckStatus, number>
  groups: { group: string; label: string; status: CheckStatus; checks: ValidationCheck[] }[]
}

const STATUS_LABEL: Record<CheckStatus, string> = { pass: 'Lulus', warn: 'Peringatan', fail: 'Gagal', info: 'Info' }
const STATUS_ICON: Record<CheckStatus, string> = { pass: '✓', warn: '!', fail: '✗', info: 'i' }

export function StatusBadge({ status, children }: { status: CheckStatus; children?: React.ReactNode }) {
  return <span className={`vstatus vstatus-${status}`}>{STATUS_ICON[status]} {children ?? STATUS_LABEL[status]}</span>
}

export default function ValidationReport({ report }: { report: ValidationResult }) {
  return (
    <div className="validation">
      <div className="validation-summary">
        <StatusBadge status={report.status}>
          {report.status === 'pass' ? 'Siap diimplementasikan' : report.status === 'warn' ? 'Bisa lanjut, ada peringatan' : 'Belum lolos validasi'}
        </StatusBadge>
        <span className="muted small">
          {report.counts.fail} gagal · {report.counts.warn} peringatan · {report.counts.pass} lulus · {report.counts.info} info
        </span>
      </div>
      <div className="validation-groups">
        {report.groups.map((g) => (
          <div key={g.group} className="validation-group">
            <h3><StatusBadge status={g.status}>{g.label}</StatusBadge></h3>
            {!g.checks.length && <div className="muted small">Tidak ada yang perlu dicek.</div>}
            {g.checks.map((c) => (
              <div key={c.code} className={`vcheck vcheck-${c.status}`}>
                <div className="vcheck-title">
                  <span className={`vdot vstatus-${c.status}`}>{STATUS_ICON[c.status]}</span> {c.title}
                </div>
                {c.detail && <div className="small muted">{c.detail}</div>}
                {c.items.length > 0 && (
                  <ul className="small">{c.items.slice(0, 8).map((item) => <li key={item}>{item}</li>)}</ul>
                )}
                {c.items.length > 8 && <div className="small muted">+{c.items.length - 8} lainnya</div>}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}
