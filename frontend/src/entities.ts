// Inventory screens are generated from these definitions; each maps 1:1 to a backend CRUD endpoint.

export type FieldType = 'text' | 'number' | 'bool' | 'select' | 'ref' | 'json' | 'textarea' | 'datetime'

export interface Field {
  name: string
  label: string
  type?: FieldType
  options?: string[]
  ref?: string // entity key whose rows populate the dropdown
  required?: boolean
  readonly?: boolean // shown in the table, never sent in the form
  hideInTable?: boolean
  blankDefault?: boolean // optional select that starts empty on "Tambah" instead of the first option
}

// Standard areas (same list as backend services/areas.py AREAS); a POP may also carry a custom one.
export const AREA_NAMES = [
  'Sumatera', 'Jabodetabek', 'Jawa Barat & Banten', 'Jawa Tengah & DIY', 'Jawa Timur',
  'Bali & Nusa Tenggara', 'Kalimantan', 'Sulawesi', 'Maluku & Papua', 'Internasional',
]

export interface Entity {
  key: string
  label: string
  endpoint: string
  display: string // field used when other entities reference this one
  group: string
  external?: boolean
  query?: string // fixed filter appended to list requests, e.g. "role=p,pe,bng,dci"
  fields: Field[]
}

const externalFields: Field[] = [
  { name: 'source_system', label: 'Source System' },
  { name: 'external_id', label: 'External ID' },
  { name: 'attributes', label: 'Attributes (JSON)', type: 'json', hideInTable: true },
  { name: 'synced_at', label: 'Synced', type: 'datetime', readonly: true },
]

