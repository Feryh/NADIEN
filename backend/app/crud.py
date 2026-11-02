"""Generic CRUD router factory for inventory tables."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import Base, get_db
from .schemas import partial

_RESERVED_PARAMS = {"q", "skip", "limit"}


def _coerce(column: Any, value: str) -> Any:
    try:
        py_type = column.type.python_type
    except NotImplementedError:
        return value
    if py_type is bool:
        return value.lower() in ("1", "true", "yes")
    try:
        return py_type(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"Invalid value for {column.name}: {value!r}")


def _commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, f"Constraint violation: {exc.orig}") from exc


def crud_router(
    prefix: str,
    model: type[Base],
    create_schema: type[BaseModel],
    read_schema: type[BaseModel],
    tag: str,
    search_field: str | tuple[str, ...] = "name",
) -> APIRouter:
    update_schema = partial(create_schema, f"{create_schema.__name__.removesuffix('Create')}Update")
    columns = {c.name: c for c in model.__table__.columns}
    search_columns = [columns[f] for f in ((search_field,) if isinstance(search_field, str) else search_field) if f in columns]
    router = APIRouter(prefix=prefix, tags=[tag])

    @router.get("", response_model=list[read_schema])
    def list_items(
        request: Request,
        db: Session = Depends(get_db),
        q: str | None = None,
        skip: int = 0,
        limit: int = Query(1000, le=10000),
    ):
        """List records. Any column is an exact-match filter (`?pop_id=3`); commas mean any-of (`?role=p,pe`)."""
        stmt = select(model)
        if q and search_columns:
            stmt = stmt.where(or_(*(c.ilike(f"%{q}%") for c in search_columns)))
        for key, value in request.query_params.items():
            if key in columns and key not in _RESERVED_PARAMS:
                if "," in value:  # ?role=p,pe,bng -> IN (...)
                    stmt = stmt.where(columns[key].in_([_coerce(columns[key], v) for v in value.split(",") if v]))
                else:
                    stmt = stmt.where(columns[key] == _coerce(columns[key], value))
        return db.scalars(stmt.order_by(model.id).offset(skip).limit(limit)).all()

    @router.get("/{item_id}", response_model=read_schema)
    def get_item(item_id: int, db: Session = Depends(get_db)):
        obj = db.get(model, item_id)
        if obj is None:
            raise HTTPException(404, f"{model.__name__} {item_id} not found")
        return obj

    @router.post("", response_model=read_schema, status_code=201)
    def create_item(payload: create_schema, db: Session = Depends(get_db)):
        obj = model(**payload.model_dump())
        db.add(obj)
        _commit(db)
        db.refresh(obj)
        return obj

    @router.patch("/{item_id}", response_model=read_schema)
    def update_item(item_id: int, payload: update_schema, db: Session = Depends(get_db)):
        obj = db.get(model, item_id)
        if obj is None:
            raise HTTPException(404, f"{model.__name__} {item_id} not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(obj, key, value)
        _commit(db)
        db.refresh(obj)
        return obj

    @router.delete("/{item_id}", status_code=204)
    def delete_item(item_id: int, db: Session = Depends(get_db)):
        obj = db.get(model, item_id)
        if obj is None:
            raise HTTPException(404, f"{model.__name__} {item_id} not found")
        db.delete(obj)
        _commit(db)
        return Response(status_code=204)

    return router
