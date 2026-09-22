# Primeiros passos

[Documentação](README.md) / Primeiros passos

## Pré-requisitos

- Python 3.11, utilizado pelo instalador Windows e pela imagem Docker.
- PostgreSQL com pgvector, já preparado ou iniciado pelo Docker Compose.
- Credenciais do provedor de IA e do bot Discord.
- Documentos revisados para formar a base de conhecimento.

Execute os comandos na raiz do repositório. Este guia usa o bot como processo Python local. Para executar tudo em contêineres, consulte o [guia Docker](../GUIA_DOCKER.md).

## 1. Preparar o ambiente

No Windows:

```powershell
.\setup_maquina.bat
.\.venv\Scripts\Activate.ps1
```

Se a ativação do ambiente não estiver disponível, use `.\.venv\Scripts\python.exe` no lugar de `python` nos próximos comandos.

No Linux ou macOS:

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
```

## 2. Configurar os serviços

Edite `.env` com base no [arquivo de exemplo](../.env.example).

| Variável | O que configurar |
| --- | --- |
| `DATABASE_URL` | Credenciais e endereço do PostgreSQL. |
| `GENERATION_PROVIDER` | Provedor das respostas: `gemini` ou `openai`. |
| `GENERATION_API_KEY`, `GENERATION_MODEL` | Credencial e modelo usados somente na geração. |
| `GENERATION_BASE_URL` | Endpoint usado somente por providers de geração compatíveis com OpenAI. |
| `EMBEDDING_PROVIDER` | Provedor dos embeddings, independente da geração. |
| `EMBEDDING_API_KEY`, `EMBEDDING_MODEL` | Credencial e modelo usados somente nos embeddings. |
| `EMBEDDING_BASE_URL` | Endpoint usado somente por providers de embeddings compatíveis com OpenAI. |
| `EMBEDDING_DIMENSIONS` | Dimensão compatível com o esquema do banco; esta versão exige `1536`. |
| `EMBEDDING_PREPROCESSING_VERSION` | Versão que identifica o preparo dos textos; mantenha `rag-text-v1` até uma migração planejada. |
| `DISCORD_TOKEN` | Token necessário para executar o Discord. |

A configuração de exemplo usa Gemini para respostas e embeddings. As duas credenciais devem ser preenchidas, mesmo quando contêm a mesma chave. Se trocar o provedor, revise também os modelos de reformulação e enriquecimento de contexto em `.env.example`. A [referência de configuração](configuracao.md) explica precedência, compatibilidade e diferenças intencionais dos defaults.

### Endereço do banco

| Onde o bot é executado | Endereço do PostgreSQL do Compose |
| --- | --- |
| Python local, na mesma máquina | `localhost:5432` |
| Contêiner deste projeto | `postgres:5432` |

Para Python local, substitua `@postgres:5432` por `@localhost:5432` na conexão de exemplo e mantenha as credenciais compatíveis com o banco.

Se utilizar o PostgreSQL fornecido pelo projeto:

```sh
docker compose up -d postgres
docker compose ps postgres
```

Aguarde o estado saudável.

### Banco novo

Em um volume novo, a inicialização aplica `setup_1536.sql` e as migrações do
`docker/postgres/init/00-bootstrap.sh`, incluindo
`sql/migrate_canonical_identity.sql` depois de `sql/add_analytical_context.sql`.
Não é necessário aplicar essa migração manualmente.

### Banco existente

A inicialização automática não é repetida em volumes existentes. Faça backup ou
snapshot e aplique as migrações correspondentes em uma base de testes antes da
implantação. Para a identidade editorial, confirme primeiro que
`document_sections` existe; se necessário, aplique o contexto analítico e depois a
migração canônica:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/add_analytical_context.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/migrate_canonical_identity.sql
```

Se `document_sections` já existir, aplique somente
`sql/migrate_canonical_identity.sql`. Consulte [SQL e migrações](../sql/README.md)
para a verificação e o rollback. Não execute `setup_1536.sql` ou `setup_3072.sql`
em uma base com dados: esses scripts são destrutivos e não são upgrades.

## 3. Preparar e indexar documentos

Revise o conteúdo da pasta `documentos/`, configurada por `DOCS_DIR`. Confira também se os materiais locais necessários foram copiados para esta máquina: o clone não transfere arquivos ignorados ou ainda não versionados.

Com o banco e o provedor de IA disponíveis:

```sh
python ingest.py ./documentos --no-recursive
```

O comando indexa os arquivos da pasta indicada sem percorrer suas subpastas. A ingestão consulta os serviços de IA e grava no banco. Para atualizar documentos existentes ou recuperar falhas, siga o [guia de operação](operacao.md).
Em execuções posteriores, hashes evitam embeddings quando conteúdo e
preprocessamento não mudaram. O modo recursivo e as exclusões de diretórios de
backup também estão descritos no guia de operação.

A migração de identidade é somente de schema: não converte o corpus, não ingere,
reindexa ou recalcula embeddings. Dados legados continuam válidos; as colunas
canônicas ficam nulas quando a identidade editorial é desconhecida. O preenchimento
de identidade ocorre somente em uma ingestão/projeção que o suporte correspondente
tenha publicado.

## 4. Iniciar o Discord

Discord no Windows:

```powershell
.\iniciar.bat
```

Ou, com o ambiente Python ativo:

```sh
python bot.py
```

Use `!ping`, `!status` e `!ajuda` para conferir a disponibilidade. Depois envie uma pergunta sobre um documento conhecido.

## Problemas frequentes

| Sintoma | O que conferir |
| --- | --- |
| Python local não encontra o host `postgres` | Use `localhost` para acessar o banco do Compose pela máquina hospedeira. |
| Contêiner não alcança um provedor local de IA | `127.0.0.1` aponta para o próprio contêiner; use um endereço acessível pela rede do serviço. |
| Erro de dimensão ou identidade dos vetores | Confira provider, modelo, dimensão, preprocessamento e o [registro do índice](identidade-indice-vetorial.md). |
| Colunas canônicas ausentes | Aplique `sql/migrate_canonical_identity.sql` depois de `sql/add_analytical_context.sql`, sem executar um `setup_*.sql`. |
| Documento legado sem identidade editorial | `canonical_id`, `schema_version`, `document_revision`, `metadata` ou `section_key` nulos podem ser o estado esperado; não preencha por aproximação. |
| Documento não aparece nas respostas | Confira o diretório usado, o resultado da ingestão e os relatórios de falha. |
