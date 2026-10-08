"""Rotas e regras da área: estoque_ti."""
from ..templates_loader import ler_template
import sqlite3
from flask import abort, flash, redirect, request, url_for
from ..base import agora, auditar, current_user, db, permissao, render_page, tem_permissao, texto_form
from ..estoques_ti import _ativo, _usuarios, bp

@bp.route('/ti/estoque', methods=['GET', 'POST'])
@permissao('ti_estoque')
def equipamentos():
    conn = db()
    if request.method == 'POST':
        if not tem_permissao('ti_estoque', 'gerenciar'):
            abort(403)
        try:
            patrimonio = texto_form('patrimonio', 'Patrimônio', 60).upper()
            nome = texto_form('nome', 'Equipamento', 150)
            categoria = texto_form('categoria', 'Categoria', 60)
            serie = texto_form('serie', 'Número de série', 100, False).upper()
            observacao = texto_form('observacao', 'Observação', 1000, False)
            status = request.form.get('status', 'Disponível')
            if status not in ('Disponível', 'Manutenção', 'Inativo'):
                raise ValueError('Situação inválida para um novo equipamento.')
            cur = conn.execute("""INSERT INTO ti_equipamentos
                (patrimonio,nome,categoria,serie,status,observacao,criado_em) VALUES (?,?,?,?,?,?,?)""", (patrimonio, nome, categoria, serie, status, observacao, agora()))
            _equipamento_evento(conn, cur.lastrowid, 'Cadastro: ' + status, None, observacao)
            conn.commit()
            flash('Equipamento cadastrado.', 'sucesso')
            return redirect(url_for('estoques_ti.equipamento', equipamento_id=cur.lastrowid))
        except (ValueError, sqlite3.IntegrityError) as exc:
            conn.rollback()
            flash('Patrimônio ou número de série já cadastrado.' if isinstance(exc, sqlite3.IntegrityError) else str(exc), 'erro')
            return redirect(url_for('estoques_ti.equipamentos'))
    busca = request.args.get('q', '').strip()[:150]
    status = request.args.get('status', '')
    sql = """SELECT e.*,u.nome AS colaborador FROM ti_equipamentos e LEFT JOIN usuarios u ON u.id=e.usuario_id
             WHERE (lower(e.nome) LIKE lower(?) OR lower(e.patrimonio) LIKE lower(?) OR lower(e.serie) LIKE lower(?))"""
    args = ['%' + busca + '%'] * 3
    if status:
        sql += ' AND e.status=?'
        args.append(status)
    itens = conn.execute(sql + ' ORDER BY e.nome,e.patrimonio', args).fetchall()
    return render_page('Estoque de TI', EQUIPAMENTOS_LISTA, itens=itens, busca=busca, status=status)

def _equipamento_evento(conn, equipamento_id, acao, usuario_id, observacao):
    conn.execute("""INSERT INTO ti_equipamento_historico
        (equipamento_id,acao,usuario_id,observacao,autor_id,criado_em) VALUES (?,?,?,?,?,?)""", (equipamento_id, acao, usuario_id, observacao, current_user()['id'], agora()))
    auditar('ti_estoque', acao, equipamento_id, observacao, conn=conn)

@bp.route('/ti/estoque/<int:equipamento_id>', methods=['GET', 'POST'])
@permissao('ti_estoque')
def equipamento(equipamento_id):
    conn = db()
    item = conn.execute("""SELECT e.*,u.nome AS colaborador FROM ti_equipamentos e
                          LEFT JOIN usuarios u ON u.id=e.usuario_id WHERE e.id=?""", (equipamento_id,)).fetchone()
    if not item:
        abort(404)
    if request.method == 'POST':
        if not tem_permissao('ti_estoque', 'gerenciar'):
            abort(403)
        try:
            observacao = texto_form('observacao', 'Motivo / observação', 1000)
            acao = request.form.get('acao')
            conn.execute('BEGIN IMMEDIATE')
            atual = conn.execute('SELECT * FROM ti_equipamentos WHERE id=?', (equipamento_id,)).fetchone()
            if acao == 'alocar':
                if atual['status'] != 'Disponível':
                    raise ValueError('O equipamento não está disponível para alocação.')
                usuario_id = _ativo(conn, request.form.get('usuario_id'))
                conn.execute("UPDATE ti_equipamentos SET status='Alocado',usuario_id=? WHERE id=?", (usuario_id, equipamento_id))
                _equipamento_evento(conn, equipamento_id, 'Alocação', usuario_id, observacao)
            elif acao == 'devolver':
                if atual['status'] != 'Alocado':
                    raise ValueError('Este equipamento não possui alocação em aberto.')
                status = request.form.get('status', 'Disponível')
                if status not in ('Disponível', 'Manutenção'):
                    raise ValueError('Situação de devolução inválida.')
                conn.execute('UPDATE ti_equipamentos SET status=?,usuario_id=NULL WHERE id=?', (status, equipamento_id))
                _equipamento_evento(conn, equipamento_id, 'Devolução: ' + status, atual['usuario_id'], observacao)
            elif acao == 'situacao':
                if atual['status'] == 'Alocado':
                    raise ValueError('Registre a devolução antes de alterar a situação.')
                status = request.form.get('status')
                if status not in ('Disponível', 'Manutenção', 'Inativo'):
                    raise ValueError('Situação inválida.')
                conn.execute('UPDATE ti_equipamentos SET status=? WHERE id=?', (status, equipamento_id))
                _equipamento_evento(conn, equipamento_id, 'Situação: ' + status, None, observacao)
            else:
                raise ValueError('Operação inválida.')
            conn.commit()
            flash('Equipamento atualizado. A operação foi registrada no histórico.', 'sucesso')
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), 'erro')
        return redirect(url_for('estoques_ti.equipamento', equipamento_id=equipamento_id))
    historico = conn.execute("""SELECT h.*,u.nome AS colaborador,a.nome AS autor
        FROM ti_equipamento_historico h LEFT JOIN usuarios u ON u.id=h.usuario_id
        JOIN usuarios a ON a.id=h.autor_id WHERE h.equipamento_id=? ORDER BY h.id DESC""", (equipamento_id,)).fetchall()
    return render_page(item['patrimonio'] + ' · ' + item['nome'], EQUIPAMENTO_DETALHE, item=item, historico=historico, usuarios=_usuarios(conn))
EQUIPAMENTOS_LISTA = ler_template('estoque_ti/equipamentos_lista.html')
EQUIPAMENTO_DETALHE = ler_template('estoque_ti/equipamento_detalhe.html')
