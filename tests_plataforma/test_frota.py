"""Valida operações, permissões e concorrência usando somente bancos temporários."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import shutil
import sqlite3
import unittest
import uuid

from sagel import create_app
from sagel.base import hash_senha, hoje
from sagel.frota import REPORTS


class FrotaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.senha_hash = hash_senha('teste123')

    def setUp(self):
        self.folder = Path(__file__).resolve().parent / ('frota_tmp_' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.database = self.folder / 'teste.db'
        self.app = create_app({'TESTING': True, 'DATABASE': str(self.database),
                               'SECRET_KEY': 'teste', 'STORAGE_DIR': str(self.folder / 'anexos'),
                               'BACKUP_DIR': str(self.folder / 'backups')})
        with self.connect() as conn:
            conn.execute('''INSERT INTO usuarios(id,nome,usuario,email,senha,cargo,admin,perfil,ativo)
                VALUES (1,'Gestor','gestor','gestor@teste.local',?,'Gestor',1,'admin',1)''', (self.senha_hash,))
            conn.execute('''INSERT INTO usuarios(id,nome,usuario,email,senha,cargo,admin,perfil,ativo)
                VALUES (2,'Pessoa','pessoa','pessoa@teste.local',?,'Colaborador',0,'colaborador',1)''', (self.senha_hash,))
        conn.close()
        self.client = self.app.test_client()
        self.autenticar(self.client)
        self.dia = hoje()

    def tearDown(self):
        shutil.rmtree(self.folder)

    def connect(self):
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        return conn

    def row(self, sql, params=()):
        conn = self.connect()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def autenticar(self, client, user_id=1):
        with client.session_transaction() as sess:
            sess['usuario_id'] = user_id
            sess['csrf_token'] = 'token-teste'

    def post(self, path, data=None, **kwargs):
        return self.client.post(path, data={'csrf_token': 'token-teste', **(data or {})}, **kwargs)

    def cadastrar(self, placa='ABC1D23', km=100):
        result = self.post('/frota', {'placa': placa, 'modelo': 'Caminhão', 'ano': '2024',
                            'tipo_combustivel': 'Diesel S10', 'quilometragem': str(km), 'observacoes': ''})
        self.assertEqual(result.status_code, 302)
        item = self.row('SELECT * FROM frota_veiculos WHERE placa=?', (placa,))
        self.assertIsNotNone(item)
        return item['id']

    def entrada(self, quantidade='100.000', **extras):
        return self.post('/combustivel', {'tipo': 'Entrada', 'combustivel': 'Diesel S10',
                          'quantidade': quantidade, 'valor': '550.00', 'data': self.dia,
                          'referencia': 'Nota de teste', **extras})

    def abastecer(self, veiculo, quantidade='30.000', km=100, **extras):
        return self.post('/combustivel', {'tipo': 'Saída', 'combustivel': 'Diesel S10',
                          'quantidade': quantidade, 'valor': '165.00', 'data': self.dia,
                          'veiculo_id': str(veiculo), 'motorista': 'Motorista Teste',
                          'quilometragem': str(km), 'tanque_cheio': '1', **extras})

    def saldo(self):
        return self.row("SELECT COALESCE(SUM(CASE tipo WHEN 'Entrada' THEN quantidade_ml ELSE -quantidade_ml END),0) s FROM combustivel_movimentos WHERE cancelado=0")['s']

    def test_cadastro_busca_edicao_sem_apagar_historico(self):
        veiculo = self.cadastrar()
        self.assertEqual(self.client.get('/frota?q=ABC').status_code, 200)
        self.assertEqual(self.client.get(f'/frota/veiculos/{veiculo}').status_code, 200)
        data = {'placa': 'ABC1D23', 'modelo': 'Caminhão atualizado', 'ano': '2024',
                'tipo_combustivel': 'Diesel S10', 'quilometragem': '90', 'ativo': '1'}
        self.post(f'/frota/veiculos/{veiculo}', data)
        self.assertEqual(self.row('SELECT quilometragem FROM frota_veiculos WHERE id=?', (veiculo,))[0], 100)
        data.update(quilometragem='150', ativo='0')
        self.post(f'/frota/veiculos/{veiculo}', data)
        self.assertEqual(self.row('SELECT ativo FROM frota_veiculos WHERE id=?', (veiculo,))[0], 0)
        self.assertEqual(self.row('SELECT count(*) FROM frota_veiculos')[0], 1)

    def test_placa_duplicada_e_campos_invalidos(self):
        self.cadastrar()
        for placa, ano in [('abc-1d23', '2024'), ('invalida', '2024'), ('DEF1G23', '-1')]:
            self.post('/frota', {'placa': placa, 'modelo': 'Teste', 'ano': ano,
                                'tipo_combustivel': 'Diesel S10', 'quilometragem': '0'})
        self.assertEqual(self.row('SELECT count(*) FROM frota_veiculos')[0], 1)

    def test_estoque_exato_e_recusa_saldo_insuficiente(self):
        veiculo = self.cadastrar()
        self.entrada('0,100')
        self.entrada('0.200')
        self.abastecer(veiculo, '0.300')
        self.assertEqual(self.saldo(), 0)
        self.abastecer(veiculo, '0.001')
        self.assertEqual(self.saldo(), 0)
        self.assertEqual(self.row('SELECT count(*) FROM combustivel_movimentos')[0], 3)

    def test_validacao_combustivel_data_quilometragem_veiculo(self):
        veiculo = self.cadastrar()
        self.entrada()
        for changes in ({'combustivel': 'Gasolina'}, {'quilometragem': '99'}, {'veiculo_id': '9999'},
                        {'quantidade': 'NaN'}, {'quantidade': '-1'}, {'data': '2100-01-01'},
                        {'quantidade': '1.0001'}, {'valor': 'Infinity'}, {'data': '2026-99-99'}):
            self.abastecer(veiculo, **changes)
        self.assertEqual(self.row('SELECT count(*) FROM combustivel_movimentos')[0], 1)
        self.assertEqual(self.saldo(), 100000)

    def test_cancelamento_preserva_historico_e_nunca_negativa_saldo(self):
        veiculo = self.cadastrar()
        self.entrada()
        self.abastecer(veiculo, '70', km=130)
        self.post('/combustivel/1/cancelar', {'motivo': 'Teste de cancelamento de entrada consumida'})
        self.assertEqual(self.saldo(), 30000)
        self.assertEqual(self.row('SELECT cancelado FROM combustivel_movimentos WHERE id=1')[0], 0)
        self.post('/combustivel/2/cancelar', {'motivo': 'Lançamento incorreto'})
        self.assertEqual(self.saldo(), 100000)
        self.post('/combustivel/2/cancelar', {'motivo': 'Repetição'})
        self.assertEqual(self.saldo(), 100000)
        self.post('/combustivel/1/cancelar', {'motivo': 'Nota lançada em duplicidade'})
        self.assertEqual(self.saldo(), 0)
        self.assertEqual(self.row('SELECT count(*) FROM combustivel_movimentos')[0], 2)
        self.assertEqual(self.row('SELECT quilometragem FROM frota_veiculos WHERE id=?', (veiculo,))[0], 130)

    def test_consumo_considera_abastecimentos_parciais(self):
        veiculo = self.cadastrar()
        self.entrada('300')
        self.abastecer(veiculo, '50', km=100)
        self.abastecer(veiculo, '20', km=200, tanque_cheio='0')
        self.abastecer(veiculo, '30', km=600)
        conn = self.connect()
        try:
            rows = conn.execute(REPORTS['combustivel_consumo']['sql']).fetchall()
        finally:
            conn.close()
        self.assertIsNone(rows[0]['Consumo (km/L)'])
        self.assertIsNone(rows[1]['Consumo (km/L)'])
        self.assertEqual(rows[2]['Consumo (km/L)'], 10.0)
        self.assertEqual(self.client.get('/combustivel').status_code, 200)

    def test_saida_retorno_e_inativacao_bloqueada_em_uso(self):
        veiculo = self.cadastrar()
        dados = {'motorista': 'Teste', 'destino': 'Filial', 'saida': self.dia, 'km_saida': '100'}
        self.post(f'/frota/veiculos/{veiculo}/utilizacoes', dados)
        self.post(f'/frota/veiculos/{veiculo}/utilizacoes', dados)
        self.assertEqual(self.row('SELECT count(*) FROM frota_utilizacoes')[0], 1)
        self.post(f'/frota/veiculos/{veiculo}', {'placa': 'ABC1D23', 'modelo': 'Caminhão', 'ano': '2024',
                  'tipo_combustivel': 'Diesel S10', 'quilometragem': '100', 'ativo': '0'})
        self.assertEqual(self.row('SELECT ativo FROM frota_veiculos')[0], 1)
        self.post('/frota/utilizacoes/1/encerrar', {'status': 'Concluída', 'retorno': self.dia, 'km_retorno': '90'})
        self.assertEqual(self.row('SELECT status FROM frota_utilizacoes')[0], 'Em uso')
        self.post('/frota/utilizacoes/1/encerrar', {'status': 'Concluída', 'retorno': self.dia, 'km_retorno': '250'})
        self.assertEqual(self.row('SELECT status FROM frota_utilizacoes')[0], 'Concluída')
        self.assertEqual(self.row('SELECT quilometragem FROM frota_veiculos')[0], 250)

    def test_manutencao_historico_e_bloqueio_de_utilizacao(self):
        veiculo = self.cadastrar()
        self.post(f'/frota/veiculos/{veiculo}/manutencoes', {'descricao': 'Troca de óleo', 'oficina': 'Oficina',
                  'data': self.dia, 'status': 'Em andamento', 'quilometragem': '100', 'custo': '100.00'})
        self.post(f'/frota/veiculos/{veiculo}/utilizacoes', {'motorista': 'Teste', 'destino': 'Filial', 'saida': self.dia, 'km_saida': '100'})
        self.assertEqual(self.row('SELECT count(*) FROM frota_utilizacoes')[0], 0)
        self.post('/frota/manutencoes/1/status', {'status': 'Concluída', 'observacao': 'Serviço completo', 'custo': '125,50', 'quilometragem': '110'})
        self.assertEqual(self.row('SELECT custo_centavos FROM frota_manutencoes')[0], 12550)
        self.assertEqual(self.row('SELECT count(*) FROM frota_manutencao_historico')[0], 2)
        self.post('/frota/manutencoes/1/status', {'status': 'Cancelada', 'observacao': 'Indevido', 'custo': '0', 'quilometragem': '110'})
        self.assertEqual(self.row('SELECT status FROM frota_manutencoes')[0], 'Concluída')
        self.assertEqual(self.client.get(f'/frota/veiculos/{veiculo}').status_code, 200)

    def test_documentos_upload_download_e_arquivamento(self):
        veiculo = self.cadastrar()
        self.post(f'/frota/veiculos/{veiculo}/documentos', {'titulo': 'Licenciamento', 'validade': self.dia,
                  'arquivo': (BytesIO(b'%PDF-1.4 teste'), 'licenciamento.pdf')}, content_type='multipart/form-data')
        self.assertEqual(self.row('SELECT count(*) FROM frota_documentos')[0], 1)
        response = self.client.get('/frota/documentos/1/arquivo')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response.headers['Content-Disposition'])
        response.close()
        self.post('/frota/documentos/1/arquivar')
        self.assertEqual(self.row('SELECT arquivado FROM frota_documentos')[0], 1)
        self.post(f'/frota/veiculos/{veiculo}/documentos', {'titulo': 'Inválido', 'arquivo': (BytesIO(b'<html>'), 'script.html')}, content_type='multipart/form-data')
        self.assertEqual(self.row('SELECT count(*) FROM frota_documentos')[0], 1)

    def test_permissoes_csrf_e_registros_inexistentes(self):
        veiculo = self.cadastrar()
        self.assertEqual(self.client.post('/combustivel', data={'tipo': 'Entrada'}).status_code, 400)
        self.assertEqual(self.client.get('/frota/veiculos/999').status_code, 404)
        self.assertEqual(self.post('/frota/manutencoes/999/status').status_code, 404)
        self.assertEqual(self.post('/combustivel/999/cancelar', {'motivo': 'teste'}).status_code, 404)
        self.autenticar(self.client, 2)
        for path in ('/frota', f'/frota/veiculos/{veiculo}', '/combustivel', '/frota/documentos/999/arquivo'):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.entrada().status_code, 403)
        self.assertEqual(self.post(f'/frota/veiculos/{veiculo}/utilizacoes', {}).status_code, 403)

    def test_relatorios_sql_funcionam_com_filtro_externo(self):
        self.cadastrar()
        self.entrada()
        conn = self.connect()
        try:
            for slug, report in REPORTS.items():
                sql = 'SELECT * FROM (' + report['sql'] + ') r'
                params = ()
                if report['data_coluna']:
                    sql += ' WHERE "' + report['data_coluna'] + '" >= ?'
                    params = ('2000-01-01',)
                with self.subTest(slug=slug):
                    conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_duas_saidas_concorrentes_nao_excedem_saldo(self):
        veiculo = self.cadastrar()
        self.entrada('100')

        def enviar(_):
            client = self.app.test_client()
            self.autenticar(client)
            response = client.post('/combustivel', data={'csrf_token': 'token-teste', 'tipo': 'Saída',
                'combustivel': 'Diesel S10', 'quantidade': '70', 'valor': '350', 'data': self.dia,
                'veiculo_id': str(veiculo), 'motorista': 'Teste', 'quilometragem': '100'})
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(enviar, range(2))), [302, 302])
        self.assertEqual(self.saldo(), 30000)
        self.assertEqual(self.row("SELECT COUNT(*) FROM combustivel_movimentos WHERE tipo='Saída'")[0], 1)


if __name__ == '__main__':
    unittest.main()