export const ENTITIES: Entity[] = [
  // ---------------------------------------------------------------- Node Inventory
  {
    key: 'pops', label: 'Metro POP', endpoint: '/pops', display: 'code', group: 'Node Inventory',
    fields: [
      { name: 'code', label: 'Code', required: true },
      { name: 'name', label: 'Name', required: true },
      { name: 'tier', label: 'Tier', type: 'select', options: ['core', 'aggregation', 'access'] },
      { name: 'region', label: 'Area', type: 'select', options: AREA_NAMES, blankDefault: true },
      { name: 'city', label: 'City' },
      { name: 'address', label: 'Address', type: 'textarea', hideInTable: true },
      { name: 'latitude', label: 'Lat', type: 'number' },
      { name: 'longitude', label: 'Lon', type: 'number' },
      { name: 'status', label: 'Status', type: 'select', options: ['active', 'planned', 'decommissioned'] },
    ],
  },
  {
    key: 'devices', label: 'Router', endpoint: '/devices', display: 'hostname', group: 'Node Inventory',
    fields: [
      { name: 'hostname', label: 'Hostname', required: true },
      { name: 'pop_id', label: 'POP', type: 'ref', ref: 'pops', required: true },
      // Set by itself when the POP is changed here; sync and "Rapikan POP" never move a locked device.
      { name: 'pop_locked', label: 'POP dikunci (diatur manual)', type: 'bool', hideInTable: true },
      { name: 'role', label: 'Role', type: 'select', options: ['p', 'pe', 'agg', 'switch', 'csr', 'access', 'dci', 'olt', 'bng'] },
      { name: 'vendor_id', label: 'Vendor', type: 'ref', ref: 'vendors' },
      { name: 'model_id', label: 'Model', type: 'ref', ref: 'device-models' },
      { name: 'software_id', label: 'Software', type: 'ref', ref: 'software' },
      { name: 'mgmt_ip', label: 'Mgmt IP' },
      { name: 'loopback_ipv4', label: 'Loopback' },
      { name: 'isis_net', label: 'IS-IS NET', hideInTable: true },
      { name: 'srv6_locator', label: 'SRv6 Locator' },
      { name: 'status', label: 'Status', type: 'select', options: ['active', 'planned', 'maintenance', 'decommissioned'] },
    ],
  },
  {
    key: 'dwdm-nodes', label: 'DWDM', endpoint: '/dwdm-nodes', display: 'name', group: 'Node Inventory', external: true,
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'pop_id', label: 'POP', type: 'ref', ref: 'pops' },
      { name: 'vendor', label: 'Vendor' },
      { name: 'model', label: 'Model' },
      { name: 'node_type', label: 'Type', type: 'select', options: ['roadm', 'ila', 'terminal'] },
      { name: 'status', label: 'Status' },
      ...externalFields,
    ],
  },
  {
    key: 'odfs', label: 'ODF', endpoint: '/odfs', display: 'name', group: 'Node Inventory', external: true,
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'pop_id', label: 'POP', type: 'ref', ref: 'pops' },
      { name: 'rack', label: 'Rack' },
      { name: 'port_count', label: 'Ports', type: 'number' },
      { name: 'ports_used', label: 'Used', type: 'number' },
      ...externalFields,
    ],
  },
  {
    key: 'patch-panels', label: 'Patch Panel', endpoint: '/patch-panels', display: 'name', group: 'Node Inventory', external: true,
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'pop_id', label: 'POP', type: 'ref', ref: 'pops' },
      { name: 'odf_id', label: 'ODF', type: 'ref', ref: 'odfs' },
      { name: 'rack', label: 'Rack' },
      { name: 'connector_type', label: 'Connector', type: 'select', options: ['LC', 'SC', 'MPO'] },
      { name: 'port_count', label: 'Ports', type: 'number' },
      { name: 'ports_used', label: 'Used', type: 'number' },
      ...externalFields,
    ],
  },
  // ---------------------------------------------------------------- Link Inventory
  {
    key: 'links', label: 'Metro-E / Bundle', endpoint: '/links', display: 'name', group: 'Link Inventory',
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'link_type', label: 'Type', type: 'select', options: ['metro_e', 'bundle'] },
      { name: 'a_device_id', label: 'A Device', type: 'ref', ref: 'devices', required: true },
      { name: 'a_interface_id', label: 'A Interface', type: 'ref', ref: 'interfaces', hideInTable: true },
      { name: 'b_device_id', label: 'B Device', type: 'ref', ref: 'devices', required: true },
      { name: 'b_interface_id', label: 'B Interface', type: 'ref', ref: 'interfaces', hideInTable: true },
      { name: 'capacity_gbps', label: 'Capacity G', type: 'number' },
      { name: 'current_gbps', label: 'Current G', type: 'number' },
      { name: 'reserved_gbps', label: 'Reserved G', type: 'number' },
      { name: 'growth_gbps_per_month', label: 'Growth G/mo', type: 'number' },
      { name: 'latency_ms', label: 'Latency ms', type: 'number' },
      { name: 'distance_km', label: 'km', type: 'number', hideInTable: true },
      { name: 'status', label: 'Status', type: 'select', options: ['up', 'down', 'planned'] },
      { name: 'discovery_source', label: 'Source', type: 'select', options: ['lldp', 'config', 'manual'] },
      { name: 'fiber_id', label: 'Fiber', type: 'ref', ref: 'fibers', hideInTable: true },
      { name: 'wavelength_id', label: 'Wavelength', type: 'ref', ref: 'wavelengths', hideInTable: true },
    ],
  },
  {
    key: 'fibers', label: 'Fiber', endpoint: '/fibers', display: 'name', group: 'Link Inventory', external: true,
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'a_pop_id', label: 'A POP', type: 'ref', ref: 'pops' },
      { name: 'b_pop_id', label: 'B POP', type: 'ref', ref: 'pops' },
      { name: 'length_km', label: 'km', type: 'number' },
      { name: 'core_count', label: 'Cores', type: 'number' },
      { name: 'cores_used', label: 'Used', type: 'number' },
      { name: 'cable_type', label: 'Cable', type: 'select', options: ['aerial', 'underground', 'submarine'] },
      { name: 'attenuation_db', label: 'dB', type: 'number', hideInTable: true },
      ...externalFields,
    ],
  },
  {
    key: 'wavelengths', label: 'DWDM Wavelength', endpoint: '/wavelengths', display: 'name', group: 'Link Inventory', external: true,
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'a_dwdm_node_id', label: 'A Node', type: 'ref', ref: 'dwdm-nodes' },
      { name: 'b_dwdm_node_id', label: 'B Node', type: 'ref', ref: 'dwdm-nodes' },
      { name: 'fiber_id', label: 'Fiber', type: 'ref', ref: 'fibers' },
      { name: 'channel', label: 'Channel' },
      { name: 'frequency_thz', label: 'THz', type: 'number' },
      { name: 'rate_gbps', label: 'Rate G', type: 'number' },
      { name: 'status', label: 'Status' },
      ...externalFields,
    ],
  },
  // ---------------------------------------------------------------- Device Inventory
  {
    key: 'vendors', label: 'Vendor', endpoint: '/vendors', display: 'name', group: 'Device Inventory',
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'os_family', label: 'OS Family' },
    ],
  },
  {
    key: 'device-models', label: 'Model', endpoint: '/device-models', display: 'name', group: 'Device Inventory',
    fields: [
      { name: 'vendor_id', label: 'Vendor', type: 'ref', ref: 'vendors', required: true },
      { name: 'name', label: 'Model', required: true },
      { name: 'device_type', label: 'Type', type: 'select', options: ['router', 'switch', 'dwdm', 'other'] },
      { name: 'max_throughput_gbps', label: 'Max Gbps', type: 'number' },
    ],
  },
  {
    key: 'software', label: 'Software', endpoint: '/software', display: 'version', group: 'Device Inventory',
    fields: [
      { name: 'vendor_id', label: 'Vendor', type: 'ref', ref: 'vendors', required: true },
      { name: 'version', label: 'Version', required: true },
      { name: 'is_recommended', label: 'Recommended', type: 'bool' },
      { name: 'supports_srv6', label: 'SRv6', type: 'bool' },
      { name: 'notes', label: 'Notes', type: 'textarea', hideInTable: true },
    ],
  },
  {
    key: 'interfaces', label: 'Interface', endpoint: '/interfaces', display: 'name', group: 'Device Inventory',
    fields: [
      { name: 'device_id', label: 'Device', type: 'ref', ref: 'devices', required: true },
      { name: 'name', label: 'Name', required: true },
      { name: 'if_type', label: 'Type', type: 'select', options: ['physical', 'bundle', 'subinterface'] },
      { name: 'parent_id', label: 'Bundle', type: 'ref', ref: 'interfaces' },
      { name: 'speed_gbps', label: 'Speed G', type: 'number' },
      { name: 'mtu', label: 'MTU', type: 'number' },
      { name: 'admin_status', label: 'Admin', type: 'select', options: ['up', 'down'] },
      { name: 'oper_status', label: 'Oper', type: 'select', options: ['up', 'down'] },
      { name: 'description', label: 'Description' },
      { name: 'ipv4_address', label: 'IPv4', hideInTable: true },
      { name: 'optic_type', label: 'Optic', hideInTable: true },
    ],
  },
  // ---------------------------------------------------------------- Customer Database
  {
    key: 'customers', label: 'Customer', endpoint: '/customers', display: 'name', group: 'Customer Database',
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'account_no', label: 'Account', required: true },
      { name: 'segment', label: 'Segment', type: 'select', options: ['enterprise', 'wholesale', 'carrier', 'government', 'retail'] },
      { name: 'contact_email', label: 'Email' },
    ],
  },
  {
    key: 'services', label: 'Service', endpoint: '/services', display: 'circuit_id', group: 'Customer Database',
    fields: [
      { name: 'circuit_id', label: 'Circuit ID', required: true },
      { name: 'customer_id', label: 'Customer', type: 'ref', ref: 'customers', required: true },
      { name: 'service_type', label: 'Type', type: 'select', options: ['internet', 'iplc', 'l2vpn', 'l3vpn'], required: true },
      { name: 'bandwidth_mbps', label: 'Mbps', type: 'number', required: true },
      { name: 'a_pop_id', label: 'A POP', type: 'ref', ref: 'pops' },
      { name: 'a_device_id', label: 'A Device', type: 'ref', ref: 'devices' },
      { name: 'a_interface', label: 'A Interface', hideInTable: true },
      { name: 'b_pop_id', label: 'B POP', type: 'ref', ref: 'pops' },
      { name: 'b_device_id', label: 'B Device', type: 'ref', ref: 'devices', hideInTable: true },
      { name: 'b_interface', label: 'B Interface', hideInTable: true },
      { name: 'vlan_id', label: 'VLAN', type: 'number' },
      { name: 'vrf_name', label: 'VRF' },
      { name: 'route_distinguisher', label: 'RD', hideInTable: true },
      { name: 'route_target', label: 'RT', hideInTable: true },
      { name: 'protection', label: 'Protection', type: 'select', options: ['none', '1+1', '1:1'] },
      { name: 'status', label: 'Status', type: 'select', options: ['planned', 'reserved', 'active', 'terminated'] },
      { name: 'notes', label: 'Notes', type: 'textarea', hideInTable: true },
    ],
  },
  // ---------------------------------------------------------------- Integrations
  {
    key: 'integration-sources', label: 'API Sources', endpoint: '/integrations/sources', display: 'name', group: 'Integrations',
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'system_type', label: 'System', type: 'select', options: ['nms', 'inventory', 'dwdm_ems', 'oss'], required: true },
      { name: 'target_entity', label: 'Feeds', type: 'select', options: ['devices', 'dwdm_nodes', 'odfs', 'patch_panels', 'fibers', 'dwdm_wavelengths'], required: true },
      { name: 'base_url', label: 'Base URL' },
      { name: 'auth_type', label: 'Auth', type: 'select', options: ['none', 'token', 'basic', 'oauth2'] },
      { name: 'enabled', label: 'Enabled', type: 'bool' },
      { name: 'last_sync_at', label: 'Last Sync', type: 'datetime', readonly: true },
      { name: 'last_status', label: 'Last Status', readonly: true },
      { name: 'notes', label: 'Notes', type: 'textarea', hideInTable: true },
    ],
  },
]

// Device views split by category (same /devices endpoint, filtered by role).
const deviceEntity = ENTITIES.find((e) => e.key === 'devices') as Entity
const DEVICE_CATEGORIES: [string, string, string][] = [
  ['devices-router', 'Router (P/PE/BNG/DCI)', 'p,pe,bng,dci'],
  ['devices-metro', 'Metro / Aggregation', 'agg'],
  ['devices-switch', 'Switch (DS)', 'switch'],
  ['devices-csr', 'CSR (Cell Site Router)', 'csr'],
  ['devices-olt', 'OLT', 'olt'],
  ['devices-access', 'Access / Switch', 'access'],
]
ENTITIES.splice(
  1,
  0,
  ...DEVICE_CATEGORIES.map(([key, label, roles]) => ({
    ...deviceEntity,
    key,
    label,
    group: 'Perangkat per Kategori',
    query: `role=${roles}`,
  })),
)

export const ENTITY_BY_KEY: Record<string, Entity> = Object.fromEntries(ENTITIES.map((e) => [e.key, e]))
export const GROUPS = [...new Set(ENTITIES.map((e) => e.group))]
