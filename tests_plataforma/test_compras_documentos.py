import io
from pathlib import Path
import shutil
import unittest
import uuid

from sagel import create_app
from sagel.base import db, hash_senha
from sagel.compras_documentos import init_schema


class ComprasDocumentosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = hash_senha('teste123')

    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1] / ('teste-compras-' + uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'testes-compras',
                               'DATABASE': str(self.folder / 'teste.db'),
                               'STORAGE_DIR': str(self.folder / 'anexos'),
                               'BACKUP_DIR': str(self.folder / 'backups')})
        self.client = self.app.test_client()
        with self.app.app_context():
            conn = db()
            for ident, name, role, sector in (
                (1, 'Admin', 'admin', 'TI'), (2, 'Comprador', 'compras', 'Compras'),
                (3, 'Documentalista', 'documentacao', 'Documentação'),
                (4, 'Solicitante', 'colaborador', 'Financeiro'),
                (5, 'Outra pessoa', 'colaborador', 'Operações'),
                (6, 'Técnico', 'ti', 'TI')):
                conn.execute('''INSERT INTO usuarios(id,nome,usuario,senha,cargo,perfil,setor,ativo,email)
                    VALUES(?,?,?,?,?,?,?,1,?)''',
                    (ident, name, f'user{ident}', self.password, role, role, sector, f'user{ident}@teste.local'))
            conn.commit()
        self.login(4)

    def tearDown(self):
        # Remoção limitada ao diretório isolado criado por este teste.
        if self.folder.parent == Path(__file__).resolve().parents[1] and self.folder.name.startswith('teste-compras-'):
            shutil.rmtree(self.folder)

    def login(self, ident):
        with self.client.session_transaction() as session:
            session.clear()
            session['usuario_id'] = ident
            session['csrf_token'] = 'teste-token'

    def post(self, path, data=None, **kwargs):
        payload = dict(data or {})
        payload['csrf_token'] = 'teste-token'
        return self.client.post(path, data=payload, **kwargs)

    def row(self, sql, args=()):
        with self.app.app_context():
            return db().execute(sql, args).fetchone()

    def create_request(self):
        result = self.post('/compras', {'titulo': 'Material exclusivo solicitante', 'setor': 'Financeiro',
                           'centro_custo': 'CC100', 'justificativa': 'Reposição mensal',
                           'itens': 'Papel A4 | 2 | resma\nCaneta | 3 | unidade',
                           'anexo': (io.BytesIO(b'requisicao'), 'requisicao.txt')})
        self.assertEqual(result.status_code, 302)
        return self.row('SELECT max(id) FROM cp_solicitacoes')[0]

    def add_quote(self, sid, price1='12,50', price2='3.00'):
        self.post('/compras/fornecedores', {'nome': 'Fornecedor de teste', 'email': 'fornecedor@teste.local'})
        fid = self.row('SELECT max(id) FROM cp_fornecedores')[0]
        with self.app.app_context():
            items = db().execute('SELECT id FROM cp_itens WHERE solicitacao_id=? ORDER BY id', (sid,)).fetchall()
        data = {'acao': 'cotacao', 'fornecedor_id': str(fid), 'prazo': '2026-12-31', 'condicoes': '30 dias'}
        data.update({f"preco_{i['id']}": price for i, price in zip(items, [price1, price2])})
        result = self.post(f'/compras/{sid}', data)
        self.assertEqual(result.status_code, 302)
        return self.row('SELECT max(id) FROM cp_cotacoes')[0]

    def create_document(self, **extra):
        data = {'nome': 'Contrato reservado', 'categoria': 'Contratos', 'descricao': 'Descrição privada',
                'palavras_chave': 'contrato, teste', 'responsavel_id': '3', 'validade': '2020-01-01',
                'visibilidade': 'restrito', 'usuarios': ['4'], 'publicar': '1',
                'arquivo': (io.BytesIO(b'conteudo-original'), 'contrato.txt')}
        data.update(extra)
        response = self.post('/documentos', data)
        self.assertEqual(response.status_code, 302)
        return self.row('SELECT max(id) FROM gd_documentos')[0]

    def test_complete_purchase_flow_and_totals(self):
        sid = self.create_request()
        self.assertEqual(self.client.get(f'/compras/{sid}').status_code, 200)
        self.assertEqual(self.post(f'/compras/{sid}', {'acao': 'aprovar'}).status_code, 403)
        self.login(2)
        # Impedir pular a autorização e criar um pedido vazio.
        self.post(f'/compras/{sid}', {'acao': 'pedido'})
        self.assertEqual(self.row('SELECT count(*) FROM cp_pedidos')[0], 0)
        self.post(f'/compras/{sid}', {'acao': 'aprovar'})
        qid = self.add_quote(sid)
        self.assertEqual(self.row('SELECT total_centavos FROM cp_cotacoes WHERE id=?', (qid,))[0], 3400)
        cheaper = self.add_quote(sid, '10.00', '2.00')
        fid = self.row('SELECT fornecedor_id FROM cp_cotacoes WHERE id=?', (cheaper,))[0]
        self.assertEqual(self.client.get(f'/compras/fornecedores/{fid}').status_code, 200)
        self.post(f'/compras/fornecedores/{fid}', {'nome': 'Fornecedor atualizado', 'email': 'novo@teste.local'})
        self.assertEqual(self.row('SELECT nome FROM cp_fornecedores WHERE id=?', (fid,))[0], 'Fornecedor atualizado')
        self.assertEqual(self.row('SELECT fornecedor_nome FROM cp_cotacoes WHERE id=?', (cheaper,))[0], 'Fornecedor de teste')
        self.assertEqual(self.client.get(f'/compras/{sid}').status_code, 200)
        self.post(f'/compras/{sid}', {'acao': 'aprovar_final', 'cotacao_id': str(cheaper), 'observacao': 'Menor valor'})
        self.post(f'/compras/{sid}', {'acao': 'pedido'})
        self.assertEqual(self.row('SELECT cotacao_id FROM cp_pedidos')[0], cheaper)
        self.assertEqual(self.client.get(f'/compras/{sid}/pedido').status_code, 200)
        self.post(f'/compras/{sid}', {'acao': 'atualizar_pedido', 'status': 'parcial', 'observacao': 'Papel recebido'})
        self.post(f'/compras/{sid}', {'acao': 'atualizar_pedido', 'status': 'concluido', 'observacao': 'Todos os itens recebidos'})
        self.post(f'/compras/{sid}', {'acao': 'atualizar_pedido', 'status': 'cancelado', 'observacao': 'Não permitido após concluir'})
        self.assertEqual(self.row('SELECT status FROM cp_pedidos')[0], 'concluido')
        self.assertGreaterEqual(self.row('SELECT count(*) FROM cp_historico WHERE solicitacao_id=?', (sid,))[0], 8)

    def test_purchase_ownership_and_attachment_permissions(self):
        sid = self.create_request()
        self.login(5)
        self.assertNotIn('Material exclusivo solicitante', self.client.get('/compras').get_data(as_text=True))
        for path in (f'/compras/{sid}', f'/compras/{sid}/anexo', f'/compras/{sid}/pedido'):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.post(f'/compras/{sid}', {'acao': 'cancelar', 'observacao': 'Tentativa'}).status_code, 403)
        self.assertEqual(self.post('/compras/fornecedores', {'nome': 'Não autorizado'}).status_code, 403)
        self.login(4)
        response = self.client.get(f'/compras/{sid}/anexo')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'requisicao')
        response.close()

    def test_return_and_resubmit_preserves_history(self):
        sid = self.create_request()
        self.login(2)
        self.post(f'/compras/{sid}', {'acao': 'devolver', 'observacao': 'Corrigir quantidade'})
        self.login(4)
        self.post(f'/compras/{sid}', {'acao': 'reenviar', 'titulo': 'Material corrigido', 'setor': 'Financeiro',
                 'centro_custo': 'CC100', 'justificativa': 'Quantidade corrigida', 'itens': 'Papel A4 | 4 | resma'})
        self.assertEqual(self.row('SELECT status FROM cp_solicitacoes WHERE id=?', (sid,))[0], 'solicitada')
        history = self.row('SELECT observacao FROM cp_historico WHERE solicitacao_id=? ORDER BY id DESC', (sid,))[0]
        self.assertIn('Papel A4 (2 resma)', history)
        self.assertEqual(self.row('SELECT quantidade FROM cp_itens WHERE solicitacao_id=?', (sid,))[0], 4)

    def test_rejects_invalid_values_and_csrf(self):
        self.assertEqual(self.client.post('/compras', data={'titulo': 'sem token'}).status_code, 400)
        self.post('/compras', {'titulo': 'Inválida', 'setor': 'TI', 'centro_custo': 'CC',
                              'justificativa': 'Teste', 'itens': 'Item | nan | unidade'})
        self.assertEqual(self.row('SELECT count(*) FROM cp_solicitacoes')[0], 0)
        sid = self.create_request()
        self.login(2)
        self.post(f'/compras/{sid}', {'acao': 'aprovar'})
        self.add_quote(sid, '-1', '2')
        self.assertEqual(self.row('SELECT count(*) FROM cp_cotacoes')[0], 0)

    def test_document_private_versions_and_publication(self):
        self.login(3)
        did = self.create_document()
        original = self.row('SELECT versao_atual_id FROM gd_documentos WHERE id=?', (did,))[0]
        self.post(f'/documentos/{did}', {'acao': 'versao', 'observacao': 'Revisão pendente',
                 'arquivo': (io.BytesIO(b'conteudo-revisado'), 'contrato-v2.txt')})
        draft = self.row('SELECT max(id) FROM gd_versoes WHERE documento_id=?', (did,))[0]
        self.assertNotEqual(original, draft)
        self.assertEqual(self.client.get(f'/documentos/{did}').status_code, 200)
        self.login(4)
        self.assertIn('Contrato reservado', self.client.get('/documentos?q=contrato').get_data(as_text=True))
        detail = self.client.get(f'/documentos/{did}')
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn('Revisão pendente', detail.get_data(as_text=True))
        response = self.client.get(f'/documentos/{did}/versoes/{original}/conteudo')
        self.assertEqual(response.data, b'conteudo-original')
        response.close()
        self.assertEqual(self.client.get(f'/documentos/{did}/versoes/{draft}/conteudo').status_code, 403)
        self.assertEqual(self.post(f'/documentos/{did}', {'acao': 'arquivar'}).status_code, 403)
        self.login(3)
        self.post(f'/documentos/{did}', {'acao': 'publicar', 'versao_id': str(draft)})
        self.login(4)
        response = self.client.get(f'/documentos/{did}/versoes/{draft}/conteudo')
        self.assertEqual(response.data, b'conteudo-revisado')
        response.close()
        response = self.client.get(f'/documentos/{did}/versoes/{original}/conteudo')
        self.assertEqual(response.data, b'conteudo-original')
        response.close()
        self.assertGreaterEqual(self.row("SELECT count(*) FROM auditoria WHERE modulo='documentos' AND acao LIKE 'Download%' ")[0], 3)

    def test_document_acl_by_user_profile_sector_and_archive(self):
        self.login(3)
        did = self.create_document(usuarios=[], perfis=['ti'], setores='Financeiro')
        self.login(4)
        self.assertEqual(self.client.get(f'/documentos/{did}').status_code, 200)
        self.login(6)
        self.assertEqual(self.client.get(f'/documentos/{did}').status_code, 200)
        self.login(5)
        self.assertNotIn('Contrato reservado', self.client.get('/documentos').get_data(as_text=True))
        self.assertNotIn('Contratos', self.client.get('/documentos').get_data(as_text=True))
        self.assertEqual(self.client.get(f'/documentos/{did}').status_code, 403)
        version = self.row('SELECT versao_atual_id FROM gd_documentos WHERE id=?', (did,))[0]
        self.assertEqual(self.client.get(f'/documentos/{did}/versoes/{version}/conteudo').status_code, 403)
        self.login(3)
        self.post(f'/documentos/{did}', {'acao': 'arquivar'})
        self.login(4)
        self.assertEqual(self.client.get(f'/documentos/{did}').status_code, 403)
        self.login(3)
        self.post(f'/documentos/{did}', {'acao': 'arquivar'})
        self.login(4)
        self.assertEqual(self.client.get(f'/documentos/{did}').status_code, 200)

    def test_document_rejects_unsafe_links_and_migrates_legacy_once(self):
        self.login(3)
        data = {'nome': 'Documento inválido', 'categoria': 'Outros', 'responsavel_id': '3',
                'visibilidade': 'todos', 'publicar': '1', 'link': 'javascript:alert(1)'}
        self.post('/documentos', data)
        self.assertEqual(self.row('SELECT count(*) FROM gd_documentos')[0], 0)
        with self.app.app_context():
            conn = db()
            conn.execute("INSERT INTO documentos(nome,categoria,link) VALUES('Legado seguro','Políticas','https://example.com/documento')")
            conn.execute("INSERT INTO documentos(nome,categoria,link) VALUES('Legado inválido','Outros','javascript:alert(1)')")
            conn.commit()
            init_schema(conn)
            init_schema(conn)
            conn.commit()
        self.assertEqual(self.row('SELECT count(*) FROM gd_documentos')[0], 2)
        self.assertEqual(self.row('SELECT count(*) FROM documentos')[0], 2)
        bad = self.row("SELECT id,versao_atual_id FROM gd_documentos WHERE nome='Legado inválido'")
        response = self.client.get(f"/documentos/{bad['id']}/versoes/{bad['versao_atual_id']}/conteudo")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].startswith('/documentos/'))
        self.login(4)
        good = self.row("SELECT id,versao_atual_id FROM gd_documentos WHERE nome='Legado seguro'")
        response = self.client.get(f"/documentos/{good['id']}/versoes/{good['versao_atual_id']}/conteudo")
        self.assertEqual(response.headers['Location'], 'https://example.com/documento')


if __name__ == '__main__':
    unittest.main()

