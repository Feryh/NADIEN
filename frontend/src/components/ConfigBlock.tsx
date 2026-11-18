import { useState } from 'react'

/** A config or rollback listing with copy and an optional .txt download. */
export function ConfigBlock({ title, lines, href }: { title: string; lines: string[]; href?: string }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard?.writeText(lines.join('\n')).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    })
  }
  return (
    <div className="mop-step">
      <div className="toolbar">
        <span className="mop-step-title">{title}</span>
        <div className="spacer" />
        <button onClick={copy}>{copied ? 'Tersalin' : 'Salin'}</button>
        {href && <a className="button" href={href}>.txt</a>}
      </div>
      <pre>{lines.join('\n')}</pre>
    </div>
  )
}
