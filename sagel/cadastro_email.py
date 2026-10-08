from .templates_loader import ler_template
'Cadastro só é efetivado após código de uso único enviado por e-mail.'
import hashlib
import hmac
import secrets
import smtplib
import sqlite3
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from flask import Blueprint, current_app, flash, redirect, request, session
from .base import PERFIS, agora, auditar, current_user, db, hash_senha, render_page, texto_form
bp = Blueprint('cadastro_email', __name__)
CARGOS_CADASTRO = {key: label for key, label in PERFIS.items() if key != 'admin'}

def init_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS cadastros_pendentes(
      id TEXT PRIMARY KEY,email TEXT NOT NULL,usuario TEXT NOT NULL,senha_hash TEXT NOT NULL,
      codigo_hash TEXT NOT NULL,expira_em TEXT NOT NULL,enviado_em TEXT NOT NULL,
      tentativas INTEGER NOT NULL DEFAULT 0,pronto INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS cadastro_envios(
      id INTEGER PRIMARY KEY AUTOINCREMENT,email_chave TEXT NOT NULL,ip_chave TEXT NOT NULL,data TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS cadastro_envios_data ON cadastro_envios(data);
    """)
    existing = {row['name'] for row in conn.execute('PRAGMA table_info(cadastros_pendentes)')}
    if 'perfil_solicitado' not in existing:
        conn.execute("ALTER TABLE cadastros_pendentes ADD COLUMN perfil_solicitado TEXT NOT NULL DEFAULT 'colaborador'")
    existing = {row['name'] for row in conn.execute('PRAGMA table_info(usuarios)')}
    if 'perfil_solicitado' not in existing:
        conn.execute('ALTER TABLE usuarios ADD COLUMN perfil_solicitado TEXT')

def _digest(value):
    return hmac.new(current_app.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()

def _aberto():
    return db().execute("SELECT valor FROM configuracoes WHERE chave='cadastro_aberto'").fetchone()['valor'] == '1'

def enviar_codigo(email, codigo):
    smtp = current_app.config.get('SAGEL_SMTP', {})
    if not smtp.get('host') or not smtp.get('remetente'):
        raise RuntimeError('Envio de e-mail não configurado')
    mode = smtp.get('seguranca', 'starttls')
    if mode not in ('starttls', 'ssl'):
        raise RuntimeError('É necessário usar TLS no envio de e-mail')
    message = EmailMessage()
    message['Subject'] = 'SAGEL — confirme seu cadastro'
    message['From'] = smtp['remetente']
    message['To'] = email
    message.set_content(f'Seu código de confirmação da intranet SAGEL é: {codigo}\n\nDigite esse código na tela de cadastro para criar sua conta.\nO código expira em 10 minutos e pode ser usado uma única vez.\n\nSe você não solicitou este cadastro, ignore esta mensagem.\nNão compartilhe seu código.\n\nSAGEL — Portal corporativo')
    context = ssl.create_default_context()
    if mode == 'ssl':
        client = smtplib.SMTP_SSL(smtp['host'], int(smtp.get('porta', 465)), timeout=15, context=context)
    else:
        client = smtplib.SMTP(smtp['host'], int(smtp.get('porta', 587)), timeout=15)
    with client:
        if mode == 'starttls':
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        if smtp.get('usuario'):
            client.login(smtp['usuario'], smtp.get('senha', ''))
        refused = client.send_message(message)
        if refused:
            raise RuntimeError('Destinatário recusado')

def _preparar_envio(email, usuario, senha_hash, substituir=None, perfil_solicitado='colaborador'):
    conn = db()
    now = agora()
    cutoff = (datetime.fromisoformat(now) - timedelta(hours=1)).isoformat(timespec='seconds')
    minute = (datetime.fromisoformat(now) - timedelta(seconds=60)).isoformat(timespec='seconds')
    email_key = _digest('email:' + email)
    ip_key = _digest('ip:' + (request.remote_addr or 'local'))
    conn.execute('BEGIN IMMEDIATE')
    conn.execute('DELETE FROM cadastro_envios WHERE data<?', (cutoff,))
    conn.execute('DELETE FROM cadastros_pendentes WHERE expira_em<?', (now,))
    attempts = conn.execute('SELECT COUNT(*) FROM cadastro_envios WHERE email_chave=?', (email_key,)).fetchone()[0]
    by_ip = conn.execute('SELECT COUNT(*) FROM cadastro_envios WHERE ip_chave=?', (ip_key,)).fetchone()[0]
    recent = conn.execute('SELECT 1 FROM cadastro_envios WHERE email_chave=? AND data>?', (email_key, minute)).fetchone()
    if attempts >= 5 or by_ip >= 20 or recent:
        conn.rollback()
        raise ValueError('Aguarde um minuto antes de solicitar outro código. O limite é de cinco envios por e-mail a cada hora.')
    if substituir:
        conn.execute('DELETE FROM cadastros_pendentes WHERE id=?', (substituir,))
    ident = secrets.token_urlsafe(32)
    code = f'{secrets.randbelow(1000000):06d}'
    expiry = (datetime.fromisoformat(now) + timedelta(minutes=10)).isoformat(timespec='seconds')
    conn.execute('INSERT INTO cadastros_pendentes(id,email,usuario,senha_hash,codigo_hash,expira_em,enviado_em,perfil_solicitado) VALUES(?,?,?,?,?,?,?,?)', (ident, email, usuario, senha_hash, _digest(ident + ':' + code), expiry, now, perfil_solicitado))
    conn.execute('INSERT INTO cadastro_envios(email_chave,ip_chave,data) VALUES(?,?,?)', (email_key, ip_key, now))
    conn.commit()
    try:
        enviar_codigo(email, code)
    except Exception as error:
        current_app.logger.warning('Não foi possível enviar confirmação: %s', type(error).__name__)
        conn.execute('DELETE FROM cadastros_pendentes WHERE id=?', (ident,))
        conn.commit()
        if session.get('cadastro_pendente') == substituir:
            session.pop('cadastro_pendente', None)
        raise ValueError('Não foi possível enviar o código. O administrador precisa conferir o serviço de e-mail. Sua conta ainda não foi criada.') from None
    conn.execute('UPDATE cadastros_pendentes SET pronto=1 WHERE id=?', (ident,))
    auditar('acesso', 'Código de cadastro enviado')
    conn.commit()
    session['cadastro_pendente'] = ident

def iniciar():
    from .principal import email_valido, usuario_valido, senha_valida, verificar_duplicado
    if current_user():
        return redirect('/dashboard')
    if not _aberto():
        return render_page('Cadastro', ler_template('cadastro/iniciar.html'), acesso=True)
    if request.method == 'POST':
        try:
            email = email_valido(texto_form('email', 'o e-mail', 254))
            usuario = usuario_valido(texto_form('usuario', 'o nome de usuário', 50))
            senha = senha_valida(request.form.get('senha', ''))
            cargo = request.form.get('cargo', '')
            if cargo not in CARGOS_CADASTRO:
                raise ValueError('Selecione seu cargo ou área na empresa.')
            verificar_duplicado(db(), usuario, email)
            _preparar_envio(email, usuario, hash_senha(senha), session.get('cadastro_pendente'), cargo)
            return redirect('/cadastro/confirmar')
        except (ValueError, sqlite3.IntegrityError) as error:
            db().rollback()
            flash(str(error) if isinstance(error, ValueError) else 'Não foi possível iniciar o cadastro.', 'erro')
    smtp = current_app.config.get('SAGEL_SMTP', {})
    return render_page('Criar conta', FORMULARIO, acesso=True, envio_configurado=bool(smtp.get('host') and smtp.get('remetente')), cargos=CARGOS_CADASTRO)

@bp.route('/cadastro/confirmar', methods=['GET', 'POST'])
def confirmar():
    from .principal import verificar_duplicado
    if current_user():
        return redirect('/dashboard')
    if not _aberto():
        return redirect('/cadastro')
    conn = db()
    ident = session.get('cadastro_pendente', '')
    pending = conn.execute('SELECT * FROM cadastros_pendentes WHERE id=? AND pronto=1', (ident,)).fetchone()
    if not pending:
        flash('Preencha o cadastro para receber um código de confirmação.', 'erro')
        return redirect('/cadastro')
    if request.method == 'POST':
        if request.form.get('acao') == 'reenviar':
            try:
                verificar_duplicado(conn, pending['usuario'], pending['email'])
                _preparar_envio(pending['email'], pending['usuario'], pending['senha_hash'], ident, pending['perfil_solicitado'])
                flash('Um novo código foi enviado. O código anterior não é mais válido.', 'sucesso')
            except ValueError as error:
                conn.rollback()
                flash(str(error), 'erro')
            return redirect('/cadastro/confirmar' if session.get('cadastro_pendente') else '/cadastro')
        conn.execute('BEGIN IMMEDIATE')
        pending = conn.execute('SELECT * FROM cadastros_pendentes WHERE id=? AND pronto=1', (ident,)).fetchone()
        if not pending:
            conn.rollback()
            flash('Este código já foi utilizado. Faça login ou inicie um novo cadastro.', 'erro')
            return redirect('/')
        code = request.form.get('codigo', '').strip()
        if pending['expira_em'] <= agora():
            conn.rollback()
            flash('O código expirou. Solicite um novo código.', 'erro')
        elif pending['tentativas'] >= 5:
            conn.rollback()
            flash('Limite de tentativas atingido. Solicite um novo código.', 'erro')
        elif len(code) != 6 or not code.isascii() or (not code.isdigit()) or (not hmac.compare_digest(pending['codigo_hash'], _digest(ident + ':' + code))):
            conn.execute('UPDATE cadastros_pendentes SET tentativas=tentativas+1 WHERE id=?', (ident,))
            conn.commit()
            flash('Código incorreto. Confira o e-mail recebido.', 'erro')
        else:
            try:
                verificar_duplicado(conn, pending['usuario'], pending['email'])
                requested = pending['perfil_solicitado']
                if requested not in CARGOS_CADASTRO:
                    raise ValueError('Cargo inválido.')
                result = conn.execute("INSERT INTO usuarios(nome,usuario,email,senha,cargo,admin,perfil,ativo,perfil_solicitado) VALUES(?,?,?,?,?,0,'colaborador',0,?)", (pending['usuario'], pending['usuario'], pending['email'], pending['senha_hash'], CARGOS_CADASTRO[requested], requested))
                conn.execute('DELETE FROM cadastros_pendentes WHERE email=?', (pending['email'],))
                auditar('usuarios', 'Cadastro com e-mail confirmado', result.lastrowid)
                conn.commit()
                session.pop('cadastro_pendente', None)
                flash('E-mail confirmado. Seu cadastro aguarda aprovação do administrador para liberar o acesso.', 'sucesso')
                return redirect('/')
            except (ValueError, sqlite3.IntegrityError):
                conn.rollback()
                flash('O usuário ou e-mail já foi cadastrado. Faça login ou corrija seus dados.', 'erro')
                return redirect('/cadastro')
    return render_page('Confirme seu e-mail', CONFIRMACAO, acesso=True, email=pending['email'])
FORMULARIO = ler_template('cadastro/formulario.html')
CONFIRMACAO = ler_template('cadastro/confirmacao.html')
