# SAGEL Intranet

Aplicação web para organizar rotinas administrativas em uma intranet, desenvolvida em **Python, Flask e PostgreSQL**, com interface em HTML, CSS e JavaScript.

Este repositório apresenta o código da aplicação. Bancos operacionais, cadastros, documentos internos, anexos e credenciais não fazem parte da distribuição. A disponibilização do código no GitHub não hospeda a intranet em funcionamento.

## Funcionalidades

- **Acesso e administração:** autenticação, perfis, permissões por módulo, confirmação de cadastro por e-mail e aprovação administrativa.
- **Frota e combustível:** veículos, utilização, manutenção, abastecimento e acompanhamento de consumo.
- **Estoques:** controle de equipamentos de TI, alocação, devolução e movimentação de EPIs.
- **Chamados de TI:** abertura, acompanhamento, comentários e encerramento de solicitações.
- **Compras:** solicitações, fornecedores, cotações, aprovações e acompanhamento de pedidos.
- **Documentação:** arquivos, links, versões e acesso conforme permissões.
- **Organização interna:** colaboradores, avisos e tarefas.
- **Relatórios e auditoria:** exportações em PDF e Excel e registro de ações.

## Tecnologias

| Camada | Tecnologias |
| --- | --- |
| Aplicação | Python, Flask e Jinja |
| Persistência | PostgreSQL com psycopg 3; SQLite para testes e desenvolvimento local |
| Interface | HTML, CSS e JavaScript |
| Autenticação | bcrypt e PyJWT; cookies HttpOnly e proteção CSRF |
| Exportação | ReportLab e openpyxl |
| Configuração | Variáveis de ambiente e python-dotenv |

## Executar em desenvolvimento

Use uma instalação atual de Python compatível com as dependências de `requirements.txt` e um banco separado do ambiente operacional.

```powershell
git clone https://github.com/rsantana011/intranetsagel.git
cd intranetsagel
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edite `.env` com as credenciais do seu banco PostgreSQL de desenvolvimento. O banco e o usuário precisam existir; a aplicação cria suas tabelas ao iniciar. Para uma avaliação local com SQLite, defina `SAGEL_DATABASE_BACKEND=sqlite`.

```powershell
.venv\Scripts\python.exe INTRA.py
```

Acesse `http://127.0.0.1:5000`. O ponto de entrada também aceita conexões pela rede local. O servidor de desenvolvimento Flask não substitui uma implantação com servidor WSGI e HTTPS.

### Configuração

| Variável | Uso |
| --- | --- |
| `SAGEL_DATABASE_BACKEND` | `postgresql` ou `sqlite` |
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB` | Destino do banco |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_SSLMODE` | Autenticação e modo SSL |
| `SAGEL_SECRET_KEY` | Chave da aplicação; se vazia, é gerada e preservada localmente em `instance/secret.key` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | Serviço de e-mail |
| `SMTP_FROM`, `SMTP_SECURITY` | Remetente e segurança do envio (`starttls` ou `ssl`) |

As variáveis SMTP prevalecem sobre os campos correspondentes de `instance/smtp.json`, quando esse arquivo local existe. Preserve a chave existente ao migrar uma instalação.

### Primeiro acesso

Não há conta administrativa nem senha padrão distribuída neste repositório. Uma instalação nova exige provisionar o primeiro administrador de forma controlada antes de aprovar novos cadastros. O cadastro por e-mail cria contas pendentes de aprovação; ele não concede acesso administrativo automaticamente.

## Testes

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests_plataforma -v
```

Os testes usam dados fictícios e bancos SQLite temporários. Os testes da ponte PostgreSQL e da migração são offline; isso não substitui um teste de integração em uma instância PostgreSQL separada.

## Estrutura

```text
INTRA.py             Entrada da aplicação
sagel/               Configuração, acesso a dados e regras de negócio
sagel/modulos/       Rotas organizadas por funcionalidade
sagel/templates/     Páginas e componentes HTML
static/              Estilos, scripts e imagens da interface
tests_plataforma/    Testes automatizados da aplicação atual
migrarpostgres.py    Ferramenta de migração SQLite → PostgreSQL
.env.example         Modelo de configuração sem segredos
```

## Dados e operação

`.env`, ambientes virtuais, caches, configurações da IDE, bancos SQLite, arquivos de instância, anexos e backups são ignorados pelo Git. Arquivos ignorados não são apagados da máquina. Antes de enviar novas alterações, revise os arquivos que serão incluídos.

Faça backup do banco e também dos anexos e da chave da aplicação. A migração exige planejamento e cópias verificadas; consulte `python migrarpostgres.py --help` antes de utilizar a ferramenta. Nenhuma migração é necessária para consultar este repositório.

## Organização para manutenção e experiência de uso

Consulte [o mapa do código](ESTRUTURA-DO-PROJETO.md) para localizar banco de dados, login, frota e todas as outras áreas. `INTRA.py` contém um índice rápido; os módulos e as funções compartilhadas possuem comentários de seção. As rotas continuam separadas dos templates.

A interface inclui navegação de retorno ao início, atalho para pular ao conteúdo, foco visível pelo teclado e respeito à preferência por movimento reduzido. Formulários apresentam indicação de envio, bloqueiam cliques repetidos por até 15 segundos e verificam o limite total de anexos de 16 MB no navegador. As validações do servidor permanecem obrigatórias; o bloqueio no navegador não substitui idempotência no servidor.

Na Frota, a ação Inativar/Reativar tem uma página de confirmação com identificação do veículo, explicação do efeito e aviso de manutenções pendentes. O histórico é preservado, veículos em uso não podem ser inativados e mudanças concorrentes de situação são recusadas.
