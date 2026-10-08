from .templates_loader import ler_template
'Frota e combustível: registros locais, saldos exatos e histórico auditável.'
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
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
bp = Blueprint('frota', __name__)
COMBUSTIVEIS = ('Diesel S10', 'Diesel S500', 'Gasolina', 'Etanol', 'GNV')
ESTADOS_MANUTENCAO = ('Agendada', 'Em andamento', 'Concluída', 'Cancelada')

def init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS frota_veiculos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placa TEXT NOT NULL UNIQUE,
            modelo TEXT NOT NULL, ano INTEGER NOT NULL,
            tipo_combustivel TEXT NOT NULL,
            quilometragem INTEGER NOT NULL DEFAULT 0 CHECK(quilometragem >= 0),
            ativo INTEGER NOT NULL DEFAULT 1 CHECK(ativo IN (0,1)),
            observacoes TEXT NOT NULL DEFAULT '', criado_em TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS frota_placa_sem_caixa ON frota_veiculos(lower(placa));
        CREATE TABLE IF NOT EXISTS frota_manutencoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            veiculo_id INTEGER NOT NULL REFERENCES frota_veiculos(id),
            descricao TEXT NOT NULL, oficina TEXT NOT NULL DEFAULT '',
            data TEXT NOT NULL, previsao TEXT,
            status TEXT NOT NULL CHECK(status IN ('Agendada','Em andamento','Concluída','Cancelada')),
            quilometragem INTEGER NOT NULL CHECK(quilometragem >= 0),
            custo_centavos INTEGER NOT NULL DEFAULT 0 CHECK(custo_centavos >= 0),
            observacoes TEXT NOT NULL DEFAULT '',
            autor_id INTEGER NOT NULL REFERENCES usuarios(id), criado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS frota_manutencao_historico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manutencao_id INTEGER NOT NULL REFERENCES frota_manutencoes(id),
            status TEXT NOT NULL, observacao TEXT NOT NULL,
            autor_id INTEGER NOT NULL REFERENCES usuarios(id), data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS frota_documentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            veiculo_id INTEGER NOT NULL REFERENCES frota_veiculos(id),
            titulo TEXT NOT NULL, validade TEXT,
            chave TEXT NOT NULL, nome_arquivo TEXT NOT NULL,
            arquivado INTEGER NOT NULL DEFAULT 0 CHECK(arquivado IN (0,1)),
            autor_id INTEGER NOT NULL REFERENCES usuarios(id), criado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS frota_utilizacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            veiculo_id INTEGER NOT NULL REFERENCES frota_veiculos(id),
            motorista TEXT NOT NULL, destino TEXT NOT NULL,
            saida TEXT NOT NULL, retorno TEXT,
            km_saida INTEGER NOT NULL CHECK(km_saida >= 0),
            km_retorno INTEGER CHECK(km_retorno >= km_saida),
            status TEXT NOT NULL DEFAULT 'Em uso' CHECK(status IN ('Em uso','Concluída','Cancelada')),
            observacoes TEXT NOT NULL DEFAULT '',
            autor_id INTEGER NOT NULL REFERENCES usuarios(id), criado_em TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS frota_uso_aberto
            ON frota_utilizacoes(veiculo_id) WHERE status='Em uso';
        CREATE TABLE IF NOT EXISTS combustivel_movimentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL CHECK(tipo IN ('Entrada','Saída')),
            combustivel TEXT NOT NULL,
            quantidade_ml INTEGER NOT NULL CHECK(quantidade_ml > 0),
            valor_centavos INTEGER NOT NULL DEFAULT 0 CHECK(valor_centavos >= 0),
            veiculo_id INTEGER REFERENCES frota_veiculos(id),
            motorista TEXT, quilometragem INTEGER CHECK(quilometragem >= 0),
            tanque_cheio INTEGER NOT NULL DEFAULT 0 CHECK(tanque_cheio IN (0,1)),
            data TEXT NOT NULL, referencia TEXT NOT NULL DEFAULT '',
            observacoes TEXT NOT NULL DEFAULT '',
            cancelado INTEGER NOT NULL DEFAULT 0 CHECK(cancelado IN (0,1)),
            motivo_cancelamento TEXT, cancelado_em TEXT,
            autor_id INTEGER NOT NULL REFERENCES usuarios(id), criado_em TEXT NOT NULL,
            CHECK(tipo='Entrada' OR (veiculo_id IS NOT NULL AND motorista IS NOT NULL AND quilometragem IS NOT NULL))
        );
        CREATE INDEX IF NOT EXISTS combustivel_saldo ON combustivel_movimentos(combustivel,cancelado);
        CREATE INDEX IF NOT EXISTS combustivel_veiculo ON combustivel_movimentos(veiculo_id,id);
        CREATE INDEX IF NOT EXISTS frota_documento_validade ON frota_documentos(arquivado,validade);
    """)

def _inteiro(nome, rotulo, minimo=0, maximo=100000000):
    valor = request.form.get(nome, '').strip()
    if not re.fullmatch('\\d{1,12}', valor):
        raise ValueError(f'Informe {rotulo} como um número inteiro válido.')
    numero = int(valor)
    if not minimo <= numero <= maximo:
        raise ValueError(f'{rotulo.capitalize()} deve estar entre {minimo} e {maximo}.')
    return numero

def _data(nome, rotulo, obrigatorio=True, futuro=True):
    valor = request.form.get(nome, '').strip()
    if not valor and (not obrigatorio):
        return None
    try:
        resultado = date.fromisoformat(valor)
    except (TypeError, ValueError):
        raise ValueError(f'Informe uma data válida para {rotulo}.') from None
    if resultado.isoformat() != valor or resultado.year < 1900:
        raise ValueError(f'Informe uma data válida para {rotulo}.')
    if not futuro and valor > hoje():
        raise ValueError(f'A data de {rotulo} não pode estar no futuro.')
    return valor

def _escalado(nome, rotulo, casas, positivo=False):
    bruto = request.form.get(nome, '').strip().replace(',', '.')
    if not re.fullmatch('\\d{1,10}(?:\\.\\d{1,' + str(casas) + '})?', bruto):
        raise ValueError(f'Informe {rotulo} com até {casas} casas decimais, sem separador de milhar.')
    try:
        numero = Decimal(bruto)
        inteiro = int((numero * 10 ** casas).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError(f'Informe {rotulo} válido.') from None
    if inteiro < (1 if positivo else 0) or inteiro > 1000000000000:
        raise ValueError(f'Informe {rotulo} maior que zero e dentro do limite permitido.' if positivo else f'Informe {rotulo} dentro do limite permitido.')
    return inteiro

def _gerenciar(modulo):
    if not tem_permissao(modulo, 'gerenciar'):
        abort(403)

def _erro(conn, exc, destino, **args):
    conn.rollback()
    flash(str(exc), 'erro')
    return redirect(url_for(destino, **args))

def _moeda(centavos):
    return f'{centavos / 100:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')

def _litros(ml):
    return f'{ml / 1000:,.3f}'.replace(',', '_').replace('.', ',').replace('_', '.')
CONSUMO_SQL = """WITH bases AS (
    SELECT m.*, (SELECT MAX(p.id) FROM combustivel_movimentos p
        WHERE p.veiculo_id=m.veiculo_id AND p.tipo='Saída' AND p.cancelado=0
        AND p.tanque_cheio=1 AND p.id<m.id) anterior_id
    FROM combustivel_movimentos m
), dados AS (
    SELECT b.*, p.quilometragem km_anterior,
        (SELECT SUM(x.quantidade_ml) FROM combustivel_movimentos x
         WHERE x.veiculo_id=b.veiculo_id AND x.tipo='Saída' AND x.cancelado=0
         AND x.id>b.anterior_id AND x.id<=b.id) ml_intervalo
    FROM bases b LEFT JOIN combustivel_movimentos p ON p.id=b.anterior_id
)
SELECT m.*, v.placa, v.modelo,
    CASE WHEN m.tipo='Saída' AND m.cancelado=0 AND m.tanque_cheio=1
        AND m.anterior_id IS NOT NULL AND m.quilometragem>m.km_anterior
        THEN ROUND((m.quilometragem-m.km_anterior)*1000.0/m.ml_intervalo,2)
        ELSE NULL END consumo
