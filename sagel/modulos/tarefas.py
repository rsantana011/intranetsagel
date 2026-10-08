"""Rotas e regras da área: tarefas."""
from ..templates_loader import ler_template
from datetime import date
from flask import abort, flash, redirect, request
from ..base import agora, auditar, current_user, db, permissao, render_page, tem_permissao, texto_form
from ..principal import bp

@bp.route('/tarefas', methods=['GET', 'POST'])
@permissao('tarefas')
def tarefas():
    conn = db()
    user = current_user()
    gestor = tem_permissao('tarefas', 'gerenciar')
    if request.method == 'POST':
        try:
            if request.form.get('acao') == 'status':
                item = conn.execute('SELECT * FROM tarefas WHERE id=?', (request.form.get('id'),)).fetchone()
                if not item:
                    abort(404)
                if not gestor and item['responsavel_id'] != user['id'] and (item['responsavel'] != user['nome']):
                    abort(403)
                status = request.form.get('status')
                if status not in ('Pendente', 'Em andamento', 'Concluída'):
                    raise ValueError('Status inválido.')
                conn.execute('UPDATE tarefas SET status=?,atualizado_em=? WHERE id=?', (status, agora(), item['id']))
                auditar('tarefas', 'Alteração de status', item['id'], status)
            else:
                title = texto_form('titulo', 'o título', 200)
                description = texto_form('descricao', 'a descrição', 5000, False)
                ident = request.form.get('responsavel_id') if gestor else user['id']
                responsible = conn.execute('SELECT id,nome FROM usuarios WHERE id=? AND ativo=1', (ident,)).fetchone()
                if not responsible:
                    raise ValueError('Selecione um responsável ativo.')
                deadline = request.form.get('prazo', '')
                if deadline:
                    date.fromisoformat(deadline)
                item = conn.execute('INSERT INTO tarefas(titulo,descricao,responsavel,responsavel_id,criado_por,prazo,data) VALUES(?,?,?,?,?,?,?)', (title, description, responsible['nome'], responsible['id'], user['id'], deadline or None, agora()))
                auditar('tarefas', 'Criação', item.lastrowid)
            conn.commit()
            flash('Tarefa salva.', 'sucesso')
            return redirect('/tarefas')
        except ValueError as error:
            conn.rollback()
            flash(str(error), 'erro')
    status = request.args.get('status', '')
    conditions, params = ([], [])
    if not gestor:
        conditions.append('(responsavel_id=? OR criado_por=? OR responsavel=?)')
        params += [user['id'], user['id'], user['nome']]
    if status:
        conditions.append('status=?')
        params.append(status)
    items = conn.execute('SELECT * FROM tarefas' + (' WHERE ' + ' AND '.join(conditions) if conditions else '') + ' ORDER BY id DESC', params).fetchall()
    users = conn.execute('SELECT id,nome FROM usuarios WHERE ativo=1 ORDER BY nome').fetchall()
    return render_page('Tarefas', ler_template('tarefas/tarefas.html'), items=items, users=users, gestor=gestor)
