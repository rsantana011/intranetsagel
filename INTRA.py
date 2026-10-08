"""Ponto de entrada da intranet SAGEL. Execute este arquivo."""

# MAPA DO CÓDIGO — cada área continua separada em seu próprio arquivo.
# BANCO DE DADOS: sagel/base.py (conexão) e sagel/database.py (PostgreSQL).
# LOGIN: sagel/modulos/login.py; confirmação de e-mail: sagel/cadastro_email.py.
# FROTA: sagel/modulos/veiculos.py; combustível: sagel/modulos/combustivel.py.
# COMPRAS: sagel/modulos/compras.py; documentos: sagel/modulos/documentacao.py.
# ESTOQUES: sagel/modulos/epi.py e sagel/modulos/estoque_ti.py.
# CHAMADOS: sagel/modulos/chamados.py.
# USUÁRIOS E PERMISSÕES: sagel/modulos/usuarios.py e sagel/base.py.
# OUTRAS ÁREAS: sagel/modulos/; registro central: sagel/modulos/__init__.py.
# TELAS: sagel/templates/; aparência/interações: static/sagel.css e sagel.js.
# GUIA COMPLETO: ESTRUTURA-DO-PROJETO.md.
from sagel import create_app

app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
