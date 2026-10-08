"""Rotas e regras da área: documentacao."""
from ..templates_loader import ler_template
from datetime import date, timedelta
from urllib.parse import urlsplit
from flask import abort, flash, redirect, request, url_for
from ..base import (
    agora,
    auditar,
    baixar_anexo,
    current_user,
    db,
    hoje,
    permissao,
    render_page,
    salvar_anexo,
    tem_permissao,
    texto_form,
)
from ..compras_documentos import CATEGORIAS, PERFIS, _date, _document_allowed, _integer, bp

def _safe_link(link):
    if not link:
        return None
    try:
        if len(link) > 2000 or any((ord(char) < 32 for char in link)) or '\\' in link:
            raise ValueError
        parsed = urlsplit(link)
        if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        return link
    except (ValueError, TypeError):
        raise ValueError('O link deve ser um endereço http:// ou https:// válido, sem usuário ou senha.') from None

def _document(did, conn=None):
    conn = conn or db()
    row = conn.execute("""SELECT d.*,u.nome AS responsavel FROM gd_documentos d
        LEFT JOIN usuarios u ON u.id=d.responsavel_id WHERE d.id=?""", (did,)).fetchone()
    if not row:
        abort(404)
    if not _document_allowed(row, conn):
        abort(403)
    return row

def _doc_metadata(conn):
    name = texto_form('nome', 'Nome do documento', limite=200)
    category = texto_form('categoria', 'Categoria', limite=100)
    description = texto_form('descricao', 'Descrição', limite=4000, obrigatorio=False)
    keywords = texto_form('palavras_chave', 'Palavras-chave', limite=500, obrigatorio=False)
    responsible = _integer(request.form.get('responsavel_id'), 'Responsável')
    if not conn.execute('SELECT 1 FROM usuarios WHERE id=? AND ativo=1', (responsible,)).fetchone():
        raise ValueError('Selecione um responsável ativo.')
    expiry = _date(request.form.get('validade', ''), 'Validade')
    visibility = request.form.get('visibilidade', 'restrito')
    if visibility not in ('todos', 'restrito'):
        raise ValueError('Visibilidade inválida.')
    profiles = set(request.form.getlist('perfis'))
    if not profiles.issubset(PERFIS):
        raise ValueError('Perfil de acesso inválido.')
    sectors = {v.strip() for v in request.form.get('setores', '').split(',') if v.strip()}
    if len(sectors) > 100 or any((len(s) > 100 for s in sectors)):
        raise ValueError('Informe no máximo 100 setores, com até 100 caracteres cada.')
    users = {_integer(v, 'Usuário autorizado') for v in request.form.getlist('usuarios')}
    if len(users) > 500:
        raise ValueError('Selecione no máximo 500 usuários.')
    for uid in users:
        if not conn.execute('SELECT 1 FROM usuarios WHERE id=? AND ativo=1', (uid,)).fetchone():
            raise ValueError('Um usuário autorizado não está ativo.')
    acl = [('perfil', p) for p in sorted(profiles)] + [('setor', s) for s in sorted(sectors)] + [('usuario', str(u)) for u in sorted(users)]
    return ((name, category, description, keywords, responsible, expiry, visibility), acl)

def _doc_content():
    link = _safe_link(texto_form('link', 'Link', limite=2000, obrigatorio=False))
    upload = request.files.get('arquivo')
    if link and upload and upload.filename:
        raise ValueError('Escolha arquivo ou link para esta versão, não os dois.')
    if not link and (not (upload and upload.filename)):
        raise ValueError('Anexe um arquivo ou informe o link do documento.')
    key, filename = salvar_anexo(upload)
    return (key, filename, link)

def _set_acl(conn, did, acl):
    conn.execute('DELETE FROM gd_acessos WHERE documento_id=?', (did,))
    conn.executemany('INSERT INTO gd_acessos(documento_id,tipo,valor) VALUES(?,?,?)', [(did, *a) for a in acl])

def _doc_form_context(conn, doc=None):
    acl = conn.execute('SELECT tipo,valor FROM gd_acessos WHERE documento_id=?', (doc['id'],)).fetchall() if doc else []
    return dict(categorias=CATEGORIAS, perfis=PERFIS, usuarios=conn.execute('SELECT id,nome,setor FROM usuarios WHERE ativo=1 ORDER BY nome').fetchall(), acl_perfis={a['valor'] for a in acl if a['tipo'] == 'perfil'}, acl_usuarios={int(a['valor']) for a in acl if a['tipo'] == 'usuario'}, acl_setores=', '.join((a['valor'] for a in acl if a['tipo'] == 'setor')))
DOC_METADATA_FORM = ler_template('documentacao/doc_metadata_form.html')

