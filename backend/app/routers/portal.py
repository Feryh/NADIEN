from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..database import get_db
from ..services.auth import CurrentUser, current_user

# Customer accounts: their own services only, read-only. The guard keeps every other /api path away from them.
router = APIRouter(prefix="/portal", tags=["Customer Portal"])


def _customer(db: Session, me: CurrentUser) -> m.Customer:
    customer = db.get(m.Customer, me.customer_id) if me.customer_id else None
    if customer is None:
        raise HTTPException(404, "Akun ini belum dihubungkan ke data customer.")
    return customer


@router.get("/services")
def my_services(db: Session = Depends(get_db), me: CurrentUser = Depends(current_user)):
    customer = _customer(db, me)
    pops = dict(db.execute(select(m.Pop.id, m.Pop.code)).tuples().all())
    devices = {d.id: d for d in db.execute(select(m.Device.id, m.Device.pop_id))}
    services = db.scalars(select(m.Service).where(m.Service.customer_id == customer.id).order_by(m.Service.circuit_id))

    def pop(pop_id: int | None, device_id: int | None) -> str | None:
        if pop_id is None and device_id in devices:
            pop_id = devices[device_id].pop_id
        return pops.get(pop_id)

    rows = [{"id": s.id, "circuit_id": s.circuit_id, "service_type": s.service_type, "bandwidth_mbps": s.bandwidth_mbps,
             "status": s.status, "protection": s.protection, "vlan_id": s.vlan_id,
             "a_pop": pop(s.a_pop_id, s.a_device_id), "b_pop": pop(s.b_pop_id, s.b_device_id)} for s in services]
    return {"customer": {"name": customer.name, "account_no": customer.account_no}, "services": rows}
