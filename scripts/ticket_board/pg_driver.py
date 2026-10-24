"""psycopg where this host has it; on a host installed for Oracle alone, a stand-in that refuses when used (SYRD-645).

The Oracle notification listener imports `notify_listener` for its pane helpers, until SYRD-649 moves them out.
That module's PostgreSQL code never runs there. But it, and execution_lease_pass (SYRD-616) through it, imported
psycopg at their tops, and psycopg is deliberately absent from an Oracle-only install, so the Oracle listener could
not start on such a host.
"""

from __future__ import annotations

try:
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb
except ImportError:

    class PsycopgAbsent(Exception):
        """There is no PostgreSQL driver here: this host's ticket boards are on Oracle."""

    class _Psycopg:
        Error = PsycopgAbsent

        @staticmethod
        def connect(*args, **kwargs):
            raise PsycopgAbsent("psycopg is not installed: this host was installed for Oracle ticket boards only")

    psycopg = _Psycopg()
    sql = None
    Jsonb = None
