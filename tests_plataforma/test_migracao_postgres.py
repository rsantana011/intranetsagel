"""Testes offline: nunca usam o .env nem abrem conexão PostgreSQL."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import shutil
import uuid
import types
import unittest
from unittest.mock import patch

import migrarpostgres as migration


class MigrationTests(unittest.TestCase):
    def source(self, statements):
        folder = Path(__file__).resolve().parent / ('migration_test_' + uuid.uuid4().hex)
        folder.mkdir()
        def cleanup():
            assert folder.resolve().parent == Path(__file__).resolve().parent and folder.name.startswith('migration_test_')
            shutil.rmtree(folder)
        self.addCleanup(cleanup)
        path = folder / 'source.db'
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.executescript(statements)
        source = migration.open_source(path)
        self.addCleanup(source.close)
        return source

    def test_source_snapshot_is_read_only_and_preserves_deleted_id_ceiling(self):
        source = self.source('''CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE);
            INSERT INTO users(id,name) VALUES(42,'preserved'); DELETE FROM users;
            INSERT INTO users(name) VALUES('next');''')
        with self.assertRaises(sqlite3.OperationalError):
            source.execute("INSERT INTO users(name) VALUES('forbidden')")
        inventory = migration.source_inventory(source)
        self.assertEqual(inventory['sequences']['users'], 43)
        self.assertEqual(inventory['tables']['users']['count'], 1)
        self.assertTrue(inventory['integrity_ok'])

    def test_source_inventory_keeps_partial_and_case_insensitive_unique_indexes(self):
        source = self.source('''CREATE TABLE vehicles(id INTEGER PRIMARY KEY AUTOINCREMENT,plate TEXT UNIQUE COLLATE NOCASE,active INTEGER);
            CREATE UNIQUE INDEX plate_active ON vehicles(plate COLLATE NOCASE) WHERE active=1;''')
        inventory = migration.source_inventory(source)
        indexes = inventory['tables']['vehicles']['indexes']
        self.assertEqual(len(indexes), 2)
        self.assertTrue(all(i['unique'] for i in indexes))
        self.assertTrue(all(i['columns'][0]['coll'] == 'NOCASE' for i in indexes))
        self.assertIn('WHERE', next(i['sql'] for i in indexes if i['sql']))

    def test_conversion_preserves_literals_operators_numbers_and_precision(self):
        actual = migration.convert_ddl("CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT, n INTEGER CHECK(n>=10000 AND n<>12), x REAL DEFAULT 12.50, raw BLOB, label TEXT DEFAULT 'INTEGER ? AUTOINCREMENT COLLATE NOCASE;');")
        self.assertIn('BIGSERIAL PRIMARY KEY', actual)
        self.assertIn('n BIGINT', actual)
        self.assertIn('n >= 10000 AND n <> 12', actual)
        self.assertIn('DOUBLE PRECISION DEFAULT 12.50', actual)
        self.assertIn('raw BYTEA', actual)
        self.assertIn("'INTEGER ? AUTOINCREMENT COLLATE NOCASE;'", actual)

    def test_conversion_of_case_insensitive_partial_index(self):
        actual = migration.convert_ddl('CREATE UNIQUE INDEX x ON users(email COLLATE NOCASE) WHERE email!=\'\';', index=True)
        self.assertIn('lower(email)', actual)
        self.assertIn("WHERE email != ''", actual)

    def test_unsupported_collation_refuses_instead_of_changing_semantics(self):
        with self.assertRaises(migration.MigrationError):
            migration.convert_ddl('CREATE TABLE x(name TEXT COLLATE custom)')

    def test_foreign_key_orphans_block_migration(self):
        source = self.source('''CREATE TABLE parent(id INTEGER PRIMARY KEY AUTOINCREMENT);
            CREATE TABLE child(id INTEGER PRIMARY KEY AUTOINCREMENT,parent_id INTEGER REFERENCES parent(id));
            INSERT INTO child(parent_id) VALUES(999);''')
        inventory = migration.source_inventory(source)
        self.assertEqual(inventory['foreign_key_errors'], 1)
        with self.assertRaises(migration.MigrationError):
            migration.validate_source(inventory)

    def test_view_or_trigger_cannot_be_silently_omitted(self):
        source = self.source('CREATE TABLE t(id INTEGER); CREATE VIEW v AS SELECT id FROM t;')
        with self.assertRaises(migration.MigrationError):
            migration.validate_source(migration.source_inventory(source))

    def test_topological_order_and_cycles(self):
        tables = {'child': {'foreign_keys': [{'table': 'parent'}]}, 'parent': {'foreign_keys': []}}
        self.assertEqual(migration.table_order(tables), ['parent', 'child'])
        tables['parent']['foreign_keys'].append({'table': 'child'})
        with self.assertRaises(migration.MigrationError):
            migration.table_order(tables)

    def test_fingerprint_detects_content_drift_and_duplicate_counts(self):
        source = self.source('CREATE TABLE t(id INTEGER, value TEXT); INSERT INTO t VALUES(1,\'a\'),(2,\'b\'),(2,\'b\');')
        original = migration.table_fingerprint(source.execute('SELECT id,value FROM t ORDER BY id'))
        reversed_rows = migration.table_fingerprint(source.execute('SELECT id,value FROM t ORDER BY id DESC'))
        modified = migration.table_fingerprint(source.execute("SELECT id,CASE WHEN id=1 THEN 'x' ELSE value END FROM t"))
        distinct = migration.table_fingerprint(source.execute('SELECT DISTINCT id,value FROM t'))
        self.assertEqual(original, reversed_rows)
        self.assertNotEqual(original, modified)
        self.assertNotEqual(original, distinct)

    def test_schema_identifiers_are_validated(self):
        for name in ('public', 'migration_20261007'):
            self.assertEqual(migration.validate_schema(name), name)
        for name in ('public;DROP SCHEMA public', 'pg_catalog', 'information_schema', 'x.y', 'a' * 64):
            with self.subTest(name=name), self.assertRaises(migration.MigrationError):
                migration.validate_schema(name)

    def test_existing_schema_is_refused_without_a_mutation(self):
        statements = []
        class FakeConnection:
            def execute(self, statement, parameters):
                statements.append(statement)
                return types.SimpleNamespace(fetchone=lambda: (777,) if len(statements) == 1 else (1,))
        module = types.SimpleNamespace(sql=object())
        with patch.dict('sys.modules', {'psycopg': module}):
            with self.assertRaises(migration.MigrationError):
                migration.ensure_empty_destination(FakeConnection(), 'public')
        self.assertTrue(all(statement.lstrip().startswith('SELECT') for statement in statements))

    def test_main_defaults_to_read_only_and_redacts_connection_errors(self):
        source = self.source('CREATE TABLE t(id INTEGER);')
        with patch.object(migration, 'open_source', return_value=source), patch.object(migration, 'connect_postgres', side_effect=RuntimeError('password=DO_NOT_PRINT')) as connect, contextlib.redirect_stdout(io.StringIO()) as output:
            result = migration.main([])
        connect.assert_called_once_with(read_only=True)
        self.assertEqual(result, 2)
        self.assertNotIn('DO_NOT_PRINT', output.getvalue())

    def test_import_has_no_connection_side_effects(self):
        with patch.object(sqlite3, 'connect', side_effect=AssertionError('Import must not connect')):
            spec = importlib.util.spec_from_file_location('migration_import_test', Path(migration.__file__))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self.assertTrue(callable(module.main))


@unittest.skipUnless(os.getenv('SAGEL_TEST_POSTGRES_DSN'), 'PostgreSQL temporário não configurado')
class MigrationIntegrationTests(unittest.TestCase):
    source = MigrationTests.source
    def test_migration_is_atomic_preserves_sequences_and_refuses_existing_data(self):
        import psycopg
        from psycopg import sql
        source = self.source('''CREATE TABLE itens(id INTEGER PRIMARY KEY AUTOINCREMENT,valor REAL, total INTEGER);
            INSERT INTO itens VALUES(100,1.23456789123,9000000000);
            DELETE FROM itens; INSERT INTO itens VALUES(5,1.23456789123,9000000000);''')
        schema = 'sagel_migration_test_' + uuid.uuid4().hex
        with psycopg.connect(os.environ['SAGEL_TEST_POSTGRES_DSN']) as pg:
            try:
                result = migration.migrate(source, pg, migration.source_inventory(source), schema)
                self.assertTrue(result['content_verified'])
                row = pg.execute(sql.SQL('INSERT INTO {}.itens(valor,total) VALUES(1,1) RETURNING id').format(sql.Identifier(schema))).fetchone()
                self.assertEqual(row[0], 101)
                pg.commit()
                with self.assertRaises(migration.MigrationError):
                    migration.migrate(source, pg, migration.source_inventory(source), schema)
                self.assertEqual(pg.execute(sql.SQL('SELECT COUNT(*) FROM {}.itens').format(sql.Identifier(schema))).fetchone()[0], 2)
            finally:
                pg.rollback()
                pg.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))

    def test_invalid_affinity_rolls_back_entire_new_schema(self):
        import psycopg
        source = self.source("CREATE TABLE itens(id INTEGER PRIMARY KEY, total INTEGER); INSERT INTO itens VALUES(1,'invalid');")
        schema = 'sagel_migration_test_' + uuid.uuid4().hex
        with psycopg.connect(os.environ['SAGEL_TEST_POSTGRES_DSN']) as pg:
            with self.assertRaises(psycopg.Error):
                migration.migrate(source, pg, migration.source_inventory(source), schema)
            self.assertIsNone(pg.execute('SELECT oid FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone())


if __name__ == '__main__':
    unittest.main()
