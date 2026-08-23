"""
Database access layer.

Reads connection settings from environment variables so the same code
works locally and in the deployment pipeline. Never hard-code credentials.
"""
import os
import threading
from contextlib import contextmanager
import psycopg2
import psycopg2.extras
import psycopg2.pool

psycopg2.extras.register_uuid()

# Lazily-created pool (not at import time) so importing this module without a
# reachable DB still works — matches the previous behaviour where get_conn()
# only ever touched the DB when actually called, not on import.
_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                # ThreadedConnectionPool.getconn() raises immediately when
                # exhausted rather than queuing — keep this >= the sync
                # thread-pool size (see main.py's _tune_sync_thread_pool) so
                # the DB pool is never a tighter, error-raising bottleneck
                # than the thread-pool queue already in front of it.
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    int(os.getenv("DB_POOL_MIN", "2")),
                    int(os.getenv("DB_POOL_MAX", "80")),
                    host=os.getenv("DB_HOST", "localhost"),
                    port=os.getenv("DB_PORT", "5432"),
                    dbname=os.getenv("DB_NAME", "oneclickhire"),
                    user=os.getenv("DB_USER", "postgres"),
                    password=os.getenv("DB_PASSWORD", ""),
                    cursor_factory=psycopg2.extras.RealDictCursor,
                )
    return _pool


def get_conn():
    return _get_pool().getconn()


def _release_conn(conn, broken=False):
    # A connection that errored with a connection-level failure (not just a
    # bad query) must be discarded rather than handed to the next borrower —
    # putconn() without close=True would return a dead connection to the
    # pool and the next query on it would fail immediately.
    _get_pool().putconn(conn, close=broken)


def query(sql, params=None, fetch=True):
    conn = get_conn()
    broken = False
    try:
        with conn.cursor() as cur:
            # Only pass params to execute() when there actually are some.
            # psycopg2 scans the SQL for %s-style placeholders whenever a
            # params argument is given (even []), so raw SQL containing a
            # literal '%s'-like substring (e.g. ILIKE '%source%') raises
            # "IndexError: list index out of range" if we always pass [].
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            if fetch:
                rows = cur.fetchall()
                conn.commit()
                return rows
            conn.commit()
            return None
    except psycopg2.OperationalError:
        broken = True
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        _release_conn(conn, broken)


def query_one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


@contextmanager
def transaction():
    """Hold ONE pooled connection across multiple statements, commit once
    on clean exit, roll back on any exception. Use for multi-write
    sequences that must be atomic. Inside the block, use tx_exec(cur, ...)
    for each statement. Example:
        with transaction() as cur:
            tx_exec(cur, "UPDATE ...", [...])
            tx_exec(cur, "INSERT ...", [...])
    """
    conn = get_conn()
    broken = False
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except psycopg2.OperationalError:
        broken = True
        try: conn.rollback()
        except Exception: pass
        raise
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        _release_conn(conn, broken)


def tx_exec(cur, sql, params=None):
    """Execute one statement on an existing transaction cursor, mirroring
    query()'s param guard (psycopg2 scans for %s whenever params is passed,
    so only pass when truthy). Returns fetched rows if the statement returns
    any (RETURNING / SELECT), else None."""
    if params:
        cur.execute(sql, params)
    else:
        cur.execute(sql)
    if cur.description:   # statement returned rows (SELECT / RETURNING)
        return cur.fetchall()
    return None
