"""Rotas e regras da área: chamados."""
from ..templates_loader import ler_template
from datetime import datetime, timedelta
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
)
from ..estoques_ti import PRIORIDADES, TRANSICOES, _ativo, bp

def _responsaveis(conn):
    return conn.execute("""SELECT id,nome FROM usuarios u WHERE ativo=1 AND
        (perfil='admin' OR EXISTS(SELECT 1 FROM permissoes p WHERE p.perfil=u.perfil
          AND p.modulo='chamados' AND p.acao='gerenciar')) ORDER BY nome""").fetchall()

def _ticket(conn, chamado_id):
    ticket = conn.execute("""SELECT c.*,u.nome AS solicitante,r.nome AS responsavel FROM ti_chamados c
        JOIN usuarios u ON u.id=c.solicitante_id LEFT JOIN usuarios r ON r.id=c.responsavel_id WHERE c.id=?""", (chamado_id,)).fetchone()
    if ticket is None:
        abort(404)
    if ticket['solicitante_id'] != current_user()['id'] and (not tem_permissao('chamados', 'gerenciar')):
        abort(403)
    return ticket

def _prazo(inicio, prioridade):
    return (datetime.fromisoformat(inicio) + timedelta(hours=PRIORIDADES[prioridade])).isoformat(timespec='seconds')

def _evento(conn, chamado_id, acao, mensagem):
    conn.execute("""INSERT INTO ti_chamado_historico (chamado_id,autor_id,acao,mensagem,criado_em)
                    VALUES (?,?,?,?,?)""", (chamado_id, current_user()['id'], acao, mensagem, agora()))
    auditar('chamados', acao, chamado_id, conn=conn)

def _anexar(conn, chamado_id):
    chave, nome = salvar_anexo(request.files.get('anexo'))
    if chave:
        conn.execute("""INSERT INTO ti_chamado_anexos (chamado_id,autor_id,chave,nome,criado_em)
                        VALUES (?,?,?,?,?)""", (chamado_id, current_user()['id'], chave, nome, agora()))

def _notificar(conn, ticket, mensagem):
    conn.execute("""INSERT INTO ti_notificacoes (usuario_id,chamado_id,mensagem,criado_em)
                    VALUES (?,?,?,?)""", (ticket['solicitante_id'], ticket['id'], mensagem, agora()))

@bp.route('/chamados', methods=['GET', 'POST'])
@permissao('chamados')
def chamados():
    conn = db()
    if request.method == 'POST':
        try:
            titulo = texto_form('titulo', 'Título', 150)
            descricao = texto_form('descricao', 'Descrição', 5000)
            categoria = texto_form('categoria', 'Categoria', 80)
            prioridade = request.form.get('prioridade')
            if prioridade not in PRIORIDADES:
                raise ValueError('Prioridade inválida.')
            inicio = agora()
            cur = conn.execute("""INSERT INTO ti_chamados
                (titulo,descricao,categoria,prioridade,solicitante_id,criado_em,atualizado_em,prazo_sla)
                VALUES (?,?,?,?,?,?,?,?)""", (titulo, descricao, categoria, prioridade, current_user()['id'], inicio, inicio, _prazo(inicio, prioridade)))
            _anexar(conn, cur.lastrowid)
            _evento(conn, cur.lastrowid, 'Abertura', 'Chamado aberto pelo solicitante.')
            conn.commit()
            flash('Chamado aberto. Acompanhe o atendimento nesta página.', 'sucesso')
            return redirect(url_for('estoques_ti.chamado', chamado_id=cur.lastrowid))
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), 'erro')
            return redirect(url_for('estoques_ti.chamados'))
    busca = request.args.get('q', '').strip()[:150]
    status = request.args.get('status', '')
    prioridade = request.args.get('prioridade', '')
    sql = """SELECT c.*,u.nome AS solicitante,r.nome AS responsavel FROM ti_chamados c
        JOIN usuarios u ON u.id=c.solicitante_id LEFT JOIN usuarios r ON r.id=c.responsavel_id
        WHERE (lower(c.titulo) LIKE lower(?) OR lower(c.descricao) LIKE lower(?))"""
    args = ['%' + busca + '%'] * 2
    if not tem_permissao('chamados', 'gerenciar'):
        sql += ' AND c.solicitante_id=?'
        args.append(current_user()['id'])
    if status:
        sql += ' AND c.status=?'
        args.append(status)
    if prioridade:
        sql += ' AND c.prioridade=?'
        args.append(prioridade)
    tickets = conn.execute(sql + ' ORDER BY c.id DESC', args).fetchall()
    notificacoes = conn.execute("""SELECT * FROM ti_notificacoes WHERE usuario_id=? AND lida=0
                                  ORDER BY id DESC""", (current_user()['id'],)).fetchall()
    return render_page('Chamados de TI', CHAMADOS_LISTA, tickets=tickets, busca=busca, status=status, prioridade=prioridade, prioridades=PRIORIDADES, statuses=TRANSICOES, notificacoes=notificacoes, momento=agora())

