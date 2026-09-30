# Ensaio Jev — development de 30/09/2026

**Adoção incompleta; #93 aberta.** Foram coletadas 38 respostas pointwise/batch, 38 respostas A/B e 19 decisões de calibração do gate. O responsável aprovou globalmente as primeiras 38 respostas e pediu ao assistente que analisasse a qualidade e os contextos. A análise foi realizada, identificada como análise do assistente. Correções e artefatos fazem parte do PR de rascunho #120, ainda não integrado à master. Não houve mudança de default, deploy ou reindexação operacional.

## Corpus, fontes e critérios

O responsável autorizou US$ 1,25, o corpus MaxPedido elegível e development, delegou os critérios iniciais e assumiu a revisão humana. [protocol.json](protocol.json) registra, antes das chamadas: ganho absoluto mínimo de 5 pontos percentuais, nenhum erro crítico novo, p95 até 120 segundos e `1,20 × p95(A) + 5 segundos`, fallback até 5% dos acionamentos e custo até `1,20 × custo(A)`. São critérios iniciais delegados, não calibração ou prova estatística. Uma resposta entre os 13 respondíveis representa 7,69 pontos.

Banco persistente isolado `sabidao_jev93`, contêiner `sabidao-jev93-postgres`, PostgreSQL 16/pgvector 0.8.6, somente `127.0.0.1:55493`. As 14 migrações foram aplicadas no banco vazio, na ordem do CI. Feedback vazio, contextualização desativada, Logística e backups fora da ingestão.

O primeiro snapshot tinha 21 documentos, 1.772 seções e 1.887 chunks. As 14 referências respondíveis históricas usavam `MAXIMA_RAG_UNIFICADO.md`, ausente do corpus ativo. Os zeros do primeiro diagnóstico não medem falha de recuperação e não entram na decisão. Dataset histórico e backup preservados; preparação em [report-preparation.json](report-preparation.json).

Vitoradriao aprovou 13 vínculos de fontes, incluindo `StatusPedidos (PUT)`, status 5, e rejeitou a consulta `ORDER BY 3 DESC`. Ela foi removida do documento SQL existente a seu pedido. Não se recomendou outro SQL nem se mudou o gabarito para aprovar uma substituição. O caso sem fonte válida ficou excluído de [development-v2.json](development-v2.json): 19 casos, 13 respondíveis/4 ambíguos/2 sem evidência. Os dez casos de holdout históricos não foram alterados ou executados.

Snapshot atualizado: 21 documentos, 1.771 seções, 1.886 chunks, `jev93-20260930-5a07b8f135c8-e3b0c442`. Cobertura média pré-Jev: top-20 **0,820515** e top-40 **0,897438**, N=13. Pela regra registrada, manter cap 20: a ablação paga cap 40 não foi executada porque top-20 já atende 0,80. Identidades e diagnóstico em [report.json](report.json).

## Pointwise/batch e revisão

Coleta `ad2c91a`, [protocolo v3](protocol-v3.json), mesma pool, consulta, conteúdo integral e cap. Há 19 respostas por braço. Execução completa; status nativo `incomplete` por fallbacks pointwise. [pointwise-batch-v3.json](pointwise-batch-v3.json) preserva estados, decisões, usage, referências sanitizadas e métricas por estágio/contexto final.

| Pipeline, N=19 | Pointwise | Batch |
|---|---:|---:|
| Jev aplicado | 15/19 | 19/19 |
| Fallback acionado | 4/19 (21,05%) | 0/19 |
| p50 | 12,765 s | 5,922 s |
| p95 | 22,250 s | 10,921 s |
| Recall@20, N=13 | 0,7949 | 0,8205 |

Latência do trace de `rag.ask`, nearest-rank, sem auditoria. Tempos do callback completo do avaliador ficam separados. Fallback: três timeouts e um erro de provider; falha não conta como aplicação bem-sucedida.

Auditoria original/reversed/stable_rotation: composição integral aplicada em 15/19 pointwise e 19/19 batch. A ordem de saída variou em 19/19 nos dois modos; desempates pela posição original e ruído entre chamadas também podem explicar variação. Não recomenda ensemble. Prompts históricos diferentes são confundidor: o efeito não isola apenas batching. Batch foi escolhido como candidato de development por latência/fallback/Recall, sem aprovar default.

