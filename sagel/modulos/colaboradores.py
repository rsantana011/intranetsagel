"""Rotas e regras da área: colaboradores."""
from ..templates_loader import ler_template
from ..base import autenticado, db, render_page
from ..principal import bp

@bp.route('/funcionarios')
@autenticado
def funcionarios():
    users = db().execute('SELECT nome,cargo,setor,email FROM usuarios WHERE ativo=1 ORDER BY nome').fetchall()
    return render_page('Colaboradores', ler_template('colaboradores/funcionarios.html'), users=users)
