"""Rotas e regras da área: combustivel."""

# ============================================================
# COMBUSTÍVEL: ENTRADAS, ABASTECIMENTOS E SALDOS
# Regras e rotas desta área. Interface: sagel/templates/.
# Banco e segurança compartilhados: sagel/base.py.
# Mapa completo de manutenção: ESTRUTURA-DO-PROJETO.md.
# ============================================================
from ..templates_loader import ler_template
from datetime import date
from flask import abort, flash, redirect, request, url_for
from ..base import agora, auditar, current_user, db, hoje, permissao, render_page, texto_form
from ..frota import COMBUSTIVEIS, CONSUMO_SQL, _data, _erro, _escalado, _gerenciar, _inteiro, _litros, _moeda, bp

def _saldo(conn, combustivel):
    return conn.execute("""SELECT COALESCE(SUM(CASE WHEN tipo='Entrada'
                          THEN quantidade_ml ELSE -quantidade_ml END),0)
                          FROM combustivel_movimentos WHERE combustivel=? AND cancelado=0""", (combustivel,)).fetchone()[0]

@bp.route('/combustivel', methods=['GET', 'POST'])
@permissao('combustivel')
def combustivel():
    conn = db()
    if request.method == 'POST':
        _gerenciar('combustivel')
        try:
            tipo = request.form.get('tipo', '')
            if tipo not in ('Entrada', 'Saída'):
                raise ValueError('Selecione entrada ou abastecimento.')
            combustivel_tipo = texto_form('combustivel', 'Combustível', 30)
            if combustivel_tipo not in COMBUSTIVEIS:
                raise ValueError('Selecione um combustível válido.')
            ml = _escalado('quantidade', 'a quantidade em litros', 3, True)
            valor = _escalado('valor', 'o valor total', 2)
            data = _data('data', 'movimentação', futuro=False)
            referencia = texto_form('referencia', 'Documento / fornecedor', 200, False)
            observacoes = texto_form('observacoes', 'Observações', 2000, False)
            veiculo_id = motorista = km = None
            tanque = 0
            if tipo == 'Saída':
                veiculo_id = _inteiro('veiculo_id', 'veículo', 1)
                motorista = texto_form('motorista', 'Motorista', 120)
                km = _inteiro('quilometragem', 'quilometragem')
                tanque = 1 if request.form.get('tanque_cheio') == '1' else 0
            conn.execute('BEGIN IMMEDIATE')
            if tipo == 'Saída':
                veiculo_item = conn.execute('SELECT * FROM frota_veiculos WHERE id=?', (veiculo_id,)).fetchone()
                if not veiculo_item:
                    raise ValueError('O veículo selecionado não existe.')
                if not veiculo_item['ativo']:
                    raise ValueError('O veículo selecionado está inativo.')
                if veiculo_item['tipo_combustivel'] != combustivel_tipo:
                    raise ValueError('O combustível selecionado é diferente do cadastrado para esse veículo.')
                if km < veiculo_item['quilometragem']:
                    raise ValueError('A quilometragem não pode ser menor que a atual do veículo.')
                ultima_data = conn.execute("SELECT MAX(data) FROM combustivel_movimentos WHERE veiculo_id=? AND tipo='Saída' AND cancelado=0", (veiculo_id,)).fetchone()[0]
                if ultima_data and data < ultima_data:
                    raise ValueError('Registre os abastecimentos do veículo em ordem de data.')
                if ml > _saldo(conn, combustivel_tipo):
                    raise ValueError('Saldo insuficiente para esse abastecimento. Registre a entrada de combustível primeiro.')
            cursor = conn.execute("""INSERT INTO combustivel_movimentos
                (tipo,combustivel,quantidade_ml,valor_centavos,veiculo_id,motorista,quilometragem,tanque_cheio,
                 data,referencia,observacoes,autor_id,criado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (tipo, combustivel_tipo, ml, valor, veiculo_id, motorista, km, tanque, data, referencia, observacoes, current_user()['id'], agora()))
            if tipo == 'Saída':
                conn.execute('UPDATE frota_veiculos SET quilometragem=? WHERE id=?', (km, veiculo_id))
            auditar('combustivel', 'Registrar ' + tipo.lower(), cursor.lastrowid, f"{combustivel_tipo}; {_litros(ml)} L; veículo={veiculo_id or '—'}", conn=conn)
            conn.commit()
            flash('Entrada registrada.' if tipo == 'Entrada' else 'Abastecimento registrado.', 'sucesso')
        except ValueError as exc:
            return _erro(conn, exc, 'frota.combustivel')
        return redirect(url_for('frota.combustivel'))
    filtros, params = ([], [])
    selecionado = request.args.get('combustivel', '')
    if selecionado in COMBUSTIVEIS:
        filtros.append('m.combustivel=?')
        params.append(selecionado)
    veiculo_filtro = request.args.get('veiculo', '', type=str)
    if veiculo_filtro.isdigit():
        filtros.append('m.veiculo_id=?')
        params.append(int(veiculo_filtro))
    inicio = request.args.get('inicio', '')
    fim = request.args.get('fim', '')
    for valor, operador in ((inicio, '>='), (fim, '<=')):
        if valor:
            try:
                validado = date.fromisoformat(valor).isoformat()
                filtros.append('m.data' + operador + '?')
                params.append(validado)
            except ValueError:
                flash('Um filtro de data inválido foi ignorado.', 'erro')
    consulta = CONSUMO_SQL + (' WHERE ' + ' AND '.join(filtros) if filtros else '') + ' ORDER BY m.id DESC LIMIT 300'
    movimentos = conn.execute(consulta, params).fetchall()
    saldos = [(tipo, _saldo(conn, tipo)) for tipo in COMBUSTIVEIS]
    veiculos = conn.execute('SELECT * FROM frota_veiculos ORDER BY placa').fetchall()
    return render_page('Combustível', COMBUSTIVEL_HTML, movimentos=movimentos, saldos=saldos, tipos=COMBUSTIVEIS, veiculos=veiculos, datahoje=hoje(), moeda=_moeda, litros=_litros, selecionado=selecionado, veiculo_filtro=veiculo_filtro, inicio=inicio, fim=fim)

@bp.post('/combustivel/<int:movimento_id>/cancelar')
@permissao('combustivel', 'gerenciar')
def cancelar_movimento(movimento_id):
    conn = db()
    if not conn.execute('SELECT 1 FROM combustivel_movimentos WHERE id=?', (movimento_id,)).fetchone():
        abort(404)
    try:
        motivo = texto_form('motivo', 'Motivo do cancelamento', 1000)
        conn.execute('BEGIN IMMEDIATE')
        item = conn.execute('SELECT * FROM combustivel_movimentos WHERE id=?', (movimento_id,)).fetchone()
        if item['cancelado']:
            raise ValueError('Essa movimentação já foi cancelada.')
        if item['tipo'] == 'Entrada' and _saldo(conn, item['combustivel']) < item['quantidade_ml']:
            raise ValueError('Essa entrada já abasteceu veículos. O cancelamento deixaria o saldo negativo.')
        conn.execute('UPDATE combustivel_movimentos SET cancelado=1,motivo_cancelamento=?,cancelado_em=? WHERE id=?', (motivo, agora(), movimento_id))
        auditar('combustivel', 'Cancelar movimentação', movimento_id, motivo, conn=conn)
        conn.commit()
        flash('Movimentação cancelada e saldo atualizado. O registro permanece no histórico.', 'sucesso')
    except ValueError as exc:
        return _erro(conn, exc, 'frota.combustivel')
    return redirect(url_for('frota.combustivel'))
COMBUSTIVEL_HTML = ler_template('combustivel/combustivel_html.html')