[human-review-rerank-v3.json](human-review-rerank-v3.json) registra a aprovação humana global das 38 respostas, vinculada aos hashes privados; não fornece contagens tipadas de acerto/suficiência. A análise solicitada ao assistente está em [assistant-context-review-v3.json](assistant-context-review-v3.json): **12 contextos suficientes, 4 que exigem esclarecimento e 3 insuficientes**.

Achados: duas perguntas vagas receberam cenário assumido; a pergunta sobre críticas não reteve `MXSHISTORICOCRITICA`; “campos principais” não prova lista exaustiva nem inexistência de coluna oculta. O arquivo privado de análise contém uma resposta de referência por caso. Não substitui a resposta coletada, não modifica prompts/gabaritos e não equivale a confirmação independente.

## A/B operacional

Coleta `1af344d`, [protocolo v4](protocol-v4.json), batch/cap 20, mesmo builder/snapshot e geração/embeddings/strict/limites preservados. Preparação sem calls. O saldo foi priorizado para `end_to_end_same_snapshot`; ablação existing/Jev em pool congelada não executada. O par pointwise/batch separado usa pool congelada.

[ab-batch-v4.json](ab-batch-v4.json) tem execução completa e identidades compatíveis. Há `state_sha256` real nas 19 aplicações Jev, evidência operacional solicitada pela #91. Isso não aprova adoção da #93.

| Indicador | Existing | Jev batch |
|---|---:|---:|
| Respostas | 19 | 19 |
| Rankings aplicados | 0/19 | 19/19 |
| p50 do pipeline | 7,875 s | 6,561 s |
| p95 do pipeline | 15,625 s | 13,531 s |
| Acertos pelo proxy de frases | 8/13 | 10/13 |
| Esclarecimentos pelo proxy | 0/4 | 2/4 |
| Abstenções corretas pelo proxy | 2/2 | 2/2 |

**Limitação de A:** 19/19 chamadas do reranker DeepSeek terminaram `empty_response`, `finish_reason=length`, 200 tokens de raciocínio e zero tokens de texto. O pipeline conservou o ranking recuperado. A comparação representa o funcionamento atual, incluindo essa falha; não comprova superioridade contra existing com saída válida. Thinking e limites não foram alterados para melhorar o resultado. Corrigir esse comportamento exige nova identidade/protocolo e nova coleta.

As 38 respostas A/B foram analisadas pelo assistente em [assistant-ab-review-v4.json](assistant-ab-review-v4.json): núcleo sustentado/completo 10/13 em A e 12/13 em B; esclarecimento adequado 2/4 e 3/4; abstenção adequada 2/2 em ambos. Em A, um log foi omitido apesar de estar no contexto; a referência de críticas faltou nos dois contextos. Essas contagens são julgamentos do assistente, não revisão humana independente, e não validam cada afirmação periférica.

Proxies determinísticos permanecem separados: frase equivalente a “ERP faz GET” pode falhar na busca por “ERP realiza GET”. Não alterar gabarito para fazer resultado passar. Esclarecimento 0,80 ainda não é atendido, há sinais de falsa ausência e custo nativo completo/razão de custos indisponíveis. nDCG indisponível sem julgamentos graduados; nenhum gate nDCG inventado.

## Gate provisório

[Plano de candidatos](gate-calibration-plan.json) e [registro dos 19 estados antes das calls](gate-calibration-v4-registration.json). Apenas chunks retidos do braço batch: renderização determinística com o renderer da coleta, spans/hashes conferidos e consulta conferida contra o state do rerank. Sem nova seleção/ampliação de contexto. Os dois follow-ups mantiveram pergunta original: o gate não recebe histórico separado, embora o gerador o receba; limitação registrada.

[gate-calibration-v4.json](gate-calibration-v4.json) tem 19 decisões reais e a grade avaliada contra **rótulos provisórios do assistente**, conforme pedido do responsável. Candidato: confiança **0,50**, margem **0,30**, ambas as decisões negativas. Preservou 12/12 suficientes e identificou corretamente 3/4 esclarecimentos e 2/3 insuficientes. Erro genérico inconclusivo; falta da referência de críticas julgada suficiente. Falhas/inconclusivos não contam como sucesso.

