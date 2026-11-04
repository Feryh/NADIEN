"""Generic CRUD router factory for inventory tables."""

from collections.abc import Callable
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


def _duplicate_detail(db: Session, model: type[Base], values: dict[str, Any], own_id: int | None) -> str | None:
    """'code "NDC" sudah dipakai Pop lain (id 142)' for the unique column that collided, if any."""
    for column in model.__table__.columns:
        if not column.unique or column.name not in values:
            continue
        stmt = select(model).where(column == values[column.name])
        if own_id is not None:
            stmt = stmt.where(model.id != own_id)
        other = db.scalars(stmt).first()
        if other is not None:
            return f'{column.name} "{values[column.name]}" sudah dipakai {model.__name__} lain (id {other.id})'
    return None


def _commit(db: Session, model: type[Base] | None = None, values: dict[str, Any] | None = None,
            own_id: int | None = None) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        detail = _duplicate_detail(db, model, values or {}, own_id) if model is not None else None
        raise HTTPException(409, detail or f"Constraint violation: {exc.orig}") from exc


# Called before an update/delete with (object, changed fields or None for delete); raises HTTPException to refuse.
Guard = Callable[[Any, dict[str, Any] | None], None]


def crud_router(
    prefix: str,
    model: type[Base],
    create_schema: type[BaseModel],
    read_schema: type[BaseModel],
    tag: str,
    search_field: str | tuple[str, ...] = "name",
    guard: Guard | None = None,
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
        values = payload.model_dump()
        obj = model(**values)
        db.add(obj)
        _commit(db, model, values)
        db.refresh(obj)
        return obj

    @router.patch("/{item_id}", response_model=read_schema)
    def update_item(item_id: int, payload: update_schema, db: Session = Depends(get_db)):
        obj = db.get(model, item_id)
        if obj is None:
            raise HTTPException(404, f"{model.__name__} {item_id} not found")
        changes = payload.model_dump(exclude_unset=True)
        if guard:
            guard(obj, changes)
        for key, value in changes.items():
            setattr(obj, key, value)
        _commit(db, model, changes, item_id)
        db.refresh(obj)
        return obj

    @router.delete("/{item_id}", status_code=204)
    def delete_item(item_id: int, db: Session = Depends(get_db)):
        obj = db.get(model, item_id)
        if obj is None:
            raise HTTPException(404, f"{model.__name__} {item_id} not found")
        if guard:
            guard(obj, None)
        db.delete(obj)
        _commit(db)
        return Response(status_code=204)

    return router
