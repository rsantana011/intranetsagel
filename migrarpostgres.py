"""Migração explícita, transacional e sem substituir dados existentes.

Sem argumentos (ou --check), abre ambos os bancos somente para leitura e exibe
metadados. --apply aceita exclusivamente um schema PostgreSQL vazio ou novo.
Não importa a aplicação Flask, não cria usuários/seeds e nunca apaga schemas.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / '.env'


class MigrationError(Exception):
    """Mensagem segura para apresentação; não contém valores de registros."""


def validate_schema(name):
    if (not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', name)
            or name.lower().startswith('pg_') or name.lower() == 'information_schema'):
        raise MigrationError('Nome de schema inválido ou reservado.')
    return name


def sqlite_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def open_source(path):
    path = Path(path).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')  # Um único snapshot durante inspeção e cópia.
    return conn


def connect_postgres(*, read_only, env_file=ENV_FILE):
    # dotenv_values lê somente o arquivo explicitamente selecionado, sem alterá-lo
    # nem injetar suas credenciais no ambiente de outros processos.
    import psycopg
    from dotenv import dotenv_values

    values = dotenv_values(env_file) if Path(env_file).is_file() else {}
    names = {'host': 'POSTGRES_HOST', 'port': 'POSTGRES_PORT',
             'dbname': 'POSTGRES_DB', 'user': 'POSTGRES_USER',
             'password': 'POSTGRES_PASSWORD', 'sslmode': 'POSTGRES_SSLMODE'}
    config = {key: os.environ.get(variable) or values.get(variable)
              for key, variable in names.items()}
    if not all(config[key] for key in ('host', 'dbname', 'user', 'password')):
        raise MigrationError('Configuração PostgreSQL incompleta no .env do projeto ou no ambiente.')
    config['port'] = config['port'] or '5432'
    config['connect_timeout'] = 10
    if read_only:
        config['options'] = '-c default_transaction_read_only=on'
    return psycopg.connect(**config)


def source_inventory(conn):
    unsupported = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('view','trigger')").fetchone()[0]
    tables = {}
    for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        name = row['name']
        quoted = sqlite_identifier(name)
        columns = [dict(item) for item in conn.execute(f'PRAGMA table_info({quoted})')]
        foreign_keys = [dict(item) for item in conn.execute(f'PRAGMA foreign_key_list({quoted})')]
        indexes = []
        for index in conn.execute(f'PRAGMA index_list({quoted})'):
            definition = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index['name'],)).fetchone()
            indexes.append({'name': index['name'], 'unique': bool(index['unique']),
                            'origin': index['origin'],
                            'sql': definition[0] if definition else None,
                            'columns': [dict(item) for item in conn.execute(f'PRAGMA index_xinfo({sqlite_identifier(index["name"])})') if item['key']]})
        tables[name] = {'sql': row['sql'], 'columns': columns,
                        'foreign_keys': foreign_keys, 'indexes': indexes,
                        'count': conn.execute(f'SELECT COUNT(*) FROM {quoted}').fetchone()[0]}
    sequences = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'").fetchone():
        sequences = dict(conn.execute('SELECT name,seq FROM sqlite_sequence'))
    integrity_ok = [row[0] for row in conn.execute('PRAGMA quick_check')] == ['ok']
    fk_errors = sum(1 for _ in conn.execute('PRAGMA foreign_key_check'))
    return {'tables': tables, 'sequences': sequences, 'integrity_ok': integrity_ok,
            'foreign_key_errors': fk_errors, 'unsupported_objects': unsupported}


def table_order(tables):
    dependencies = {name: {fk['table'] for fk in table['foreign_keys'] if fk['table'] != name}
                    for name, table in tables.items()}
    if any(deps - tables.keys() for deps in dependencies.values()):
        raise MigrationError('Há referência a uma tabela ausente na origem.')
    result = []
    while len(result) < len(tables):
        ready = sorted(name for name, deps in dependencies.items()
                       if name not in result and deps.issubset(result))
        if not ready:
            raise MigrationError('Dependências circulares exigem uma migração específica.')
        result.extend(ready)
    return result


# Cada string/identificador/comentário é um token: as substituições não atingem
# conteúdos DEFAULT, nomes citados ou texto de CHECKs.
TOKEN = re.compile(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|--[^\n]*|/\*[\s\S]*?\*/|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|>=|<=|<>|!=|==|\|\||[A-Za-z_][A-Za-z0-9_]*|\s+|.)")


def convert_ddl(statement, *, index=False):
    tokens = TOKEN.findall(statement)
    # Comentários não são necessários para recriar o schema e complicariam o
    # reconhecimento de sequências de palavras. Literais continuam intactos.
    tokens = [t for t in tokens if not t.isspace() and not t.startswith(('--', '/*'))]
    output = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        word = token.upper()
        following = [item.upper() for item in tokens[i:i + 4]]
        if word == 'COLLATE':
            if i + 1 >= len(tokens) or tokens[i + 1].upper() != 'NOCASE':
                raise MigrationError('Collation SQLite não suportada; necessária revisão do schema.')
            if index:
                if not output or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*|"(?:""|[^"])*"', output[-1]):
                    raise MigrationError('Índice COLLATE NOCASE complexo exige revisão manual.')
                output[-1] = 'lower(' + output[-1] + ')'
            i += 2
            continue
        if following[:3] == ['INTEGER', 'PRIMARY', 'KEY']:
            output.extend(['BIGSERIAL', 'PRIMARY', 'KEY'])
            i += 4 if len(following) == 4 and following[3] == 'AUTOINCREMENT' else 3
            continue
        if word in ('AUTOINCREMENT', 'WITHOUT', 'VIRTUAL', 'STRICT', 'GENERATED'):
            raise MigrationError('DDL SQLite exige conversão específica não disponível.')
        if token.startswith(('`', '[')):
            raise MigrationError('Identificador SQLite exige conversão específica.')
        if word == 'INTEGER':
            token = 'BIGINT'
        elif word == 'REAL':
            token = 'DOUBLE PRECISION'
        elif word == 'BLOB':
            token = 'BYTEA'
        output.append(token)
        i += 1
    return ' '.join(output)


def validate_source(inventory):
    if not inventory['integrity_ok'] or inventory['foreign_key_errors']:
        raise MigrationError('A origem não passou na verificação de integridade/FKs; nada foi copiado.')
    if inventory['unsupported_objects']:
        raise MigrationError('A origem contém views/triggers; necessária migração específica.')
    if not inventory['tables']:
        raise MigrationError('A origem não possui tabelas de aplicação.')
    allowed = {'INTEGER', 'TEXT', 'REAL', 'BLOB'}
    for table in inventory['tables'].values():
        if any(column['type'].upper() not in allowed for column in table['columns']):
            raise MigrationError('Tipo SQLite fora do conjunto revisado: INTEGER, TEXT, REAL e BLOB.')
        convert_ddl(table['sql'])
        for idx in table['indexes']:
            if idx['sql']:
                convert_ddl(idx['sql'], index=True)
    return table_order(inventory['tables'])


def postgres_inventory(conn, schema):
    from psycopg import sql
    validate_schema(schema)
    tables = {}
    rows = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name", (schema,)).fetchall()
    for (name,) in rows:
        columns = conn.execute('SELECT column_name,data_type FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position', (schema, name)).fetchall()
        indexes = conn.execute('SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s ORDER BY indexname', (schema, name)).fetchall()
        count = conn.execute(sql.SQL('SELECT COUNT(*) FROM {}.{}').format(sql.Identifier(schema), sql.Identifier(name))).fetchone()[0]
        tables[name] = {'columns': [list(c) for c in columns], 'indexes': [list(i) for i in indexes], 'count': count}
    foreign_keys = []
    rows = conn.execute('''SELECT c.conname,n.nspname,t.relname,rn.nspname,rt.relname,c.convalidated,
        ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY AS k(num,ord) JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.num ORDER BY k.ord),
        ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY AS k(num,ord) JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.num ORDER BY k.ord),c.confmatchtype
        FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_class rt ON rt.oid=c.confrelid JOIN pg_namespace rn ON rn.oid=rt.relnamespace
        WHERE c.contype='f' AND n.nspname=%s ORDER BY t.relname,c.conname''', (schema,)).fetchall()
    for constraint, child_schema, child, parent_schema, parent, validated, child_cols, parent_cols, match_type in rows:
        populated = sql.SQL(' AND ').join(sql.SQL('c.{} IS NOT NULL').format(sql.Identifier(c)) for c in child_cols)
        matches = sql.SQL(' AND ').join(sql.SQL('p.{}=c.{}').format(sql.Identifier(p), sql.Identifier(c)) for c, p in zip(child_cols, parent_cols))
        query = sql.SQL('SELECT COUNT(*) FROM {}.{} c WHERE ({}) AND NOT EXISTS (SELECT 1 FROM {}.{} p WHERE {})').format(sql.Identifier(child_schema), sql.Identifier(child), populated, sql.Identifier(parent_schema), sql.Identifier(parent), matches)
        invalid = conn.execute(query).fetchone()[0]
        if match_type == 'f' and len(child_cols) > 1:
            any_null = sql.SQL(' OR ').join(sql.SQL('c.{} IS NULL').format(sql.Identifier(c)) for c in child_cols)
            any_value = sql.SQL(' OR ').join(sql.SQL('c.{} IS NOT NULL').format(sql.Identifier(c)) for c in child_cols)
            invalid += conn.execute(sql.SQL('SELECT COUNT(*) FROM {}.{} c WHERE ({}) AND ({})').format(sql.Identifier(child_schema), sql.Identifier(child), any_null, any_value)).fetchone()[0]
        foreign_keys.append({'table': child, 'constraint': constraint, 'validated': validated, 'invalid_rows': invalid})
    sequences = conn.execute('SELECT sequencename,data_type,last_value FROM pg_sequences WHERE schemaname=%s ORDER BY sequencename', (schema,)).fetchall()
    return {'tables': tables, 'sequences': [list(s) for s in sequences], 'foreign_keys': foreign_keys}


def comparison_report(source, destination):
    rows = []
    for name in sorted(source['tables'].keys() | destination['tables'].keys()):
        left = source['tables'].get(name)
        right = destination['tables'].get(name)
        rows.append({'table': name, 'sqlite_count': left['count'] if left else None,
                     'postgres_count': right['count'] if right else None,
                     'sqlite_columns': [[c['name'], c['type']] for c in left['columns']] if left else [],
                     'postgres_columns': right['columns'] if right else [],
                     'sqlite_indexes': [{'name': i['name'], 'unique': i['unique'], 'columns': [[c['name'], c['coll']] for c in i['columns']]} for i in left['indexes']] if left else [],
                     'postgres_indexes': right['indexes'] if right else []})
    return {'read_only': True, 'tables': rows, 'sqlite_integrity_ok': source['integrity_ok'],
            'sqlite_fk_errors': source['foreign_key_errors'], 'sqlite_unsupported_objects': source['unsupported_objects'],
            'postgres_foreign_keys': destination['foreign_keys'],
            'sqlite_sequences': source['sequences'], 'postgres_sequences': destination['sequences']}


def row_hash(row):
    normalized = []
    for value in row:
        if isinstance(value, memoryview):
            value = bytes(value)
        if isinstance(value, bytes):
            normalized.append(['blob', value.hex()])
        elif isinstance(value, float):
            # PostgreSQL pode normalizar -0.0. São valores numericamente iguais.
            normalized.append(['float', '0' if value == 0 else value.hex()])
        elif value is None:
            normalized.append(['null'])
        elif isinstance(value, int):
            normalized.append(['int', str(value)])
        elif isinstance(value, str):
            normalized.append(['text', value])
        else:
            raise MigrationError('Tipo de valor inesperado durante a conferência.')
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=True, separators=(',', ':')).encode('ascii')).digest()


def table_fingerprint(cursor):
    # Soma e xor de hashes SHA-256 formam uma conferência de multiconjunto,
    # independente de ordenação/collation e com memória constante.
    count = total = xor = 0
    while batch := cursor.fetchmany(1000):
        for row in batch:
            value = int.from_bytes(row_hash(row), 'big')
            count += 1
            total = (total + value) % (1 << 256)
            xor ^= value
    return count, total, xor


def ensure_empty_destination(conn, schema):
    from psycopg import sql
    exists = conn.execute('SELECT oid FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone()
    if not exists:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        return
    oid = exists[0]
    count = conn.execute('''SELECT (SELECT COUNT(*) FROM pg_class WHERE relnamespace=%s)
        + (SELECT COUNT(*) FROM pg_proc WHERE pronamespace=%s)
        + (SELECT COUNT(*) FROM pg_type WHERE typnamespace=%s)
        + (SELECT COUNT(*) FROM pg_collation WHERE collnamespace=%s)''', (oid, oid, oid, oid)).fetchone()[0]
    if count:
        raise MigrationError('O schema de destino já contém objetos. Use --check para comparar; não há substituição nem mesclagem automática.')


def migrate(source, postgres, inventory, schema):
    from psycopg import sql
    validate_schema(schema)
    order = validate_source(inventory)
    # O chamador deve fornecer conexão nova; assim este bloco controla o COMMIT.
    if postgres.info.transaction_status != 0:
        raise MigrationError('A migração exige uma conexão sem transação anterior.')
    with postgres.transaction():
        postgres.execute('SELECT pg_advisory_xact_lock(1935763308, hashtext(%s))', (schema,))
        ensure_empty_destination(postgres, schema)
        postgres.execute(sql.SQL('SET LOCAL search_path TO {}, pg_catalog').format(sql.Identifier(schema)))
        for name in order:
            table = inventory['tables'][name]
            postgres.execute(convert_ddl(table['sql']))
        # Índices originais, inclusive índices únicos parciais, são recriados.
        # Índices automáticos UNIQUE/PK já vêm do CREATE TABLE.
        for name in order:
            for index in inventory['tables'][name]['indexes']:
                if index['sql']:
                    postgres.execute(convert_ddl(index['sql'], index=True))
                elif index['unique'] and any(c['coll'].upper() == 'NOCASE' for c in index['columns']):
                    expressions = []
                    for column in index['columns']:
                        if column['name'] is None or column['coll'].upper() not in ('BINARY', 'NOCASE'):
                            raise MigrationError('Índice automático complexo exige revisão específica.')
                        expr = sql.Identifier(column['name'])
                        if column['coll'].upper() == 'NOCASE':
                            expr = sql.SQL('lower({})').format(expr)
                        expressions.append(expr)
                    suffix = hashlib.sha256((name + ':' + index['name']).encode()).hexdigest()[:16]
                    postgres.execute(sql.SQL('CREATE UNIQUE INDEX {} ON {} ({})').format(sql.Identifier('migration_nocase_' + suffix), sql.Identifier(name), sql.SQL(', ').join(expressions)))
        for name in order:
            table = inventory['tables'][name]
            columns = [column['name'] for column in table['columns']]
            projection = ','.join(sqlite_identifier(c) for c in columns)
            source_cursor = source.execute(f'SELECT {projection} FROM {sqlite_identifier(name)}')
            insert = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(sql.Identifier(name), sql.SQL(',').join(map(sql.Identifier, columns)), sql.SQL(',').join(sql.Placeholder() for _ in columns))
            with postgres.cursor() as cursor:
                while batch := source_cursor.fetchmany(1000):
                    cursor.executemany(insert, [tuple(row) for row in batch])
            # Conferência efetiva de conteúdo antes de efetivar a transação.
            original = table_fingerprint(source.execute(f'SELECT {projection} FROM {sqlite_identifier(name)}'))
            copied = table_fingerprint(postgres.execute(sql.SQL('SELECT {} FROM {}').format(sql.SQL(',').join(map(sql.Identifier, columns)), sql.Identifier(name))))
            if original != copied:
                raise MigrationError('A conferência de conteúdo divergiu; a migração será revertida.')
            # Preserva inclusive o teto histórico AUTOINCREMENT de IDs excluídos.
            for column in table['columns']:
                relation = sql.Identifier(schema, name).as_string(postgres)
                sequence = postgres.execute('SELECT pg_get_serial_sequence(%s,%s)', (relation, column['name'])).fetchone()[0]
                if not sequence:
                    continue
                maximum = source.execute(f'SELECT MAX({sqlite_identifier(column["name"])}) FROM {sqlite_identifier(name)}').fetchone()[0]
                high_water = max(int(maximum or 0), int(inventory['sequences'].get(name, 0) or 0), 0)
                postgres.execute('SELECT setval(%s::regclass,%s,%s)', (sequence, max(high_water, 1), high_water > 0))
    return {'tables': len(order), 'rows': sum(inventory['tables'][name]['count'] for name in order), 'content_verified': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Inspeciona sem modificar bancos (padrão).')
    mode.add_argument('--apply', action='store_true', help='Copia para schema vazio/novo em uma transação.')
    parser.add_argument('--sqlite', type=Path, default=ROOT / 'intranet.db')
    parser.add_argument('--schema', default='public')
    args = parser.parse_args(argv)
    source = postgres = None
    try:
        validate_schema(args.schema)
        source = open_source(args.sqlite)
        inventory = source_inventory(source)
        postgres = connect_postgres(read_only=not args.apply)
        if args.apply:
            result = migrate(source, postgres, inventory, args.schema)
            print(json.dumps({'migration_committed': True, **result}, ensure_ascii=False, indent=2))
        else:
            postgres.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            print(json.dumps(comparison_report(inventory, postgres_inventory(postgres, args.schema)), ensure_ascii=False, indent=2))
        return 0
    except MigrationError as error:
        print(str(error))
        return 2
    except Exception as error:
        # Mensagens libpq e SQL podem conter DSN, credenciais e valores de linhas.
        # Exibir apenas a classe evita vazá-los, inclusive em falhas de conexão.
        print('Operação interrompida sem publicar a migração. Tipo de erro: ' + type(error).__name__)
        return 2
    finally:
        if postgres is not None:
            postgres.close()
        if source is not None:
            source.close()


if __name__ == '__main__':
    raise SystemExit(main())
