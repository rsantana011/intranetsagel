"""Cadastro por cargo, aprovação de acesso e política de senha.

Usa somente bancos isolados e substitui todo envio de e-mail por um mock.
"""
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sagel import create_app
from sagel.base import PERFIS, conferir_senha, db, hash_senha
from sagel.principal import senha_valida


class SenhaECargosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_senha('sagel@25')

    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1] / ('teste-cargos-' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(self.cleanup_folder)
        self.app = create_app({
            'TESTING': True,
            'SECRET_KEY': 'teste-cargos-chave-com-32-caracteres',
            'DATABASE': str(self.folder / 'teste.db'),
            'STORAGE_DIR': str(self.folder / 'anexos'),
            'BACKUP_DIR': str(self.folder / 'backups'),
            'SAGEL_SMTP': {'host': 'smtp.example.test', 'remetente': 'intranet@example.test'},
        })
        self.client = self.app.test_client()
        self.sender = patch('sagel.cadastro_email.enviar_codigo')
        self.send = self.sender.start()
        self.addCleanup(self.sender.stop)
        with self.app.app_context():
            conn = db()
            conn.execute("INSERT INTO usuarios(id,nome,usuario,email,senha,cargo,perfil,admin,ativo) VALUES(1,'Admin','admin','admin@example.test',?,'Administrador','admin',1,1)", (self.password_hash,))
            conn.execute("INSERT INTO usuarios(id,nome,usuario,email,senha,cargo,perfil,admin,ativo) VALUES(2,'Gestor','gestor','gestor@example.test',?,'TI','ti',0,1)", (self.password_hash,))
            conn.execute("INSERT OR IGNORE INTO permissoes VALUES('ti','usuarios','gerenciar')")
            conn.commit()

    def cleanup_folder(self):
        target = self.folder.resolve()
        root = Path(__file__).resolve().parents[1]
        assert target.parent == root and target.name.startswith('teste-cargos-')
        if target.exists():
            shutil.rmtree(target)

    def post(self, path, data=None, client=None):
        client = client or self.client
        with client.session_transaction() as session:
            session['csrf_token'] = 'teste-csrf'
        return client.post(path, data={'csrf_token': 'teste-csrf', **(data or {})})

    def row(self, sql, args=()):
        with self.app.app_context():
            return db().execute(sql, args).fetchone()

    def change(self, sql, args=()):
        with self.app.app_context():
            db().execute(sql, args)
            db().commit()

    def login_as(self, ident):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['usuario_id'] = ident
        return client

    def start_registration(self, cargo='compras', user='pessoa', password='sagel@25'):
        response = self.post('/cadastro', {'email': user + '@example.test', 'usuario': user,
                                          'senha': password, 'cargo': cargo})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/cadastro/confirmar')
        return self.send.call_args.args[1]

    def confirm_registration(self, cargo='compras', user='pessoa'):
        code = self.start_registration(cargo, user)
        self.assertEqual(self.post('/cadastro/confirmar', {'codigo': code}).location, '/')
        return self.row('SELECT * FROM usuarios WHERE usuario=?', (user,))

    def test_password_requires_only_eight_characters_number_and_special(self):
        for password in ('sagel@25', '1234567!', 'Abc 123!', 'a123456@', 'Abc12é!x', 'Abc12!😀x'):
            with self.subTest(password=password):
                self.assertEqual(senha_valida(password), password)
                self.assertTrue(conferir_senha(hash_senha(password), password))
        for password in ('', 'Abc12!', 'Abcd1234!', 'abcdefgh', 'abcdefg!', '12345678', 'Abc 1234', 'abc１２３@x'):
            with self.subTest(invalid=password):
                with self.assertRaises(ValueError):
                    senha_valida(password)

    def test_all_requested_flowchart_roles_remain_inactive_until_approved(self):
        for cargo in ('colaborador', 'frota', 'almoxarifado', 'ti', 'compras', 'documentacao'):
            with self.subTest(cargo=cargo):
                user = self.confirm_registration(cargo, 'pessoa_' + cargo)
                self.assertEqual(user['cargo'], PERFIS[cargo])
                self.assertEqual(user['perfil_solicitado'], cargo)
                self.assertEqual(user['perfil'], 'colaborador')
                self.assertEqual(user['admin'], 0)
                self.assertEqual(user['ativo'], 0)

    def test_invalid_or_admin_cargo_never_sends_code_or_creates_account(self):
        for cargo in ('', 'admin', 'diretor', 'TI', '../../admin'):
            with self.subTest(cargo=cargo):
                response = self.post('/cadastro', {'email': 'novo@example.test', 'usuario': 'novo',
                                                   'senha': 'sagel@25', 'cargo': cargo})
                self.assertNotEqual(response.location, '/cadastro/confirmar')
                self.assertEqual(self.row('SELECT COUNT(*) FROM cadastros_pendentes')[0], 0)
                self.assertEqual(self.row("SELECT COUNT(*) FROM usuarios WHERE usuario='novo'")[0], 0)
        self.send.assert_not_called()

    def test_forged_permissions_do_not_self_grant_access(self):
        response = self.post('/cadastro', {'email': 'novo@example.test', 'usuario': 'novo',
            'senha': 'sagel@25', 'cargo': 'ti', 'perfil': 'admin', 'admin': '1', 'ativo': '1'})
        self.assertEqual(response.location, '/cadastro/confirmar')
        code = self.send.call_args.args[1]
        self.post('/cadastro/confirmar', {'codigo': code, 'cargo': 'admin', 'perfil': 'admin', 'ativo': '1'})
        user = self.row("SELECT * FROM usuarios WHERE usuario='novo'")
        self.assertEqual((user['perfil'], user['admin'], user['ativo'], user['perfil_solicitado']),
                         ('colaborador', 0, 0, 'ti'))

    def test_resend_preserves_cargo_and_latest_code_creates_pending_user(self):
        with patch('sagel.cadastro_email.secrets.randbelow', return_value=123456):
            self.start_registration('documentacao')
        self.change("UPDATE cadastro_envios SET data='2000-01-01T00:00:00'")
        with patch('sagel.cadastro_email.secrets.randbelow', return_value=654321):
            self.post('/cadastro/confirmar', {'acao': 'reenviar', 'cargo': 'admin'})
        self.assertEqual(self.send.call_count, 2)
        self.post('/cadastro/confirmar', {'codigo': '123456'})
        self.assertIsNone(self.row("SELECT * FROM usuarios WHERE usuario='pessoa'"))
        self.post('/cadastro/confirmar', {'codigo': '654321'})
        user = self.row("SELECT * FROM usuarios WHERE usuario='pessoa'")
        self.assertEqual(user['perfil_solicitado'], 'documentacao')
        self.assertEqual(user['cargo'], 'Documentação')
        self.assertEqual(user['ativo'], 0)

    def test_correct_password_explains_approval_and_never_issues_access_cookie(self):
        user = self.confirm_registration('ti')
        response = self.post('/', {'usuario': user['usuario'], 'senha': 'sagel@25'})
        self.assertIn('aprova', response.text.lower())
        self.assertNotEqual(response.location, '/dashboard')
        self.assertFalse(any(header.startswith('sagel_acesso=') for header in response.headers.getlist('Set-Cookie')))
        self.assertEqual(self.client.get('/dashboard').location, '/')
        self.assertEqual(self.client.get('/compras').location, '/')
        forged_session = self.login_as(user['id'])
        self.assertEqual(forged_session.get('/dashboard').location, '/')

    def test_wrong_password_does_not_disclose_pending_account(self):
        self.confirm_registration('compras')
        response = self.post('/', {'usuario': 'pessoa', 'senha': 'errada'})
        self.assertIn('Usuário ou senha incorretos', response.text)
        self.assertNotIn('aguardando aprovação', response.text.lower())

    def test_only_admin_approves_and_grants_selected_profile(self):
        user = self.confirm_registration('frota')
        data = {'acao': 'aprovar_perfil', 'id': str(user['id']), 'perfil': 'frota'}
        manager = self.login_as(2)
        self.assertEqual(self.post('/admin', data, manager).status_code, 403)
        self.assertEqual(self.row('SELECT ativo FROM usuarios WHERE id=?', (user['id'],))[0], 0)
        admin = self.login_as(1)
        response = self.post('/admin', data, admin)
        self.assertIn(response.status_code, (200, 302))
        updated = self.row('SELECT * FROM usuarios WHERE id=?', (user['id'],))
        self.assertEqual((updated['perfil'], updated['ativo'], updated['admin']), ('frota', 1, 0))
        self.assertFalse(updated['perfil_solicitado'])
        self.assertEqual(self.post('/', {'usuario': 'pessoa', 'senha': 'sagel@25'}).location, '/dashboard')
        self.assertEqual(self.client.get('/frota').status_code, 200)
        self.assertEqual(self.client.get('/admin').status_code, 403)
        self.assertEqual(self.client.get('/ti/estoque').status_code, 403)

    def test_admin_may_choose_other_non_admin_profile_and_approval_is_one_time(self):
        user = self.confirm_registration('compras')
        admin = self.login_as(1)
        self.post('/admin', {'acao': 'aprovar_perfil', 'id': user['id'], 'perfil': 'colaborador'}, admin)
        first = self.row('SELECT * FROM usuarios WHERE id=?', (user['id'],))
        self.assertEqual((first['perfil'], first['ativo']), ('colaborador', 1))
        self.post('/admin', {'acao': 'aprovar_perfil', 'id': user['id'], 'perfil': 'ti'}, admin)
        second = self.row('SELECT * FROM usuarios WHERE id=?', (user['id'],))
        self.assertEqual(second['perfil'], 'colaborador')
        self.assertEqual(second['versao_sessao'], first['versao_sessao'])

    def test_admin_profile_cannot_be_granted_through_approval_action(self):
        user = self.confirm_registration('ti')
        self.post('/admin', {'acao': 'aprovar_perfil', 'id': user['id'], 'perfil': 'admin'}, self.login_as(1))
        user = self.row('SELECT * FROM usuarios WHERE id=?', (user['id'],))
        self.assertEqual((user['perfil'], user['admin'], user['ativo']), ('colaborador', 0, 0))
        self.assertEqual(user['perfil_solicitado'], 'ti')

    def test_only_admin_can_reject_and_rejection_keeps_access_disabled(self):
        user = self.confirm_registration('documentacao')
        data = {'acao': 'recusar_perfil', 'id': user['id']}
        self.assertEqual(self.post('/admin', data, self.login_as(2)).status_code, 403)
        self.post('/admin', data, self.login_as(1))
        user = self.row('SELECT * FROM usuarios WHERE id=?', (user['id'],))
        self.assertEqual(user['ativo'], 0)
        self.assertFalse(user['perfil_solicitado'])
        self.assertNotEqual(self.post('/', {'usuario': 'pessoa', 'senha': 'sagel@25'}).location, '/dashboard')

    def test_approval_requires_csrf(self):
        user = self.confirm_registration()
        response = self.login_as(1).post('/admin', data={'acao': 'aprovar_perfil', 'id': user['id'], 'perfil': 'compras'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.row('SELECT ativo FROM usuarios WHERE id=?', (user['id'],))[0], 0)

    def test_user_management_permission_cannot_escalate_ti_to_admin(self):
        pending = self.confirm_registration('ti')
        manager = self.login_as(2)
        self.assertEqual(manager.get('/admin').status_code, 403)
        self.assertEqual(manager.get('/permissoes').status_code, 403)
        common = {'nome': 'Nome alterado', 'senha': 'sagel@25', 'perfil': 'admin',
                  'cargo': 'Administrador', 'ativo': '1', 'setor': 'TI'}
        attempts = (
            {'id': 2, 'usuario': 'gestor', 'email': 'gestor@example.test'},
            {'id': '', 'usuario': 'admin_forjado', 'email': 'forjado@example.test'},
            {'id': 1, 'usuario': 'admin', 'email': 'admin@example.test'},
            {'id': pending['id'], 'usuario': 'pessoa', 'email': 'pessoa@example.test'},
        )
        for operation in attempts:
            with self.subTest(ident=operation['id']):
                response = self.post('/admin', {**common, **operation}, manager)
                self.assertEqual(response.status_code, 403)
        admin = self.row('SELECT * FROM usuarios WHERE id=1')
        ti = self.row('SELECT * FROM usuarios WHERE id=2')
        waiting = self.row('SELECT * FROM usuarios WHERE id=?', (pending['id'],))
        self.assertEqual((admin['nome'], admin['perfil'], admin['ativo']), ('Admin', 'admin', 1))
        self.assertEqual((ti['nome'], ti['perfil'], ti['admin']), ('Gestor', 'ti', 0))
        self.assertEqual((waiting['ativo'], waiting['perfil_solicitado']), (0, 'ti'))
        self.assertIsNone(self.row("SELECT * FROM usuarios WHERE usuario='admin_forjado'"))


if __name__ == '__main__':
    unittest.main()
