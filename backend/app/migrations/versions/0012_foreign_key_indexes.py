"""indexes on foreign keys that are deleted through or filtered on

Revision ID: 0012
Revises: 0011

Deleting an interface sets links.a_interface_id / b_interface_id to NULL: without an index each deleted row
scans all links, and a sync can remove many ports at once. The others are the same case on smaller tables.
"""

from alembic import op


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None

INDEXES = (
    ('ix_interfaces_parent_id', 'interfaces', 'parent_id'),
    ('ix_links_a_interface', 'links', 'a_interface_id'),
    ('ix_links_b_interface', 'links', 'b_interface_id'),
    ('ix_services_customer_id', 'services', 'customer_id'),
    ('ix_services_a_device_id', 'services', 'a_device_id'),
    ('ix_services_b_device_id', 'services', 'b_device_id'),
    ('ix_service_paths_service_id', 'service_paths', 'service_id'),
    ('ix_topology_events_link_id', 'topology_events', 'link_id'),
    ('ix_topology_events_device_id', 'topology_events', 'device_id'),
)


def upgrade() -> None:
    for name, table, column in INDEXES:
        op.create_index(name, table, [column])


def downgrade() -> None:
    for name, table, _column in INDEXES:
        op.drop_index(name, table_name=table)
