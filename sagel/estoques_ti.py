from .templates_loader import ler_template
'Estoque de EPI, patrimônio de TI e atendimento interno.'
from datetime import date, datetime, timedelta
import sqlite3
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
)
bp = Blueprint('estoques_ti', __name__)
PRIORIDADES = {'Baixa': 96, 'Normal': 48, 'Alta': 24, 'Urgente': 4}
TRANSICOES = {'Aberto': ('Em atendimento', 'Aguardando solicitante', 'Resolvido'), 'Em atendimento': ('Aguardando solicitante', 'Resolvido'), 'Aguardando solicitante': ('Em atendimento', 'Resolvido'), 'Resolvido': ('Em atendimento', 'Encerrado'), 'Encerrado': ()}

def init_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS epi_itens (
      id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL,
      ca TEXT NOT NULL DEFAULT '', tamanho TEXT NOT NULL DEFAULT '',
      lote TEXT NOT NULL DEFAULT '', validade TEXT, unidade TEXT NOT NULL DEFAULT 'un',
      estoque_minimo INTEGER NOT NULL DEFAULT 0 CHECK(estoque_minimo >= 0),
      saldo INTEGER NOT NULL DEFAULT 0 CHECK(saldo >= 0), ativo INTEGER NOT NULL DEFAULT 1,
      criado_em TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS epi_movimentos (
      id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL REFERENCES epi_itens(id),
      tipo TEXT NOT NULL CHECK(tipo IN ('Entrada','Saída')), quantidade INTEGER NOT NULL CHECK(quantidade>0),
      colaborador_id INTEGER REFERENCES usuarios(id), observacao TEXT NOT NULL,
      nome_epi TEXT NOT NULL DEFAULT '',ca_epi TEXT NOT NULL DEFAULT '',tamanho_epi TEXT NOT NULL DEFAULT '',
      lote_epi TEXT NOT NULL DEFAULT '',validade_epi TEXT,unidade_epi TEXT NOT NULL DEFAULT 'un',
      autor_id INTEGER NOT NULL REFERENCES usuarios(id), criado_em TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ti_equipamentos (
      id INTEGER PRIMARY KEY AUTOINCREMENT, patrimonio TEXT NOT NULL UNIQUE,
      nome TEXT NOT NULL, categoria TEXT NOT NULL, serie TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'Disponível', usuario_id INTEGER REFERENCES usuarios(id),
      observacao TEXT NOT NULL DEFAULT '', criado_em TEXT NOT NULL,
      CHECK(status IN ('Disponível','Alocado','Manutenção','Inativo')),
      CHECK((status='Alocado' AND usuario_id IS NOT NULL) OR
            (status<>'Alocado' AND usuario_id IS NULL)));
    CREATE UNIQUE INDEX IF NOT EXISTS ti_serie_unica ON ti_equipamentos(serie) WHERE serie<>'';
    CREATE TABLE IF NOT EXISTS ti_equipamento_historico (
      id INTEGER PRIMARY KEY AUTOINCREMENT, equipamento_id INTEGER NOT NULL REFERENCES ti_equipamentos(id),
      acao TEXT NOT NULL, usuario_id INTEGER REFERENCES usuarios(id),
      observacao TEXT NOT NULL, autor_id INTEGER NOT NULL REFERENCES usuarios(id), criado_em TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ti_chamados (
      id INTEGER PRIMARY KEY AUTOINCREMENT, titulo TEXT NOT NULL, descricao TEXT NOT NULL,
      categoria TEXT NOT NULL, prioridade TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Aberto',
      solicitante_id INTEGER NOT NULL REFERENCES usuarios(id), responsavel_id INTEGER REFERENCES usuarios(id),
      criado_em TEXT NOT NULL, atualizado_em TEXT NOT NULL, prazo_sla TEXT NOT NULL,
      resolvido_em TEXT, encerrado_em TEXT);
    CREATE INDEX IF NOT EXISTS ti_chamado_solicitante ON ti_chamados(solicitante_id,status);
    CREATE TABLE IF NOT EXISTS ti_chamado_historico (
      id INTEGER PRIMARY KEY AUTOINCREMENT, chamado_id INTEGER NOT NULL REFERENCES ti_chamados(id),
      autor_id INTEGER NOT NULL REFERENCES usuarios(id), acao TEXT NOT NULL,
      mensagem TEXT NOT NULL, criado_em TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ti_chamado_anexos (
      id INTEGER PRIMARY KEY AUTOINCREMENT, chamado_id INTEGER NOT NULL REFERENCES ti_chamados(id),
      autor_id INTEGER NOT NULL REFERENCES usuarios(id), chave TEXT NOT NULL,
      nome TEXT NOT NULL, criado_em TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ti_notificacoes (
      id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
      chamado_id INTEGER NOT NULL REFERENCES ti_chamados(id), mensagem TEXT NOT NULL,
      criado_em TEXT NOT NULL, lida INTEGER NOT NULL DEFAULT 0);
    """)

def _inteiro(valor, rotulo, minimo=1, maximo=1000000):
    try:
        n = int(str(valor).strip())
    except (ValueError, TypeError):
        raise ValueError(f'{rotulo}: informe um número inteiro.') from None
    if n < minimo or n > maximo:
        raise ValueError(f'{rotulo}: informe um valor entre {minimo} e {maximo}.')
    return n

def _ativo(conn, usuario_id):
    usuario_id = _inteiro(usuario_id, 'Colaborador', maximo=2147483647)
    if conn.execute('SELECT id FROM usuarios WHERE id=? AND ativo=1', (usuario_id,)).fetchone() is None:
        raise ValueError('Selecione um colaborador ativo.')
    return usuario_id

def _usuarios(conn):
    return conn.execute('SELECT id,nome,setor FROM usuarios WHERE ativo=1 ORDER BY nome').fetchall()

def indicadores(conn):
    resultado = []
    if tem_permissao('epi'):
        valor = conn.execute('SELECT count(*) FROM epi_itens WHERE ativo=1 AND saldo<=estoque_minimo').fetchone()[0]
        resultado.append({'modulo': 'epi', 'titulo': 'EPIs para reposição', 'valor': valor, 'url': '/epi'})
    if tem_permissao('ti_estoque'):
        valor = conn.execute("SELECT count(*) FROM ti_equipamentos WHERE status='Disponível'").fetchone()[0]
        resultado.append({'modulo': 'ti_estoque', 'titulo': 'Equipamentos disponíveis', 'valor': valor, 'url': '/ti/estoque'})
    if tem_permissao('chamados'):
        filtro, args = ('', []) if tem_permissao('chamados', 'gerenciar') else (' AND solicitante_id=?', [current_user()['id']])
        valor = conn.execute("SELECT count(*) FROM ti_chamados WHERE status NOT IN ('Resolvido','Encerrado')" + filtro, args).fetchone()[0]
        resultado.append({'modulo': 'chamados', 'titulo': 'Chamados em aberto', 'valor': valor, 'url': '/chamados'})
        atrasados = conn.execute("SELECT count(*) FROM ti_chamados WHERE status NOT IN ('Resolvido','Encerrado') AND prazo_sla<?" + filtro, [agora(), *args]).fetchone()[0]
        resultado.append({'modulo': 'chamados', 'titulo': 'Chamados fora do SLA', 'valor': atrasados, 'url': '/chamados'})
    return resultado
REPORTS = {'epi_estoque': {'titulo': 'Estoque de EPI', 'modulo': 'epi', 'data_coluna': None, 'sql': """SELECT nome AS "EPI",ca AS "CA",tamanho AS "Tamanho",lote AS "Lote",validade AS "Validade",
                 saldo AS "Saldo",unidade AS "Unidade",estoque_minimo AS "Minimo",
                 CASE ativo WHEN 1 THEN 'Ativo' ELSE 'Inativo' END AS "Situacao" FROM epi_itens"""}, 'epi_movimentos': {'titulo': 'Entregas e movimentações de EPI', 'modulo': 'epi', 'data_coluna': 'Data', 'sql': """SELECT m.criado_em AS "Data",m.nome_epi AS "EPI",m.ca_epi AS "CA",m.lote_epi AS "Lote",
                 m.validade_epi AS "Validade",m.tipo AS "Movimento",m.quantidade AS "Quantidade",m.unidade_epi AS "Unidade",
                 u.nome AS "Colaborador",a.nome AS "Operador",m.observacao AS "Observacao"
                 FROM epi_movimentos m JOIN epi_itens i ON i.id=m.item_id LEFT JOIN usuarios u ON u.id=m.colaborador_id
                 JOIN usuarios a ON a.id=m.autor_id"""}, 'ti_patrimonio': {'titulo': 'Patrimônio e alocações de TI', 'modulo': 'ti_estoque', 'data_coluna': None, 'sql': """SELECT e.patrimonio AS "Patrimonio",e.nome AS "Equipamento",e.categoria AS "Categoria",e.serie AS "Serie",
                 e.status AS "Situacao",u.nome AS "Colaborador",u.setor AS "Setor" FROM ti_equipamentos e
                 LEFT JOIN usuarios u ON u.id=e.usuario_id"""}, 'ti_movimentos': {'titulo': 'Histórico de equipamentos de TI', 'modulo': 'ti_estoque', 'data_coluna': 'Data', 'sql': """SELECT h.criado_em AS "Data",e.patrimonio AS "Patrimonio",e.nome AS "Equipamento",h.acao AS "Acao",
                 u.nome AS "Colaborador",a.nome AS "Operador",h.observacao AS "Observacao" FROM ti_equipamento_historico h
                 JOIN ti_equipamentos e ON e.id=h.equipamento_id LEFT JOIN usuarios u ON u.id=h.usuario_id
                 JOIN usuarios a ON a.id=h.autor_id"""}, 'ti_chamados': {'titulo': 'Chamados e SLA', 'modulo': 'chamados', 'data_coluna': 'Abertura', 'sql': """SELECT c.id AS "Chamado",c.titulo AS "Titulo",c.categoria AS "Categoria",c.prioridade AS "Prioridade",
                 c.status AS "Situacao",u.nome AS "Solicitante",r.nome AS "Responsavel",c.criado_em AS "Abertura",
                 c.prazo_sla AS "Prazo",c.resolvido_em AS "Resolucao",c.encerrado_em AS "Encerramento"
                 FROM ti_chamados c JOIN usuarios u ON u.id=c.solicitante_id LEFT JOIN usuarios r ON r.id=c.responsavel_id"""}}
