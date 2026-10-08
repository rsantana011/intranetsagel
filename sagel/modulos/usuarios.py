"""Rotas e regras da área: usuarios."""

# ============================================================
# USUÁRIOS, PERFIS E APROVAÇÃO DE CADASTROS
# Regras e rotas desta área. Interface: sagel/templates/.
# Banco e segurança compartilhados: sagel/base.py.
# Mapa completo de manutenção: ESTRUTURA-DO-PROJETO.md.
# ============================================================
from ..templates_loader import ler_template
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta
from flask import abort, flash, redirect, request, url_for
from ..base import (
    MODULOS,
    PERFIS,
    agora,
    auditar,
    current_user,
    db,
    emitir_acesso,
    hash_senha,
    permissao,
    render_page,
    texto_form,
)
from ..principal import bp, email_valido, senha_valida, usuario_valido, verificar_duplicado

@bp.route('/admin', methods=['GET', 'POST'])
@permissao('usuarios', 'gerenciar')
def admin():
    conn = db()
    reset_link = None
    if request.method == 'POST':
        try:
            if request.form.get('acao') in ('aprovar_perfil', 'recusar_perfil'):
                if current_user()['perfil'] != 'admin':
                    abort(403)
                conn.execute('BEGIN IMMEDIATE')
                pending = conn.execute("SELECT * FROM usuarios WHERE id=? AND perfil_solicitado IS NOT NULL AND perfil_solicitado!='' AND ativo=0", (request.form.get('id'),)).fetchone()
                if not pending:
                    raise ValueError('Este cadastro já foi analisado ou não está pendente.')
                if request.form['acao'] == 'aprovar_perfil':
                    role = request.form.get('perfil', '')
                    if role not in PERFIS or role == 'admin':
                        raise ValueError('Selecione o perfil que será liberado para o colaborador.')
                    conn.execute('UPDATE usuarios SET perfil=?,perfil_solicitado=NULL,ativo=1,versao_sessao=versao_sessao+1 WHERE id=?', (role,pending['id']))
                    auditar('usuarios','Perfil aprovado',pending['id'],f"Solicitado: {pending['perfil_solicitado']}; liberado: {role}")
                    flash('Cadastro aprovado. O colaborador já pode entrar com os acessos do perfil escolhido.', 'sucesso')
                else:
                    conn.execute('UPDATE usuarios SET perfil_solicitado=NULL,ativo=0,versao_sessao=versao_sessao+1 WHERE id=?', (pending['id'],))
                    auditar('usuarios','Cadastro recusado',pending['id'])
                    flash('Cadastro recusado. A conta continua sem acesso.', 'sucesso')
                conn.commit()
                return redirect('/admin')
            elif request.form.get('acao') == 'recuperar':
                conn.execute('BEGIN IMMEDIATE')
                item = conn.execute("SELECT r.* FROM recuperacoes r JOIN usuarios u ON u.id=r.usuario_id WHERE r.id=? AND r.status IN ('Pendente','Liberado') AND u.ativo=1", (request.form.get('id'),)).fetchone()
                if not item:
                    raise ValueError('Solicitação não encontrada ou conta inativa.')
                token = secrets.token_urlsafe(32)
                conn.execute("UPDATE recuperacoes SET status='Substituído',token_hash=NULL WHERE usuario_id=? AND id!=? AND status='Liberado'", (item['usuario_id'], item['id']))
                conn.execute("UPDATE recuperacoes SET status='Liberado',token_hash=?,expira_em=? WHERE id=?", (hashlib.sha256(token.encode()).hexdigest(), (datetime.fromisoformat(agora()) + timedelta(hours=1)).isoformat(timespec='seconds'), item['id']))
                auditar('usuarios', 'Link de recuperação liberado', item['usuario_id'])
                conn.commit()
                reset_link = url_for('principal.redefinir', token=token, _external=True)
                flash('Link válido por uma hora. Entregue-o somente ao titular após confirmar sua identidade.', 'sucesso')
            else:
                ident = int(request.form.get('id') or 0)
                name = texto_form('nome', 'o nome', 120)
                username = usuario_valido(texto_form('usuario', 'o usuário', 50))
                email = email_valido(texto_form('email', 'o e-mail', 254))
                role = request.form.get('perfil')
                if role not in PERFIS:
                    raise ValueError('Perfil inválido.')
                active = 1 if request.form.get('ativo') == '1' else 0
                title = texto_form('cargo', 'o cargo', 100)
                department = texto_form('setor', 'o setor', 100, False)
                password = request.form.get('senha', '')
                if not ident or password:
                    senha_valida(password)
                conn.execute('BEGIN IMMEDIATE')
                verificar_duplicado(conn, username, email, ident)
                if ident:
                    previous = conn.execute('SELECT * FROM usuarios WHERE id=?', (ident,)).fetchone()
                    if not previous:
                        raise ValueError('Usuário não encontrado.')
                    if previous['perfil_solicitado'] and current_user()['perfil'] != 'admin':
                        abort(403)
                    if previous['perfil'] == 'admin' and previous['ativo'] and (role != 'admin' or not active):
                        if conn.execute("SELECT COUNT(*) FROM usuarios WHERE perfil='admin' AND ativo=1").fetchone()[0] <= 1:
                            raise ValueError('É necessário manter pelo menos um administrador ativo.')
                    conn.execute('UPDATE usuarios SET nome=?,usuario=?,email=?,cargo=?,setor=?,perfil=?,admin=?,ativo=?,versao_sessao=versao_sessao+1 WHERE id=?', (name, username, email, title, department, role, int(role == 'admin'), active, ident))
                    if active:
                        conn.execute('UPDATE usuarios SET perfil_solicitado=NULL WHERE id=?', (ident,))
                    if password:
                        conn.execute('UPDATE usuarios SET senha=? WHERE id=?', (hash_senha(password), ident))
                else:
                    ident = conn.execute('INSERT INTO usuarios(nome,usuario,email,senha,cargo,setor,perfil,admin,ativo) VALUES(?,?,?,?,?,?,?,?,?)', (name, username, email, hash_senha(password), title, department, role, int(role == 'admin'), active)).lastrowid
                auditar('usuarios', 'Cadastro atualizado', ident, f'Perfil: {role}; ativo: {active}')
                conn.commit()
                flash('Usuário salvo. Alterações de perfil encerram as sessões anteriores.', 'sucesso')
                if ident == current_user()['id']:
                    updated = conn.execute('SELECT * FROM usuarios WHERE id=?', (ident,)).fetchone()
                    return emitir_acesso(redirect('/admin'), updated)
                return redirect('/admin')
        except (ValueError, sqlite3.IntegrityError) as error:
            conn.rollback()
            flash(str(error) if isinstance(error, ValueError) else 'Usuário ou e-mail já cadastrado.', 'erro')
    selected = conn.execute('SELECT * FROM usuarios WHERE id=?', (request.args.get('editar'),)).fetchone()
    users = conn.execute('SELECT * FROM usuarios ORDER BY nome').fetchall()
    recoveries = conn.execute("SELECT r.*,u.nome,u.email FROM recuperacoes r JOIN usuarios u ON u.id=r.usuario_id WHERE r.status IN ('Pendente','Liberado') ORDER BY r.id DESC").fetchall()
    pending_users = conn.execute("SELECT id,nome,email,cargo,perfil_solicitado FROM usuarios WHERE ativo=0 AND perfil_solicitado IS NOT NULL AND perfil_solicitado!='' ORDER BY id").fetchall()
    return render_page('Usuários e perfis', ADMIN, selected=selected, users=users, recoveries=recoveries, reset_link=reset_link, pending_users=pending_users)
