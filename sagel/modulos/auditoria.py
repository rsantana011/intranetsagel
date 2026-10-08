"""Rotas e regras da área: auditoria."""

# ============================================================
# AUDITORIA E HISTÓRICO DE AÇÕES
# Regras e rotas desta área. Interface: sagel/templates/.
# Banco e segurança compartilhados: sagel/base.py.
# Mapa completo de manutenção: ESTRUTURA-DO-PROJETO.md.
# ============================================================
from ..templates_loader import ler_template
from datetime import date, timedelta
from flask import abort, request
from ..base import db, permissao, render_page
from ..principal import bp

@bp.route('/auditoria')
@permissao('auditoria')
def auditoria():
    conditions, args = ([], [])
    module = request.args.get('modulo', '')
    if module:
        conditions.append('a.modulo=?')
        args.append(module)
    start, end = (request.args.get('inicio', ''), request.args.get('fim', ''))
    try:
        if start:
            date.fromisoformat(start)
            conditions.append('a.data>=?')
            args.append(start)
        if end:
            date.fromisoformat(end)
            conditions.append('a.data<?')
            args.append((date.fromisoformat(end) + timedelta(days=1)).isoformat())
    except ValueError:
        abort(400)
    rows = db().execute('SELECT a.*,u.nome FROM auditoria a LEFT JOIN usuarios u ON u.id=a.usuario_id' + (' WHERE ' + ' AND '.join(conditions) if conditions else '') + ' ORDER BY a.id DESC LIMIT 1000', args).fetchall()
    return render_page('Segurança e auditoria', ler_template('auditoria/auditoria.html'), rows=rows)
