# Promoção e rollback de lotes canônicos

[Documentação](README.md) / Promoção de lotes

Rename, split e merge mudam a seleção de documentos e sua projeção no banco.
Prepare cada lote antes de publicar o manifesto candidato. A promoção é uma
ação operacional explícita; lint, ingestão comum e a migração SQL não a iniciam.
Esta entrega fornece o mecanismo, sem promover os documentos reais do corpus.

## Preparar o lote

Mantenha os dois manifestos e os arquivos que eles referenciam no mesmo
checkout. O manifesto anterior deve representar a seleção atualmente
publicada. Para split e merge, o candidato declara cada predecessor
`superseded`/`exclude` e aponta seus sucessores canônicos por `document_id` e
`section_keys`. Um rename da mesma identidade canônica pode remover a entrada
antiga do manifesto candidato: o plano reconhece o mesmo `document_id` em
outro caminho. Cada sucessor precisa estar `active` e `include`. Revise
aliases e revisões antes de prosseguir.

Para split e merge, use caminhos novos para os sucessores quando os bytes
mudarem. O preview precisa validar simultaneamente os arquivos do manifesto
anterior e do candidato; reutilizar um caminho com conteúdo diferente exigiria
publicar o novo arquivo antes da troca de banco. Esse caso é rejeitado pelo
plano desta versão.

Mantenha os arquivos predecessores para permitir rollback. O lint aceita sua
presença no diretório imediato quando o manifesto os declara explicitamente
`superseded`/`exclude`, com sucessores resolvidos; um arquivo sem entrada no
manifesto continua sendo erro de seleção. Para um rename de mesmo ID, o
manifesto candidato não pode repetir esse ID na entrada antiga. O comando
arquiva o arquivo predecessor em `runtime/canonical-archive/<batch_id>/` após
o commit e guarda seus bytes no ledger; o rollback o restaura antes de
republicar o manifesto anterior. Não remova o arquivo arquivado manualmente.

Produza e aprove os dois resultados da
[verificação da #69](verificacao-migracao-canonica.md): relatório de preservação
por unidade e gate de consultas pareadas. O gate exige avaliação em snapshots
descartáveis, identidade experimental e limites congelados. Registre o hash do
corpus anterior e o hash esperado do corpus candidato a partir dessas rodadas;
o plano rejeita estado desconhecido ou divergente.
O fingerprint do corpus inclui `documents.source`; produza a referência e o
candidato com os mesmos caminhos absolutos resolvidos que serão usados na
promoção, alternando bancos descartáveis quando necessário. Um checkout em
outro caminho pode produzir hash diferente com texto e vetores iguais.

Com a base preparada, gere o plano offline:

```sh
python promotion.py preview \
  --before-manifest contracts/canonical-docs/manifest.yaml \
  --after-manifest runtime/manifesto-candidato.yaml \
  --report runtime/lote-relatorio.json \
  --expected-corpus-sha256 HASH_ANTES \
  --expected-candidate-corpus-sha256 HASH_DEPOIS \
  --output runtime/plano-lote.json
```

O preview valida o mapa, IDs, revisões, caminhos, hashes e elegibilidade sem
consultar banco ou providers. Revise o plano e sua identidade antes da ação.
Se o checkout Windows tiver convertido apenas LF/CRLF de uma fonte, o plano
registra o caminho em `normalized_source_paths`; qualquer outra alteração de
bytes em relação ao snapshot original bloqueia o preview.
Arquivos em `runtime/` não devem ser publicados com dados brutos de clientes.

## Promover

Em banco existente, aplique primeiro a migração aditiva
`sql/add_canonical_promotion.sql` conforme o [guia SQL](../sql/README.md).
Use banco descartável e providers simulados para ensaiar falhas e rollback.
Suspenda mudanças editoriais e confirme que o checkout contém ambos os
manifestos e os arquivos aprovados. Depois execute a ação explícita:

```sh
python promotion.py apply \
  --plan runtime/plano-lote.json \
  --preservation runtime/lote-preservacao.json \
  --gate runtime/lote-gate.json \
  --root .
```

O comando exige os gates `passed` do mesmo lote e snapshot. Todos os
sucessores e vetores são preparados antes da transação. A troca de
predecessores por sucessores, a conferência de fingerprint e o registro do
snapshot de rollback ocorrem em uma transação PostgreSQL. Falha antes do commit
preserva a seleção anterior; reaplicar um plano idêntico é idempotente somente
se o estado persistido ainda corresponder ao resultado registrado. Revisão
inferior não é promoção válida.

Depois do commit, o manifesto do filesystem é publicado por substituição de
arquivo. Git/filesystem e PostgreSQL não compartilham uma transação. Enquanto
os hashes divergirem, o caminho de full-context falha fechado, sem misturar
gerações. Se a publicação do arquivo falhar, reexecute `apply` com o mesmo
plano e os mesmos gates para concluir a publicação pendente, sem recalcular
vetores; também é possível iniciar o rollback identificado. Confirme o estado
da publicação antes de reabrir esse caminho. O
índice de chunks continua sob a fronteira transacional do PostgreSQL; coordene
a janela de publicação e as consultas em andamento com a operação do bot.

## Reverter

Um rollback exige o plano identificado e o estado pós-promoção esperado:

```sh
python promotion.py rollback --plan runtime/plano-lote.json --root .
```

O snapshot registrado guarda documentos, seções, chunks, vetores, manifesto e
aliases anteriores. A restauração das linhas ocorre em transação, sem recalcular
embeddings. O comando restaura os arquivos predecessores e arquiva os arquivos
sucessores que o manifesto anterior não inventaria; depois republica o
manifesto anterior no filesystem. Se a publicação
do arquivo falhar após o commit, o estado fica pendente e full-context continua
bloqueado até a reconciliação; não trate esse estado como rollback concluído.
Guarde o ledger e o plano pelo prazo operacional do lote. Um downgrade de
revisão por ingestão comum é rejeitado; use o rollback identificado.

O ledger contém texto e vetores do corpus anterior. Restrinja acesso e backup
como aos dados do banco; não copie o snapshot para logs, PRs ou relatórios
sanitizados. Os registros operacionais mostram apenas IDs de lote, hashes,
contagens e estados.

O resultado do mecanismo e seus testes não constituem promoção operacional,
revisão humana do corpus nem benchmark de qualidade. Eles precisam ser
executados e conferidos no lote real autorizado.
