"""Initialize the dedicated demo DB, owned by the dedicated demo SQL user."""
import os
import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn, make_dsn
from sera.demo.store import Store


def main():
    dsn=parse_dsn(os.environ['DATABASE_URL'])
    database=dsn['dbname']
    if database!='sera_demo':
        raise RuntimeError('Bootstrap is limited to the dedicated sera_demo database')
    conn=psycopg2.connect(make_dsn(**{**dsn,'dbname':'postgres'}),connect_timeout=10)
    try:
        conn.autocommit=True
        with conn.cursor() as cur:
            cur.execute('SELECT 1 FROM pg_database WHERE datname=%s',(database,))
            if not cur.fetchone():
                # Cloud SQL built-in users normally have CREATEDB. If restricted, an admin
                # must create sera_demo owned by sera_demo; fail visibly rather than rotate users.
                cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    finally:
        conn.close()
    store=Store();store.migrate()
    print('Dedicated demo database ready')


if __name__=='__main__':
    main()
