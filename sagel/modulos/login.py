"""Rotas e regras da área: login."""

# ============================================================
# LOGIN, SAÍDA, PERFIL E RECUPERAÇÃO DE ACESSO
# Regras e rotas desta área. Interface: sagel/templates/.
# Banco e segurança compartilhados: sagel/base.py.
# Mapa completo de manutenção: ESTRUTURA-DO-PROJETO.md.
# ============================================================
from ..templates_loader import ler_template
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta
from flask import flash, redirect, request, session
from ..base import (
    agora,
    auditar,
    autenticado,
    conferir_senha,
    current_user,
    db,
    emitir_acesso,
    hash_senha,
    render_page,
    texto_form,
)
from ..principal import bp, email_valido, senha_valida, verificar_duplicado

@bp.route('/', methods=['GET', 'POST'])
def login():
    if current_user():
        return redirect('/dashboard')
    if request.method == 'POST':
        name = request.form.get('usuario', '').strip()[:254]
        password = request.form.get('senha', '')
        conn = db()
        key = hashlib.sha256(((request.remote_addr or '') + '|' + name.lower()).encode()).hexdigest()
        attempt = conn.execute('SELECT * FROM tentativas_login WHERE chave=?', (key,)).fetchone()
        cutoff = (datetime.fromisoformat(agora()) - timedelta(minutes=15)).isoformat(timespec='seconds')
        if attempt and attempt['inicio'] > cutoff and (attempt['quantidade'] >= 10):
            flash('Muitas tentativas. Aguarde 15 minutos antes de tentar novamente.', 'erro')
            return (render_page('Acesso', LOGIN, acesso=True), 429)
        user = conn.execute('SELECT * FROM usuarios WHERE lower(usuario)=lower(?) OR lower(email)=lower(?)', (name, name)).fetchone()
        password_correct = user and conferir_senha(user['senha'], password)
        if password_correct and not user['ativo']:
            flash('Sua senha está correta. Seu cadastro aguarda aprovação do administrador.' if user['perfil_solicitado'] else 'Sua conta está inativa. Procure o administrador.', 'erro')
            return render_page('Acesso', LOGIN, acesso=True)
        valid = password_correct and user['ativo']
        if valid:
            if not user['senha'].startswith(('$2b$', '$2a$', '$2y$')) and len(password.encode()) <= 72:
                conn.execute('UPDATE usuarios SET senha=? WHERE id=?', (hash_senha(password), user['id']))
            conn.execute('DELETE FROM tentativas_login WHERE chave=?', (key,))
            conn.execute('INSERT INTO auditoria(usuario_id,modulo,acao,data) VALUES(?,?,?,?)', (user['id'], 'acesso', 'Login', agora()))
            conn.commit()
            session.clear()
            session['csrf_token'] = secrets.token_urlsafe(32)
            return emitir_acesso(redirect('/dashboard'), user)
        if not attempt or attempt['inicio'] <= cutoff:
            conn.execute('INSERT INTO tentativas_login(chave,quantidade,inicio) VALUES(?,?,?) ON CONFLICT(chave) DO UPDATE SET quantidade=excluded.quantidade,inicio=excluded.inicio', (key, 1, agora()))
        else:
            conn.execute('UPDATE tentativas_login SET quantidade=quantidade+1 WHERE chave=?', (key,))
        auditar('acesso', 'Tentativa de login inválida')
        conn.commit()
        flash('Usuário ou senha incorretos.', 'erro')
    return render_page('Acesso', LOGIN, acesso=True)
LOGIN = ler_template('login/login.html')

@bp.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    from ..cadastro_email import iniciar
    return iniciar()

@bp.route('/logout', methods=['POST'])
@autenticado
def logout():
    user = current_user()
    auditar('acesso', 'Logout')
    db().execute('UPDATE usuarios SET versao_sessao=versao_sessao+1 WHERE id=?', (user['id'],))
    db().commit()
    session.clear()
    response = redirect('/')
    response.delete_cookie('sagel_acesso')
    return response

