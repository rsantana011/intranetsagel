"""Rotas e regras da área: epi."""
from ..templates_loader import ler_template
from datetime import date
from flask import abort, flash, redirect, request, url_for
from ..base import agora, auditar, current_user, db, hoje, permissao, render_page, tem_permissao, texto_form
from ..estoques_ti import _ativo, _inteiro, _usuarios, bp

def _data(valor):
    if not valor:
        return None
    try:
        return date.fromisoformat(valor).isoformat()
    except ValueError:
        raise ValueError('Informe uma data válida.') from None

@bp.route('/epi', methods=['GET', 'POST'])
@permissao('epi')
def epi():
    conn = db()
    if request.method == 'POST':
        if not tem_permissao('epi', 'gerenciar'):
            abort(403)
        try:
            nome = texto_form('nome', 'Nome', 150)
            ca = texto_form('ca', 'CA', 40, False)
            tamanho = texto_form('tamanho', 'Tamanho', 40, False)
            lote = texto_form('lote', 'Lote', 60, False)
            unidade = texto_form('unidade', 'Unidade', 20)
            validade = _data(request.form.get('validade', ''))
            minimo = _inteiro(request.form.get('estoque_minimo'), 'Estoque mínimo', 0)
            cur = conn.execute("""INSERT INTO epi_itens
                (nome,ca,tamanho,lote,validade,unidade,estoque_minimo,criado_em) VALUES (?,?,?,?,?,?,?,?)""", (nome, ca, tamanho, lote, validade, unidade, minimo, agora()))
            auditar('epi', 'Cadastrar EPI', cur.lastrowid, nome, conn=conn)
            conn.commit()
            flash('EPI cadastrado. Registre uma entrada para adicionar o saldo.', 'sucesso')
            return redirect(url_for('estoques_ti.epi_item', item_id=cur.lastrowid))
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), 'erro')
            return redirect(url_for('estoques_ti.epi'))
    busca = request.args.get('q', '').strip()[:150]
    itens = conn.execute("""SELECT * FROM epi_itens WHERE lower(nome) LIKE lower(?) OR lower(ca) LIKE lower(?) OR lower(lote) LIKE lower(?)
                            ORDER BY ativo DESC,nome,tamanho""", ('%' + busca + '%',) * 3).fetchall()
    return render_page('Estoque de EPI', EPI_LISTA, itens=itens, busca=busca, data_hoje=hoje())

@bp.route('/epi/<int:item_id>', methods=['GET', 'POST'])
@permissao('epi')
def epi_item(item_id):
    conn = db()
    item = conn.execute('SELECT * FROM epi_itens WHERE id=?', (item_id,)).fetchone()
    if not item:
        abort(404)
    if request.method == 'POST':
        if not tem_permissao('epi', 'gerenciar'):
            abort(403)
        try:
            acao = request.form.get('acao')
            conn.execute('BEGIN IMMEDIATE')
            item = conn.execute('SELECT * FROM epi_itens WHERE id=?', (item_id,)).fetchone()
            if acao == 'movimentar':
                if not item['ativo']:
                    raise ValueError('Reative o EPI antes de movimentar o estoque.')
                tipo = request.form.get('tipo')
                if tipo not in ('Entrada', 'Saída'):
                    raise ValueError('Tipo de movimentação inválido.')
                quantidade = _inteiro(request.form.get('quantidade'), 'Quantidade')
                observacao = texto_form('observacao', 'Origem ou motivo da movimentação', 1000)
                colaborador = _ativo(conn, request.form.get('colaborador_id')) if tipo == 'Saída' else None
                if tipo == 'Saída' and item['validade'] and (item['validade'] < hoje()):
                    raise ValueError('Este lote está vencido e não pode ser entregue.')
                if tipo == 'Saída' and quantidade > item['saldo']:
                    raise ValueError('Saldo insuficiente. A saída não foi registrada.')
                ajuste = quantidade if tipo == 'Entrada' else -quantidade
                conn.execute('UPDATE epi_itens SET saldo=saldo+? WHERE id=?', (ajuste, item_id))
                cur = conn.execute("""INSERT INTO epi_movimentos
                    (item_id,tipo,quantidade,colaborador_id,observacao,autor_id,criado_em,
                     nome_epi,ca_epi,tamanho_epi,lote_epi,validade_epi,unidade_epi)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (item_id, tipo, quantidade, colaborador, observacao, current_user()['id'], agora(), item['nome'], item['ca'], item['tamanho'], item['lote'], item['validade'], item['unidade']))
                auditar('epi', tipo + ' de EPI', cur.lastrowid, f'Item {item_id}; quantidade {quantidade}', conn=conn)
                flash('Movimentação registrada e saldo atualizado.', 'sucesso')
            elif acao == 'editar':
                nome = texto_form('nome', 'Nome', 150)
                ca = texto_form('ca', 'CA', 40, False)
                tamanho = texto_form('tamanho', 'Tamanho', 40, False)
                lote = texto_form('lote', 'Lote', 60, False)
                validade = _data(request.form.get('validade', ''))
                unidade = texto_form('unidade', 'Unidade', 20)
                minimo = _inteiro(request.form.get('estoque_minimo'), 'Estoque mínimo', 0)
                ativo = 1 if request.form.get('ativo') == '1' else 0
                identidade = (ca, tamanho, lote, validade, unidade)
                anterior = tuple((item[chave] for chave in ('ca', 'tamanho', 'lote', 'validade', 'unidade')))
                if identidade != anterior and conn.execute('SELECT 1 FROM epi_movimentos WHERE item_id=? LIMIT 1', (item_id,)).fetchone():
                    raise ValueError('Este lote já possui movimentações. Cadastre outro EPI para um novo lote, CA, tamanho, unidade ou validade.')
                conn.execute("""UPDATE epi_itens SET nome=?,ca=?,tamanho=?,lote=?,validade=?,unidade=?,
                                estoque_minimo=?,ativo=? WHERE id=?""", (nome, ca, tamanho, lote, validade, unidade, minimo, ativo, item_id))
                auditar('epi', 'Atualizar EPI', item_id, nome, conn=conn)
                flash('Cadastro atualizado.', 'sucesso')
            else:
                raise ValueError('Operação inválida.')
            conn.commit()
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), 'erro')
        return redirect(url_for('estoques_ti.epi_item', item_id=item_id))
    movimentos = conn.execute("""SELECT m.*,u.nome AS colaborador,a.nome AS autor
        FROM epi_movimentos m LEFT JOIN usuarios u ON u.id=m.colaborador_id
        JOIN usuarios a ON a.id=m.autor_id WHERE m.item_id=? ORDER BY m.id DESC""", (item_id,)).fetchall()
    return render_page(item['nome'], EPI_DETALHE, item=item, movimentos=movimentos, usuarios=_usuarios(conn), data_hoje=hoje())
EPI_LISTA = ler_template('epi/epi_lista.html')
EPI_DETALHE = ler_template('epi/epi_detalhe.html')