FROM dados m LEFT JOIN frota_veiculos v ON v.id=m.veiculo_id"""

def indicadores(conn):
    limite = (date.fromisoformat(hoje()) + timedelta(days=30)).isoformat()
    return [{'titulo': 'Veículos ativos', 'valor': conn.execute('SELECT COUNT(*) FROM frota_veiculos WHERE ativo=1').fetchone()[0], 'url': '/frota', 'modulo': 'frota'}, {'titulo': 'Manutenções pendentes', 'valor': conn.execute("SELECT COUNT(*) FROM frota_manutencoes WHERE status IN ('Agendada','Em andamento')").fetchone()[0], 'url': '/frota', 'modulo': 'frota'}, {'titulo': 'Veículos em uso', 'valor': conn.execute("SELECT COUNT(*) FROM frota_utilizacoes WHERE status='Em uso'").fetchone()[0], 'url': '/frota', 'modulo': 'frota'}, {'titulo': 'Documentos vencidos ou a vencer em 30 dias', 'valor': conn.execute("SELECT COUNT(*) FROM frota_documentos d JOIN frota_veiculos v ON v.id=d.veiculo_id WHERE d.arquivado=0 AND v.ativo=1 AND d.validade<=?", (limite,)).fetchone()[0], 'url': '/frota', 'modulo': 'frota'}]
REPORTS = {'frota_veiculos': {'titulo': 'Frota · veículos', 'modulo': 'frota', 'data_coluna': None, 'sql': """SELECT placa AS "Placa",modelo AS "Modelo",ano AS "Ano",tipo_combustivel AS "Combustível",
                quilometragem AS "Quilometragem",CASE ativo WHEN 1 THEN 'Ativo' ELSE 'Inativo' END AS "Situação"
                FROM frota_veiculos"""}, 'frota_manutencoes': {'titulo': 'Frota · manutenções', 'modulo': 'frota', 'data_coluna': 'Data', 'sql': """SELECT m.data AS "Data",v.placa AS "Placa",m.descricao AS "Serviço",m.oficina AS "Oficina",
                m.status AS "Situação",m.previsao AS "Previsão",m.quilometragem AS "Quilometragem",
                ROUND(m.custo_centavos/100.0,2) AS "Custo (R$)" FROM frota_manutencoes m
                JOIN frota_veiculos v ON v.id=m.veiculo_id"""}, 'frota_utilizacao': {'titulo': 'Frota · utilização', 'modulo': 'frota', 'data_coluna': 'Saída', 'sql': """SELECT u.saida AS "Saída",u.retorno AS "Retorno",v.placa AS "Placa",u.motorista AS "Motorista",
                u.destino AS "Destino",u.status AS "Situação",u.km_saida AS "Km de saída",u.km_retorno AS "Km de retorno",
                CASE WHEN u.status='Concluída' THEN u.km_retorno-u.km_saida ELSE NULL END AS "Km percorridos"
                FROM frota_utilizacoes u JOIN frota_veiculos v ON v.id=u.veiculo_id"""}, 'frota_documentos': {'titulo': 'Frota · validade dos documentos', 'modulo': 'frota', 'data_coluna': 'Validade', 'sql': """SELECT v.placa AS "Placa",d.titulo AS "Documento",d.validade AS "Validade",
                CASE d.arquivado WHEN 1 THEN 'Arquivado' ELSE 'Vigente' END AS "Situação"
                FROM frota_documentos d JOIN frota_veiculos v ON v.id=d.veiculo_id"""}, 'combustivel_movimentos': {'titulo': 'Combustível · histórico', 'modulo': 'combustivel', 'data_coluna': 'Data', 'sql': """SELECT m.data AS "Data",m.tipo AS "Movimento",m.combustivel AS "Combustível",
                ROUND(m.quantidade_ml/1000.0,3) AS "Litros",ROUND(m.valor_centavos/100.0,2) AS "Valor total (R$)",
                v.placa AS "Placa",m.motorista AS "Motorista",m.quilometragem AS "Quilometragem",
                CASE m.cancelado WHEN 1 THEN 'Cancelado' ELSE 'Válido' END AS "Situação",
                m.referencia AS "Referência",m.motivo_cancelamento AS "Motivo do cancelamento"
                FROM combustivel_movimentos m LEFT JOIN frota_veiculos v ON v.id=m.veiculo_id"""}, 'combustivel_consumo': {'titulo': 'Combustível · consumo entre tanques completos', 'modulo': 'combustivel', 'data_coluna': 'Data', 'sql': """SELECT data AS "Data",placa AS "Placa",motorista AS "Motorista",quilometragem AS "Quilometragem",
                ROUND(quantidade_ml/1000.0,3) AS "Litros do abastecimento",consumo AS "Consumo (km/L)"
                FROM (""" + CONSUMO_SQL + ") consumo_rel WHERE tipo='Saída' AND cancelado=0"}, 'combustivel_saldos': {'titulo': 'Combustível · saldo atual', 'modulo': 'combustivel', 'data_coluna': None, 'sql': """SELECT combustivel AS "Combustível",
                ROUND(SUM(CASE WHEN tipo='Entrada' THEN quantidade_ml ELSE -quantidade_ml END)/1000.0,3) AS "Saldo (L)"
                FROM combustivel_movimentos WHERE cancelado=0 GROUP BY combustivel"""}}
