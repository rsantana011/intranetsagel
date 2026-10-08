"""Rotas e regras da área: dashboard."""

# ============================================================
# PÁGINA INICIAL E INDICADORES
# Regras e rotas desta área. Interface: sagel/templates/.
# Banco e segurança compartilhados: sagel/base.py.
# Mapa completo de manutenção: ESTRUTURA-DO-PROJETO.md.
# ============================================================
from ..templates_loader import ler_template
from flask import current_app
from ..base import current_user, db, permissao, render_page, tem_permissao
from ..principal import bp

@bp.route('/dashboard')
@permissao('dashboard')
def dashboard():
    conn = db()
    indicadores = []
    for module in current_app.config['SAGEL_MODULOS']:
        if hasattr(module, 'indicadores'):
            for item in module.indicadores(conn):
                slug = item.get('modulo') or ('frota' if item['url'].startswith('/frota') else 'combustivel' if item['url'].startswith('/combustivel') else 'documentos' if item['url'].startswith('/documentos') else 'compras' if item['url'].startswith('/compras') else 'epi' if item['url'].startswith('/epi') else 'ti_estoque' if item['url'].startswith('/ti/estoque') else 'chamados')
                if tem_permissao(slug):
                    indicadores.append(item)
    notices = conn.execute('SELECT * FROM avisos ORDER BY id DESC LIMIT 5').fetchall() if tem_permissao('avisos') else []
    user = current_user()
    notifications = conn.execute('SELECT * FROM ti_notificacoes WHERE usuario_id=? AND lida=0 ORDER BY id DESC LIMIT 5', (user['id'],)).fetchall() if tem_permissao('chamados') else []
    tasks = conn.execute("SELECT * FROM tarefas WHERE status!='Concluída' AND (responsavel_id=? OR responsavel=?) ORDER BY prazo IS NULL,prazo,id DESC LIMIT 6", (user['id'], user['nome'])).fetchall() if tem_permissao('tarefas') else []
    recovery = conn.execute("SELECT COUNT(*) FROM recuperacoes WHERE status='Pendente'").fetchone()[0] if tem_permissao('usuarios', 'gerenciar') else 0
    pending_accounts = conn.execute("SELECT COUNT(*) FROM usuarios WHERE ativo=0 AND perfil_solicitado IS NOT NULL AND perfil_solicitado!=''").fetchone()[0] if user['perfil']=='admin' else 0
    return render_page('Bem-vindo, ' + user['nome'], ler_template('dashboard/dashboard.html'), indicadores=indicadores, notices=notices, tasks=tasks, recovery=recovery, notifications=notifications, pending_accounts=pending_accounts)
