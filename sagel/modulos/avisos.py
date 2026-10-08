"""Rotas e regras da área: avisos."""
from ..templates_loader import ler_template
from flask import abort, flash, redirect, request
from ..base import agora, auditar, current_user, db, permissao, render_page, tem_permissao, texto_form
from ..principal import bp

@bp.route('/avisos', methods=['GET', 'POST'])
@permissao('avisos')
def avisos():
    if request.method == 'POST':
        if not tem_permissao('avisos', 'gerenciar'):
            abort(403)
        try:
            titulo = texto_form('titulo', 'o título', 200)
            mensagem = texto_form('mensagem', 'a mensagem', 10000)
            item = db().execute('INSERT INTO avisos(titulo,mensagem,data,autor) VALUES(?,?,?,?)', (titulo, mensagem, agora(), current_user()['nome']))
            auditar('avisos', 'Publicação', item.lastrowid)
            db().commit()
            flash('Aviso publicado.', 'sucesso')
            return redirect('/avisos')
        except ValueError as error:
            flash(str(error), 'erro')
    notices = db().execute('SELECT * FROM avisos ORDER BY id DESC').fetchall()
    return render_page('Avisos', ler_template('avisos/avisos.html'), notices=notices)
