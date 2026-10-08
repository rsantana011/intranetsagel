import math
import secrets
import sqlite3
import psycopg
from psycopg.rows import dict_row
import shutil
import subprocess
import uuid
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import bcrypt
import jwt
from flask import (abort, current_app, g, redirect, render_template,
                   render_template_string, request, send_from_directory, session, url_for)
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from .database import BancoPostgres, adaptar_sql

load_dotenv(Path(__file__).resolve().parent.parent / '.env')

PERFIS = {'admin': 'Administrador', 'frota': 'Gestor de Frota',
          'almoxarifado': 'Almoxarifado', 'compras': 'Compras', 'ti': 'TI',
          'documentacao': 'Documentação', 'colaborador': 'Colaborador'}
MODULOS = {
    'colaboradores': ('Colaboradores', '/funcionarios'),
    'dashboard': ('Página inicial', '/dashboard'), 'frota': ('Frota', '/frota'),
    'combustivel': ('Combustível', '/combustivel'), 'epi': ('Estoque EPI', '/epi'),
    'chamados': ('Chamados TI', '/chamados'), 'ti_estoque': ('Estoque TI', '/ti/estoque'),
    'compras': ('Compras', '/compras'), 'documentos': ('Documentação', '/documentos'),
    'relatorios': ('Relatórios', '/relatorios'), 'avisos': ('Avisos', '/avisos'),
    'tarefas': ('Tarefas', '/tarefas'), 'usuarios': ('Usuários e perfis', '/admin'),
    'configuracoes': ('Configurações', '/configuracoes'), 'auditoria': ('Auditoria', '/auditoria')}

def agora():
    return datetime.now(timezone(timedelta(hours=-4))).replace(tzinfo=None).isoformat(timespec='seconds')

def hoje():
    return agora()[:10]

# ============================================================
# BANCO DE DADOS — configuração PostgreSQL e conexões
# ============================================================
def parametros_postgres():
    params = {key: os.getenv('POSTGRES_' + env) for key, env in (
        ('host', 'HOST'), ('port', 'PORT'), ('dbname', 'DB'),
        ('user', 'USER'), ('password', 'PASSWORD'), ('sslmode', 'SSLMODE'))}
    params = {key: value for key, value in params.items() if value is not None}
    params.update(current_app.config.get('POSTGRES_CONNECT_OPTIONS', {}))
    params.setdefault('connect_timeout', 10)
    return params

def db():
    if 'db' not in g:
        backend = current_app.config['DATABASE_BACKEND']
        if backend == 'sqlite':
            g.db = sqlite3.connect(current_app.config['DATABASE'], timeout=30)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys=ON')
        elif backend == 'postgresql':
            try:
                g.db = BancoPostgres(psycopg.connect(**parametros_postgres(), row_factory=dict_row))
            except psycopg.Error:
                raise RuntimeError('Não foi possível conectar ao PostgreSQL. Confira a configuração e o serviço.') from None
        else:
            raise ValueError('DATABASE_BACKEND deve ser sqlite ou postgresql.')
    return g.db

def fechar_db(error=None):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()

# ============================================================
# LOGIN E SEGURANÇA — senhas, sessão e identificação do usuário
# ============================================================
def hash_senha(senha):
    if len(senha.encode('utf-8')) > 72:
        raise ValueError('A senha excede o tamanho permitido.')
    return bcrypt.hashpw(senha.encode('utf-8'), bcrypt.gensalt(rounds=12)).decode()

def conferir_senha(salva, senha):
    try:
        if salva.startswith(('$2a$', '$2b$', '$2y$')):
            return bcrypt.checkpw(senha.encode('utf-8'), salva.encode())
        if salva.startswith(('scrypt:', 'pbkdf2:')):
            return check_password_hash(salva, senha)
        return secrets.compare_digest(salva.encode(), senha.encode())
    except (ValueError, TypeError):
        return False

def current_user():
    if hasattr(g, 'usuario'):
        return g.usuario
    ident = None
    versao = None
    if current_app.testing and session.get('usuario_id'):
        ident = session['usuario_id']
    elif request.cookies.get('sagel_acesso'):
        try:
            dados = jwt.decode(request.cookies['sagel_acesso'], current_app.secret_key,
                               algorithms=['HS256'], issuer='sagel-local', audience='sagel-web',
                               options={'require': ['exp', 'iat', 'sub', 'ver', 'iss', 'aud']})
            ident = int(dados['sub'])
            versao = dados['ver']
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            pass
    user = db().execute('SELECT * FROM usuarios WHERE id=? AND ativo=1', (ident,)).fetchone() if ident else None
    if user and versao is not None and versao != user['versao_sessao']:
        user = None
    g.usuario = user
    return user

