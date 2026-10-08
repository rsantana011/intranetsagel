"""Operações reais em banco isolado: estoque, patrimônio e chamados."""
import io
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
import unittest
from uuid import uuid4

from sagel import create_app
from sagel.base import hash_senha


class EstoquesTITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.senha = hash_senha('teste123')

    def setUp(self):
        self.pasta = Path(__file__).resolve().parents[1] / ('test-ti-'+uuid4().hex)
        self.pasta.mkdir()
        self.banco = self.pasta / 'teste.db'
        self.app = create_app({'TESTING':True,'DATABASE':str(self.banco),'SECRET_KEY':'teste',
                               'STORAGE_DIR':str(self.pasta/'anexos'),'BACKUP_DIR':str(self.pasta/'backups')})
        self.client = self.app.test_client()
        with self.conn() as conn:
            for ident, nome, perfil, ativo in [(1,'Administrador','admin',1),(2,'Pessoa A','colaborador',1),
                    (3,'Pessoa B','colaborador',1),(4,'Técnico','ti',1),(5,'Inativo','colaborador',0)]:
                conn.execute('''INSERT INTO usuarios (id,nome,usuario,email,senha,cargo,admin,perfil,setor,ativo)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    (ident,nome,'user'+str(ident),f'user{ident}@example.test',self.senha,'Colaborador',int(perfil=='admin'),perfil,'TI',ativo))
        self.login(1)

    def tearDown(self):
        shutil.rmtree(self.pasta)

    @contextmanager
    def conn(self):
        conn = sqlite3.connect(str(self.banco))
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def login(self, ident):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['usuario_id'] = ident
            sess['csrf_token'] = 'token-teste'

    def post(self, url, **data):
        return self.client.post(url, data={'csrf_token':'token-teste',**data}, follow_redirects=False)

    def item(self):
        resposta = self.post('/epi', nome='Luva teste', ca='123', tamanho='M', lote='L1',
                             unidade='par', estoque_minimo='2', validade='2099-01-01')
        self.assertEqual(resposta.status_code,302)
        with self.conn() as conn:
            return conn.execute('SELECT id FROM epi_itens').fetchone()[0]

    def equipamento(self):
        resposta = self.post('/ti/estoque', patrimonio='TI-001', nome='Notebook teste',
                             categoria='Notebook', serie='SERIE1',status='Disponível',observacao='Teste')
        self.assertEqual(resposta.status_code,302)
        with self.conn() as conn:
            return conn.execute('SELECT id FROM ti_equipamentos').fetchone()[0]

    def ticket(self, usuario=2, anexo=False):
        self.login(usuario)
        dados = {'titulo':'Falha específica pessoa A','descricao':'Sistema não abre.',
                 'categoria':'Sistemas','prioridade':'Alta'}
        if anexo:
            dados['anexo'] = (io.BytesIO(b'Evidencia textual.'),'evidencia.txt')
        resposta = self.post('/chamados', **dados)
        self.assertEqual(resposta.status_code,302)
        with self.conn() as conn:
            return conn.execute('SELECT MAX(id) FROM ti_chamados').fetchone()[0]

    def test_epi_saldo_atomico_historico_e_colaborador(self):
        ident = self.item()
        self.post(f'/epi/{ident}', acao='movimentar',tipo='Entrada',quantidade='5',observacao='Recebimento')
        self.post(f'/epi/{ident}', acao='movimentar',tipo='Saída',quantidade='3',colaborador_id='2',observacao='Entrega')
        self.post(f'/epi/{ident}', acao='movimentar',tipo='Saída',quantidade='3',colaborador_id='3',observacao='Sem saldo')
        self.post(f'/epi/{ident}', acao='movimentar',tipo='Saída',quantidade='1',colaborador_id='99999',observacao='ID inválido')
        self.post(f'/epi/{ident}', acao='movimentar',tipo='Saída',quantidade='1',colaborador_id='5',observacao='Usuário inativo')
        self.post(f'/epi/{ident}', acao='movimentar',tipo='Entrada',quantidade='-1',observacao='Inválida')
        with self.conn() as conn:
            self.assertEqual(conn.execute('SELECT saldo FROM epi_itens WHERE id=?',(ident,)).fetchone()[0],2)
            movimentos = conn.execute('SELECT * FROM epi_movimentos ORDER BY id').fetchall()
            self.assertEqual(len(movimentos),2)
            self.assertEqual(movimentos[1]['colaborador_id'],2)
        self.assertEqual(self.client.get(f'/epi/{ident}').status_code,200)

    def test_epi_vencido_e_sem_gerencia(self):
        ident = self.item()
        self.post(f'/epi/{ident}', acao='movimentar',tipo='Entrada',quantidade='2',observacao='Recebimento')
        with self.conn() as conn:
            conn.execute('UPDATE epi_itens SET validade=? WHERE id=?',('2020-01-01',ident))
        self.post(f'/epi/{ident}', acao='movimentar',tipo='Saída',quantidade='1',colaborador_id='2',observacao='Vencido')
        self.login(2)
        resposta = self.post(f'/epi/{ident}', acao='movimentar',tipo='Entrada',quantidade='5',observacao='Acesso negado')
        self.assertEqual(resposta.status_code,403)
        with self.conn() as conn:
            self.assertEqual(conn.execute('SELECT saldo FROM epi_itens').fetchone()[0],2)

    def test_duas_entregas_concorrentes_nao_consumem_o_mesmo_saldo(self):
        ident = self.item()
        self.post(f'/epi/{ident}',acao='movimentar',tipo='Entrada',quantidade='5',observacao='Entrada')
        def entregar(colaborador):
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess['usuario_id'] = 1
                sess['csrf_token'] = 'token-teste'
            return client.post(f'/epi/{ident}',data={'csrf_token':'token-teste','acao':'movimentar',
                'tipo':'Saída','quantidade':'4','colaborador_id':str(colaborador),'observacao':'Entrega simultânea'}).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(executor.map(entregar,[2,3])),[302,302])
        with self.conn() as conn:
            self.assertEqual(conn.execute('SELECT saldo FROM epi_itens').fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT count(*) FROM epi_movimentos WHERE tipo='Saída'").fetchone()[0],1)

    def test_permissao_somente_leitura_bloqueia_post_e_preserva_dados(self):
        ident = self.equipamento()
        with self.conn() as conn:
            conn.execute("INSERT OR IGNORE INTO permissoes VALUES ('colaborador','ti_estoque','ver')")
        self.login(2)
        self.assertEqual(self.client.get(f'/ti/estoque/{ident}').status_code,200)
        self.assertEqual(self.post(f'/ti/estoque/{ident}',acao='alocar',usuario_id='2',observacao='Tentativa').status_code,403)
        with self.conn() as conn:
            self.assertEqual(conn.execute('SELECT status FROM ti_equipamentos').fetchone()[0],'Disponível')

    def test_alocacao_exclusiva_devolucao_e_usuario_valido(self):
        ident = self.equipamento()
        self.post(f'/ti/estoque/{ident}',acao='alocar',usuario_id='99999',observacao='Inválido')
        with self.conn() as conn:
            self.assertEqual(conn.execute('SELECT status FROM ti_equipamentos').fetchone()[0],'Disponível')
        self.post(f'/ti/estoque/{ident}',acao='alocar',usuario_id='2',observacao='Entrega A')
        self.post(f'/ti/estoque/{ident}',acao='alocar',usuario_id='3',observacao='Entrega B')
        self.post(f'/ti/estoque/{ident}',acao='situacao',status='Inativo',observacao='Alteração inválida')
        with self.conn() as conn:
            item = conn.execute('SELECT * FROM ti_equipamentos').fetchone()
            self.assertEqual(item['status'],'Alocado')
            self.assertEqual(item['usuario_id'],2)
        self.post(f'/ti/estoque/{ident}',acao='devolver',status='Manutenção',observacao='Tela quebrada')
        self.post(f'/ti/estoque/{ident}',acao='alocar',usuario_id='3',observacao='Indisponível')
        with self.conn() as conn:
            item = conn.execute('SELECT * FROM ti_equipamentos').fetchone()
            self.assertEqual(item['status'],'Manutenção')
            self.assertIsNone(item['usuario_id'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ti_equipamento_historico').fetchone()[0],3)
        self.assertEqual(self.client.get(f'/ti/estoque/{ident}').status_code,200)

    def test_chamados_privados_e_anexos_protegidos(self):
        ident = self.ticket(anexo=True)
        with self.conn() as conn:
            anexo = conn.execute('SELECT id FROM ti_chamado_anexos').fetchone()[0]
        with self.client.get(f'/chamados/anexos/{anexo}') as resposta:
            self.assertEqual(resposta.status_code,200)
            self.assertEqual(resposta.data,b'Evidencia textual.')
        self.login(3)
        self.assertNotIn(b'Falha espec',self.client.get('/chamados').data)
        self.assertEqual(self.client.get(f'/chamados/{ident}').status_code,403)
        self.assertEqual(self.client.get(f'/chamados/anexos/{anexo}').status_code,403)
        self.assertEqual(self.post(f'/chamados/{ident}',acao='comentar',mensagem='Tentativa').status_code,403)
        self.login(2)
        self.assertEqual(self.post(f'/chamados/{ident}',acao='atender',responsavel_id='4',status='Resolvido',mensagem='Forjado').status_code,403)
        with self.conn() as conn:
            self.assertEqual(conn.execute('SELECT status FROM ti_chamados').fetchone()[0],'Aberto')

    def test_fluxo_chamado_sla_reabertura_encerramento_notificacao(self):
        ident = self.ticket()
        self.login(4)
        self.assertEqual(self.client.get(f'/chamados/{ident}').status_code,200)
        # Não se pode encerrar diretamente um chamado sem resolução.
        self.post(f'/chamados/{ident}',acao='atender',responsavel_id='4',status='Encerrado',prioridade='Alta',mensagem='Inválido')
        self.post(f'/chamados/{ident}',acao='atender',responsavel_id='9999',status='Resolvido',prioridade='Alta',mensagem='Inválido')
        with self.conn() as conn:
            self.assertEqual(conn.execute('SELECT status FROM ti_chamados').fetchone()[0],'Aberto')
        self.post(f'/chamados/{ident}',acao='atender',responsavel_id='4',status='Resolvido',prioridade='Urgente',mensagem='Correção aplicada')
        self.login(2)
        self.assertIn(b'resolvido',self.client.get('/chamados').data)
        self.post(f'/chamados/{ident}',acao='reabrir',mensagem='O problema voltou')
        with self.conn() as conn:
            ticket = conn.execute('SELECT * FROM ti_chamados').fetchone()
            self.assertEqual(ticket['status'],'Em atendimento')
            self.assertIsNone(ticket['resolvido_em'])
            self.assertEqual((datetime.fromisoformat(ticket['prazo_sla'])-datetime.fromisoformat(ticket['criado_em'])).total_seconds(),14400)
        self.login(4)
        self.post(f'/chamados/{ident}',acao='atender',responsavel_id='4',status='Resolvido',prioridade='Urgente',mensagem='Correção definitiva')
        self.login(2)
        self.post(f'/chamados/{ident}',acao='confirmar',mensagem='Resolvido, obrigado')
        self.post(f'/chamados/{ident}',acao='comentar',mensagem='Bloqueado após encerramento')
        with self.conn() as conn:
            ticket = conn.execute('SELECT * FROM ti_chamados').fetchone()
            self.assertEqual(ticket['status'],'Encerrado')
            self.assertIsNotNone(ticket['encerrado_em'])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ti_chamado_historico WHERE mensagem='Bloqueado após encerramento'").fetchone()[0],0)
            self.assertGreaterEqual(conn.execute('SELECT COUNT(*) FROM ti_notificacoes WHERE usuario_id=2').fetchone()[0],3)

    def test_validacao_ids_csrf_arquivos_e_xss(self):
        self.assertEqual(self.client.get('/epi/99999').status_code,404)
        self.assertEqual(self.client.get('/ti/estoque/99999').status_code,404)
        self.assertEqual(self.client.get('/chamados/99999').status_code,404)
        self.assertIn(self.client.post('/chamados',data={'titulo':'Sem CSRF'}).status_code,(400,403))
        self.login(2)
        self.post('/chamados',titulo='Arquivo inválido',descricao='Teste',categoria='Teste',prioridade='Normal',
                  anexo=(io.BytesIO(b'<html>script</html>'),'evidencia.html'))
        with self.conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ti_chamados').fetchone()[0],0)
        criado = self.post('/chamados',titulo='<script>alert(1)</script>',descricao='<img src=x onerror=alert(1)>',categoria='Teste',prioridade='Normal')
        self.assertEqual(criado.status_code, 302)
        resposta = self.client.get(criado.headers['Location'])
        self.assertNotIn(b'<script>alert(1)</script>',resposta.data)
        self.assertIn(b'&lt;script&gt;',resposta.data)


if __name__ == '__main__':
    unittest.main()
