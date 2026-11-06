"""Limits PostgreSQL enforces and the SQLite test database does not."""

from sqlalchemy import BigInteger

from app import models as m


def test_should_store_if_index_above_signed_32_bit_when_observium_reports_one():
    assert isinstance(m.ObserviumPort.__table__.c.if_index.type, BigInteger)
