# Ensaio Jev — development de 30/09/2026

**Estado: incompleto.** A preparação, a revisão das fontes e a recuperação dos casos ocorreram. Uma execução do modo pointwise produziu 19 resultados, mas foi interrompida por divergência no limite de saída do DeepSeek antes do braço batch. Não há par completo pointwise/batch, A/B ou B/C; nenhuma adoção, calibração ou confirmação é declarada.

O responsável autorizou US$ 1,25, o corpus MaxPedido elegível e o development revisado, delegou os critérios iniciais e assumiu a revisão humana. Os critérios são iniciais: ganho absoluto de 5 pontos percentuais, nenhum erro crítico novo, p95 até 120 segundos e até `1,20 × p95(A) + 5 segundos`, fallback até 5% dos acionamentos e custo até `1,20 × custo(A)`. Não são limiares calibrados. Uma resposta adicional entre 13 respondíveis representa 7,69 pontos; o recorte não sustenta generalização estatística.

## Corpus e revisão real

Banco persistente novo: `sabidao_jev93`, contêiner `sabidao-jev93-postgres`, PostgreSQL 16/pgvector 0.8.6, somente `127.0.0.1:55493`. As 14 migrações publicadas foram aplicadas no banco vazio, na ordem do CI. Feedback vazio, contextualização desativada; Logística e backups fora da ingestão. A instalação operacional anterior não foi reindexada.

O snapshot inicial tinha 21 documentos, 1.772 seções e 1.887 chunks. Todas as 14 referências respondíveis do dataset histórico apontavam para `MAXIMA_RAG_UNIFICADO.md`, ausente do corpus ativo. O arquivo histórico segue preservado em `docbkp/rag_unificado`. Os zeros do primeiro diagnóstico, portanto, não medem falha de recuperação; suas métricas são indisponíveis para a decisão. A preparação e os hashes originais estão em [report-preparation.json](report-preparation.json) e [protocol.json](protocol.json).

Vitoradriao revisou o material de fontes nesta sessão: aprovou os outros 12 vínculos, rejeitou a consulta `ORDER BY 3 DESC` e aprovou explicitamente a referência do follow-up de erro na seção 8.2 `StatusPedidos (PUT)` de `01-LAYOUT-INTEGRACAO.md`, com status 5. A consulta rejeitada foi removida da documentação ativa, no arquivo SQL existente. Não se utilizaram consultas de indenização, títulos com status 51 ou pedidos processados do dia como substitutas.

O caso SQL sem referência válida ficou excluído de [development-v2.json](development-v2.json). Os fatos e comportamentos dos outros 19 casos foram preservados: 13 respondíveis, 4 ambíguos e 2 sem evidência. Dataset e dez casos de holdout históricos não foram alterados ou executados. Não se mudou o gabarito SQL para aprovar uma consulta diferente. Revisão de fontes não equivale a revisão das respostas reais ou de suficiência dos contextos.

A atualização autorizada ocorreu somente no banco isolado: 21 documentos, 1.771 seções e 1.886 chunks, snapshot `jev93-20260930-5a07b8f135c8-e3b0c442`. Cobertura média pré-Jev: top-20 **0,820515** e top-40 **0,897438**, em 13 casos com referências válidas. Pela regra registrada, manter cap 20; não executar a ablação paga cap 40 quando top-20 já atende 0,80. Identidades e resultados estão em [report.json](report.json).

## Interrupções e correção demonstrada

A primeira tentativa do script privado perdeu uma resposta ao salvar UUID/datetime. A serialização foi corrigida; aquela resposta não entra nas comparações e suas chamadas continuam contabilizadas. A tentativa seguinte registrou os 19 resultados pointwise e as auditorias de ordem, antes de parar. A latência de auditoria é separada da latência do pipeline normal.

