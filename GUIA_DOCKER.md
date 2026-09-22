# Executar com Docker

[Documentação](docs/README.md) / Docker

O Compose fornece PostgreSQL com pgvector, o bot Discord e uma ferramenta de ingestão. Use Docker Engine ou Docker Desktop com Compose.

## 1. Preparar configuração e documentos

Na raiz do repositório, crie `.env` a partir do exemplo caso ainda não exista:

```sh
test -f .env || cp .env.example .env
```

No Windows, `setup_maquina.bat` também cria esse arquivo sem substituir uma configuração existente.

Preencha `DISCORD_TOKEN`, as credenciais do provedor de IA e a conexão do banco. O Compose exige `POSTGRES_PASSWORD`; o valor ilustrativo de `.env.example` serve somente para desenvolvimento local e deve ser substituído por uma senha exclusiva em cada implantação. Dentro do Compose, `DATABASE_URL` deve apontar para `postgres`. Se alterar `POSTGRES_DB`, `POSTGRES_USER` ou `POSTGRES_PASSWORD`, ajuste a conexão do bot para os mesmos valores.

Por padrão, a porta do PostgreSQL é publicada somente em `127.0.0.1:5432`. Isso permite o acesso legítimo por ferramentas executadas no host sem expor o banco na rede. Personalize a porta local com `POSTGRES_PORT`; mantenha `POSTGRES_BIND_ADDRESS=127.0.0.1`. Os serviços do Compose acessam o banco pela rede interna em `postgres:5432` e não dependem dessa publicação.

Os serviços de IA não são iniciados pelo Compose. Se utilizar um provedor local, configure um endereço acessível pelos contêineres: `127.0.0.1` dentro de um contêiner aponta para ele próprio.

Revise os documentos em `documentos/`. Essa pasta é montada como somente leitura em `/app/documentos` e não é copiada para a imagem.

## 2. Construir a imagem e preparar o banco

```sh
docker compose build
docker compose up -d postgres
docker compose ps postgres
docker compose logs --tail=100 postgres
```

Aguarde o banco ficar saudável. Em um volume novo, a inicialização aplica o esquema de 1536 dimensões e as migrações listadas em `docker/postgres/init/00-bootstrap.sh`.

O bootstrap aplica `sql/migrate_canonical_identity.sql` depois de
`sql/add_analytical_context.sql`. Essa migração adiciona a identidade editorial e
as chaves de seção sem converter o corpus ou reindexar vetores.

Volumes existentes não são reinicializados. Para atualizá-los, faça backup ou
snapshot, valide em uma base de testes e aplique a migração aditiva conforme o
[guia SQL](sql/README.md). Não remova `postgres_data`, não use `docker compose down -v`
e não execute `setup_*.sql` em um volume com dados.

Com o serviço `postgres` saudável e o SQL montado no container, a aplicação da
migração em um volume existente pode ser feita com:

```sh
docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /opt/bot-maxima/sql/migrate_canonical_identity.sql'
```

`document_sections` precisa existir antes desse comando; se ainda não existir,
aplique antes:

```sh
docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /opt/bot-maxima/sql/add_analytical_context.sql'
```

Depois, execute a migração canônica. Ela não
preenche identidade desconhecida: dados legados permanecem válidos e as colunas
canônicas continuam nulas quando não há evidência editorial.

## 3. Indexar os documentos

```sh
docker compose --profile tools run --rm ingest
```

Para definir explicitamente que somente os arquivos da raiz de `documentos/` serão processados:

```sh
docker compose --profile tools run --rm ingest python ingest.py ./documentos --no-recursive
```

A ingestão precisa de acesso ao banco e aos serviços de IA. Para atualização de fontes e recuperação de falhas, consulte o [guia de operação](docs/operacao.md).

A atualização do schema não executa a ingestão nem exige `--force`: não há conversão
do corpus, reindexação ou recálculo de embeddings. Provider, modelo, dimensão,
preprocessamento, RRF e defaults de ranking permanecem inalterados.

## 4. Iniciar o Discord

```sh
docker compose up -d discord_bot
docker compose ps
docker compose logs --tail=100 discord_bot
```

No Discord, use `!ping` e `!status` para conferir a disponibilidade. Envie também uma pergunta sobre um documento conhecido para verificar o acesso ao banco e a geração da resposta.

## Atualizar ou parar

No servidor de implantação:

```sh
git pull
docker compose build
docker compose up -d discord_bot
```

Antes de subir uma implantação, injete secrets pelo mecanismo protegido do ambiente ou por um `.env` com acesso restrito. Nunca reutilize os valores de exemplo nem publique o PostgreSQL em `0.0.0.0` sem um requisito de rede explícito e controles externos de firewall e autenticação.

Se a migração falhar, interrompa a atualização, preserve os logs e restaure o
backup/snapshot validado ou a versão anterior da aplicação. Não há script down
publicado; não remova tabelas, colunas ou o volume para fazer rollback. A migração
é aditiva e pode permanecer instalada durante o rollback do código.

Para parar os serviços e manter o volume do banco:

```sh
docker compose down
```

## Dados persistentes

| Local | Conteúdo |
| --- | --- |
| `postgres_data` | Banco PostgreSQL em volume nomeado. |
| `documentos/` | Fontes de conhecimento montadas como somente leitura. |
| `runtime/` | Relatórios de execução e falhas da ingestão. |

Credenciais locais, cópias de segurança, logs e arquivos temporários são excluídos do contexto de construção da imagem.
