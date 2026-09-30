# Ensaio Jev — development e confirmação de 30/09/2026

**Comparações coletadas; adoção de reranker e gate rejeitada pelos critérios registrados. A #93 permanece aberta pelas pendências de revisão/protocolo e integração.** Na confirmação, existing e Jev acertaram os fatos esperados em 8/8 perguntas respondíveis: ganho observado zero, abaixo dos 5 pontos percentuais exigidos. O gate acertou esclarecimento em 1/2 perguntas ambíguas, abaixo de 0,80. Manter existing e gate desligado. Resultados e correções pertencem ao PR de rascunho #120, ainda não integrado; nenhuma mudança de default, deploy ou reindexação operacional.

## Protocolo e identidade

O responsável autorizou o corpus MaxPedido e development, delegou os critérios iniciais e depois autorizou ampliar o orçamento para concluir a comparação. US$ 10 é o limite de trabalho escolhido pelo assistente, não o valor originalmente informado pelo responsável. [Registro da ampliação](budget-expansion-registration.json), [protocolo v5](protocol-v5.json) e [pré-registro da confirmação](confirmation-registration-v1.json) preservam a sequência anterior às chamadas. Critérios: ganho factual absoluto ≥0,05; nenhum erro crítico novo; coverage ≥0,80; acerto ≥0,70; abstenção/esclarecimento ≥0,80; Recall@20 ≥0,80; p95 ≤120 s e ≤1,20 × baseline + 5 s; fallback ≤5%; razão de custo ≤1,20. Não relaxados após resultados.

Banco Docker persistente isolado `sabidao_jev93`, PostgreSQL 16/pgvector 0.8.6, em `127.0.0.1:55493`. Snapshot `jev93-20260930-5a07b8f135c8-e3b0c442`: 21 documentos, 1.771 seções, 1.886 chunks; feedback vazio, contextualização desligada, Logística/backups excluídos. Corpus/feedback/identidades detalhados no [relatório](report.json). Geração/reformulação e rerank existing `deepseek-flash` em `api.deepseek.com`; alias mutável. Jev `jev-1.13.0`; embeddings `gemini-embedding-001`, 1536 dimensões. Mesmo gerador, embeddings, builder, strict e limites entre braços.

O primeiro diagnóstico usava referências de uma fonte ausente e não mede falha de recuperação. Responsável aprovou 13 vínculos válidos, rejeitou `ORDER BY 3 DESC` e autorizou remover esse SQL da documentação. Caso excluído de [development](development-v2.json), sem alterar gabarito histórico: 19 casos, 13 respondíveis/4 ambíguos/2 sem evidência. Os dez casos históricos de holdout foram preservados e não executados nesta confirmação. Cobertura média pré-Jev, N=13: top-20 **0,820515**, top-40 **0,897438**. Pela regra pré-registrada, cap 20 suficiente; ablação paga cap 40 não indicada.

## Development completo

| Par/view | Respostas | p95 baseline/candidato | Acertos pelo proxy de fatos |
|---|---:|---:|---:|
| Pointwise/batch, pool congelada v3 | 38 | 22,250 / 10,921 s | Consultar artefato v3 |
| Existing/Jev, pool congelada v5 | 38 | 25,078 / 12,859 s | 8/13 / 9/13 |
| Existing/Jev, ponta a ponta v5 | 38 | 24,390 / 13,750 s | 9/13 / 10/13 |
| Jev/Jev+gate, ponta a ponta v4 | 38 | 10,545 / 16,812 s | 10/13 / 8/13 |

p95 do pipeline `rag.ask`, nearest-rank, N=19 por braço, exclui auditoria de permutações. [Pointwise/batch](pointwise-batch-v3.json): pointwise aplicado 15/19 com três timeouts/um erro; batch 19/19, zero fallback. Recall@20 0,7949/0,8205. Prompts distintos são confundidor; não atribuir tudo a batching. Auditoria original/reversed/stable_rotation mudou ordem em 19/19 por modo, sem recomendar ensemble. Batch foi candidato selecionado em development, sem adoção.

[A/B v5](ab-corrected-existing-v5.json) tem **76/76 respostas**, ambos os views completos e rerank aplicado 19/19 por braço/view, sem fallback. `state_sha256` real nas chamadas Jev, evidência da #91. A/B v4 anterior tinha 19/19 rankings existing vazios pelo raciocínio consumir o limite de 200 tokens. Foi preservado como diagnóstico e substituído por coleta com nova identidade, nunca usado como prova de superioridade contra baseline funcional.

