"""Single place that knows how to reach the database.

Reads DATABASE_URL from the environment (or .env) and hands out pooled
psycopg 3 connections whose rows come back as dicts.
"""
import atexit
import os

from dotenv import load_dotenv
from flask import g
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

load_dotenv()

_pool = None


def database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    return url


def connect_kwargs(url):
    kwargs = {"row_factory": dict_row, "connect_timeout": 15}
    # Supabase transaction pooler (port 6543) does not support prepared statements.
    if ":6543" in url:
        kwargs["prepare_threshold"] = None
    return kwargs


def get_pool():
    global _pool
    if _pool is None:
        url = database_url()
        _pool = ConnectionPool(
            url,
            min_size=1,
            max_size=int(os.environ.get("DB_POOL_SIZE", "4")),
            kwargs=connect_kwargs(url),
            check=ConnectionPool.check_connection,
            open=True,
        )
        atexit.register(close_pool)
    return _pool


def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def connection():
    """Context manager for scripts: `with connection() as conn:` (commits on success)."""
    return get_pool().connection()


def get_db():
    """Connection for the current Flask request; returned to the pool on teardown."""
    if "db" not in g:
        g._db_ctx = get_pool().connection()
        g.db = g._db_ctx.__enter__()
    return g.db


def close_db(exc=None):
    ctx = g.pop("_db_ctx", None)
    g.pop("db", None)
    if ctx is not None:
        # Commits if the request finished cleanly, rolls back otherwise.
        if exc is None:
            ctx.__exit__(None, None, None)
        else:
            ctx.__exit__(type(exc), exc, exc.__traceback__)
