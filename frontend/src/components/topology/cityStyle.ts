import type cytoscape from 'cytoscape'

// ELK reserves [top=28, left=14, bottom=14, right=14] around every compound's children (elkGraph.ts). Cytoscape has
// only one padding for all sides (padding-left/-top/... are aliases of `padding`), so the drawn box uses the
// smaller side value and draws its label above the box, inside the 28 px band ELK kept free on top. 12 instead of
// 14 leaves room for the borders Cytoscape adds outside the padding (leaf border 2 + compound border 1), so a drawn
// box always sits inside ELK's box and never over a line ELK routed around it.
export const COMPOUND_PADDING_PX = 12

// Interface labels at the link ends only once the view is zoomed in to 1.2: below that they pile up unreadably.
const END_LABEL_FONT = 9
const END_LABEL_MIN_ZOOM = 1.2

export const CITY_STYLE = [
  {
    selector: 'node',
    style: {
      label: 'data(display)',
      color: '#f1f5f9',
      'font-size': 11,
      'text-valign': 'center',
      'text-halign': 'center',
      'min-zoomed-font-size': 6,
    },
  },
  {
    selector: 'node[kind="device"], node[kind="summary"]',
    style: {
      shape: 'round-rectangle',
      width: 'data(w)',
      height: 'data(h)',
      'background-color': 'data(healthColor)',
      'border-color': 'data(roleColor)',
      'border-width': 2,
      'text-max-width': 'data(w)',
      'text-wrap': 'ellipsis',
    },
  },
  { selector: 'node[kind="summary"]', style: { 'border-style': 'dashed', 'border-color': '#94a3b8', 'background-color': '#1e293b' } },
  {
    selector: 'node[kind="external_pop"]',
    style: {
      shape: 'round-rectangle',
      width: 'data(w)',
      height: 'data(h)',
      'background-color': '#1e293b',
      'border-color': '#64748b',
      'border-style': 'dashed',
      'border-width': 1.5,
      color: '#cbd5e1',
    },
  },
  {
    selector: 'node[kind="city_box"], node[kind="pop_lane"], node[kind="cluster"]',
    style: {
      shape: 'rectangle',
      padding: `${COMPOUND_PADDING_PX}px`,
      // Box = children + padding only; the label must not stretch it beyond what ELK laid out.
      'compound-sizing-wrt-labels': 'exclude',
      'text-valign': 'top',
      'text-halign': 'center',
      'text-margin-y': -1,
      'font-size': 11,
      'font-weight': 'bold',
      'border-width': 1,
      'min-zoomed-font-size': 5,
    },
  },
  {
    selector: 'node[kind="city_box"]',
    style: { 'background-color': '#0c4a6e', 'background-opacity': 0.08, 'border-color': '#38bdf8', 'border-opacity': 0.5, color: '#7dd3fc' },
  },
  {
    selector: 'node[kind="pop_lane"]',
    style: { 'background-color': '#1e293b', 'background-opacity': 0.45, 'border-color': '#475569', color: '#cbd5e1' },
  },
  {
    selector: 'node[kind="cluster"]',
    style: { 'background-opacity': 0, 'border-style': 'dashed', 'border-color': '#94a3b8', color: '#94a3b8', 'font-weight': 'normal' },
  },
  { selector: 'node[?center]', style: { 'border-color': '#facc15', 'border-width': 4, 'font-weight': 'bold' } },
  { selector: 'node[status="maintenance"], node[status="decommissioned"]', style: { opacity: 0.5 } },
  {
    selector: 'edge',
    style: {
      width: 'data(width)',
      'line-color': 'data(colorHex)',
      'curve-style': 'straight',
      'edge-distances': 'node-position',
      label: '',
      'source-label': 'data(sourceLabel)',
      'target-label': 'data(targetLabel)',
      'source-text-offset': 24,
      'target-text-offset': 24,
      'font-size': END_LABEL_FONT,
      'min-zoomed-font-size': END_LABEL_FONT * END_LABEL_MIN_ZOOM,
      color: '#e2e8f0',
      'text-background-color': '#0f172a',
      'text-background-opacity': 0.9,
      'text-background-padding': '1px',
      opacity: 0.85,
    },
  },
  {
    selector: 'edge[curve="segments"]',
    style: { 'curve-style': 'segments', 'segment-weights': 'data(weights)', 'segment-distances': 'data(distances)' },
  },
  // Only routed edges carry ELK's end points; without them Cytoscape's default (towards the node centre) is right.
  { selector: 'edge[sourceEndpoint]', style: { 'source-endpoint': 'data(sourceEndpoint)', 'target-endpoint': 'data(targetEndpoint)' } },
  { selector: 'edge[?down]', style: { 'line-style': 'dashed', 'line-color': '#ef4444' } },
  { selector: 'edge.labeled, edge:selected', style: { label: 'data(display)', opacity: 1 } },
  { selector: 'node.spof', style: { 'underlay-color': '#f97316', 'underlay-opacity': 0.5, 'underlay-padding': 6 } },
  { selector: 'edge.spof', style: { 'underlay-color': '#f97316', 'underlay-opacity': 0.55, 'underlay-padding': 5 } },
  { selector: 'node.fresh', style: { 'border-color': '#4ade80', 'border-width': 4, 'underlay-color': '#4ade80', 'underlay-opacity': 0.35, 'underlay-padding': 6 } },
  { selector: 'edge.fresh', style: { 'underlay-color': '#4ade80', 'underlay-opacity': 0.45, 'underlay-padding': 4, opacity: 1 } },
  { selector: 'edge.went-down', style: { 'underlay-color': '#ef4444', 'underlay-opacity': 0.55, 'underlay-padding': 5, opacity: 1 } },
  // Problem highlight dims with its own class (the tap highlight clears `.faded` and would undo it). After `.fresh`
  // so it also dims fresh green links; went-down links are problem links and are never `.calm`.
  { selector: '.calm', style: { opacity: 0.12 } },
  { selector: '.faded', style: { opacity: 0.08 } },
  { selector: '.hit', style: { 'border-color': '#facc15', 'border-width': 4 } },
  { selector: 'node:selected', style: { 'overlay-color': '#38bdf8', 'overlay-opacity': 0.25, 'overlay-padding': 4 } },
  // The stylesheet is checked at runtime by Cytoscape; its typings reject `data(...)` mappers on several of these
  // properties, the same mismatch the stylesheet in pages/Topology.tsx works around.
] as unknown as cytoscape.CytoscapeOptions['style']
