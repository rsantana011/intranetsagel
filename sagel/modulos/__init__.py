"""Registro central das rotas; cada área permanece em seu arquivo."""

def registrar_rotas():
    from . import (
        login,
        dashboard,
        colaboradores,
        avisos,
        tarefas,
        usuarios,
        configuracoes,
        auditoria,
        relatorios,
        compras,
        documentacao,
        epi,
        estoque_ti,
        chamados,
        veiculos,
        combustivel,
    )
