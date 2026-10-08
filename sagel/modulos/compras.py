"""Rotas e regras da área: compras."""
from ..templates_loader import ler_template
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from flask import abort, flash, redirect, request, url_for
from ..base import (
    agora,
    auditar,
    baixar_anexo,
    current_user,
    db,
    permissao,
    render_page,
    salvar_anexo,
    tem_permissao,
    texto_form,
    valor_decimal,
)
from ..compras_documentos import ESTADOS, _date, _integer, bp

def _money(value):
    try:
        value = str(value).strip().replace(',', '.')
        amount = Decimal(value)
        if not amount.is_finite() or amount <= 0 or amount > Decimal('1000000000'):
            raise ValueError
        cents = int((amount * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        if cents <= 0:
            raise ValueError
        return cents
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError('Informe um preço positivo, com até duas casas decimais.') from None

def _brl(cents):
    return ('R$ ' + f'{cents / 100:,.2f}').replace(',', '_').replace('.', ',').replace('_', '.')

def _request_items(text):
    items = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split('|')]
        if len(parts) != 3 or not parts[0] or len(parts[0]) > 200 or (not parts[2]) or (len(parts[2]) > 30):
            raise ValueError('Em cada linha dos itens, use: descrição | quantidade | unidade.')
        qty = valor_decimal(parts[1], minimo=0.001)
        if qty > 1000000:
            raise ValueError('Quantidade acima do limite por item.')
        items.append((parts[0], qty, parts[2]))
    if not items or len(items) > 100:
        raise ValueError('Informe de 1 a 100 itens para a solicitação.')
    return items

def _purchase(sid, conn=None):
    conn = conn or db()
    row = conn.execute("""SELECT s.*,u.nome AS solicitante FROM cp_solicitacoes s
        JOIN usuarios u ON u.id=s.solicitante_id WHERE s.id=?""", (sid,)).fetchone()
    if not row:
        abort(404)
    if row['solicitante_id'] != current_user()['id'] and (not (tem_permissao('compras', 'gerenciar') or tem_permissao('compras', 'aprovar'))):
        abort(403)
    return row

def _history(conn, sid, action, note=''):
    conn.execute('INSERT INTO cp_historico(solicitacao_id,usuario_id,acao,observacao,criado_em) VALUES(?,?,?,?,?)', (sid, current_user()['id'], action, note, agora()))
    auditar('compras', action, sid, note, conn=conn)

@bp.route('/compras', methods=['GET', 'POST'])
@permissao('compras')
def compras():
    conn = db()
    if request.method == 'POST':
        try:
            title = texto_form('titulo', 'Título', limite=160)
            dept = texto_form('setor', 'Setor', limite=100)
            center = texto_form('centro_custo', 'Centro de custo', limite=100)
            reason = texto_form('justificativa', 'Justificativa', limite=4000)
            items = _request_items(texto_form('itens', 'Itens', limite=20000))
            key, name = salvar_anexo(request.files.get('anexo'))
            conn.execute('BEGIN IMMEDIATE')
            sid = conn.execute("""INSERT INTO cp_solicitacoes
                (titulo,solicitante_id,setor,centro_custo,justificativa,criado_em,atualizado_em,anexo_chave,anexo_nome)
                VALUES(?,?,?,?,?,?,?,?,?)""", (title, current_user()['id'], dept, center, reason, agora(), agora(), key, name)).lastrowid
            conn.executemany('INSERT INTO cp_itens(solicitacao_id,descricao,quantidade,unidade) VALUES(?,?,?,?)', [(sid, *item) for item in items])
            _history(conn, sid, 'Solicitação criada')
            conn.commit()
            flash('Solicitação enviada para aprovação.', 'sucesso')
            return redirect(url_for('.compra_detalhe', sid=sid))
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), 'erro')
    manager = tem_permissao('compras', 'gerenciar') or tem_permissao('compras', 'aprovar')
    where, args = ([], []) if manager else (['s.solicitante_id=?'], [current_user()['id']])
    status = request.args.get('status', '')
    if status in ESTADOS:
        where.append('s.status=?')
        args.append(status)
    query = request.args.get('q', '').strip()[:200]
    if query:
        where.append('(lower(s.titulo) LIKE lower(?) OR lower(s.setor) LIKE lower(?))')
        args.extend(['%' + query + '%'] * 2)
    rows = conn.execute("""SELECT s.*,u.nome AS solicitante,p.status AS pedido_status
        FROM cp_solicitacoes s JOIN usuarios u ON u.id=s.solicitante_id
        LEFT JOIN cp_pedidos p ON p.solicitacao_id=s.id """ + ('WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY s.id DESC', args).fetchall()
    counts = {state: sum((row['status'] == state for row in rows)) for state in ESTADOS}
    return render_page('Compras', COMPRAS_LISTA, rows=rows, estados=ESTADOS, counts=counts, filtro=status, q=query, manager=manager)
COMPRAS_LISTA = ler_template('compras/compras_lista.html')

@bp.route('/compras/<int:sid>', methods=['GET', 'POST'])
@permissao('compras')
def compra_detalhe(sid):
    conn = db()
    solicitation = _purchase(sid, conn)
    if request.method == 'POST':
        try:
            conn.execute('BEGIN IMMEDIATE')
            solicitation = _purchase(sid, conn)
            action = request.form.get('acao', '')
            state = solicitation['status']
            if action in ('aprovar', 'devolver', 'aprovar_final'):
                if not tem_permissao('compras', 'aprovar'):
                    abort(403)
            elif action in ('cotacao', 'pedido', 'atualizar_pedido'):
                if not tem_permissao('compras', 'gerenciar'):
                    abort(403)
            elif action in ('reenviar', 'cancelar'):
                if solicitation['solicitante_id'] != current_user()['id'] and (not tem_permissao('compras', 'gerenciar')):
                    abort(403)
            else:
                raise ValueError('Ação inválida.')
            if action == 'aprovar':
                if state != 'solicitada':
                    raise ValueError('A solicitação não está aguardando aprovação.')
                conn.execute("UPDATE cp_solicitacoes SET status='cotacao',atualizado_em=? WHERE id=?", (agora(), sid))
                _history(conn, sid, 'Solicitação aprovada para cotação')
            elif action == 'devolver':
                if state != 'solicitada':
                    raise ValueError('Somente solicitações aguardando aprovação podem ser devolvidas.')
                note = texto_form('observacao', 'Motivo do ajuste', limite=2000)
                conn.execute("UPDATE cp_solicitacoes SET status='ajuste',atualizado_em=? WHERE id=?", (agora(), sid))
                _history(conn, sid, 'Devolvida para ajuste', note)
            elif action == 'reenviar':
                if state != 'ajuste':
                    raise ValueError('Esta solicitação não está em ajuste.')
                title = texto_form('titulo', 'Título', limite=160)
                dept = texto_form('setor', 'Setor', limite=100)
                center = texto_form('centro_custo', 'Centro de custo', limite=100)
                reason = texto_form('justificativa', 'Justificativa', limite=4000)
                items = _request_items(texto_form('itens', 'Itens', limite=20000))
                old_items = conn.execute('SELECT descricao,quantidade,unidade FROM cp_itens WHERE solicitacao_id=?', (sid,)).fetchall()
                old_text = '; '.join((f"{i['descricao']} ({i['quantidade']:g} {i['unidade']})" for i in old_items))
                conn.execute('DELETE FROM cp_itens WHERE solicitacao_id=?', (sid,))
                conn.executemany('INSERT INTO cp_itens(solicitacao_id,descricao,quantidade,unidade) VALUES(?,?,?,?)', [(sid, *i) for i in items])
                conn.execute("UPDATE cp_solicitacoes SET titulo=?,setor=?,centro_custo=?,justificativa=?,status='solicitada',atualizado_em=? WHERE id=?", (title, dept, center, reason, agora(), sid))
                _history(conn, sid, 'Solicitação ajustada e reenviada', f"Dados anteriores: {solicitation['titulo']}; setor {solicitation['setor']}; centro {solicitation['centro_custo']}; justificativa {solicitation['justificativa']}; itens: {old_text}")
            elif action == 'cancelar':
                if state not in ('solicitada', 'ajuste', 'cotacao'):
                    raise ValueError('A compra já aprovada deve ser acompanhada pelo pedido.')
                if state == 'cotacao' and (not tem_permissao('compras', 'gerenciar')):
                    abort(403)
                note = texto_form('observacao', 'Motivo do cancelamento', limite=2000)
                conn.execute("UPDATE cp_solicitacoes SET status='cancelada',atualizado_em=? WHERE id=?", (agora(), sid))
                _history(conn, sid, 'Solicitação cancelada', note)
            elif action == 'cotacao':
                if state != 'cotacao':
                    raise ValueError('A solicitação precisa estar na etapa de cotação.')
                supplier = conn.execute('SELECT * FROM cp_fornecedores WHERE id=? AND ativo=1', (_integer(request.form.get('fornecedor_id'), 'Fornecedor'),)).fetchone()
                if not supplier:
                    raise ValueError('Escolha um fornecedor ativo.')
                terms = texto_form('condicoes', 'Condições de pagamento', limite=2000)
                deadline = _date(texto_form('prazo', 'Prazo de entrega', limite=10), 'Prazo de entrega')
                items = conn.execute('SELECT * FROM cp_itens WHERE solicitacao_id=?', (sid,)).fetchall()
                prices, total = ([], 0)
                for item in items:
                    cents = _money(request.form.get(f"preco_{item['id']}"))
                    item_total = int((Decimal(str(item['quantidade'])) * cents).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                    if item_total <= 0:
                        raise ValueError('O total de cada item deve ser de pelo menos um centavo.')
                    total += item_total
                    prices.append((item['id'], cents, item_total))
                if total > 100000000000000:
                    raise ValueError('Valor total acima do limite da solicitação.')
                key, name = salvar_anexo(request.files.get('anexo'))
                qid = conn.execute("""INSERT INTO cp_cotacoes(solicitacao_id,fornecedor_id,fornecedor_nome,
                    condicoes,prazo,total_centavos,criado_por,criado_em,anexo_chave,anexo_nome)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (sid, supplier['id'], supplier['nome'], terms, deadline, total, current_user()['id'], agora(), key, name)).lastrowid
                conn.executemany('INSERT INTO cp_cotacao_itens(cotacao_id,item_id,preco_centavos,total_centavos) VALUES(?,?,?,?)', [(qid, *p) for p in prices])
                _history(conn, sid, 'Cotação registrada', f"Cotação #{qid}; fornecedor {supplier['nome']}; total {_brl(total)}")
            elif action == 'aprovar_final':
                if state != 'cotacao':
                    raise ValueError('A solicitação não está aguardando aprovação de cotação.')
                quote = conn.execute('SELECT * FROM cp_cotacoes WHERE id=? AND solicitacao_id=?', (_integer(request.form.get('cotacao_id'), 'Cotação'), sid)).fetchone()
                if not quote:
                    raise ValueError('Selecione uma cotação desta solicitação.')
                note = texto_form('observacao', 'Justificativa da escolha', limite=2000)
                conn.execute("UPDATE cp_solicitacoes SET status='aprovada',cotacao_aprovada_id=?,aprovado_por=?,aprovado_em=?,atualizado_em=? WHERE id=?", (quote['id'], current_user()['id'], agora(), agora(), sid))
                _history(conn, sid, 'Compra aprovada', f"Cotação #{quote['id']} selecionada. {note}")
            elif action == 'pedido':
                if state != 'aprovada' or not solicitation['cotacao_aprovada_id']:
                    raise ValueError('É necessária a aprovação final antes de emitir o pedido.')
                pid = conn.execute('INSERT INTO cp_pedidos(solicitacao_id,cotacao_id,criado_em,atualizado_em,criado_por) VALUES(?,?,?,?,?)', (sid, solicitation['cotacao_aprovada_id'], agora(), agora(), current_user()['id'])).lastrowid
                conn.execute("UPDATE cp_solicitacoes SET status='pedido',atualizado_em=? WHERE id=?", (agora(), sid))
                _history(conn, sid, 'Pedido interno emitido', f'Pedido #{pid}')
            elif action == 'atualizar_pedido':
                order = conn.execute('SELECT * FROM cp_pedidos WHERE solicitacao_id=?', (sid,)).fetchone()
                status = request.form.get('status')
                if not order or order['status'] in ('concluido', 'cancelado'):
                    raise ValueError('O pedido não está aberto para atualização.')
                if status not in ('parcial', 'concluido', 'cancelado'):
                    raise ValueError('Escolha atendimento parcial, concluído ou cancelado.')
                note = texto_form('observacao', 'Observação do acompanhamento', limite=2000)
                conn.execute('UPDATE cp_pedidos SET status=?,atualizado_em=? WHERE id=?', (status, agora(), order['id']))
                _history(conn, sid, 'Pedido atualizado', f'{status}: {note}')
            conn.commit()
            flash('Alteração registrada.', 'sucesso')
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), 'erro')
        return redirect(url_for('.compra_detalhe', sid=sid))
    items = conn.execute('SELECT * FROM cp_itens WHERE solicitacao_id=? ORDER BY id', (sid,)).fetchall()
    quotes = conn.execute('SELECT * FROM cp_cotacoes WHERE solicitacao_id=? ORDER BY total_centavos,id', (sid,)).fetchall()
    quote_items = {q['id']: conn.execute("""SELECT qi.*,i.descricao,i.quantidade,i.unidade
        FROM cp_cotacao_itens qi JOIN cp_itens i ON i.id=qi.item_id WHERE qi.cotacao_id=? ORDER BY i.id""", (q['id'],)).fetchall() for q in quotes}
    order = conn.execute('SELECT * FROM cp_pedidos WHERE solicitacao_id=?', (sid,)).fetchone()
    history = conn.execute("""SELECT h.*,u.nome FROM cp_historico h LEFT JOIN usuarios u ON u.id=h.usuario_id
        WHERE solicitacao_id=? ORDER BY h.id DESC""", (sid,)).fetchall()
    suppliers = conn.execute('SELECT id,nome FROM cp_fornecedores WHERE ativo=1 ORDER BY nome').fetchall()
    items_text = """
""".join((f"{i['descricao']} | {i['quantidade']:g} | {i['unidade']}" for i in items))
    return render_page(f'Compra #{sid}', COMPRA_DETALHE, s=solicitation, items=items, quotes=quotes, quote_items=quote_items, order=order, history=history, suppliers=suppliers, estados=ESTADOS, brl=_brl, items_text=items_text)
COMPRA_DETALHE = ler_template('compras/compra_detalhe.html')

@bp.get('/compras/<int:sid>/anexo')
@permissao('compras')
def compra_anexo(sid):
    s = _purchase(sid)
    if not s['anexo_chave']:
        abort(404)
    auditar('compras', 'Download de anexo da solicitação', sid)
    db().commit()
    return baixar_anexo(s['anexo_chave'], s['anexo_nome'])

@bp.get('/compras/<int:sid>/cotacoes/<int:qid>/anexo')
@permissao('compras')
def cotacao_anexo(sid, qid):
    _purchase(sid)
    quote = db().execute('SELECT * FROM cp_cotacoes WHERE id=? AND solicitacao_id=?', (qid, sid)).fetchone()
    if not quote or not quote['anexo_chave']:
        abort(404)
    auditar('compras', 'Download de proposta', sid, f'Cotação #{qid}')
    db().commit()
    return baixar_anexo(quote['anexo_chave'], quote['anexo_nome'])

@bp.get('/compras/<int:sid>/pedido')
@permissao('compras')
def pedido_impressao(sid):
    s = _purchase(sid)
    order = db().execute("""SELECT p.*,q.fornecedor_nome,q.condicoes,q.prazo,q.total_centavos
        FROM cp_pedidos p JOIN cp_cotacoes q ON q.id=p.cotacao_id WHERE p.solicitacao_id=?""", (sid,)).fetchone()
    if not order:
        abort(404)
    items = db().execute("""SELECT i.*,qi.preco_centavos,qi.total_centavos FROM cp_cotacao_itens qi
        JOIN cp_itens i ON i.id=qi.item_id WHERE qi.cotacao_id=?""", (order['cotacao_id'],)).fetchall()
    return render_page(f"Pedido de compra #{order['id']}", ler_template('compras/pedido_impressao.html'), s=s, p=order, items=items, brl=_brl)

@bp.route('/compras/fornecedores', methods=['GET', 'POST'])
@permissao('compras', 'gerenciar')
def fornecedores():
    conn = db()
    if request.method == 'POST':
        try:
            if request.form.get('acao') == 'situacao':
                fid = _integer(request.form.get('id'), 'Fornecedor')
                active = request.form.get('ativo')
                if active not in ('0', '1'):
                    raise ValueError('Situação inválida.')
                if not conn.execute('SELECT 1 FROM cp_fornecedores WHERE id=?', (fid,)).fetchone():
                    abort(404)
                conn.execute('UPDATE cp_fornecedores SET ativo=? WHERE id=?', (int(active), fid))
                auditar('compras', 'Situação do fornecedor alterada', fid, 'Ativo' if active == '1' else 'Inativo')
            else:
                name = texto_form('nome', 'Nome do fornecedor', limite=200)
                doc = texto_form('documento', 'CNPJ / CPF', limite=30, obrigatorio=False)
                contact = texto_form('contato', 'Contato', limite=200, obrigatorio=False)
                email = texto_form('email', 'E-mail', limite=254, obrigatorio=False)
                if email and (not re.fullmatch('[^\\s@]+@[^\\s@]+\\.[^\\s@]+', email)):
                    raise ValueError('E-mail inválido.')
                fid = conn.execute('INSERT INTO cp_fornecedores(nome,documento,contato,email,criado_em) VALUES(?,?,?,?,?)', (name, doc, contact, email, agora())).lastrowid
                auditar('compras', 'Fornecedor cadastrado', fid, name)
            conn.commit()
            flash('Fornecedor atualizado.', 'sucesso')
            return redirect(url_for('.fornecedores'))
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), 'erro')
    rows = conn.execute('SELECT * FROM cp_fornecedores ORDER BY ativo DESC,nome').fetchall()
    return render_page('Fornecedores', ler_template('compras/fornecedores.html'), rows=rows)

@bp.route('/compras/fornecedores/<int:fid>', methods=['GET', 'POST'])
@permissao('compras', 'gerenciar')
def fornecedor_editar(fid):
    conn = db()
    supplier = conn.execute('SELECT * FROM cp_fornecedores WHERE id=?', (fid,)).fetchone()
    if not supplier:
        abort(404)
    if request.method == 'POST':
        try:
            name = texto_form('nome', 'Nome do fornecedor', limite=200)
            document = texto_form('documento', 'CNPJ / CPF', limite=30, obrigatorio=False)
            contact = texto_form('contato', 'Contato', limite=200, obrigatorio=False)
            email = texto_form('email', 'E-mail', limite=254, obrigatorio=False)
            if email and (not re.fullmatch('[^\\s@]+@[^\\s@]+\\.[^\\s@]+', email)):
                raise ValueError('E-mail inválido.')
            conn.execute('UPDATE cp_fornecedores SET nome=?,documento=?,contato=?,email=? WHERE id=?', (name, document, contact, email, fid))
            auditar('compras', 'Fornecedor editado', fid, name)
            conn.commit()
            flash('Cadastro do fornecedor atualizado. As propostas já registradas mantêm seus dados originais.', 'sucesso')
            return redirect(url_for('.fornecedores'))
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), 'erro')
    return render_page('Editar fornecedor', ler_template('compras/fornecedor_editar.html'), f=supplier)
