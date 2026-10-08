# Onde editar cada parte da intranet SAGEL

Execute **INTRA.py**. Os módulos continuam separados: não execute as abas individualmente.

## Banco de dados

- `sagel/base.py`: variáveis de conexão, abertura/fechamento do banco, tabelas comuns e backups.
- `sagel/database.py`: adaptação das consultas para PostgreSQL, transações e compatibilidade.
- `sagel/frota.py`, `sagel/estoques_ti.py` e `sagel/compras_documentos.py`: tabelas, validadores e consultas compartilhadas de cada grupo.
- `.env`: configuração privada da instalação; não enviar ao GitHub.
- `instance/`: chave, configuração de e-mail, anexos e backups privados.

## Regras e telas por área

| Área | Arquivo de regras e rotas |
| --- | --- |
| LOGIN, SAÍDA, PERFIL E RECUPERAÇÃO DE ACESSO | `sagel/modulos/login.py` |
| PÁGINA INICIAL E INDICADORES | `sagel/modulos/dashboard.py` |
| USUÁRIOS, PERFIS E APROVAÇÃO DE CADASTROS | `sagel/modulos/usuarios.py` |
| FROTA: VEÍCULOS, UTILIZAÇÃO, MANUTENÇÃO E DOCUMENTOS | `sagel/modulos/veiculos.py` |
| COMBUSTÍVEL: ENTRADAS, ABASTECIMENTOS E SALDOS | `sagel/modulos/combustivel.py` |
| COMPRAS: SOLICITAÇÕES, COTAÇÕES, FORNECEDORES E APROVAÇÕES | `sagel/modulos/compras.py` |
| DOCUMENTAÇÃO: ARQUIVOS, VERSÕES E PERMISSÕES | `sagel/modulos/documentacao.py` |
| ESTOQUE E ENTREGA DE EPIs | `sagel/modulos/epi.py` |
| ESTOQUE E ALOCAÇÃO DE EQUIPAMENTOS DE TI | `sagel/modulos/estoque_ti.py` |
| CHAMADOS E ATENDIMENTO DE TI | `sagel/modulos/chamados.py` |
| CADASTRO DE COLABORADORES | `sagel/modulos/colaboradores.py` |
| AVISOS INTERNOS | `sagel/modulos/avisos.py` |
| TAREFAS E RESPONSÁVEIS | `sagel/modulos/tarefas.py` |
| RELATÓRIOS E EXPORTAÇÕES | `sagel/modulos/relatorios.py` |
| CONFIGURAÇÕES E BACKUPS | `sagel/modulos/configuracoes.py` |
| AUDITORIA E HISTÓRICO DE AÇÕES | `sagel/modulos/auditoria.py` |

## Interface e funções comuns

- `sagel/templates/`: HTML separado por área; o nome do arquivo carregado aparece no Python do módulo.
- `sagel/templates/base.html`: estrutura de todas as páginas, menu, mensagens e navegação.
- `static/sagel.css`: cores, layout, foco de teclado e acessibilidade.
- `static/sagel.js`: interações, validação de anexos e proteção contra clique repetido.
- `sagel/cadastro_email.py`: confirmação de cadastro por e-mail.
- `sagel/base.py`: senhas, identificação do usuário, permissões, CSRF e auditoria, identificados por comentários de seção.
- `sagel/__init__.py`: fábrica da aplicação e registro dos módulos.
- `sagel/modulos/__init__.py`: lista central das áreas.

## Manutenção

1. Localize a área na tabela e altere apenas os arquivos necessários.
2. Preserve a verificação de permissões, CSRF, transações e auditoria.
3. Execute `.venv\Scripts\python.exe -m unittest discover -s tests_plataforma -v` em uma cópia isolada.
4. Reinicie a aplicação para carregar mudanças em Python e templates.

Não apague bancos ou arquivos de `instance/`. As mudanças de interface não alteram o fluxo de aprovação. A inativação de veículos preserva seus históricos e permite reativação; um veículo em uso precisa retornar antes de ser inativado.