def emitir_acesso(response, user):
    now = datetime.now(timezone.utc)
    token = jwt.encode({'sub': str(user['id']), 'ver': user['versao_sessao'], 'iat': now,
                        'exp': now + timedelta(hours=8), 'iss': 'sagel-local', 'aud': 'sagel-web'},
                       current_app.secret_key, algorithm='HS256')
    response.set_cookie('sagel_acesso', token, max_age=28800, httponly=True,
                        secure=current_app.config['SESSION_COOKIE_SECURE'], samesite='Lax')
    return response

# ============================================================
# PERMISSÕES — controle de acesso por módulo e ação
# ============================================================
def tem_permissao(modulo, acao='ver'):
    user = current_user()
    if not user:
        return False
    if user['perfil'] == 'admin':
        return True
    if modulo == 'usuarios':
        return False
    # ACESSO INDIVIDUAL — uma lista explícita substitui as permissões do perfil.
    # A página inicial apenas reúne módulos autorizados e permanece acessível.
    if db().execute('SELECT 1 FROM acessos_individuais WHERE usuario_id=?', (user['id'],)).fetchone():
        if modulo == 'dashboard' and acao == 'ver':
            return True
        return db().execute('SELECT 1 FROM permissoes_usuario WHERE usuario_id=? AND modulo=? AND acao=?',
                            (user['id'], modulo, acao)).fetchone() is not None
    return db().execute('SELECT 1 FROM permissoes WHERE perfil=? AND modulo=? AND acao=?',
                        (user['perfil'], modulo, acao)).fetchone() is not None

def permissao(modulo, acao='ver'):
    def decorator(func):
        @wraps(func)
        def check(*args, **kwargs):
            if not current_user():
                return redirect(url_for('principal.login'))
            if not tem_permissao(modulo, acao):
                abort(403)
            return func(*args, **kwargs)
        return check
    return decorator

def autenticado(func):
    @wraps(func)
    def check(*args, **kwargs):
        if not current_user():
            return redirect(url_for('principal.login'))
        return func(*args, **kwargs)
    return check

# ============================================================
# FORMULÁRIOS — proteção CSRF
# ============================================================
def csrf_field():
    token = session.setdefault('csrf_token', secrets.token_urlsafe(32))
    return Markup('<input type="hidden" name="csrf_token" value="{}">').format(escape(token))

def proteger_post():
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        expected = session.get('csrf_token', '')
        actual = request.form.get('csrf_token', '') or request.headers.get('X-CSRF-Token', '')
        if not expected or not secrets.compare_digest(expected.encode(), actual.encode()):
            abort(400, description='A sessão do formulário expirou. Reabra a página e tente novamente.')

# ============================================================
# AUDITORIA — registro de ações
# ============================================================
def auditar(modulo, acao, registro_id=None, detalhes='', conn=None):
    conn = conn or db()
    user = current_user()
    conn.execute('INSERT INTO auditoria(usuario_id,modulo,acao,registro_id,detalhes,data) VALUES(?,?,?,?,?,?)',
                 (user['id'] if user else None, modulo, acao, str(registro_id) if registro_id is not None else None,
                  str(detalhes)[:2000], agora()))

# ============================================================
# INTERFACE — renderização compartilhada
# ============================================================
def render_page(titulo, corpo, **ctx):
    ctx.setdefault('titulo', titulo)
    conteudo = render_template_string(corpo, **ctx)
    return render_template('base.html', titulo=titulo, conteudo=Markup(conteudo),
                           acesso=ctx.get('acesso', False))

def texto_form(nome, rotulo=None, limite=200, obrigatorio=True):
    value = request.form.get(nome, '').strip()
    if obrigatorio and not value:
        raise ValueError(f'Informe {rotulo or nome}.')
    if len(value) > limite:
        raise ValueError(f'{rotulo or nome}: máximo de {limite} caracteres.')
    return value

def valor_decimal(texto, minimo=0):
    try:
        value = float(str(texto).strip().replace(',', '.'))
    except (ValueError, TypeError):
        raise ValueError('Informe um número válido.')
    if not math.isfinite(value) or value < minimo or value > 1e12:
        raise ValueError(f'O valor deve ser maior ou igual a {minimo} e estar dentro do limite permitido.')
    return value

