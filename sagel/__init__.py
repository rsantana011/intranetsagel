import secrets
import json
import sqlite3
import os
from pathlib import Path

from flask import Flask, request

from . import base

# ============================================================
# INICIALIZAÇÃO — configurações, banco, módulos e respostas HTTP
# ============================================================
def create_app(config=None):
    root = Path(__file__).resolve().parent.parent
    app = Flask(__name__, static_folder=str(root / 'static'))
    instance = root / 'instance'
    app.config.update(DATABASE=str(root / 'intranet.db'), STORAGE_DIR=str(instance / 'anexos'),
                      DATABASE_BACKEND=os.getenv('SAGEL_DATABASE_BACKEND', 'postgresql' if os.getenv('POSTGRES_DB') else 'sqlite'),
                      BACKUP_DIR=str(instance / 'backups'), MAX_CONTENT_LENGTH=17 * 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=False)
    smtp_file = instance / 'smtp.json'
    if smtp_file.exists():
        try:
            app.config['SAGEL_SMTP'] = json.loads(smtp_file.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            app.logger.warning('Configuração de e-mail inválida; envio indisponível.')
    # Environment variables take precedence over local SMTP configuration.
    smtp = dict(app.config.get('SAGEL_SMTP', {}))
    for field, variable in {'host': 'SMTP_HOST', 'porta': 'SMTP_PORT',
                            'usuario': 'SMTP_USER', 'senha': 'SMTP_PASSWORD',
                            'remetente': 'SMTP_FROM', 'seguranca': 'SMTP_SECURITY'}.items():
        if variable in os.environ:
            smtp[field] = os.environ[variable]
    app.config['SAGEL_SMTP'] = smtp
    app.config['SECRET_KEY'] = os.getenv('SAGEL_SECRET_KEY') or None
    app.config.update(config or {})
    # Existing tests declare a temporary SQLite file. Never let .env redirect them.
    if app.testing and 'DATABASE_BACKEND' not in (config or {}):
        app.config['DATABASE_BACKEND'] = 'sqlite'
    if app.config['DATABASE_BACKEND'] not in ('sqlite', 'postgresql'):
        raise ValueError('DATABASE_BACKEND deve ser sqlite ou postgresql.')
    if not app.config.get('SECRET_KEY'):
        instance.mkdir(parents=True, exist_ok=True)
        keyfile = instance / 'secret.key'
        if not keyfile.exists():
            try:
                with keyfile.open('x', encoding='utf-8') as out:
                    out.write(secrets.token_hex(48))
            except FileExistsError:
                pass
        app.secret_key = keyfile.read_text(encoding='utf-8').strip()
    if app.config['DATABASE_BACKEND'] == 'sqlite':
        Path(app.config['DATABASE']).parent.mkdir(parents=True, exist_ok=True)
    app.teardown_appcontext(base.fechar_db)
    app.before_request(base.proteger_post)
    app.jinja_env.globals.update(current_user=base.current_user, tem_permissao=base.tem_permissao,
        csrf_field=base.csrf_field, hoje=base.hoje, perfil_nome=lambda p: base.PERFIS.get(p, p),
        modulos=base.MODULOS, perfis=base.PERFIS)
    from . import principal, frota, estoques_ti, compras_documentos, cadastro_email
    from .modulos import registrar_rotas
    registrar_rotas()
    modules = [principal, frota, estoques_ti, compras_documentos, cadastro_email]
    with app.app_context():
        conn = base.db()
        if app.config['DATABASE_BACKEND'] == 'sqlite':
            conn.execute('PRAGMA journal_mode=WAL')
        base.init_schema(conn)
        for module in modules:
            if hasattr(module, 'init_schema'):
                module.init_schema(conn)
        if isinstance(conn, base.BancoPostgres):
            conn.adequar_tipos()
        conn.commit()
    app.config['SAGEL_MODULOS'] = modules[1:]
    reports = {}
    for module in modules:
        app.register_blueprint(module.bp)
        reports.update(getattr(module, 'REPORTS', {}))
    app.config['SAGEL_REPORTS'] = reports

    @app.before_request
    def backup_diario():
        if app.testing or request.endpoint == 'static':
            return
        conn = base.db()
        enabled = conn.execute("SELECT valor FROM configuracoes WHERE chave='backup_automatico'").fetchone()
        last = conn.execute("SELECT valor FROM configuracoes WHERE chave='ultimo_backup'").fetchone()
        if enabled and enabled['valor'] == '1' and (not last or last['valor'] != base.hoje()):
            try:
                base.backup_banco()
                conn.execute("INSERT INTO configuracoes VALUES('ultimo_backup',?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor", (base.hoje(),))
                conn.commit()
            except (OSError, sqlite3.Error) as error:
                conn.rollback()
                app.logger.error('Falha no backup automático: %s', type(error).__name__)

    @app.after_request
    def headers(response):
        if request.method == 'GET' and response.status_code == 200 and request.endpoint != 'static' and base.current_user():
            base.auditar('navegacao', 'Consulta', detalhes=request.url_rule.rule if request.url_rule else '')
            base.db().commit()
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        return response

    for code in (400, 403, 404, 413):
        def handler(error, status=code):
            texts = {400: 'Não foi possível enviar o formulário. Reabra a página e confira os dados.',
                     403: 'Seu perfil não tem acesso a esta operação.', 404: 'O registro ou a página não foi encontrado.',
                     413: 'O arquivo enviado excede o limite de 16 MB.'}
            return base.render_page('Não foi possível continuar', '<div class="box"><p>{{mensagem}}</p><p><a href="/dashboard">Voltar ao início</a></p></div>', mensagem=texts[status]), status
        app.register_error_handler(code, handler)
    return app
