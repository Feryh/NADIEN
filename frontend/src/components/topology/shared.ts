// Drawing helpers shared by the old schematic levels (pages/Topology.tsx) and the ELK city/neighbours levels,
// so both read the same role colours, line widths and short hostnames.

export const ROLE_COLOR: Record<string, string> = {
  p: '#6366f1', dci: '#f97316', pe: '#0ea5e9', bng: '#e879f9', agg: '#14b8a6', switch: '#22d3ee', csr: '#fb7185', access: '#64748b', olt: '#a3e635',
}

export const UNKNOWN_HEX = '#94a3b8'

export const SEGMENT_COLOR: Record<'backbone' | 'distribution' | 'last_mile', string> = {
  backbone: '#818cf8', distribution: '#2dd4bf', last_mile: '#f59e0b',
}

export function edgeWidth(capacity: number): number {
  return Math.min(1.5 + Math.log10(Math.max(capacity, 1)) * 2.2, 12)
}

/** Hostname without the POP it is drawn in (JKT-NDC-P1-8201 in POP JKT -> NDC-P1-8201). */
export function shortName(label: string, pop?: string): string {
  if (!pop) return label
  const prefix = `${pop.toUpperCase()}-`
  if (label.toUpperCase().startsWith(prefix) && label.length > prefix.length) return label.slice(prefix.length)
  // The city view sends "?" for a device without a POP: escape it so it is matched literally, not as a quantifier.
  const escaped = pop.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return label.replace(new RegExp(`-${escaped}(?=-|$)`, 'i'), '')
}
