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
| `LLM_PROVIDER` | Provedor das respostas: `gemini` ou `openai`. |
| `GEMINI_API_KEY` | Chave do Gemini, usado na configuração de exemplo. |
| `EMBEDDING_PROVIDER` | Provedor dos embeddings; quando vazio, acompanha `LLM_PROVIDER`. |
| `EMBEDDING_MODEL` | Modelo de embeddings do perfil configurado. |
| `EMBEDDING_DIMENSIONS` | Dimensão compatível com o esquema do banco; o exemplo usa `1536`. |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | Credenciais e endereço quando usar o provedor compatível com OpenAI. |
| `OPENAI_MODEL`, `OPENAI_EMBEDDING_MODEL` | Modelos disponíveis no serviço escolhido. |
| `DISCORD_TOKEN` | Token necessário para executar o Discord. |

A configuração de exemplo usa Gemini para respostas e embeddings. Se trocar o provedor, revise também os modelos de reformulação e enriquecimento de contexto em `.env.example`.

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

Aguarde o estado saudável. A inicialização automática ocorre somente em um volume novo; bancos existentes precisam das [migrações correspondentes](../sql/README.md).

## 3. Preparar e indexar documentos

Revise o conteúdo da pasta `documentos/`, configurada por `DOCS_DIR`. Confira também se os materiais locais necessários foram copiados para esta máquina: o clone não transfere arquivos ignorados ou ainda não versionados.

Com o banco e o provedor de IA disponíveis:

```sh
python ingest.py ./documentos --no-recursive
```

O comando indexa os arquivos da pasta indicada sem percorrer suas subpastas. A ingestão consulta os serviços de IA e grava no banco. Para atualizar documentos existentes ou recuperar falhas, siga o [guia de operação](operacao.md).

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
| Erro de dimensão dos vetores | Confira o modelo, `EMBEDDING_DIMENSIONS` e o esquema SQL. |
| Documento não aparece nas respostas | Confira o diretório usado, o resultado da ingestão e os relatórios de falha. |