Na chamada `http-1990`, DeepSeek informou 371 tokens de saída para um limite solicitado de 200. O wrapper interrompeu novos despachos, embora o teto monetário ainda não estivesse esgotado. O cliente enviava `max_completion_tokens` e só tentava `max_tokens` após erro HTTP 400. A chamada respondeu HTTP 200. O [contrato oficial](https://api-docs.deepseek.com/api/create-chat-completion/) define `max_tokens` para esse endpoint.

Foi feita correção pequena no transporte: em `api.deepseek.com`, enviar `max_tokens` desde a primeira chamada. Outros hosts preservam `max_completion_tokens` e seu fallback por HTTP 400. Modelos, provider, limites numéricos, prompts, strict e configuração de geração/embeddings são preservados. Quatro testes sem rede verificam o contrato, o host exato e os caminhos de fallback/erro. Essa correção é parte da entrega pendente do PR; não é implantação operacional nem implementação completa do adapter da #19.

A execução anterior está separada em [pointwise-v2-interrupted.json](pointwise-v2-interrupted.json) e [budget-calls-v2.json](budget-calls-v2.json). Seus resultados não serão misturados com uma execução posterior à correção. [protocol-v3.json](protocol-v3.json) registra a nova identidade antes de novas chamadas. A retomada deve carregar integralmente custos/reservas anteriores no mesmo teto, mantendo o livro bloqueado original e a chamada pendente sem tratá-la como gratuita.

## Orçamento e reprodutibilidade

Até a interrupção: **1.990 chamadas HTTP**, custo conhecido estimado de **US$ 0,113891802**, reserva total conservadora de **US$ 0,759262446** e saldo conservador de **US$ 0,490737554**. Há 467 chamadas com uso desconhecido, incluindo uma pendente. A reserva não é gasto faturado confirmado; o custo total real permanece desconhecido. Não se garante que o saldo cubra todos os pares e a confirmação. O [livro da preparação inicial](budget-calls.json) permanece histórico.

O wrapper [ExperimentBudget](../../../../evaluation/experiment_budget.py) foi instalado somente nos processos locais, interceptando `httpx.Client.send`. Reserva bytes UTF-8 do corpo + 1.024 tokens de entrada e saída máxima explícita antes do envio, pelas tarifas pagas de pico registradas. Uso conhecido libera o excedente; erros, timeout e ausência de usage conservam a reserva. Excesso observado no limite de tokens interrompe novos despachos. São estimativas conservadoras, não comprovação da fatura ou garantia contratual de tokenização.

Usar um processo por livro: não há bloqueio entre processos. O wrapper não é instalado automaticamente no bot ou CLI do avaliador. Não iniciar execução paga pelo CLI desprotegido supondo que o teto esteja aplicado. Scripts e artefatos brutos ficam em `runtime/issue-93/`, ignorado pelo Git; carregam separadamente o `.env` autorizado e a conexão do banco isolado. Prompts, respostas, credenciais e fontes brutas não entram nos livros públicos.

Os perfis versionados v1/v2/v3 são templates de development; `comparison.snapshot_id=null` não é identidade real. A preparação local preenche esse campo após conferir o banco e não faz chamadas. Os pares v3 usam identidades novas, o mesmo corpus entre braços e o campo de saída corrigido. A alias `deepseek-flash` é mutável; registrar modelo efetivo e limites de versão quando houver respostas. Jev configurado: `jev-1.13.0`; embeddings: `gemini-embedding-001`, 1536 dimensões.

## Validação e pendências

Após a correção, suíte offline Python 3.11: **416 testes, 17 skips, exit 0**. Os oito testes do wrapper e quatro do transporte passaram. A correção documental passou nos oito testes de documentos canônicos. Na preparação anterior, suíte local com banco: 412 testes, uma falha de ambiente porque o Windows não possui `psql`; as quatro fixtures SQL, inclusive a afetada de feedback, passaram pelo cliente do contêiner no banco separado `sabidao_jev93_tests`. Os jobs [unit-tests e postgres-tests do CI no commit a2ae41f passaram](https://github.com/vitoradriao/sabidao/actions/runs/36715971055); isso não declara CI da correção posterior.

Restam: comparação completa pointwise/batch, auditoria e seleção de modo no development, A/B, revisão das respostas/contextos, calibração e B/C, congelamento e novo conjunto independente revisado para confirmação. Os candidatos de política estão em [gate-calibration-plan.json](gate-calibration-plan.json); nenhum candidato foi apresentado como calibrado. A categoria da pergunta sozinha não comprova que seu contexto recuperado seja suficiente.

Reranker e gate ficam `incomplete`. Gate isolado exige seu par próprio. A #91 aguarda evidência no A/B real; a #20 reutiliza os resultados, e a #53 acompanha dados/revisão/identidade. A execução de grounding da #96 é separada. Defaults e deploy seguem decisões distintas.