ADMIN = ler_template('usuarios/admin.html')

@bp.route('/permissoes', methods=['GET', 'POST'])
@permissao('usuarios', 'gerenciar')
def permissoes():
    if current_user()['perfil'] != 'admin':
        abort(403)
    role = request.values.get('perfil', 'colaborador')
    if role not in PERFIS or role == 'admin':
        abort(400)
    conn = db()
    if request.method == 'POST':
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM permissoes WHERE perfil=?', (role,))
        choices = set(request.form.getlist('permissao'))
        choices.add('dashboard:ver')
        for module in MODULOS:
            if module == 'usuarios':
                continue
            actions = {a for a in ('ver', 'gerenciar', 'aprovar') if f'{module}:{a}' in choices}
            if actions:
                actions.add('ver')
            for action in actions:
                conn.execute('INSERT INTO permissoes VALUES(?,?,?)', (role, module, action))
        conn.execute('UPDATE usuarios SET versao_sessao=versao_sessao+1 WHERE perfil=?', (role,))
        auditar('usuarios', 'Permissões alteradas', role)
        conn.commit()
        flash('Permissões atualizadas para o perfil.', 'sucesso')
        return redirect('/permissoes?perfil=' + role)
    selected = {r['modulo'] + ':' + r['acao'] for r in conn.execute('SELECT * FROM permissoes WHERE perfil=?', (role,))}
    return render_page('Permissões por perfil', ler_template('usuarios/permissoes.html'), role=role, selected=selected)