Correção demonstrada: transporte usa `max_tokens` no host oficial DeepSeek; no estágio `rerank`, `thinking.type=disabled`, conforme o [contrato oficial](https://api-docs.deepseek.com/api/create-chat-completion/). Mesmos prompt/modelo/candidatos/limite 200. Geração/reformulação mantêm seu comportamento. Configuração versiona `official-deepseek-rerank-nonthinking-v1`. Prova real anterior ao novo A/B mostrou ranking válido; o par corrigido confirma a aplicação.

[B/C v4](bc-batch-v4.json): **38/38 respostas**, rerank 19/19 nos dois braços, zero fallback. Gate 18 aplicações e um inconclusivo (confiança abaixo da política): 13 suficientes/3 esclarecimentos/2 insuficientes. Proxy de esclarecimento 3/4, abaixo de 0,80.

A pedido do responsável, o assistente comparou respostas reais e contexto retido: [A/B v5](assistant-ab-review-v5.json), núcleo esperado 11/13 existing e 12/13 Jev; esclarecimento 2/4 e 3/4; abstenção 2/2. [B/C](assistant-bc-review-v4.json), núcleo 12/13 ambos; esclarecimento 3/4 ambos; abstenção 2/2. Essas análises identificam omissões, cenário presumido e mapas de status conflitantes; não validam cada afirmação periférica nem são revisão humana independente. Em A/B v5, a referência de críticas foi retida em A mas omitida pelo gerador; faltou em B. As 38 respostas pointwise/batch têm [aprovação humana global](human-review-rerank-v3.json), sem rótulos humanos tipados.

## Gate congelado e confirmação independente

Calibração somente em development: [19 decisões reais](gate-calibration-v4.json), contra rótulos provisórios do assistente; confiança 0,50/margem 0,30 em ambas as decisões negativas. Preservou 12/12 suficientes, identificou 3/4 esclarecimentos e 2/3 insuficientes. [Política congelada](gate-policy-frozen-confirmation-v1.json) antes das novas chamadas, sem retuning após confirmação. Follow-ups não fornecem histórico separado ao gate; limitação preservada.

[12 casos novos](confirmation-v1.json): 8 respondíveis, 2 ambíguos, 2 sem evidência, propostos a partir de documentos e **aprovados pelo especialista antes da coleta**. Após correção solicitada, caso duvidoso de RPMXSVISITAFV substituído por chave CODCLI/CODUSUR/DATA da ERP_MXSVISITAFV, seção 7.104 do layout. Não se mudou/reindexou o corpus durante a confirmação; menções duvidosas do documento de rastros permanecem fora desse fato testado. Nenhum ID/pergunta histórica reutilizado. Mesma base documental; não tickets aleatórios nem amostra representativa do suporte. Comparações e política congeladas no commit `a7050b2`, código `6a2dabe`, antes da abertura.

| Confirmação/view, N=12 por braço | Respostas | p95 baseline/candidato | Fatos esperados corretos |
|---|---:|---:|---:|
| Existing/Jev, pool congelada | 24 | 9,938 / 7,046 s | 8/8 / 8/8 |
| Existing/Jev, ponta a ponta | 24 | 9,860 / 9,250 s | 8/8 / 8/8 |
| Jev/Jev+gate, ponta a ponta | 24 | 11,344 / 8,407 s | 8/8 / 8/8 |

[A/B confirmação](confirmation-ab-v1-results.json): 48/48 respostas. Existing aplicado 11/12 por view; uma seleção intencionalmente não acionada fora da zona prevista, sem fallback. Jev 12/12. [B/C confirmação](confirmation-bc-v1-results.json): 24/24 respostas, gate aplicado 12/12, nove suficientes/um esclarecimento/duas insuficiências; zero fallback. As respostas B de A/B e B/C são gerações distintas, não combinar como um único braço. Status nativo `incomplete` preservado por métricas/usage/variant_success: não significa coleta interrompida. Os views nativos têm status `complete` e zero resultados ausentes.

Diferença factual pareada observada A/B e B/C: oito acertos conjuntos, zero ganho/perda, **0 pontos percentuais**, abaixo de 5 exigidos. IC Wilson 95% individual de 8/8: **67,56%–100%**; não é intervalo da diferença pareada. N pequeno não demonstra equivalência populacional nem superioridade. Recall@20=1 nos oito casos em todos os braços; nDCG continua indisponível sem qrels graduados válidos.

[Análise documental do assistente](assistant-confirmation-review-v1.json) das 48 respostas ponta a ponta: núcleo esperado 8/8, esclarecimento 1/2 e abstenção qualificada 2/2 em todos os braços. Caso ambíguo de férias recebeu SQL abrangente antes de obter cenário; o gate o classificou `sufficient`. SQL não foi executado. Caso de desconto pediu contexto sem inventar percentual. **8/8 mede núcleo esperado, não qualidade integral de cada resposta.** Os proxies de esclarecimento/abstenção divergem da análise semântica e permanecem inalterados no artefato. Sinais lexicais de falsa ausência em frases condicionais (campanha/PIX) não comprovam erro crítico; gravidade exaustiva não aferida.

Revisão humana das 72 respostas de confirmação solicitada e pendente. [Registro de revisão](human-review-confirmation-v1.json). O material privado contém as 72 respostas, referências aprovadas e contexto efetivamente enviado; aprovação dos casos é distinta. Não há revisão humana registrada das novas 38 respostas A/B e 38 B/C de development; a análise do assistente está disponível. Não inventar revisão claim a claim.

**Decisões separadas:** reranker `rejected` pelo ganho zero/esclarecimento insuficiente; evidence gate `rejected` pelo esclarecimento 1/2 <0,80, sem melhora do núcleo factual. Gate isolado sobre existing não avaliado nem proposto para adoção. Métricas obrigatórias ausentes impediriam aprovação, mesmo com demais gates satisfeitos. Rejeição não declara todos os critérios aprovados.

## Custos, reprodução e validação

**4.201 chamadas HTTP** incluindo ingestão, tentativas, auditoria e ensaios; estimativa conhecida **US$ 1,340475138**, comprometido com reservas **US$ 2,005457016**, **575 chamadas com usage desconhecido**. Gasto faturado não verificado. [Livro v4](budget-calls-v4.json) acrescenta calls 3743–4201 e carrega o [livro v3](budget-calls-v3.json), que referencia v2. Não somar livros completos sobrepostos. Ratio de custo por pipeline indisponível: custo nativo incompleto/embeddings sem usage/cache compartilhado. Não assumir zero nem aprovar razão ≤1,20 por inferência do total agregado.

[ExperimentBudget](../../../../evaluation/experiment_budget.py) é opt-in, wrapper de `httpx.Client.send`, reserva antes do despacho, um processo por livro, sem lock entre processos. Só scripts privados o instalam. Credentials, conexão, respostas/contextos brutos ficam em `runtime/issue-93/`, ignorado pelo Git; não executar CLI pago supondo proteção automática. Perfis públicos são templates: preparação preenche snapshot/configuração e verifica sem calls; `dry_run` do avaliador não é execução offline.

Sequência privada executada: `run_development_v3.py prepare/rerank`; `run_development_v4.py prepare/ab`; `calibrate_gate.py prepare/run`; `run_expanded_v4.py bc`; `probe_existing_v5.py probe`; `run_development_v5.py prepare/ab`; `run_confirmation_v1.py prepare/ab/bc`. Perfis [A/B v5](ab-v5.profile.json), [confirmação A/B](confirmation-ab-v1.profile.json), [confirmação B/C](confirmation-bc-v1.profile.json). Resultados publicados são sanitizados, sem respostas ou conteúdo bruto. [Relatório histórico de 3.742 chamadas](report-development-legacy-3742.json) preservado; seu estado não é o atual.

Python 3.11 após última mudança de código: **420 testes, 17 skips, exit 0**, incluindo orçamento, transporte e classificação de falha operacional (`provider_error` não é abstenção correta). Quatro fixtures SQL passaram no banco descartável. [CI unit-tests/postgres-tests aprovada em a7050b2](https://github.com/vitoradriao/sabidao/actions/runs/36741117575); conferir checks do commit posterior de relatório. JSON, links, hashes, completude, encadeamento de custos e ausência de credenciais validados localmente. CodeRabbit não executou revisão por ser PR de rascunho.

## Pendências explícitas

Coleta A/B e B/C e decisão de rejeição estão realizadas; orçamento deixou de bloquear. Revisão humana das respostas ainda pendente conforme acima, assim como integração do PR. Base D0 da #96 = existing escolhida antes da abertura; D1 planejado como mesma base + grounding. **D0/D1 não foi calibrado ou pré-registrado como ensaio executável completo:** esse requisito compartilhado #53/#96 permanece pendente, e estes casos abertos não devem confirmar grounding após tuning. Não declarar #93/#53 integralmente concluídas. #20 reutiliza os achados; #91 recebe evidência real de hashes, ainda no PR. Grounding não é inferido de B/C.
