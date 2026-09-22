import os
import unittest
from pathlib import Path

import db


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.getenv("RUN_DB_INTEGRATION_TESTS") == "1",
    "requer PostgreSQL real e RUN_DB_INTEGRATION_TESTS=1",
)
class TestCanonicalIdentityPostgres(unittest.TestCase):
    def test_migration_and_constraints_contract(self):
        db.validate_database_config()
        if db.psycopg is None:
            self.skipTest("psycopg nao instalado")

        migration = (ROOT / "sql" / "migrate_canonical_identity.sql").read_text(
            encoding="utf-8"
        )
        fixture = (
            ROOT / "tests" / "postgres" / "canonical_identity_fixture.sql"
        ).read_text(encoding="utf-8")

        with db.psycopg.connect(db.get_database_url(), autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(migration)
                while cursor.nextset():
                    pass
                cursor.execute(migration)
                while cursor.nextset():
                    pass
                cursor.execute(fixture)
                while cursor.nextset():
                    pass


if __name__ == "__main__":
    unittest.main()
