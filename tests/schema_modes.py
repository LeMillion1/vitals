"""The two schemas this suite builds, and why there are two.

The application can no longer write a row without its owner, and the models say
so. Two kinds of test still need to write one: the ownership backfill services,
whose input *is* an unstamped row, and the legacy-bridge readers that pin what a
scoped reader does while a rolling backfill is only half done.

Rather than weaken the models for everyone, those modules ask for the older
schema — by ``@pytest.mark.pre_ownership_contract`` when they use the shared
``db_session``, or by calling :func:`pre_ownership_contract_metadata` directly
when they build an engine of their own.
"""

from __future__ import annotations

from contextlib import contextmanager

import vitals.models  # noqa: F401 -- register all tables on Base.metadata
from vitals.models.base import Base
from vitals.ownership import required_ownership_columns


@contextmanager
def pre_ownership_contract_metadata():
    """Relax the mandatory ownership columns for the length of one ``create_all``.

    The flags are restored in ``finally`` so a failure inside ``create_all``
    cannot leave the shared metadata describing a schema nothing else wants.
    """

    columns = [
        Base.metadata.tables[table_name].columns[column_name]
        for table_name, column_name in required_ownership_columns()
        if column_name in Base.metadata.tables[table_name].columns
    ]
    relaxed = [column for column in columns if not column.nullable]
    for column in relaxed:
        column.nullable = True
    try:
        yield
    finally:
        for column in relaxed:
            column.nullable = False
