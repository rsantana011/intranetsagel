"""Rotas e regras da área: veiculos."""
from ..templates_loader import ler_template
import re
import sqlite3
from datetime import date, timedelta
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
from ..frota import (
    COMBUSTIVEIS,
    CONSUMO_SQL,
    ESTADOS_MANUTENCAO,
    _data,
    _erro,
    _escalado,
    _gerenciar,
    _inteiro,
    _litros,
    _moeda,
    bp,
)

def _veiculo(conn, identificador, ativo=False):
    item = conn.execute('SELECT * FROM frota_veiculos WHERE id=?', (identificador,)).fetchone()
    if not item:
        abort(404)
    if ativo and (not item['ativo']):
        raise ValueError('Este veículo está inativo. Reative o cadastro para registrar uma nova operação.')
    return item

@bp.route('/frota', methods=['GET', 'POST'])
@permissao('frota')
def index():
    conn = db()
    if request.method == 'POST':
        _gerenciar('frota')
        try:
            placa, modelo, ano, combustivel, km, observacoes = _dados_veiculo()
            conn.execute('BEGIN IMMEDIATE')
            cursor = conn.execute("""INSERT INTO frota_veiculos
                (placa,modelo,ano,tipo_combustivel,quilometragem,observacoes,criado_em)
                VALUES (?,?,?,?,?,?,?)""", (placa, modelo, ano, combustivel, km, observacoes, agora()))
            auditar('frota', 'Cadastrar veículo', cursor.lastrowid, placa, conn=conn)
            conn.commit()
            flash('Veículo cadastrado.', 'sucesso')
            return redirect(url_for('frota.veiculo', veiculo_id=cursor.lastrowid))
        except sqlite3.IntegrityError:
            return _erro(conn, ValueError('Já existe um veículo com essa placa.'), 'frota.index')
        except ValueError as exc:
            return _erro(conn, exc, 'frota.index')
    busca = request.args.get('q', '').strip()[:100]
    situacao = request.args.get('situacao', 'ativos')
    filtro = ' AND ativo=1' if situacao == 'ativos' else ' AND ativo=0' if situacao == 'inativos' else ''
    veiculos = conn.execute("""SELECT v.*,
        EXISTS(SELECT 1 FROM frota_utilizacoes u WHERE u.veiculo_id=v.id AND u.status='Em uso') em_uso,
        (SELECT count(*) FROM frota_manutencoes m WHERE m.veiculo_id=v.id AND m.status IN ('Agendada','Em andamento')) pendentes
        FROM frota_veiculos v WHERE (lower(placa) LIKE lower(?) OR lower(modelo) LIKE lower(?))""" + filtro + ' ORDER BY placa', ('%' + busca + '%', '%' + busca + '%')).fetchall()
    limite = (date.fromisoformat(hoje()) + timedelta(days=30)).isoformat()
    vencidos = conn.execute("""SELECT d.*, v.placa FROM frota_documentos d JOIN frota_veiculos v ON v.id=d.veiculo_id
        WHERE d.arquivado=0 AND v.ativo=1 AND d.validade IS NOT NULL AND d.validade<=?
        ORDER BY d.validade LIMIT 30""", (limite,)).fetchall()
    return render_page('Frota', FROTA_HTML, veiculos=veiculos, busca=busca, situacao=situacao, vencidos=vencidos, tipos=COMBUSTIVEIS, datahoje=hoje(), anoatual=int(hoje()[:4]))

def _dados_veiculo():
    placa = re.sub('[\\s-]', '', texto_form('placa', 'Placa', 10)).upper()
    if not re.fullmatch('[A-Z]{3}[0-9][A-Z0-9][0-9]{2}', placa):
        raise ValueError('Informe uma placa brasileira válida, como ABC1234 ou ABC1D23.')
    modelo = texto_form('modelo', 'Modelo', 120)
    ano = _inteiro('ano', 'ano', 1900, int(hoje()[:4]) + 1)
    combustivel = texto_form('tipo_combustivel', 'Combustível', 30)
    if combustivel not in COMBUSTIVEIS:
        raise ValueError('Selecione um tipo de combustível válido.')
    km = _inteiro('quilometragem', 'quilometragem')
    observacoes = texto_form('observacoes', 'Observações', 2000, False)
    return (placa, modelo, ano, combustivel, km, observacoes)