@bp.route('/recuperar-senha', methods=['GET', 'POST'])
def recuperar():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()[:254]
        user = db().execute('SELECT id FROM usuarios WHERE lower(email)=lower(?) AND ativo=1', (email,)).fetchone()
        if user and (not db().execute("SELECT 1 FROM recuperacoes WHERE usuario_id=? AND status='Pendente'", (user['id'],)).fetchone()):
            db().execute('INSERT INTO recuperacoes(usuario_id,criado_em) VALUES(?,?)', (user['id'], agora()))
            auditar('acesso', 'Solicitação de recuperação')
            db().commit()
        flash('Se houver uma conta com esse e-mail, a solicitação ficará disponível para o administrador. Procure o TI para receber o link de recuperação.', 'sucesso')
        return redirect('/recuperar-senha')
    return render_page('Recuperar acesso', ler_template('login/recuperar.html'), acesso=True)

@bp.route('/redefinir/<token>', methods=['GET', 'POST'])
def redefinir(token):
    hashed = hashlib.sha256(token.encode()).hexdigest()
    conn = db()
    item = conn.execute("SELECT r.* FROM recuperacoes r JOIN usuarios u ON u.id=r.usuario_id WHERE token_hash=? AND r.status='Liberado' AND expira_em>? AND u.ativo=1", (hashed, agora())).fetchone()
    if not item:
        return (render_page('Recuperar acesso', ler_template('login/redefinir.html'), acesso=True), 400)
    if request.method == 'POST':
        try:
            password = senha_valida(request.form.get('senha', ''))
            conn.execute('BEGIN IMMEDIATE')
            changed = conn.execute("UPDATE recuperacoes SET status='Utilizado',token_hash=NULL WHERE id=? AND status='Liberado' AND expira_em>?", (item['id'], agora())).rowcount
            if not changed:
                raise ValueError('Este link expirou ou já foi utilizado.')
            conn.execute('UPDATE usuarios SET senha=?,versao_sessao=versao_sessao+1 WHERE id=?', (hash_senha(password), item['usuario_id']))
            auditar('acesso', 'Senha redefinida', item['usuario_id'])
            conn.commit()
            flash('Senha atualizada. Faça login novamente.', 'sucesso')
            return redirect('/')
        except ValueError as error:
            conn.rollback()
            flash(str(error), 'erro')
    return render_page('Nova senha', ler_template('login/redefinir_2.html'), acesso=True)

@bp.route('/perfil', methods=['GET', 'POST'])
@autenticado
def perfil():
    user = current_user()
    if request.method == 'POST':
        conn = db()
        try:
            name = texto_form('nome', 'o nome', 120)
            email = email_valido(texto_form('email', 'o e-mail', 254))
            if not conferir_senha(user['senha'], request.form.get('senha_atual', '')):
                raise ValueError('A senha atual está incorreta.')
            password = request.form.get('nova_senha', '')
            if password:
                senha_valida(password)
            conn.execute('BEGIN IMMEDIATE')
            verificar_duplicado(conn, user['usuario'], email, user['id'])
            conn.execute('UPDATE usuarios SET nome=?,email=? WHERE id=?', (name, email, user['id']))
            if password:
                conn.execute('UPDATE usuarios SET senha=?,versao_sessao=versao_sessao+1 WHERE id=?', (hash_senha(password), user['id']))
            auditar('usuarios', 'Perfil atualizado', user['id'])
            conn.commit()
            flash('Perfil atualizado.', 'sucesso')
            updated = conn.execute('SELECT * FROM usuarios WHERE id=?', (user['id'],)).fetchone()
            return emitir_acesso(redirect('/perfil'), updated)
        except (ValueError, sqlite3.IntegrityError) as error:
            conn.rollback()
            flash(str(error) if isinstance(error, ValueError) else 'E-mail já cadastrado.', 'erro')
    return render_page('Meu perfil', ler_template('login/perfil.html'), user=user)
