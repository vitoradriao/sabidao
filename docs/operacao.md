# Operação da base de conhecimento

[Documentação](README.md) / Operação

Execute os comandos na raiz do projeto, com o ambiente Python ativo e os serviços configurados.

## Preparar as fontes

Mantenha em `DOCS_DIR` apenas os materiais que devem fazer parte da consulta. Revise duplicações, documentos desatualizados e arquivos gerados antes de indexar.
Antes de aplicar uma mudança ampla de classificação, confira o relatório
offline descrito em [Taxonomia documental](taxonomia.md#conferência-antes-de-migrar).

Para indexar os arquivos diretamente em `documentos/`, sem percorrer subpastas:

```sh
python ingest.py ./documentos --no-recursive
```

Use `--recursive` somente quando as subpastas também contiverem fontes aprovadas.
Por padrão, diretórios chamados `docbkp`, `backup`, `backups` e `bkp` são
ignorados mesmo no modo recursivo. Ajuste `INGEST_EXCLUDED_DIRS` para mudar a
lista. Para incluir backups de forma explícita, defina a variável como vazia e
revise as fontes antes da execução.

## Atualizar o schema antes da ingestão

Em uma base existente, aplique `sql/migrate_canonical_identity.sql` antes de
usar a identidade editorial canônica. `documents` deve existir e
`document_sections` deve ter sido criada por `sql/add_analytical_context.sql`.
Se essa dependência ainda não estiver presente, aplique as migrações nesta ordem:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/add_analytical_context.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/migrate_canonical_identity.sql
```

Quando `document_sections` já existir, execute somente
`sql/migrate_canonical_identity.sql`. Faça backup ou snapshot, teste em uma base
descartável e confira a aplicação conforme o [guia SQL](../sql/README.md).

A migração adiciona `canonical_id`, `schema_version`, `document_revision` e
`metadata` em `documents`, além de `section_key` em `document_sections`, todos
nullable. Dados legados continuam válidos e permanecem nulos quando a identidade
editorial é desconhecida. Ela não converte o corpus, não altera o manifesto, não
gera embeddings, não ingere nem reindexa documentos e não muda provider, modelo,
dimensão, RRF ou defaults de ranking.

Em um banco novo, o `docker/postgres/init/00-bootstrap.sh` aplica a migração no
bootstrap, depois de `add_analytical_context.sql`. Não repita o bootstrap em um
volume existente e não use `setup_*.sql` para atualizá-lo.

Não há script down publicado. Para retornar a aplicação, restaure a versão anterior
e mantenha as colunas aditivas instaladas. Para desfazer o schema, use o
backup/snapshot validado ou uma operação reversível específica; não remova dados,
tabelas ou o volume como forma de rollback.

## Ingerir documentos canônicos

Antes de incluir um Markdown canônico, valide o front matter e as chaves de
seção com `py canonical_docs.py lint`. A ingestão também valida cada fonte
canônica antes de consultar modelos ou substituir registros. `doc_type` continua
`md`; a classificação e o tipo semântico ficam em metadados separados. O YAML
bruto não é indexado como conteúdo. Título e classificação editoriais podem
compor o contexto de recuperação enviado aos embeddings.

O `document_id` editorial identifica a fonte mesmo se seu arquivo mudar de
nome. Uma repetição idêntica não refaz o processamento; uma revisão superior
substitui a projeção da mesma identidade. A mesma revisão com conteúdo
divergente e uma revisão inferior são conflitos: corrija a fonte, não use
`--force` para contornar a revisão. Falha de validação, preparação ou escrita
preserva a versão anterior do índice.

O banco guarda uma assinatura semântica canônica em `documents.metadata`,
calculada a partir dos campos validados e do corpo com finais de linha
normalizados. Reordenar chaves YAML sem mudar seus valores não altera essa
assinatura. `content_hash` identifica somente o corpo; `processing_hash`
identifica a projeção e o processamento necessários para reutilizar vetores.

Essa capacidade não promove o manifesto nem converte os 27 Markdown legados.
Planeje a migração de cada lote com snapshot, verificação de preservação,
comparação de consultas e rollback antes de executar ingestão operacional.

## Atualizar documentos

A execução comum calcula hashes do conteúdo extraído e do preprocessamento.
Arquivos modificados são atualizados automaticamente; arquivos sem mudança não
geram novos embeddings. Origens distintas com o mesmo preprocessamento
reaproveitam os vetores, mantendo nome e caminho próprios na tabela de
documentos.

Para reingerir todas as fontes mesmo quando os hashes não mudaram:

```sh
python ingest.py ./documentos --no-recursive --force
```

No Discord, `!reindex` (alias de `!ingerir`) tem a mesma semântica de `--force`.
A recursão usada pelo comando segue `INGEST_RECURSIVE`; as exclusões seguem
`INGEST_EXCLUDED_DIRS`.

Bancos existentes precisam receber `sql/add_ingest_identity.sql` antes de usar
a ingestão incremental. A migração apenas adiciona colunas e índices; não
reindexa documentos existentes. Fontes antigas, ainda sem hashes, ganham a
identidade na primeira ingestão posterior.

Essa migração de ingestão é independente de `migrate_canonical_identity.sql`.
Aplicar o schema canônico não promove fontes legadas nem substitui a ingestão
explícita posterior. Não use `--force` ou `!reindex` apenas para concluir o
upgrade do banco.

Para repetir fontes registradas como falha:

```sh
python ingest.py --retry-failed
```

O caminho do relatório é definido por `FAILED_INGEST_REPORT`. No Compose, ele é persistido em `runtime/ingest_failures.json`.

Antes de uma atualização ampla, faça uma cópia de segurança do banco, valide a mudança em um ambiente de testes e compare as respostas com a [avaliação de referência](../evaluation/README.md).

## Logs, retenção e dados sensíveis

Por padrão, os logs operacionais não devem registrar perguntas, prompts,
respostas completas, anexos, nomes de fontes, tokens, credenciais nem URLs que
contenham credenciais. Falhas inesperadas retornam ao Discord uma mensagem
genérica com `request_id`; o log correspondente preserva apenas `request_id`,
etapa e classe do erro. Traces do RAG mantêm métricas, estados e contagens, sem
o conteúdo consultado ou os identificadores das fontes.

Na ingestão, arquivos e URLs são correlacionados por um `source_id` opaco e
estável. Os eventos registram etapa, classe do erro e contagens necessárias,
sem nomes, caminhos, conteúdo documental, corpo de resposta do provider ou
texto arbitrário de exceções.

O arquivo configurado em `FAILED_INGEST_REPORT` é uma exceção deliberada: ele
preserva o caminho ou a URL exatos para que `--retry-failed` consiga repetir a
fonte. Trate esse relatório como dado operacional sensível, restrinja seu
acesso ao processo e aos operadores autorizados e não o compartilhe como se
fosse um trecho de log sanitizado.

O aplicativo escreve logs na saída padrão e não define retenção própria. O
operador deve restringir o acesso no coletor de logs e configurar uma retenção
compatível com a política da organização; na ausência de outra exigência, use
no máximo 30 dias. Antes de compartilhar um trecho, remova secrets e dados de
usuário. Se houver suspeita de exposição, restrinja o acesso, preserve apenas a
evidência necessária e rotacione imediatamente as credenciais envolvidas.

## Fontes da web

A ferramenta também aceita URLs:

```sh
python ingest.py --url https://exemplo.com/documentacao
python ingest.py --urls-file ./documentos/urls.txt
```

Substitua a URL de exemplo por uma fonte autorizada e revise o material resultante. A coleta aceita somente HTTP(S), rejeita credenciais embutidas na URL, valida todos os endereços IPv4/IPv6 resolvidos e repete a validação em cada redirecionamento. Falhas de DNS são bloqueadas, e respostas são lidas por streaming até `WEB_MAX_DOWNLOAD_BYTES`, sem carregar antecipadamente um corpo maior que o limite. `WEB_FETCH_TIMEOUT_SECONDS` e `WEB_MAX_REDIRECTS` limitam cada tentativa.

Por padrão, qualquer host que resolva exclusivamente para endereços públicos é elegível. Em uma implantação com fontes conhecidas, defina `WEB_ALLOWED_HOSTS` com hosts exatos separados por vírgula; use `*.exemplo.com` somente quando todos os subdomínios estiverem dentro da mesma fronteira de confiança. O curinga não autoriza o domínio raiz. URLs com tokens ou credenciais não devem ser usadas como fontes.

A verificação DNS anterior à conexão reduz SSRF, mas não elimina sozinha DNS rebinding ou mudanças de rota entre resolução e conexão. Restrinja a saída do contêiner ou host no firewall/proxy aos destinos necessários e trate a allowlist como uma camada adicional. Os testes usam transporte e DNS simulados; não realizam sondas contra redes internas. As demais opções de coleta e revisão estão em `.env.example`.
