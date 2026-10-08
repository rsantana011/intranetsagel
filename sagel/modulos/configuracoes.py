"""Rotas e regras da área: configuracoes."""

# ============================================================
# CONFIGURAÇÕES E BACKUPS
# Regras e rotas desta área. Interface: sagel/templates/.
# Banco e segurança compartilhados: sagel/base.py.
# Mapa completo de manutenção: ESTRUTURA-DO-PROJETO.md.
# ============================================================
from ..templates_loader import ler_template
from flask import flash, redirect, request
from ..base import auditar, backup_banco, db, permissao, render_page
from ..principal import bp

@bp.route('/configuracoes', methods=['GET', 'POST'])
@permissao('configuracoes', 'gerenciar')
def configuracoes():
    conn = db()
    if request.method == 'POST':
        if request.form.get('acao') == 'backup':
            try:
                name = backup_banco()
                auditar('configuracoes', 'Backup manual', detalhes=name)
                conn.commit()
                flash('Cópia de segurança criada: ' + name, 'sucesso')
            except OSError:
                flash('Não foi possível gravar a cópia de segurança. Confira a pasta configurada.', 'erro')
        else:
            for key in ('cadastro_aberto', 'backup_automatico'):
                conn.execute('INSERT INTO configuracoes(chave,valor) VALUES(?,?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor', (key, '1' if request.form.get(key) == '1' else '0'))
            auditar('configuracoes', 'Configurações salvas')
            conn.commit()
            flash('Configurações salvas.', 'sucesso')
        return redirect('/configuracoes')
    values = {r['chave']: r['valor'] for r in conn.execute('SELECT * FROM configuracoes')}
    return render_page('Configurações', ler_template('configuracoes/configuracoes.html'), values=values)
