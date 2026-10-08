import hashlib
import io
import re
import shutil
import sqlite3
import unittest
import uuid
from unittest.mock import patch
from pathlib import Path

from openpyxl import load_workbook
from werkzeug.security import generate_password_hash

from sagel import create_app
from sagel.base import db, hash_senha, conferir_senha, hoje


class PrincipalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = hash_senha('Teste12!')

    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1] / ('teste-principal-' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.config = {'TESTING': True, 'SECRET_KEY': 'chave-exclusiva-teste-local-32-caracteres',
                       'DATABASE': str(self.folder / 'teste.db'), 'STORAGE_DIR': str(self.folder / 'anexos'),
                       'BACKUP_DIR': str(self.folder / 'backups')}
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        with self.app.app_context():
            conn = db()
            for ident, role in enumerate(('admin', 'colaborador', 'frota', 'almoxarifado', 'ti', 'compras', 'documentacao'), 1):
                conn.execute('INSERT INTO usuarios(id,nome,usuario,senha,email,cargo,perfil,admin) VALUES(?,?,?,?,?,?,?,?)',
                             (ident, role, role, self.password, role+'@teste.local', role, role, int(role=='admin')))
            conn.commit()

    def tearDown(self):
        if self.folder.parent == Path(__file__).resolve().parents[1] and self.folder.name.startswith('teste-principal-'):
            shutil.rmtree(self.folder)

    def authenticate(self, ident=1):
        self.client.delete_cookie('sagel_acesso')
        with self.client.session_transaction() as session:
            session.clear()
            session['usuario_id'] = ident
            session['csrf_token'] = 'token-teste'

    def post(self, url, data=None, **kwargs):
        if url == '/cadastro':
            data = {'cargo':'colaborador', **(data or {})}
        with self.client.session_transaction() as session:
            session.setdefault('csrf_token', 'token-teste')
            token = session['csrf_token']
        return self.client.post(url, data={'csrf_token': token, **(data or {})}, **kwargs)

    def row(self, sql, args=()):
        with self.app.app_context():
            return db().execute(sql, args).fetchone()

    def test_registration_policy_hash_case_duplicates_and_login(self):
        for password in ('abc12!', 'abcdefgh', 'abcdefg1', 'abcdefg!', 'Abc12345!'):
            response = self.post('/cadastro', {'usuario':'novo', 'email':'novo@teste.local', 'senha':password})
            self.assertIn('exatamente 8 caracteres', response.text)
            self.assertIsNone(self.row("SELECT id FROM usuarios WHERE usuario='novo'"))
        with patch('sagel.cadastro_email.enviar_codigo') as sender:
            response = self.post('/cadastro', {'usuario':'novo', 'email':'NOVO@teste.local', 'senha':'Abc1234!'})
            code = sender.call_args.args[1]
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.row("SELECT * FROM usuarios WHERE usuario='novo'"))
        self.post('/cadastro/confirmar', {'codigo':code})
        user = self.row("SELECT * FROM usuarios WHERE usuario='novo'")
        self.assertEqual(user['perfil'], 'colaborador')
        self.assertTrue(user['senha'].startswith('$2b$'))
        self.assertTrue(conferir_senha(user['senha'], 'Abc1234!'))
        self.assertEqual(user['ativo'], 0)
        self.authenticate(1)
        self.post('/admin', {'acao':'aprovar_perfil','id':str(user['id']),'perfil':'colaborador'})
        self.authenticate(None)
        self.post('/cadastro', {'usuario':'NOVO', 'email':'outro@teste.local', 'senha':'Abc1234!'})
        self.assertEqual(self.row("SELECT COUNT(*) FROM usuarios WHERE lower(usuario)=lower('novo')")[0], 1)
        response = self.post('/', {'usuario':'novo', 'senha':'Abc1234!'})
        self.assertEqual(response.location, '/dashboard')
        self.assertIn('HttpOnly', response.headers.get('Set-Cookie'))
        self.assertEqual(self.client.get('/dashboard').status_code, 200)
        self.assertEqual(self.client.get('/admin').status_code, 403)

    def test_legacy_password_login_and_session_revocation(self):
        with self.app.app_context():
            db().execute('UPDATE usuarios SET senha=? WHERE id=2', (generate_password_hash('antiga'),))
            db().commit()
        self.post('/', {'usuario':'colaborador', 'senha':'antiga'})
        self.assertTrue(self.row('SELECT senha FROM usuarios WHERE id=2')[0].startswith('$2b$'))
        self.assertEqual(self.client.get('/dashboard').status_code, 200)
        with self.app.app_context():
            db().execute('UPDATE usuarios SET versao_sessao=versao_sessao+1 WHERE id=2')
            db().commit()
        self.assertEqual(self.client.get('/dashboard').status_code, 302)

    def test_production_ignores_session_impersonation_and_invalid_jwt(self):
        self.app.testing = False
        self.authenticate(1)
        self.assertEqual(self.client.get('/admin').status_code, 302)
        self.client.set_cookie('sagel_acesso', 'invalid.token.value')
        self.assertEqual(self.client.get('/dashboard').status_code, 302)

    def test_all_pages_and_reports_for_admin_and_role_permissions(self):
        self.authenticate()
        for path in ('/dashboard','/frota','/combustivel','/epi','/ti/estoque','/chamados','/compras','/compras/fornecedores','/documentos','/relatorios','/admin','/permissoes','/configuracoes','/auditoria','/avisos','/tarefas','/perfil','/funcionarios'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        for key in self.app.config['SAGEL_REPORTS']:
            with self.subTest(report=key):
                response = self.client.get('/relatorios', query_string={'tipo':key, 'inicio':'2020-01-01','fim':'2099-01-01'})
                self.assertEqual(response.status_code, 200)
        for ident, allowed, denied in ((3,'/frota','/epi'),(4,'/epi','/frota'),(5,'/ti/estoque','/admin'),(6,'/compras/fornecedores','/ti/estoque'),(7,'/documentos','/frota')):
            self.authenticate(ident)
            self.assertEqual(self.client.get(allowed).status_code,200)
            self.assertEqual(self.client.get(denied).status_code,403)
        self.authenticate(2)
        self.assertEqual(self.post('/avisos', {'titulo':'Inválido','mensagem':'x'}).status_code,403)
        self.assertEqual(self.post('/documentos').status_code,403)
        self.assertEqual(self.client.get('/relatorios?tipo=frota_veiculos').status_code,403)

    def test_excel_pdf_exports_and_formula_as_text(self):
        self.authenticate()
        self.post('/tarefas', {'titulo':'=SUM(1,2)','descricao':'teste','responsavel_id':'2','prazo':hoje()})
        response = self.client.get('/relatorios?tipo=tarefas&formato=xlsx')
        self.assertEqual(response.status_code,200)
        book = load_workbook(io.BytesIO(response.data))
        self.assertEqual(book.active['A2'].value,'=SUM(1,2)')
        self.assertEqual(book.active['A2'].data_type,'s')
        response = self.client.get('/relatorios?tipo=tarefas&formato=pdf')
        self.assertTrue(response.data.startswith(b'%PDF'))
        for key in self.app.config['SAGEL_REPORTS']:
            self.assertEqual(self.client.get('/relatorios',query_string={'tipo':key,'formato':'pdf'}).status_code,200)

    def test_recovery_token_is_private_once_only_and_policy_applies(self):
        self.post('/recuperar-senha', {'email':'colaborador@teste.local'})
        item = self.row('SELECT * FROM recuperacoes')
        self.assertEqual(item['status'],'Pendente')
        self.authenticate()
        response = self.post('/admin', {'acao':'recuperar','id':str(item['id'])})
        link = re.search(r'value="(http://localhost/redefinir/[^\"]+)"', response.text).group(1)
        self.assertNotIn(link.rsplit('/',1)[-1], self.row('SELECT token_hash FROM recuperacoes')[0])
        self.authenticate(None)
        self.assertIn('exatamente 8 caracteres', self.post(link, {'senha':'abc'}).text)
        self.assertEqual(self.post(link, {'senha':'Nova123!'}).status_code,302)
        self.assertEqual(self.client.get(link).status_code,400)

    def test_csrf_last_admin_deactivation_and_backups(self):
        self.authenticate()
        self.assertEqual(self.client.post('/avisos',data={'titulo':'bad','mensagem':'bad'}).status_code,400)
        response = self.post('/admin', {'id':'1','nome':'admin','usuario':'admin','email':'admin@teste.local','perfil':'colaborador','cargo':'Admin','ativo':'1'})
        self.assertIn('pelo menos um administrador', response.text)
        self.assertEqual(self.row('SELECT perfil FROM usuarios WHERE id=1')[0],'admin')
        self.post('/configuracoes', {'acao':'backup'})
        if self.app.config['DATABASE_BACKEND'] == 'postgresql':
            backup = next((self.folder/'backups').glob('*.dump'))
            self.assertEqual(backup.read_bytes()[:5], b'PGDMP')
            return
        backup = next((self.folder/'backups').glob('*.db'))
        conn = sqlite3.connect(backup)
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        conn.close()

if __name__ == '__main__':
    unittest.main()