@bp.route('/frota/veiculos/<int:veiculo_id>', methods=['GET', 'POST'])
@permissao('frota')
def veiculo(veiculo_id):
    conn = db()
    item = _veiculo(conn, veiculo_id)
    if request.method == 'POST':
        _gerenciar('frota')
        try:
            placa, modelo, ano, combustivel, km, observacoes = _dados_veiculo()
            ativo = 1 if request.form.get('ativo') == '1' else 0
            conn.execute('BEGIN IMMEDIATE')
            item = _veiculo(conn, veiculo_id)
            if km < item['quilometragem']:
                raise ValueError('A quilometragem não pode ser reduzida. O histórico de utilização deve ser preservado.')
            em_uso = conn.execute("SELECT id FROM frota_utilizacoes WHERE veiculo_id=? AND status='Em uso'", (veiculo_id,)).fetchone()
            if not ativo and em_uso:
                raise ValueError('Encerre a utilização aberta antes de inativar o veículo.')
            conn.execute("""UPDATE frota_veiculos SET placa=?,modelo=?,ano=?,tipo_combustivel=?,
                quilometragem=?,ativo=?,observacoes=? WHERE id=?""", (placa, modelo, ano, combustivel, km, ativo, observacoes, veiculo_id))
            auditar('frota', 'Atualizar veículo', veiculo_id, f'{placa}; ativo={ativo}; km={km}', conn=conn)
            conn.commit()
            flash('Cadastro atualizado.', 'sucesso')
        except sqlite3.IntegrityError:
            return _erro(conn, ValueError('Já existe um veículo com essa placa.'), 'frota.veiculo', veiculo_id=veiculo_id)
        except ValueError as exc:
            return _erro(conn, exc, 'frota.veiculo', veiculo_id=veiculo_id)
        return redirect(url_for('frota.veiculo', veiculo_id=veiculo_id))
    manutencoes = conn.execute('SELECT * FROM frota_manutencoes WHERE veiculo_id=? ORDER BY data DESC,id DESC', (veiculo_id,)).fetchall()
    historico = conn.execute("""SELECT h.*,u.nome FROM frota_manutencao_historico h
        JOIN frota_manutencoes m ON m.id=h.manutencao_id JOIN usuarios u ON u.id=h.autor_id
        WHERE m.veiculo_id=? ORDER BY h.id DESC""", (veiculo_id,)).fetchall()
    documentos = conn.execute('SELECT * FROM frota_documentos WHERE veiculo_id=? ORDER BY arquivado,validade,id DESC', (veiculo_id,)).fetchall()
    usos = conn.execute('SELECT * FROM frota_utilizacoes WHERE veiculo_id=? ORDER BY id DESC', (veiculo_id,)).fetchall()
    pode_combustivel = tem_permissao('combustivel')
    abastecimentos = conn.execute(CONSUMO_SQL + ' WHERE m.veiculo_id=? ORDER BY m.id DESC LIMIT 30', (veiculo_id,)).fetchall() if pode_combustivel else []
    return render_page(item['placa'] + ' · ' + item['modelo'], VEICULO_HTML, v=item, manutencoes=manutencoes, historico=historico, documentos=documentos, usos=usos, abastecimentos=abastecimentos, tipos=COMBUSTIVEIS, datahoje=hoje(), moeda=_moeda, litros=_litros, anoatual=int(hoje()[:4]), pode_combustivel=pode_combustivel)