[Política provisional](gate-policy-provisional-v4.json) exclusivamente de development; não frozen, não confirmada, sem adoção. [Perfil B/C](bc-v4.profile.json) e [preparação local sem calls](bc-v4-preparation.json) validados: `ready`, sem bloqueio de código. **B/C real não executado pelo saldo insuficiente para o par completo.** Gate isolado exige par próprio; não inferir efeito da calibração ou A/B.

## Correções, orçamento e reprodução

Uma tentativa privada perdeu uma resposta por serialização UUID/datetime; não entra no par e suas chamadas continuam contabilizadas. A seguinte coletou 19 pointwise e parou na `http-1990`: 371 tokens de saída para limite de 200. Livro bloqueado preservado; coleta separada em [pointwise-v2-interrupted.json](pointwise-v2-interrupted.json).

Correção pequena: enviar `max_tokens` desde a primeira chamada ao host exato `api.deepseek.com`, seguindo o [contrato oficial](https://api-docs.deepseek.com/api/create-chat-completion/). Antes enviava `max_completion_tokens`, com fallback somente após HTTP 400, mas a chamada retornou 200. Outros hosts preservam o contrato/fallback; modelos, prompts e limites numéricos mantidos. Teste real restritivo retornou 16 tokens para limite 16.

O avaliador também passou a reconhecer `provider_error` como falha operacional. No v3, evita classificar a falha do follow-up ambíguo como abstenção de qualidade. Outcomes reanalisados offline; coleta e análise identificadas separadamente, sem repetir chamadas pagas.

Total: **3.742 chamadas HTTP**, **US$ 0,542841636** conhecido estimado nas tarifas registradas, **US$ 1,195997514** comprometido com reservas, **513 chamadas com uso desconhecido**, **US$ 0,054002486** de saldo conservador. Gasto faturado não verificado. O saldo não comporta novo B/C completo pela estimativa baseada no braço B observado (US$ 0,086915214 sozinho). Não iniciar par incompleto para gastar o restante.

[Livro v3](budget-calls-v3.json): somente calls após 1.990, referência/hash do [livro anterior](budget-calls-v2.json), carregado integralmente no mesmo teto. [Livro inicial](budget-calls.json) preservado. Pendentes/erros/timeouts/usage ausente conservam reserva. A e B compartilham cache de embeddings; aquecimento limita comparação de custo isolado.

[ExperimentBudget](../../../../evaluation/experiment_budget.py) é opt-in: wrapper `httpx.Client.send`, instalado somente nos scripts privados, reserva antes do despacho. Não é automático no bot/CLI. Um processo por livro, sem lock entre processos. Scripts, respostas/contextos brutos e conexão ficam em `runtime/issue-93/`, ignorado pelo Git. Não executar CLI pago desprotegido supondo teto aplicado.

Perfis v1–v4 são templates; `snapshot_id=null` não é identidade verificada. A preparação preenche/verifica sem calls. Scripts privados executados: `run_development_v3.py prepare/rerank`, `run_development_v4.py prepare/ab`, `calibrate_gate.py prepare/run`. `dry_run=True` impede persistência do avaliador, mas a execução chamou banco/modelos. Alias `deepseek-flash` mutável; Jev `jev-1.13.0`; embeddings `gemini-embedding-001`, 1536 dimensões.

## Validação e pendências

Python 3.11: **418 testes, 17 skips, exit 0** após as correções: oito do wrapper, quatro do transporte e dois de falha operacional. Oito testes documentais passaram. A suíte local anterior com banco teve uma falha por ausência de `psql` Windows; as quatro fixtures SQL passaram pelo cliente do contêiner, em `sabidao_jev93_tests`. [CI unit-tests/postgres-tests passou em 1af344d](https://github.com/vitoradriao/sabidao/actions/runs/36725636772), última mudança de código; isso não declara checks posteriores.

Restam B/C completo, validação independente dos julgamentos/revisão humana de A/B, critérios atendidos, congelamento e novo conjunto independente revisado/pré-registrado para confirmação. Holdout histórico não aberto para ajustar resultado. Considerar a falha do reranker DeepSeek antes de atribuir ganho/repetir. #20 reutiliza os achados, #53 acompanha revisão/identidade; grounding #96 separado. Reranker e gate permanecem `incomplete`, mantendo o funcionamento publicado e gate desligado.
