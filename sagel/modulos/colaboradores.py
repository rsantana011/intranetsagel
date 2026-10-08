"""Rotas e regras da área: colaboradores."""

# ============================================================
# CADASTRO DE COLABORADORES
# Regras e rotas desta área. Interface: sagel/templates/.
# Banco e segurança compartilhados: sagel/base.py.
# Mapa completo de manutenção: ESTRUTURA-DO-PROJETO.md.
# ============================================================
from ..templates_loader import ler_template
from ..base import permissao, db, render_page
from ..principal import bp

@bp.route('/funcionarios')
@permissao('colaboradores')
def funcionarios():
    users = db().execute('SELECT nome,cargo,setor,email FROM usuarios WHERE ativo=1 ORDER BY nome').fetchall()
    return render_page('Colaboradores', ler_template('colaboradores/funcionarios.html'), users=users)
