from .templates_loader import ler_template
import hashlib
import io
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from xml.sax.saxutils import escape as xml_escape
from flask import Blueprint, abort, current_app, flash, redirect, request, send_file, session, url_for
from .base import (
    MODULOS,
    PERFIS,
    agora,
    auditar,
    autenticado,
    backup_banco,
    conferir_senha,
    current_user,
    db,
    emitir_acesso,
    hash_senha,
    hoje,
    permissao,
    render_page,
    tem_permissao,
    texto_form,
)
bp = Blueprint('principal', __name__)

def email_valido(value):
    if len(value) > 254 or not re.fullmatch('[^\\s@]+@[^\\s@]+\\.[^\\s@]+', value):
        raise ValueError('Informe um e-mail válido.')
    return value.lower()

def usuario_valido(value):
    if not re.fullmatch('[A-Za-z0-9_.-]{1,50}', value):
        raise ValueError('O usuário deve ter até 50 letras, números, pontos, traços ou sublinhados.')
    return value

def senha_valida(value):
    if len(value) != 8 or not re.search('[0-9]', value) or not any(not c.isalnum() and not c.isspace() for c in value):
        raise ValueError('A senha deve ter exatamente 8 caracteres, pelo menos um número e um caractere especial. Letras maiúsculas não são obrigatórias.')
    return value

def verificar_duplicado(conn, usuario, email, ident=0):
    if conn.execute("SELECT 1 FROM usuarios WHERE id!=? AND (lower(usuario)=lower(?) OR (lower(email)=lower(?) AND email!=''))", (ident, usuario, email)).fetchone():
        raise ValueError('O nome de usuário ou e-mail já está cadastrado.')
REPORTS = {'tarefas': {'titulo': 'Tarefas por responsável', 'modulo': 'tarefas', 'data_coluna': 'Prazo', 'sql': 'SELECT titulo AS "Tarefa",responsavel AS "Responsável",status AS "Status",prazo AS "Prazo" FROM tarefas'}}
