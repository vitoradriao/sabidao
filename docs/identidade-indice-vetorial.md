# Identidade do índice vetorial

[Documentação](README.md) / Identidade do índice vetorial

Corpus, seções analíticas e memória de feedback compartilham o modelo configurado,
mas mantêm identidades registradas separadamente. Cada identidade contém:

- provider de embeddings;
- nome exato do modelo;
- dimensão, atualmente fixada em 1536;
- versão do preprocessamento aplicado ao texto.

A dimensão sozinha não identifica um espaço vetorial. Dois modelos com 1536
dimensões podem produzir vetores incompatíveis. Por isso, uma troca de modelo,
provider ou preprocessamento é rejeitada mesmo quando o tamanho permanece igual.

O cache de embeddings de consulta inclui essa identidade na chave. Uma mudança de
identidade não reutiliza entradas produzidas pela configuração anterior.

## Aplicar a migração

Faça backup e aplique a migração aditiva:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/add_embedding_index_identity.sql
```

A migração cria `embedding_index_identities` e a função de validação. Ela não
altera, apaga ou reindexa vetores. Em uma coleção vazia, a aplicação registra a
identidade efetiva no primeiro uso. Em uma coleção que já contém vetores, o
registro automático é recusado porque o modelo original não pode ser deduzido.

## Identificar uma base legada

Primeiro confira dimensão e volume sem alterar dados:

```sql
SELECT
    relation.relname AS table_name,
    FORMAT_TYPE(attribute.atttypid, attribute.atttypmod) AS embedding_type
FROM pg_attribute attribute
JOIN pg_class relation ON relation.oid = attribute.attrelid
JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'public'
  AND relation.relname IN ('document_chunks', 'document_sections', 'feedback_chunks')
  AND attribute.attname = 'embedding'
  AND NOT attribute.attisdropped;

SELECT
    (SELECT COUNT(*) FROM document_chunks WHERE embedding IS NOT NULL) AS corpus,
    (SELECT COUNT(*) FROM document_sections WHERE embedding IS NOT NULL) AS sections,
    (SELECT COUNT(*) FROM feedback_chunks WHERE embedding IS NOT NULL) AS feedback;
```

Confirme provider, modelo e versão de preprocessamento em arquivos de implantação,
histórico de configuração ou registros do processo que criou os vetores. Não use a
dimensão ou o conteúdo dos vetores para adivinhar o modelo.

Somente quando a origem estiver comprovada, registre cada coleção existente com os
valores confirmados:

```sql
INSERT INTO embedding_index_identities (
    index_scope,
    provider,
    model,
    dimensions,
    preprocessing_version
)
VALUES
    ('corpus', '<provider-confirmado>', '<modelo-confirmado>', 1536, '<versao-confirmada>'),
    ('sections', '<provider-confirmado>', '<modelo-confirmado>', 1536, '<versao-confirmada>'),
    ('feedback', '<provider-confirmado>', '<modelo-confirmado>', 1536, '<versao-confirmada>');
```

Inclua apenas coleções que já tenham vetores. Coleções vazias serão registradas pela
aplicação. Depois, inicie o processo com exatamente a mesma identidade; uma
divergência falhará antes de gravar ou consultar o índice.

## Quando a identidade é desconhecida ou precisa mudar

Não preencha metadados por aproximação e não sobrescreva a identidade atual. Planeje
uma reconstrução não destrutiva:

1. crie tabelas e índices paralelos em 1536 dimensões;
2. gere novamente corpus, seções e feedback com a nova identidade;
3. valide contagens, amostras de busca e cobertura antes da troca;
4. altere o tráfego em uma janela controlada;
5. preserve as estruturas antigas até concluir a validação e o período de rollback.

Esse backfill não é executado por esta migração. A troca de tabelas, reingestão ou
remoção dos vetores anteriores exige uma mudança operacional separada.

## Rollback de configuração

Se a inicialização falhar após uma alteração, restaure
`EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` e
`EMBEDDING_PREPROCESSING_VERSION` para os valores registrados em
`embedding_index_identities` e reinicie o processo. Não edite a tabela apenas para
silenciar a validação: isso permitiria consultar vetores incompatíveis.

A migração SQL pode permanecer instalada durante o rollback do código; versões
anteriores simplesmente não consultam a tabela. Assim, não é necessário remover
metadados nem tocar nos vetores existentes.
