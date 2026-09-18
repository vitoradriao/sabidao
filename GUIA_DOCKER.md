# Executar com Docker

[Documentação](docs/README.md) / Docker

O Compose fornece PostgreSQL com pgvector, serviços separados para Discord e Teams e ferramentas de ingestão e geração do pacote Teams. Use Docker Engine ou Docker Desktop com Compose.

## 1. Preparar configuração e documentos

Na raiz do repositório, crie `.env` a partir do exemplo caso ainda não exista:

```sh
test -f .env || cp .env.example .env
```

No Windows, `setup_maquina.bat` também cria esse arquivo sem substituir uma configuração existente.

Preencha as credenciais de IA e dos canais utilizados. Dentro do Compose, `DATABASE_URL` deve apontar para `postgres`. Se alterar `POSTGRES_DB`, `POSTGRES_USER` ou `POSTGRES_PASSWORD`, ajuste a conexão do bot para os mesmos valores.

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

Volumes existentes não são reinicializados. Para atualizá-los, consulte o [guia SQL](sql/README.md).

## 3. Indexar os documentos

```sh
docker compose --profile tools run --rm ingest
```

Para definir explicitamente que somente os arquivos da raiz de `documentos/` serão processados:

```sh
docker compose --profile tools run --rm ingest python ingest.py ./documentos --no-recursive
```

A ingestão precisa de acesso ao banco e aos serviços de IA. Para atualização de fontes e recuperação de falhas, consulte o [guia de operação](docs/operacao.md).

## 4. Iniciar os canais

Inicie os canais para os quais você configurou credenciais.

Discord:

```sh
docker compose up -d discord_bot
```

Teams, com geração do pacote:

```sh
docker compose --profile tools run --rm teams_package
docker compose up -d teams_bot
```

Ambos:

```sh
docker compose up -d discord_bot teams_bot
```

O pacote é gravado em `teams_manifest/build/bot-azure.zip`. Consulte o [guia do Teams](GUIA_TEAMS.md) para instalar o aplicativo.

## 5. Conferir o funcionamento

```sh
docker compose ps
docker compose logs --tail=100 discord_bot teams_bot
curl http://localhost:3978/api/health
```

O endpoint do Teams retorna:

```json
{"status":"ok","platform":"teams"}
```

Essa resposta confirma o serviço HTTP. Envie também uma pergunta sobre um documento conhecido para verificar o acesso ao banco e a geração da resposta.

O endereço de mensagens no Azure Bot deve usar HTTPS público e terminar em `/api/messages`. Encaminhe as requisições ao serviço Teams, por padrão na porta `3978`. O mapeamento de portas e a verificação de saúde do Compose usam esse valor; ajuste ambos se mudar `TEAMS_PORT`.

## Atualizar ou parar

No servidor de implantação:

```sh
git pull
docker compose build
docker compose up -d discord_bot teams_bot
```

Aplique as migrações necessárias conforme as instruções da versão.

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
| `teams_manifest/build/` | Manifesto e pacote gerado para o Teams. |

Credenciais locais, cópias de segurança, logs e arquivos temporários são excluídos do contexto de construção da imagem.