@bp.route('/chamados/<int:chamado_id>', methods=['GET', 'POST'])
@permissao('chamados')
def chamado(chamado_id):
    conn = db()
    ticket = _ticket(conn, chamado_id)
    if request.method == 'POST':
        try:
            acao = request.form.get('acao')
            conn.execute('BEGIN IMMEDIATE')
            ticket = _ticket(conn, chamado_id)
            if acao == 'comentar':
                if ticket['status'] == 'Encerrado':
                    raise ValueError('O chamado foi encerrado. Abra um novo chamado para outra solicitação.')
                mensagem = texto_form('mensagem', 'Comentário', 5000)
                _anexar(conn, chamado_id)
                _evento(conn, chamado_id, 'Comentário', mensagem)
                if tem_permissao('chamados', 'gerenciar') and ticket['solicitante_id'] != current_user()['id']:
                    _notificar(conn, ticket, f'Novo comentário da TI no chamado #{chamado_id}.')
            elif acao == 'atender':
                if not tem_permissao('chamados', 'gerenciar'):
                    abort(403)
                if ticket['status'] == 'Encerrado':
                    raise ValueError('O chamado já está encerrado.')
                mensagem = texto_form('mensagem', 'Registro do atendimento', 5000)
                prioridade = request.form.get('prioridade', ticket['prioridade'])
                if prioridade not in PRIORIDADES:
                    raise ValueError('Prioridade inválida.')
                responsavel = _ativo(conn, request.form.get('responsavel_id'))
                if responsavel not in {u['id'] for u in _responsaveis(conn)}:
                    raise ValueError('O responsável deve ter permissão para atender chamados.')
                novo = request.form.get('status', ticket['status'])
                if novo != ticket['status'] and novo not in TRANSICOES[ticket['status']]:
                    raise ValueError('Mudança de status não permitida para esta etapa.')
                resolvido = ticket['resolvido_em']
                if novo == 'Resolvido' and ticket['status'] != 'Resolvido':
                    resolvido = agora()
                elif novo in ('Aberto', 'Em atendimento', 'Aguardando solicitante'):
                    resolvido = None
                encerrado = agora() if novo == 'Encerrado' else None
                conn.execute("""UPDATE ti_chamados SET status=?,prioridade=?,responsavel_id=?,prazo_sla=?,
                    resolvido_em=?,encerrado_em=? WHERE id=?""", (novo, prioridade, responsavel, _prazo(ticket['criado_em'], prioridade), resolvido, encerrado, chamado_id))
                registro = f"{ticket['status']} → {novo}. Prioridade: {prioridade}. Responsável: {responsavel}. {mensagem}"
                _evento(conn, chamado_id, 'Atendimento', registro)
                if novo != ticket['status']:
                    _notificar(conn, ticket, f'Chamado #{chamado_id}: {novo.lower()}.')
            elif acao in ('confirmar', 'reabrir'):
                if ticket['solicitante_id'] != current_user()['id']:
                    abort(403)
                if ticket['status'] != 'Resolvido':
                    raise ValueError('Esta ação está disponível após a resolução pela TI.')
                mensagem = texto_form('mensagem', 'Observação', 2000)
                if acao == 'confirmar':
                    conn.execute("UPDATE ti_chamados SET status='Encerrado',encerrado_em=? WHERE id=?", (agora(), chamado_id))
                    _evento(conn, chamado_id, 'Encerramento confirmado', mensagem)
                    _notificar(conn, ticket, f'Chamado #{chamado_id} encerrado com sua confirmação.')
                else:
                    conn.execute("UPDATE ti_chamados SET status='Em atendimento',resolvido_em=NULL WHERE id=?", (chamado_id,))
                    _evento(conn, chamado_id, 'Reabertura pelo solicitante', mensagem)
            else:
                raise ValueError('Operação inválida.')
            conn.execute('UPDATE ti_chamados SET atualizado_em=? WHERE id=?', (agora(), chamado_id))
            conn.commit()
            flash('Chamado atualizado.', 'sucesso')
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), 'erro')
        return redirect(url_for('estoques_ti.chamado', chamado_id=chamado_id))
    historico = conn.execute("""SELECT h.*,u.nome AS autor FROM ti_chamado_historico h
        JOIN usuarios u ON u.id=h.autor_id WHERE h.chamado_id=? ORDER BY h.id""", (chamado_id,)).fetchall()
    anexos = conn.execute('SELECT * FROM ti_chamado_anexos WHERE chamado_id=? ORDER BY id', (chamado_id,)).fetchall()
    return render_page(f"Chamado #{chamado_id} · {ticket['titulo']}", CHAMADO_DETALHE, ticket=ticket, historico=historico, anexos=anexos, usuarios=_responsaveis(conn), prioridades=PRIORIDADES, transicoes=TRANSICOES[ticket['status']], momento=agora())

@bp.get('/chamados/anexos/<int:anexo_id>')
@permissao('chamados')
def chamado_anexo(anexo_id):
    conn = db()
    anexo = conn.execute('SELECT * FROM ti_chamado_anexos WHERE id=?', (anexo_id,)).fetchone()
    if not anexo:
        abort(404)
    _ticket(conn, anexo['chamado_id'])
    auditar('chamados', 'Baixar evidência', anexo['chamado_id'], anexo['nome'], conn=conn)
    conn.commit()
    return baixar_anexo(anexo['chave'], anexo['nome'])

@bp.post('/chamados/notificacoes/<int:notificacao_id>/lida')
@permissao('chamados')
def notificacao_lida(notificacao_id):
    conn = db()
    notificacao = conn.execute('SELECT id FROM ti_notificacoes WHERE id=? AND usuario_id=?', (notificacao_id, current_user()['id'])).fetchone()
    if not notificacao:
        abort(404)
    conn.execute('UPDATE ti_notificacoes SET lida=1 WHERE id=?', (notificacao_id,))
    auditar('chamados', 'Ler notificação', notificacao_id, conn=conn)
    conn.commit()
    return redirect(url_for('estoques_ti.chamados'))
CHAMADOS_LISTA = ler_template('chamados/chamados_lista.html')
CHAMADO_DETALHE = ler_template('chamados/chamado_detalhe.html')