def salvar_anexo(file):
    if not file or not file.filename:
        return None, None
    name = secure_filename(file.filename)[:180]
    extension = Path(name).suffix.lower()
    if extension not in {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.csv', '.txt', '.jpg', '.jpeg', '.png', '.webp', '.avif'}:
        raise ValueError('Formato não permitido. Envie PDF, documentos, planilhas ou imagens.')
    data = file.read(16 * 1024 * 1024 + 1)
    if not data or len(data) > 16 * 1024 * 1024:
        raise ValueError('O arquivo deve conter dados e ter no máximo 16 MB.')
    key = uuid.uuid4().hex + extension
    folder = Path(current_app.config['STORAGE_DIR'])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / key).write_bytes(data)
    return key, name

def baixar_anexo(chave, nome):
    return send_from_directory(current_app.config['STORAGE_DIR'], chave, as_attachment=True,
                               download_name=nome, mimetype='application/octet-stream')

# ============================================================
# BANCO DE DADOS — cópias de segurança
# ============================================================
def backup_banco():
    folder = Path(current_app.config['BACKUP_DIR'])
    folder.mkdir(parents=True, exist_ok=True)
    prefix = 'sagel-' + agora().replace(':', '-') + '-' + secrets.token_hex(3)
    if current_app.config['DATABASE_BACKEND'] == 'postgresql':
        executable = current_app.config.get('PG_DUMP_PATH') or os.getenv('PG_DUMP_PATH') or shutil.which('pg_dump')
        if not executable and os.name == 'nt':
            candidates = sorted((Path(os.getenv('ProgramFiles', 'C:/Program Files')) / 'PostgreSQL').glob('*/bin/pg_dump.exe'),
                                key=lambda path: tuple(int(n) for n in path.parents[1].name.split('.') if n.isdigit()), reverse=True)
            executable = str(candidates[0]) if candidates else None
        if not executable:
            raise OSError('pg_dump não encontrado. Configure PG_DUMP_PATH para o PostgreSQL instalado.')
        target = folder / (prefix + '.dump')
        temporary = target.with_suffix('.dump.partial')
        env = os.environ.copy()
        for key in ('PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSSLMODE', 'PGOPTIONS', 'PGSERVICE', 'PGSERVICEFILE'):
            env.pop(key, None)
        mapping = {'host': 'PGHOST', 'port': 'PGPORT', 'dbname': 'PGDATABASE', 'user': 'PGUSER',
                   'password': 'PGPASSWORD', 'sslmode': 'PGSSLMODE', 'options': 'PGOPTIONS', 'connect_timeout': 'PGCONNECT_TIMEOUT'}
        for key, value in parametros_postgres().items():
            if key in mapping:
                env[mapping[key]] = str(value)
        try:
            result = subprocess.run([str(executable), '--no-password', '--format=custom', '--file', str(temporary)],
                                    env=env, capture_output=True, timeout=300,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode:
                raise OSError('O backup PostgreSQL falhou. Confira pg_dump, conexão e permissões.')
            temporary.replace(target)
        except subprocess.TimeoutExpired:
            raise OSError('O backup PostgreSQL excedeu o tempo permitido.') from None
        finally:
            temporary.unlink(missing_ok=True)
        return target.name
    target = folder / (prefix + '.db')
    source = sqlite3.connect(current_app.config['DATABASE'])
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target.name

# ============================================================
# BANCO DE DADOS — criação e atualização das tabelas comuns
# ============================================================
def init_schema(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS usuarios(id INTEGER PRIMARY KEY AUTOINCREMENT,nome TEXT NOT NULL,
      usuario TEXT UNIQUE NOT NULL,senha TEXT NOT NULL,cargo TEXT NOT NULL DEFAULT 'Colaborador',admin INTEGER DEFAULT 0,email TEXT);
    CREATE TABLE IF NOT EXISTS avisos(id INTEGER PRIMARY KEY AUTOINCREMENT,titulo TEXT NOT NULL,mensagem TEXT NOT NULL,data TEXT NOT NULL,autor TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS tarefas(id INTEGER PRIMARY KEY AUTOINCREMENT,titulo TEXT NOT NULL,descricao TEXT,responsavel TEXT,status TEXT DEFAULT 'Pendente',data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS documentos(id INTEGER PRIMARY KEY AUTOINCREMENT,nome TEXT NOT NULL,categoria TEXT,link TEXT);
    CREATE TABLE IF NOT EXISTS permissoes(perfil TEXT NOT NULL,modulo TEXT NOT NULL,acao TEXT NOT NULL,PRIMARY KEY(perfil,modulo,acao));
    CREATE TABLE IF NOT EXISTS acessos_individuais(usuario_id INTEGER PRIMARY KEY REFERENCES usuarios(id));
    CREATE TABLE IF NOT EXISTS permissoes_usuario(usuario_id INTEGER NOT NULL REFERENCES usuarios(id),modulo TEXT NOT NULL,acao TEXT NOT NULL,PRIMARY KEY(usuario_id,modulo,acao));
    CREATE TABLE IF NOT EXISTS configuracoes(chave TEXT PRIMARY KEY,valor TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS auditoria(id INTEGER PRIMARY KEY AUTOINCREMENT,usuario_id INTEGER REFERENCES usuarios(id),modulo TEXT NOT NULL,acao TEXT NOT NULL,registro_id TEXT,detalhes TEXT,data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS recuperacoes(id INTEGER PRIMARY KEY AUTOINCREMENT,usuario_id INTEGER REFERENCES usuarios(id),criado_em TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'Pendente',token_hash TEXT,expira_em TEXT);
    CREATE TABLE IF NOT EXISTS tentativas_login(chave TEXT PRIMARY KEY,quantidade INTEGER NOT NULL,inicio TEXT NOT NULL);
    ''')
    migrations = {
        'usuarios': {'email': 'TEXT', 'perfil': "TEXT NOT NULL DEFAULT 'colaborador'", 'setor': "TEXT NOT NULL DEFAULT ''", 'ativo': 'INTEGER NOT NULL DEFAULT 1', 'versao_sessao': 'INTEGER NOT NULL DEFAULT 0'},
        'tarefas': {'responsavel_id': 'INTEGER REFERENCES usuarios(id)', 'criado_por': 'INTEGER REFERENCES usuarios(id)', 'prazo': 'TEXT', 'atualizado_em': 'TEXT'}}
    for table, columns in migrations.items():
        existing = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
        for column, sqltype in columns.items():
            if column not in existing:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {sqltype}')
    if not conn.execute("SELECT 1 FROM configuracoes WHERE chave='migracao_perfis'").fetchone():
        conn.execute("UPDATE usuarios SET perfil='admin' WHERE admin=1")
        conn.execute("INSERT INTO configuracoes VALUES('migracao_perfis','1')")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_email ON usuarios(lower(email)) WHERE email IS NOT NULL AND email!=''")
    if not conn.execute("SELECT 1 FROM configuracoes WHERE chave='permissoes_iniciais'").fetchone():
        common = ['dashboard', 'chamados', 'compras', 'documentos', 'avisos', 'tarefas']
        defaults = {'frota': ['frota', 'combustivel'], 'almoxarifado': ['epi'], 'ti': ['chamados', 'ti_estoque'],
                    'compras': ['compras'], 'documentacao': ['documentos'], 'colaborador': []}
        for role, managed in defaults.items():
            for module in set(common + managed + (['relatorios'] if managed else [])):
                conn.execute('INSERT OR IGNORE INTO permissoes VALUES(?,?,?)', (role, module, 'ver'))
            for module in managed:
                conn.execute('INSERT OR IGNORE INTO permissoes VALUES(?,?,?)', (role, module, 'gerenciar'))
            if role == 'compras':
                conn.execute("INSERT OR IGNORE INTO permissoes VALUES('compras','compras','aprovar')")
        conn.execute("INSERT INTO configuracoes VALUES('permissoes_iniciais','1')")
    conn.execute("INSERT OR IGNORE INTO configuracoes VALUES('cadastro_aberto','1')")
    # Preserva o acesso anterior dos perfis existentes à lista de colaboradores.
    # Novos acessos individuais não herdam esta autorização.
    if not conn.execute("SELECT 1 FROM configuracoes WHERE chave='permissao_colaboradores_v1'").fetchone():
        for role in PERFIS:
            if role != 'admin':
                conn.execute("INSERT OR IGNORE INTO permissoes VALUES(?,'colaboradores','ver')", (role,))
        conn.execute("INSERT INTO configuracoes VALUES('permissao_colaboradores_v1','1')")
    conn.execute("INSERT OR IGNORE INTO configuracoes VALUES('backup_automatico','1')")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_auditoria_data ON auditoria(data)')
