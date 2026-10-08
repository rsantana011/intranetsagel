"""Permissões individuais em PostgreSQL isolado: nunca usa o banco operacional."""
import os
import unittest
import uuid
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from sagel import create_app
from sagel.base import db


@unittest.skipUnless(os.getenv('SAGEL_TEST_POSTGRES_DSN'), 'PostgreSQL temporário não configurado')
class AcessosPostgresTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ['SAGEL_TEST_POSTGRES_DSN']
        self.schema = 'sagel_acl_test_' + uuid.uuid4().hex
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.cleanup_schema)
        config = {'TESTING': True, 'SECRET_KEY': 'test-only-acl-postgres-key',
                  'DATABASE_BACKEND': 'postgresql',
                  'POSTGRES_CONNECT_OPTIONS': {**conninfo_to_dict(self.dsn),
                                                'options': '-c search_path=' + self.schema}}
        self.app = create_app(config)
        self.client = self.app.test_client()
        with self.app.app_context():
            conn = db()
            for name,role in [('admin','admin'),('funcionario','frota')]:
                conn.execute('INSERT INTO usuarios(nome,usuario,senha,cargo,perfil,ativo) VALUES(?,?,?,?,?,1)',
                             (name,name,'invalid-test-only','Teste',role))
            conn.commit()
        with self.client.session_transaction() as sess:
            sess['usuario_id'] = 1
            sess['csrf_token'] = 'test-token'

    def cleanup_schema(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def test_grant_revoke_and_protect_routes_on_postgres(self):
        path='/admin/usuarios/2/acessos'
        response=self.client.post(path,data={'csrf_token':'test-token','modo':'individual','permissao':['frota:ver']})
        self.assertEqual(response.status_code,302)
        with self.app.app_context():
            self.assertEqual(db().execute('SELECT versao_sessao FROM usuarios WHERE id=2').fetchone()[0],1)
            self.assertEqual(db().execute('SELECT COUNT(*) FROM permissoes_usuario WHERE usuario_id=2').fetchone()[0],1)
        staff=self.app.test_client()
        with staff.session_transaction() as sess:
            sess['usuario_id']=2
            sess['csrf_token']='test-token'
        self.assertEqual(staff.get('/frota').status_code,200)
        self.assertEqual(staff.get('/funcionarios').status_code,403)
        self.assertEqual(staff.post('/frota',data={'csrf_token':'test-token'}).status_code,403)
        self.assertEqual(self.client.post(path,data={'csrf_token':'test-token','modo':'individual'}).status_code,302)
        self.assertEqual(staff.get('/frota').status_code,403)
        self.assertEqual(staff.get('/dashboard').status_code,200)
