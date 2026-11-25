"""Run the existing full workflow test against a disposable PostgreSQL schema."""
from __future__ import annotations
import os
import secrets
import unittest
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

def main() -> None:
    url = os.environ["DATABASE_URL"]
    namespace = "santa_qa_" + secrets.token_hex(8)
    with psycopg.connect(url, autocommit=True) as control:
        control.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
        try:
            import app
            app.PG = make_conninfo(url, options="-c search_path=" + namespace)
            # TestClient's local HTTP transport is not public; production cookie policy is unchanged.
            app.PROD = False
            import test_app
            suite = unittest.defaultTestLoader.loadTestsFromModule(test_app)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            if not result.wasSuccessful():
                raise RuntimeError("Hosted PostgreSQL workflow failed")
            print("SANTA_POSTGRES_PREFLIGHT_PASS", flush=True)
        finally:
            control.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))

if __name__ == "__main__":
    main()
