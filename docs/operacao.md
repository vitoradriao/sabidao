# Operação da base de conhecimento

[Documentação](README.md) / Operação

Execute os comandos na raiz do projeto, com o ambiente Python ativo e os serviços configurados.

## Preparar as fontes

Mantenha em `DOCS_DIR` apenas os materiais que devem fazer parte da consulta. Revise duplicações, documentos desatualizados e arquivos gerados antes de indexar.

Para indexar os arquivos diretamente em `documentos/`, sem percorrer subpastas:

```sh
python ingest.py ./documentos --no-recursive
```

Use `--recursive` somente quando as subpastas também contiverem fontes aprovadas.

## Atualizar documentos

Para reingerir fontes já indexadas:

```sh
python ingest.py ./documentos --no-recursive --force
```

Para repetir fontes registradas como falha:

```sh
python ingest.py --retry-failed
```

O caminho do relatório é definido por `FAILED_INGEST_REPORT`. No Compose, ele é persistido em `runtime/ingest_failures.json`.

Antes de uma atualização ampla, faça uma cópia de segurança do banco, valide a mudança em um ambiente de testes e compare as respostas com a [avaliação de referência](../evaluation/README.md).

## Fontes da web

A ferramenta também aceita URLs:

```sh
python ingest.py --url https://exemplo.com/documentacao
python ingest.py --urls-file ./documentos/urls.txt
```

Substitua a URL de exemplo por uma fonte autorizada e revise o material resultante. As opções de coleta e revisão estão em `.env.example`.

## Extrair tickets do Gatekeeper

O ponto de entrada é `scripts/extract_jira_gatekeeper_filipe.py`.

Configure no `.env`:

- `JIRA_URL` ou `JIRA_BASE_URL`.
- `JIRA_USERNAME` com `JIRA_API_TOKEN` ou `JIRA_PASSWORD`; alternativamente, `JIRA_SESSION_COOKIE`.
- `JIRA_ASSIGNEE_ALIASES` para os responsáveis desejados.

No Windows, `abrir_extrator_gatekeeper.bat` abre a janela de extração. Os arquivos Markdown são gravados em `documentos/gatekeeper_markdowns` por padrão.

Para extrair pela linha de comando, sem processamento por modelo de linguagem:

```sh
python scripts/extract_jira_gatekeeper_filipe.py --limit 20 --no-llm
```

| Diretório em `datasets/gatekeeper_filipe/` | Conteúdo |
| --- | --- |
| `raw/` | Dados brutos coletados. |
| `normalized/` | Registros normalizados. |
| `gold/` | Casos preparados pelo fluxo de extração. |
| `review/` | Materiais destinados à revisão. |

A extração não substitui a revisão técnica. Confira o conteúdo antes de usá-lo na base ou nos cenários de avaliação.
