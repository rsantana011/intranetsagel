from .templates_loader import ler_template
'Solicitações, cotações e documentos privados da SAGEL.'
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from urllib.parse import urlsplit
from flask import Blueprint, abort, flash, redirect, request, url_for
from .base import (
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
    valor_decimal,
)
bp = Blueprint('compras_documentos', __name__)
PERFIS = ('admin', 'frota', 'almoxarifado', 'compras', 'ti', 'documentacao', 'colaborador')
CATEGORIAS = ('Políticas', 'Contratos', 'Certificados', 'Relatórios', 'Atas', 'Procedimentos', 'Documentos gerenciais', 'Outros')
ESTADOS = {'solicitada': 'Aguardando aprovação', 'ajuste': 'Devolvida para ajuste', 'cotacao': 'Em cotação', 'aprovada': 'Compra aprovada', 'pedido': 'Pedido emitido', 'cancelada': 'Cancelada'}

def init_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS cp_solicitacoes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, titulo TEXT NOT NULL,
        solicitante_id INTEGER NOT NULL REFERENCES usuarios(id), setor TEXT NOT NULL,
        centro_custo TEXT NOT NULL, justificativa TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'solicitada', criado_em TEXT NOT NULL,
        atualizado_em TEXT NOT NULL, anexo_chave TEXT, anexo_nome TEXT,
        cotacao_aprovada_id INTEGER, aprovado_por INTEGER REFERENCES usuarios(id),
        aprovado_em TEXT);
    CREATE TABLE IF NOT EXISTS cp_itens (
        id INTEGER PRIMARY KEY AUTOINCREMENT, solicitacao_id INTEGER NOT NULL REFERENCES cp_solicitacoes(id),
        descricao TEXT NOT NULL, quantidade REAL NOT NULL CHECK(quantidade > 0), unidade TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cp_fornecedores (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, documento TEXT NOT NULL DEFAULT '',
        contato TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
        ativo INTEGER NOT NULL DEFAULT 1, criado_em TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cp_cotacoes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, solicitacao_id INTEGER NOT NULL REFERENCES cp_solicitacoes(id),
        fornecedor_id INTEGER NOT NULL REFERENCES cp_fornecedores(id),
        fornecedor_nome TEXT NOT NULL, condicoes TEXT NOT NULL, prazo TEXT NOT NULL,
        total_centavos INTEGER NOT NULL CHECK(total_centavos > 0), criado_por INTEGER REFERENCES usuarios(id),
        criado_em TEXT NOT NULL, anexo_chave TEXT, anexo_nome TEXT);
    CREATE TABLE IF NOT EXISTS cp_cotacao_itens (
        id INTEGER PRIMARY KEY AUTOINCREMENT, cotacao_id INTEGER NOT NULL REFERENCES cp_cotacoes(id),
        item_id INTEGER NOT NULL REFERENCES cp_itens(id), preco_centavos INTEGER NOT NULL CHECK(preco_centavos > 0),
        total_centavos INTEGER NOT NULL CHECK(total_centavos > 0));
    CREATE TABLE IF NOT EXISTS cp_pedidos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, solicitacao_id INTEGER NOT NULL UNIQUE REFERENCES cp_solicitacoes(id),
        cotacao_id INTEGER NOT NULL REFERENCES cp_cotacoes(id),
        status TEXT NOT NULL DEFAULT 'aberto', criado_em TEXT NOT NULL,
        atualizado_em TEXT NOT NULL, criado_por INTEGER REFERENCES usuarios(id));
    CREATE TABLE IF NOT EXISTS cp_historico (
        id INTEGER PRIMARY KEY AUTOINCREMENT, solicitacao_id INTEGER NOT NULL REFERENCES cp_solicitacoes(id),
        usuario_id INTEGER REFERENCES usuarios(id), acao TEXT NOT NULL, observacao TEXT NOT NULL DEFAULT '',
        criado_em TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS cp_solicitante_idx ON cp_solicitacoes(solicitante_id, criado_em);
    CREATE INDEX IF NOT EXISTS cp_cotacoes_idx ON cp_cotacoes(solicitacao_id);
    CREATE TABLE IF NOT EXISTS gd_documentos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, categoria TEXT NOT NULL,
        descricao TEXT NOT NULL DEFAULT '', palavras_chave TEXT NOT NULL DEFAULT '',
        responsavel_id INTEGER REFERENCES usuarios(id), criado_por INTEGER REFERENCES usuarios(id),
        criado_em TEXT NOT NULL, atualizado_em TEXT NOT NULL, validade TEXT,
        visibilidade TEXT NOT NULL DEFAULT 'restrito', versao_atual_id INTEGER,
        arquivado INTEGER NOT NULL DEFAULT 0, legado_id INTEGER UNIQUE);
    CREATE TABLE IF NOT EXISTS gd_versoes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, documento_id INTEGER NOT NULL REFERENCES gd_documentos(id),
        numero INTEGER NOT NULL, arquivo_chave TEXT, arquivo_nome TEXT, link TEXT,
        observacao TEXT NOT NULL DEFAULT '', criado_por INTEGER REFERENCES usuarios(id),
        criado_em TEXT NOT NULL, publicado_em TEXT, publicado_por INTEGER REFERENCES usuarios(id),
        UNIQUE(documento_id, numero));
    CREATE TABLE IF NOT EXISTS gd_acessos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, documento_id INTEGER NOT NULL REFERENCES gd_documentos(id),
        tipo TEXT NOT NULL CHECK(tipo IN ('perfil','setor','usuario')), valor TEXT NOT NULL,
        UNIQUE(documento_id, tipo, valor));
    CREATE INDEX IF NOT EXISTS gd_acessos_idx ON gd_acessos(documento_id);
    """)
    if conn.execute('SELECT 1 FROM documentos LIMIT 1').fetchone():
        for old in conn.execute('SELECT id,nome,categoria,link FROM documentos').fetchall():
            if conn.execute('SELECT 1 FROM gd_documentos WHERE legado_id=?', (old['id'],)).fetchone():
                continue
            when = agora()
            doc_id = conn.execute("""INSERT INTO gd_documentos
                (nome,categoria,criado_em,atualizado_em,visibilidade,legado_id)
                VALUES (?,?,?,?,'todos',?)""", (old['nome'], old['categoria'] or 'Outros', when, when, old['id'])).lastrowid
            version_id = conn.execute("""INSERT INTO gd_versoes
                (documento_id,numero,link,observacao,criado_em,publicado_em)
                VALUES (?,1,?,'Importado do cadastro anterior',?,?)""", (doc_id, old['link'], when, when)).lastrowid
            conn.execute('UPDATE gd_documentos SET versao_atual_id=? WHERE id=?', (version_id, doc_id))

def _integer(value, label='Registro'):
    try:
        result = int(value)
        if result < 1:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise ValueError(f'{label} inválido.') from None

def _date(value, label='Data'):
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
        if len(value) != 10:
            raise ValueError
        return parsed.isoformat()
    except (ValueError, TypeError):
        raise ValueError(f'{label} inválida.') from None

def _document_allowed(doc, conn=None):
    if tem_permissao('documentos', 'gerenciar'):
        return True
    if doc['arquivado'] or not doc['versao_atual_id']:
        return False
    if doc['visibilidade'] == 'todos':
        return True
    user = current_user()
    return bool((conn or db()).execute("""SELECT 1 FROM gd_acessos WHERE documento_id=? AND
        ((tipo='perfil' AND valor=?) OR (tipo='setor' AND valor=?) OR (tipo='usuario' AND valor=?)) LIMIT 1""", (doc['id'], user['perfil'], user['setor'] or '', str(user['id']))).fetchone())
REPORTS = {'compras_solicitacoes': {'titulo': 'Solicitações de compra', 'modulo': 'compras', 'data_coluna': 'Data', 'sql': """SELECT s.id AS "Numero",s.titulo AS "Solicitacao",u.nome AS "Solicitante",
            s.setor AS "Setor",s.centro_custo AS "Centro_de_custo",s.status AS "Etapa",
            substr(s.criado_em,1,10) AS "Data" FROM cp_solicitacoes s JOIN usuarios u ON u.id=s.solicitante_id"""}, 'compras_pedidos': {'titulo': 'Pedidos de compra', 'modulo': 'compras', 'data_coluna': 'Data', 'sql': """SELECT p.id AS "Pedido",s.id AS "Solicitacao",s.setor AS "Setor",q.fornecedor_nome AS "Fornecedor",
            q.total_centavos/100.0 AS "Total_reais",p.status AS "Situacao",q.prazo AS "Entrega_prevista",
            substr(p.criado_em,1,10) AS "Data" FROM cp_pedidos p JOIN cp_solicitacoes s ON s.id=p.solicitacao_id
            JOIN cp_cotacoes q ON q.id=p.cotacao_id"""}, 'documentos_validade': {'titulo': 'Documentos e validade', 'modulo': 'documentos', 'data_coluna': 'Data', 'sql': """SELECT d.id AS "Numero",d.nome AS "Documento",d.categoria AS "Categoria",u.nome AS "Responsavel",
            v.numero AS "Versao_vigente",d.validade AS "Validade",
            CASE WHEN d.arquivado=1 THEN 'Arquivado' WHEN d.versao_atual_id IS NULL THEN 'Rascunho' ELSE 'Publicado' END AS "Situacao",
            substr(d.criado_em,1,10) AS "Data" FROM gd_documentos d LEFT JOIN usuarios u ON u.id=d.responsavel_id
            LEFT JOIN gd_versoes v ON v.id=d.versao_atual_id"""}}

def indicadores(conn):
    cards = []
    if tem_permissao('compras'):
        manager = tem_permissao('compras', 'gerenciar') or tem_permissao('compras', 'aprovar')
        count = conn.execute("SELECT COUNT(*) FROM cp_solicitacoes WHERE status IN ('solicitada','ajuste','cotacao','aprovada')" + ('' if manager else ' AND solicitante_id=?'), () if manager else (current_user()['id'],)).fetchone()[0]
        cards.append({'titulo': 'Compras em andamento', 'valor': count, 'url': '/compras'})
    if tem_permissao('documentos'):
        due = (date.fromisoformat(hoje()) + timedelta(days=30)).isoformat()
        rows = conn.execute('SELECT * FROM gd_documentos WHERE arquivado=0 AND validade<=?', (due,)).fetchall()
        count = sum((_document_allowed(r, conn) for r in rows))
        cards.append({'titulo': 'Documentos para renovar', 'valor': count, 'url': '/documentos'})
    return cards
