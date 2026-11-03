import { useParams, NavLink, Navigate } from 'react-router-dom'
import DeviceSummary from '../components/DeviceSummary'
import ResourceTable from '../components/ResourceTable'
import { ENTITIES, ENTITY_BY_KEY, GROUPS } from '../entities'

export default function Inventory() {
  const { entityKey } = useParams()
  const entity = entityKey ? ENTITY_BY_KEY[entityKey] : undefined
  if (!entity) return <Navigate to={`/inventory/${ENTITIES[0].key}`} replace />

  return (
    <div className="inventory">
      <nav className="subnav">
        {GROUPS.map((group) => (
          <div key={group} className="subnav-group">
            <div className="subnav-title">{group}</div>
            {ENTITIES.filter((e) => e.group === group).map((e) => (
              <NavLink key={e.key} to={`/inventory/${e.key}`}>
                {e.label}
                {e.external && <span className="ext-dot" title="External / API-fed" />}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>
      <div className="inventory-main">
        {entity.endpoint === '/devices' && <DeviceSummary />}
        <ResourceTable key={entity.key} entity={entity} />
      </div>
    </div>
  )
}
