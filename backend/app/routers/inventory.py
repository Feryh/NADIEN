from fastapi import APIRouter

from .. import models as m
from .. import schemas as s
from ..crud import crud_router

router = APIRouter()

# (prefix, model, create schema, read schema, tag, search field)
_ENTITIES = [
    ("/pops", m.Pop, s.PopCreate, s.PopRead, "Node Inventory", "name"),
    ("/devices", m.Device, s.DeviceCreate, s.DeviceRead, "Node Inventory", "hostname"),
    ("/dwdm-nodes", m.DwdmNode, s.DwdmNodeCreate, s.DwdmNodeRead, "External: Node", "name"),
    ("/odfs", m.Odf, s.OdfCreate, s.OdfRead, "External: Node", "name"),
    ("/patch-panels", m.PatchPanel, s.PatchPanelCreate, s.PatchPanelRead, "External: Node", "name"),
    ("/links", m.Link, s.LinkCreate, s.LinkRead, "Link Inventory", "name"),
    ("/fibers", m.Fiber, s.FiberCreate, s.FiberRead, "External: Link", "name"),
    ("/wavelengths", m.DwdmWavelength, s.DwdmWavelengthCreate, s.DwdmWavelengthRead, "External: Link", "name"),
    ("/vendors", m.Vendor, s.VendorCreate, s.VendorRead, "Device Inventory", "name"),
    ("/device-models", m.DeviceModel, s.DeviceModelCreate, s.DeviceModelRead, "Device Inventory", "name"),
    ("/software", m.SoftwareVersion, s.SoftwareVersionCreate, s.SoftwareVersionRead, "Device Inventory", "version"),
    ("/interfaces", m.Interface, s.InterfaceCreate, s.InterfaceRead, "Device Inventory", "name"),
    ("/customers", m.Customer, s.CustomerCreate, s.CustomerRead, "Customer Database", "name"),
    ("/services", m.Service, s.ServiceCreate, s.ServiceRead, "Customer Database", "circuit_id"),
]

for prefix, model, create, read, tag, search in _ENTITIES:
    router.include_router(crud_router(prefix, model, create, read, tag, search))
