import type { CityCluster, CityLink } from './types'

const DASH = '–'

/** Uptime as "x h y m" (whole hours, then minutes); under an hour only minutes. */
export function formatUptime(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return DASH
  const totalMinutes = Math.floor(seconds / 60)
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  return hours > 0 ? `${hours} h ${minutes} m` : `${minutes} m`
}

/** Last poll in the viewer's local time, Indonesian format. */
export function formatLastPoll(iso: string | null | undefined): string {
  if (!iso) return DASH
  const when = new Date(iso)
  return Number.isNaN(when.getTime()) ? DASH : when.toLocaleString('id-ID')
}

/** "host:interface", or the host alone when the link has no interface record. */
export function linkEnd(device: string, linkInterface: string | null): string {
  return linkInterface ? `${device}:${linkInterface}` : device
}

/**
 * Every link between the same pair of drawn nodes as `clicked`, in either orientation: one drawn edge may stand for a
 * bundle of parallel links, and the panel lists them all. Empty when `clicked` no longer exists in `links`.
 */
export function bundleOf(links: CityLink[], clickedId: number): CityLink[] {
  const clicked = links.find((l) => l.id === clickedId)
  if (!clicked) return []
  return links.filter(
    (l) => (l.source === clicked.source && l.target === clicked.target) || (l.source === clicked.target && l.target === clicked.source),
  )
}

const CLUSTER_KIND_LABEL: Record<CityCluster['kind'], string> = { ring: 'Ring', mesh: 'Mesh', pair: 'Redundan' }

export function clusterSummary(clusters: CityCluster[], clusterId: string | null): string {
  if (!clusterId) return DASH
  const cluster = clusters.find((c) => c.id === clusterId)
  return cluster ? `${CLUSTER_KIND_LABEL[cluster.kind]} · ${cluster.device_ids.length} perangkat` : DASH
}
