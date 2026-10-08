import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from sagel import create_app
from sagel.base import db, conferir_senha

class CadastroEmailTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1]/('teste-email-'+uuid.uuid4().hex)
        self.folder.mkdir()
        self.app=create_app({'TESTING':True,'SECRET_KEY':'teste-chave-email-segura-32-caracteres',
            'DATABASE':str(self.folder/'teste.db'),'STORAGE_DIR':str(self.folder/'anexos'),
            'BACKUP_DIR':str(self.folder/'backups'),'SAGEL_SMTP':{}})
        self.client=self.app.test_client()
        self.sender=patch('sagel.cadastro_email.enviar_codigo')
        self.send=self.sender.start()
    def tearDown(self):
        self.sender.stop()
        assert self.folder.parent==Path(__file__).resolve().parents[1] and self.folder.name.startswith('teste-email-')
        shutil.rmtree(self.folder)
    def post(self,path,data=None,client=None):
        if path == '/cadastro':
            data = {'cargo':'colaborador', **(data or {})}
        client=client or self.client
        with client.session_transaction() as s:
            s.setdefault('csrf_token','csrf-teste')
        return client.post(path,data={'csrf_token':'csrf-teste',**(data or {})})
    def begin(self):
        response=self.post('/cadastro',{'email':'teste@example.test','usuario':'pessoa','senha':'Abcd123!'})
        self.assertEqual(response.location,'/cadastro/confirmar')
        return self.send.call_args.args[1]
    def row(self,sql):
        with self.app.app_context(): return db().execute(sql).fetchone()
    def change(self,sql):
        with self.app.app_context():
            db().execute(sql);db().commit()
    def test_only_creates_account_after_correct_code_and_cannot_reuse(self):
        code=self.begin()
        self.assertEqual(self.row('SELECT COUNT(*) FROM usuarios')[0],0)
        item=self.row('SELECT * FROM cadastros_pendentes')
        self.assertNotEqual(item['codigo_hash'],code)
        self.assertTrue(conferir_senha(item['senha_hash'],'Abcd123!'))
        self.assertEqual(self.post('/cadastro/confirmar',{'codigo':code}).location,'/')
        self.assertEqual(self.row('SELECT COUNT(*) FROM usuarios')[0],1)
        self.assertEqual(self.row('SELECT COUNT(*) FROM cadastros_pendentes')[0],0)
        self.post('/cadastro/confirmar',{'codigo':code})
        self.assertEqual(self.row('SELECT COUNT(*) FROM usuarios')[0],1)
        self.assertIn('aguarda aprovação',self.post('/',{'usuario':'pessoa','senha':'Abcd123!'}).text)
        self.assertEqual(self.row('SELECT ativo FROM usuarios')[0],0)
    def test_wrong_code_locks_after_five_attempts(self):
        code=self.begin()
        wrong='000000' if code!='000000' else '111111'
        for _ in range(5): self.post('/cadastro/confirmar',{'codigo':wrong})
        r=self.post('/cadastro/confirmar',{'codigo':code})
        self.assertIn('Limite de tentativas',r.text)
        self.assertEqual(self.row('SELECT COUNT(*) FROM usuarios')[0],0)
    def test_expired_code_is_not_accepted(self):
        code=self.begin()
        self.change("UPDATE cadastros_pendentes SET expira_em='2000-01-01T00:00:00'")
        self.assertIn('expirou',self.post('/cadastro/confirmar',{'codigo':code}).text)
        self.assertEqual(self.row('SELECT COUNT(*) FROM usuarios')[0],0)
    def test_resend_has_cooldown_and_invalidates_old_code(self):
        with patch('sagel.cadastro_email.secrets.randbelow',return_value=123456): old=self.begin()
        self.post('/cadastro/confirmar',{'acao':'reenviar'})
        self.assertEqual(self.send.call_count,1)
        self.change("UPDATE cadastro_envios SET data='2000-01-01T00:00:00'")
        with patch('sagel.cadastro_email.secrets.randbelow',return_value=654321):
            self.post('/cadastro/confirmar',{'acao':'reenviar'})
        self.assertEqual(self.send.call_count,2)
        self.assertIn('Código incorreto',self.post('/cadastro/confirmar',{'codigo':old}).text)
        self.assertEqual(self.post('/cadastro/confirmar',{'codigo':'654321'}).location,'/')
    def test_send_failure_never_creates_account_or_leaks_code(self):
        self.send.side_effect=RuntimeError('private-secret')
        r=self.post('/cadastro',{'email':'teste@example.test','usuario':'pessoa','senha':'Abcd123!'})
        self.assertIn('Não foi possível enviar',r.text)
        self.assertNotIn('private-secret',r.text)
        self.assertEqual(self.row('SELECT COUNT(*) FROM usuarios')[0],0)
        self.assertEqual(self.row('SELECT COUNT(*) FROM cadastros_pendentes')[0],0)
    def test_code_bound_to_browser_csrf_and_closed_registration(self):
        code=self.begin()
        other=self.app.test_client()
        self.post('/cadastro/confirmar',{'codigo':code},client=other)
        self.assertEqual(self.row('SELECT COUNT(*) FROM usuarios')[0],0)
        self.assertEqual(self.client.post('/cadastro/confirmar',data={'codigo':code}).status_code,400)
        self.change("UPDATE configuracoes SET valor='0' WHERE chave='cadastro_aberto'")
        self.assertEqual(self.post('/cadastro/confirmar',{'codigo':code}).location,'/cadastro')
        self.assertEqual(self.row('SELECT COUNT(*) FROM usuarios')[0],0)
    def test_missing_mail_configuration_fails_closed(self):
        self.sender.stop()
        r=self.post('/cadastro',{'email':'teste@example.test','usuario':'pessoa','senha':'Abcd123!'})
        self.assertIn('Não foi possível enviar',r.text)
        self.assertEqual(self.row('SELECT COUNT(*) FROM usuarios')[0],0)

if __name__=='__main__': unittest.main()