@bp.post('/frota/veiculos/<int:veiculo_id>/manutencoes')
@permissao('frota', 'gerenciar')
def nova_manutencao(veiculo_id):
    conn = db()
    _veiculo(conn, veiculo_id)
    try:
        descricao = texto_form('descricao', 'Serviço', 500)
        oficina = texto_form('oficina', 'Oficina', 120, False)
        data = _data('data', 'serviço')
        previsao = _data('previsao', 'previsão', False)
        status = texto_form('status', 'Situação', 30)
        if status not in ESTADOS_MANUTENCAO[:3]:
            raise ValueError('Selecione uma situação válida.')
        if status == 'Concluída' and data > hoje():
            raise ValueError('Uma manutenção concluída não pode ter data futura.')
        if previsao and previsao < data:
            raise ValueError('A previsão não pode ser anterior à data do serviço.')
        km = _inteiro('quilometragem', 'quilometragem')
        custo = _escalado('custo', 'o custo', 2)
        observacoes = texto_form('observacoes', 'Observações', 2000, False)
        conn.execute('BEGIN IMMEDIATE')
        _veiculo(conn, veiculo_id, ativo=True)
        if status == 'Em andamento' and conn.execute("SELECT 1 FROM frota_utilizacoes WHERE veiculo_id=? AND status='Em uso'", (veiculo_id,)).fetchone():
            raise ValueError('Encerre a utilização do veículo antes de iniciar a manutenção.')
        cursor = conn.execute("""INSERT INTO frota_manutencoes
            (veiculo_id,descricao,oficina,data,previsao,status,quilometragem,custo_centavos,observacoes,autor_id,criado_em)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (veiculo_id, descricao, oficina, data, previsao, status, km, custo, observacoes, current_user()['id'], agora()))
        conn.execute('INSERT INTO frota_manutencao_historico(manutencao_id,status,observacao,autor_id,data) VALUES (?,?,?,?,?)', (cursor.lastrowid, status, 'Registro inicial', current_user()['id'], agora()))
        if status in ('Em andamento', 'Concluída'):
            conn.execute('UPDATE frota_veiculos SET quilometragem=MAX(quilometragem,?) WHERE id=?', (km, veiculo_id))
        auditar('frota', 'Registrar manutenção', cursor.lastrowid, f'Veículo {veiculo_id}; {status}', conn=conn)
        conn.commit()
        flash('Manutenção registrada.', 'sucesso')
    except ValueError as exc:
        return _erro(conn, exc, 'frota.veiculo', veiculo_id=veiculo_id)
    return redirect(url_for('frota.veiculo', veiculo_id=veiculo_id))

@bp.post('/frota/manutencoes/<int:manutencao_id>/status')
@permissao('frota', 'gerenciar')
def status_manutencao(manutencao_id):
    conn = db()
    item = conn.execute('SELECT * FROM frota_manutencoes WHERE id=?', (manutencao_id,)).fetchone()
    if not item:
        abort(404)
    try:
        novo = texto_form('status', 'Situação', 30)
        observacao = texto_form('observacao', 'Justificativa', 2000)
        custo = _escalado('custo', 'o custo final', 2)
        km = _inteiro('quilometragem', 'quilometragem')
        conn.execute('BEGIN IMMEDIATE')
        item = conn.execute('SELECT * FROM frota_manutencoes WHERE id=?', (manutencao_id,)).fetchone()
        transicoes = {'Agendada': ('Em andamento', 'Concluída', 'Cancelada'), 'Em andamento': ('Concluída', 'Cancelada')}
        if novo not in transicoes.get(item['status'], ()):
            raise ValueError('Essa manutenção já foi encerrada ou a mudança de situação não é válida.')
        if novo in ('Em andamento', 'Concluída') and item['data'] > hoje():
            raise ValueError('A data programada ainda está no futuro. Registre o serviço na data correspondente.')
        if novo == 'Em andamento' and conn.execute("SELECT 1 FROM frota_utilizacoes WHERE veiculo_id=? AND status='Em uso'", (item['veiculo_id'],)).fetchone():
            raise ValueError('Encerre a utilização antes de iniciar a manutenção.')
        if km < item['quilometragem']:
            raise ValueError('A quilometragem não pode ser menor que a informada no início da manutenção.')
        conn.execute('UPDATE frota_manutencoes SET status=?,custo_centavos=?,quilometragem=? WHERE id=?', (novo, custo, km, manutencao_id))
        conn.execute('INSERT INTO frota_manutencao_historico(manutencao_id,status,observacao,autor_id,data) VALUES (?,?,?,?,?)', (manutencao_id, novo, observacao, current_user()['id'], agora()))
        if novo != 'Cancelada':
            conn.execute('UPDATE frota_veiculos SET quilometragem=MAX(quilometragem,?) WHERE id=?', (km, item['veiculo_id']))
        auditar('frota', 'Atualizar manutenção', manutencao_id, novo, conn=conn)
        conn.commit()
        flash('Manutenção atualizada.', 'sucesso')
    except ValueError as exc:
        return _erro(conn, exc, 'frota.veiculo', veiculo_id=item['veiculo_id'])
    return redirect(url_for('frota.veiculo', veiculo_id=item['veiculo_id']))

@bp.post('/frota/veiculos/<int:veiculo_id>/documentos')
@permissao('frota', 'gerenciar')
def novo_documento(veiculo_id):
    conn = db()
    _veiculo(conn, veiculo_id)
    try:
        titulo = texto_form('titulo', 'Título', 150)
        validade = _data('validade', 'validade', False)
        arquivo = request.files.get('arquivo')
        if not arquivo or not arquivo.filename:
            raise ValueError('Selecione o arquivo do documento.')
        chave, nome = salvar_anexo(arquivo)
        cursor = conn.execute("""INSERT INTO frota_documentos
            (veiculo_id,titulo,validade,chave,nome_arquivo,autor_id,criado_em) VALUES (?,?,?,?,?,?,?)""", (veiculo_id, titulo, validade, chave, nome, current_user()['id'], agora()))
        auditar('frota', 'Cadastrar documento', cursor.lastrowid, f'Veículo {veiculo_id}; {titulo}', conn=conn)
        conn.commit()
        flash('Documento anexado. A validade aparece nos alertas da frota.', 'sucesso')
    except ValueError as exc:
        return _erro(conn, exc, 'frota.veiculo', veiculo_id=veiculo_id)
    return redirect(url_for('frota.veiculo', veiculo_id=veiculo_id))

@bp.get('/frota/documentos/<int:documento_id>/arquivo')
@permissao('frota')
def arquivo_documento(documento_id):
    conn = db()
    item = conn.execute('SELECT * FROM frota_documentos WHERE id=?', (documento_id,)).fetchone()
    if not item:
        abort(404)
    auditar('frota', 'Baixar documento', documento_id, item['titulo'], conn=conn)
    conn.commit()
    return baixar_anexo(item['chave'], item['nome_arquivo'])

@bp.post('/frota/documentos/<int:documento_id>/arquivar')
@permissao('frota', 'gerenciar')
def arquivar_documento(documento_id):
    conn = db()
    item = conn.execute('SELECT * FROM frota_documentos WHERE id=?', (documento_id,)).fetchone()
    if not item:
        abort(404)
    conn.execute('UPDATE frota_documentos SET arquivado=1 WHERE id=?', (documento_id,))
    auditar('frota', 'Arquivar documento', documento_id, item['titulo'], conn=conn)
    conn.commit()
    flash('Documento arquivado. O arquivo permanece no histórico.', 'sucesso')
    return redirect(url_for('frota.veiculo', veiculo_id=item['veiculo_id']))

@bp.post('/frota/veiculos/<int:veiculo_id>/utilizacoes')
@permissao('frota', 'gerenciar')
def nova_utilizacao(veiculo_id):
    conn = db()
    _veiculo(conn, veiculo_id)
    try:
        motorista = texto_form('motorista', 'Motorista', 120)
        destino = texto_form('destino', 'Destino / finalidade', 300)
        saida = _data('saida', 'saída', futuro=False)
        km = _inteiro('km_saida', 'quilometragem de saída')
        observacoes = texto_form('observacoes', 'Observações', 2000, False)
        conn.execute('BEGIN IMMEDIATE')
        item = _veiculo(conn, veiculo_id, ativo=True)
        if km < item['quilometragem']:
            raise ValueError('A quilometragem de saída não pode ser menor que a atual do veículo.')
        if conn.execute("SELECT 1 FROM frota_utilizacoes WHERE veiculo_id=? AND status='Em uso'", (veiculo_id,)).fetchone():
            raise ValueError('Esse veículo já possui uma utilização em aberto.')
        if conn.execute("SELECT 1 FROM frota_manutencoes WHERE veiculo_id=? AND status='Em andamento'", (veiculo_id,)).fetchone():
            raise ValueError('Encerre a manutenção em andamento antes de liberar o veículo.')
        cursor = conn.execute("""INSERT INTO frota_utilizacoes
            (veiculo_id,motorista,destino,saida,km_saida,observacoes,autor_id,criado_em) VALUES (?,?,?,?,?,?,?,?)""", (veiculo_id, motorista, destino, saida, km, observacoes, current_user()['id'], agora()))
        conn.execute('UPDATE frota_veiculos SET quilometragem=? WHERE id=?', (km, veiculo_id))
        auditar('frota', 'Registrar saída de veículo', cursor.lastrowid, f'Veículo {veiculo_id}; km={km}', conn=conn)
        conn.commit()
        flash('Saída registrada. Encerre a utilização quando o veículo retornar.', 'sucesso')
    except (ValueError, sqlite3.IntegrityError) as exc:
        if isinstance(exc, sqlite3.IntegrityError):
            exc = ValueError('O veículo já possui uma utilização aberta.')
        return _erro(conn, exc, 'frota.veiculo', veiculo_id=veiculo_id)
    return redirect(url_for('frota.veiculo', veiculo_id=veiculo_id))

@bp.post('/frota/utilizacoes/<int:utilizacao_id>/encerrar')
@permissao('frota', 'gerenciar')
def encerrar_utilizacao(utilizacao_id):
    conn = db()
    item = conn.execute('SELECT * FROM frota_utilizacoes WHERE id=?', (utilizacao_id,)).fetchone()
    if not item:
        abort(404)
    try:
        status = request.form.get('status', '')
        if status not in ('Concluída', 'Cancelada'):
            raise ValueError('Selecione uma situação válida.')
        observacao = texto_form('observacao', 'Observação do encerramento', 2000, status == 'Cancelada')
        retorno = _data('retorno', 'retorno', futuro=False)
        km = _inteiro('km_retorno', 'quilometragem de retorno')
        conn.execute('BEGIN IMMEDIATE')
        item = conn.execute('SELECT * FROM frota_utilizacoes WHERE id=?', (utilizacao_id,)).fetchone()
        v = _veiculo(conn, item['veiculo_id'])
        if item['status'] != 'Em uso':
            raise ValueError('Essa utilização já foi encerrada.')
        if retorno < item['saida']:
            raise ValueError('O retorno não pode ser anterior à saída.')
        if km < max(item['km_saida'], v['quilometragem']):
            raise ValueError('A quilometragem de retorno não pode ser menor que a saída ou que a atual do veículo.')
        if status == 'Cancelada' and km != item['km_saida']:
            raise ValueError('Houve quilometragem percorrida. Conclua a utilização para preservar a distância realizada.')
        detalhe = item['observacoes'] + ("""
Encerramento: """ + observacao if observacao else '')
        conn.execute('UPDATE frota_utilizacoes SET status=?,retorno=?,km_retorno=?,observacoes=? WHERE id=?', (status, retorno, km, detalhe, utilizacao_id))
        conn.execute('UPDATE frota_veiculos SET quilometragem=MAX(quilometragem,?) WHERE id=?', (km, item['veiculo_id']))
        auditar('frota', 'Encerrar utilização', utilizacao_id, f'{status}; km={km}', conn=conn)
        conn.commit()
        flash('Utilização encerrada.', 'sucesso')
    except ValueError as exc:
        return _erro(conn, exc, 'frota.veiculo', veiculo_id=item['veiculo_id'])
    return redirect(url_for('frota.veiculo', veiculo_id=item['veiculo_id']))
FROTA_HTML = ler_template('veiculos/frota_html.html')
VEICULO_HTML = ler_template('veiculos/veiculo_html.html')