@bp.route('/documentos', methods=['GET', 'POST'])
@permissao('documentos')
def documentos():
    conn = db()
    if request.method == 'POST':
        if not tem_permissao('documentos', 'gerenciar'):
            abort(403)
        try:
            metadata, acl = _doc_metadata(conn)
            content = _doc_content()
            note = texto_form('observacao', 'Observação da versão', limite=2000, obrigatorio=False)
            publish = request.form.get('publicar') == '1'
            conn.execute('BEGIN IMMEDIATE')
            did = conn.execute("""INSERT INTO gd_documentos
                (nome,categoria,descricao,palavras_chave,responsavel_id,validade,visibilidade,criado_por,criado_em,atualizado_em)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (*metadata, current_user()['id'], agora(), agora())).lastrowid
            vid = conn.execute("""INSERT INTO gd_versoes(documento_id,numero,arquivo_chave,arquivo_nome,link,
                observacao,criado_por,criado_em,publicado_em,publicado_por) VALUES(?,1,?,?,?,?,?,?,?,?)""", (did, *content, note, current_user()['id'], agora(), agora() if publish else None, current_user()['id'] if publish else None)).lastrowid
            if publish:
                conn.execute('UPDATE gd_documentos SET versao_atual_id=? WHERE id=?', (vid, did))
            _set_acl(conn, did, acl)
            auditar('documentos', 'Documento incluído', did, 'Versão 1', conn=conn)
            if publish:
                auditar('documentos', 'Versão publicada', did, 'Versão 1', conn=conn)
            conn.commit()
            flash('Documento cadastrado.', 'sucesso')
            return redirect(url_for('.documento_detalhe', did=did))
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), 'erro')
    where, args = ([], [])
    query = request.args.get('q', '').strip()[:200]
    category = request.args.get('categoria', '').strip()[:100]
    responsible = request.args.get('responsavel', '').strip()[:200]
    expiry_status = request.args.get('validade', '')
    show_archived = request.args.get('arquivados') == '1' and tem_permissao('documentos', 'gerenciar')
    if not show_archived:
        where.append('d.arquivado=0')
    if query:
        where.append('(lower(d.nome) LIKE lower(?) OR lower(d.descricao) LIKE lower(?) OR lower(d.palavras_chave) LIKE lower(?))')
        args.extend(['%' + query + '%'] * 3)
    if category:
        where.append('d.categoria=?')
        args.append(category)
    if responsible:
        where.append('lower(u.nome) LIKE lower(?)')
        args.append('%' + responsible + '%')
    for field, operator in (('inicio', '>='), ('fim', '<=')):
        try:
            val = _date(request.args.get(field, ''), 'Período')
            if val:
                where.append(f'substr(d.criado_em,1,10){operator}?')
                args.append(val)
        except ValueError as exc:
            flash(str(exc), 'erro')
    due = (date.fromisoformat(hoje()) + timedelta(days=30)).isoformat()
    if expiry_status == 'vencidos':
        where.append('d.validade<?')
        args.append(hoje())
    elif expiry_status == 'proximos':
        where.append('d.validade BETWEEN ? AND ?')
        args.extend([hoje(), due])
    candidates = conn.execute("""SELECT d.*,u.nome AS responsavel,v.numero AS versao
        FROM gd_documentos d LEFT JOIN usuarios u ON u.id=d.responsavel_id
        LEFT JOIN gd_versoes v ON v.id=d.versao_atual_id """ + ('WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY d.atualizado_em DESC,d.id DESC', args).fetchall()
    rows = [r for r in candidates if _document_allowed(r, conn)]
    accessible = [r for r in conn.execute('SELECT * FROM gd_documentos WHERE arquivado=0').fetchall() if _document_allowed(r, conn)]
    available_categories = sorted({r['categoria'] for r in accessible})
    vencidos = sum((bool(r['validade'] and r['validade'] < hoje()) for r in accessible))
    proximos = sum((bool(r['validade'] and hoje() <= r['validade'] <= due) for r in accessible))
    context = _doc_form_context(conn) if tem_permissao('documentos', 'gerenciar') else {}
    return render_page('Documentação gerencial', DOCUMENTOS_LISTA + (DOCUMENTO_NOVO if tem_permissao('documentos', 'gerenciar') else ''), rows=rows, q=query, categoria=category, responsavel=responsible, categorias_filtro=available_categories, vencidos=vencidos, proximos=proximos, limite_validade=due, d=None, **context)
DOCUMENTOS_LISTA = ler_template('documentacao/documentos_lista.html')
DOCUMENTO_NOVO = ler_template('documentacao/documento_novo.html') + DOC_METADATA_FORM + ler_template('documentacao/documento_novo_2.html')

@bp.route('/documentos/<int:did>', methods=['GET', 'POST'])
@permissao('documentos')
def documento_detalhe(did):
    conn = db()
    doc = _document(did, conn)
    manager = tem_permissao('documentos', 'gerenciar')
    if request.method == 'POST':
        if not manager:
            abort(403)
        try:
            action = request.form.get('acao')
            conn.execute('BEGIN IMMEDIATE')
            doc = _document(did, conn)
            if action == 'metadados':
                metadata, acl = _doc_metadata(conn)
                conn.execute("""UPDATE gd_documentos SET nome=?,categoria=?,descricao=?,palavras_chave=?,
                    responsavel_id=?,validade=?,visibilidade=?,atualizado_em=? WHERE id=?""", (*metadata, agora(), did))
                _set_acl(conn, did, acl)
                auditar('documentos', 'Metadados e acessos alterados', did, conn=conn)
            elif action == 'versao':
                if doc['arquivado']:
                    raise ValueError('Restaure o documento antes de enviar uma nova versão.')
                content = _doc_content()
                note = texto_form('observacao', 'Resumo da alteração', limite=2000)
                number = conn.execute('SELECT COALESCE(MAX(numero),0)+1 FROM gd_versoes WHERE documento_id=?', (did,)).fetchone()[0]
                publish = request.form.get('publicar') == '1'
                vid = conn.execute("""INSERT INTO gd_versoes(documento_id,numero,arquivo_chave,arquivo_nome,link,
                    observacao,criado_por,criado_em,publicado_em,publicado_por) VALUES(?,?,?,?,?,?,?,?,?,?)""", (did, number, *content, note, current_user()['id'], agora(), agora() if publish else None, current_user()['id'] if publish else None)).lastrowid
                if publish:
                    conn.execute('UPDATE gd_documentos SET versao_atual_id=? WHERE id=?', (vid, did))
                    auditar('documentos', 'Versão publicada', did, f'Versão {number}', conn=conn)
                conn.execute('UPDATE gd_documentos SET atualizado_em=? WHERE id=?', (agora(), did))
                auditar('documentos', 'Nova versão incluída', did, f'Versão {number}', conn=conn)
            elif action == 'publicar':
                if doc['arquivado']:
                    raise ValueError('Restaure o documento antes de publicar uma versão.')
                vid = _integer(request.form.get('versao_id'), 'Versão')
                version = conn.execute('SELECT * FROM gd_versoes WHERE id=? AND documento_id=?', (vid, did)).fetchone()
                if not version:
                    raise ValueError('Versão não encontrada neste documento.')
                if version['link']:
                    _safe_link(version['link'])
                if not version['arquivo_chave'] and (not version['link']):
                    raise ValueError('Envie um arquivo ou link válido em uma nova versão.')
                conn.execute('UPDATE gd_versoes SET publicado_em=COALESCE(publicado_em,?),publicado_por=COALESCE(publicado_por,?) WHERE id=?', (agora(), current_user()['id'], vid))
                conn.execute('UPDATE gd_documentos SET versao_atual_id=?,atualizado_em=? WHERE id=?', (vid, agora(), did))
                auditar('documentos', 'Versão publicada como vigente', did, f"Versão {version['numero']}", conn=conn)
            elif action == 'arquivar':
                archived = 0 if doc['arquivado'] else 1
                conn.execute('UPDATE gd_documentos SET arquivado=?,atualizado_em=? WHERE id=?', (archived, agora(), did))
                auditar('documentos', 'Documento arquivado' if archived else 'Documento restaurado', did, conn=conn)
            else:
                raise ValueError('Ação inválida.')
            conn.commit()
            flash('Documento atualizado.', 'sucesso')
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), 'erro')
        return redirect(url_for('.documento_detalhe', did=did))
    versions = conn.execute("""SELECT v.*,u.nome AS autor FROM gd_versoes v
        LEFT JOIN usuarios u ON u.id=v.criado_por WHERE documento_id=? """ + ('' if manager else 'AND publicado_em IS NOT NULL ') + 'ORDER BY numero DESC', (did,)).fetchall()
    auditar('documentos', 'Documento visualizado', did)
    conn.commit()
    context = _doc_form_context(conn, doc) if manager else {}
    body = DOCUMENTO_DETALHE
    if manager:
        body += DOCUMENTO_EDICAO
    return render_page(doc['nome'], body, d=doc, versions=versions, limite_validade=(date.fromisoformat(hoje()) + timedelta(days=30)).isoformat(), **context)
DOCUMENTO_DETALHE = ler_template('documentacao/documento_detalhe.html')
DOCUMENTO_EDICAO = ler_template('documentacao/documento_edicao.html') + DOC_METADATA_FORM + ler_template('documentacao/documento_edicao_2.html')

@bp.get('/documentos/<int:did>/versoes/<int:vid>/conteudo')
@permissao('documentos')
def documento_conteudo(did, vid):
    conn = db()
    _document(did, conn)
    version = conn.execute('SELECT * FROM gd_versoes WHERE id=? AND documento_id=?', (vid, did)).fetchone()
    if not version:
        abort(404)
    if not version['publicado_em'] and (not tem_permissao('documentos', 'gerenciar')):
        abort(403)
    if version['arquivo_chave']:
        auditar('documentos', 'Download de documento', did, f"Versão {version['numero']}")
        conn.commit()
        return baixar_anexo(version['arquivo_chave'], version['arquivo_nome'])
    try:
        link = _safe_link(version['link'])
        if not link:
            raise ValueError('Esta versão não tem conteúdo. Solicite a atualização ao responsável.')
    except ValueError as exc:
        flash(str(exc), 'erro')
        return redirect(url_for('.documento_detalhe', did=did))
    auditar('documentos', 'Link de documento acessado', did, f"Versão {version['numero']}")
    conn.commit()
    return redirect(link)
