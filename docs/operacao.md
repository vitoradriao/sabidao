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
