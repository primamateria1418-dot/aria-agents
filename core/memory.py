"""
core/memory.py
ARIA™ — Supabase read/write helpers
OUP International Ltd, 2026
"""

import os
import logging
from supabase import create_client, Client

logger = logging.getLogger("aria.memory")

_client: Client = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_KEY"]
        _client = create_client(url, key)
    return _client


def supabase_insert(table: str, data: dict) -> dict | None:
    try:
        res = get_client().table(table).insert(data).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Insert failed [{table}]: {e}")
        return None


def supabase_select(
    table: str,
    filters: dict = None,
    order_by: str = None,
    limit: int = 100,
    select: str = "*"
) -> list:
    try:
        q = get_client().table(table).select(select)
        if filters:
            for k, v in filters.items():
                q = q.eq(k, v)
        if order_by:
            q = q.order(order_by)
        if limit:
            q = q.limit(limit)
        res = q.execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Select failed [{table}]: {e}")
        return []


def supabase_update(table: str, row_id: str, data: dict) -> dict | None:
    try:
        res = get_client().table(table).update(data).eq("id", row_id).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Update failed [{table}]: {e}")
        return None


def supabase_upsert(table: str, data: dict, on_conflict: str = "id") -> dict | None:
    try:
        res = get_client().table(table).upsert(data, on_conflict=on_conflict).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Upsert failed [{table}]: {e}")
        return None
