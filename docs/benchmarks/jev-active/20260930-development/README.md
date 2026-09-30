# Preparação do ensaio Jev — 30/09/2026

**Estado: incompleto.** Esta entrega registra preparação, ingestão isolada e diagnóstico anterior ao Jev. Pointwise/batch, A/B e B/C ainda não foram executados. Não há decisão de adoção, calibração do gate ou confirmação independente. A issue #93 permanece aberta.

O responsável autorizou nesta sessão US$ 1,25, o corpus elegível de MaxPedido e o development já revisado, delegou a escolha dos critérios iniciais e assumiu a revisão humana. O [protocolo](protocol.json) foi registrado antes das chamadas deste ensaio. Os critérios são iniciais: ganho absoluto de 5 pontos percentuais, nenhum erro crítico novo, p95 até 120 segundos e até `1,20 × p95(A) + 5 segundos`, fallback até 5% dos acionamentos e custo até `1,20 × custo(A)`. Esses valores não foram calibrados nem comprovados. Uma resposta adicional entre os 14 casos respondíveis representa 7,14 pontos; esse recorte não sustenta generalização estatística.

## Ambiente e identidade

A base é `a779124872c26984e641450b0dadb015e20323a3`, com #114/#95/#115 integradas. O banco novo `sabidao_jev93`, no contêiner persistente `sabidao-jev93-postgres`, usa PostgreSQL 16 e pgvector 0.8.6, exposto somente em `127.0.0.1:55493`. As 14 migrações publicadas foram aplicadas no banco vazio, na ordem do CI. A instalação operacional anterior não foi reindexada.

Foram ingeridos 21 documentos elegíveis: 1.772 seções e 1.887 chunks, sem falhas. Logística e backups continuam fora da ingestão. Feedback vazio; contextualização desativada. A identidade completa e os hashes constam em [report.json](report.json); o snapshot é `jev93-20260930-74628fa92ed7-e3b0c442`.

Os modelos configurados são `gemini-embedding-001`/1536 e `deepseek-flash`, com Jev `jev-1.13.0` previsto nos perfis. Nenhuma chamada ao gerador ou ao Jev ocorreu neste ensaio. `deepseek-flash` é um alias do provedor: sua versão efetiva ainda precisa ser registrada nas respostas de uma execução futura. O teste sintético de autenticação da sessão anterior não é evidência comparativa deste ensaio.

## Bloqueio demonstrado no development

As 20 consultas foram recuperadas antes do Jev. Todas as 14 referências dos casos respondíveis apontam para `MAXIMA_RAG_UNIFICADO.md`, ausente no corpus ativo. O arquivo histórico continua preservado em `docbkp/rag_unificado`; não foi ingerido junto com os documentos atuais. O avaliador exige correspondência de fonte, além das expressões de evidência. Os zeros do diagnóstico bruto, portanto, não medem uma falha de recuperação: cobertura/Recall 20/40 são **indisponíveis para a decisão** enquanto os vínculos forem incompatíveis.

Uma busca local nos chunks encontrou todas as expressões antigas juntas para 6 referências. As outras 8 exigem revisão e novas âncoras; candidatos parciais não comprovam equivalência semântica. Foi preparado material privado com perguntas, fatos esperados, fontes candidatas e trechos para vitoradriao revisar. Nenhum vínculo novo recebeu aprovação automática. O dataset original e os dez casos históricos de holdout não foram alterados; o holdout não foi executado nem mapeado nesta entrega.

Não se aplicou a regra paga de top-40: ela exige cobertura revisada válida, ganho mínimo de 5 pontos em relação ao top-20 e orçamento para o par completo. Os preparadores técnicos de pointwise/batch e A/B aceitaram o snapshot, mas isso não valida os denominadores. B/C permanece bloqueado também pela ausência de política calibrada do gate.

## Orçamento e chamadas

O [livro sanitizado](budget-calls.json) registra 402 chamadas HTTP de embeddings: 382 da ingestão e 20 das consultas. Todas responderam HTTP 200, sem usage. A reserva conservadora retida é **US$ 0,6005592**, restando **US$ 0,6494408** do teto autorizado. O custo total estimado e o gasto faturado são desconhecidos; ausência de usage não significa custo zero. Esse saldo não garante que todas as comparações e a confirmação caibam no orçamento.

O wrapper [ExperimentBudget](../../../../evaluation/experiment_budget.py) foi instalado somente nos processos locais do ensaio, interceptando `httpx.Client.send`. Antes de cada despacho, reserva bytes UTF-8 do corpo + 1.024 tokens de entrada e a saída máxima explícita, com tarifas pagas de pico registradas no protocolo. Uso conhecido libera a reserva excedente; falhas, timeout e uso ausente retêm a reserva. O livro persiste chamadas admitidas antes do envio, incluindo as que permaneçam pendentes. Um excesso observado no limite de tokens interrompe novos despachos.

Essa é uma estimativa conservadora para os endpoints e clientes síncronos usados aqui, não verificação da fatura nem garantia contratual sobre tokenização do provedor. Usar um único processo por livro; não há bloqueio entre processos. O wrapper não é instalado automaticamente no bot nem no comando público do avaliador. Não executar ensaios pagos pelo CLI desprotegido supondo que o teto esteja aplicado. Prompts, respostas, credenciais, URLs completas e fontes brutas não entram no livro público.

## Perfis, execução e validação

Os perfis [pointwise/batch](pointwise-batch.profile.json), [A/B](ab.profile.json) e [B/C](bc.profile.json) são templates de development. Antes de qualquer retomada, registrar o novo dataset revisado, repetir a preparação sem chamadas e preencher `comparison.snapshot_id` com a identidade conferida do banco. Não usar `null` como identidade real. A seleção do modo e as políticas serão congeladas depois do development e antes de abrir um novo conjunto independente.

A execução local ocorreu em quatro passos: ingestão protegida; preparação sem chamadas; recuperação protegida dos 20 casos; mapeamento local de referências sem chamadas. Os scripts e artefatos privados ficaram em `runtime/issue-93/`, ignorado pelo Git. Eles carregam separadamente o `.env` autorizado e a conexão do novo banco. O relatório identifica os hashes do protocolo e do wrapper efetivamente usados; não se modificou o protocolo após observar o diagnóstico.

Validação Python 3.11: suíte offline com **412 testes, 17 skips, exit 0**, incluindo os oito testes do orçamento. A suíte com PostgreSQL executou 412 testes, sem skips, com uma falha de ambiente: o teste que inicia `psql` não encontrou esse executável no Windows. As quatro fixtures SQL — inclusive a fixture de escopo de feedback desse teste — passaram pelo `psql` do contêiner, em `sabidao_jev93_tests`, separado do corpus do ensaio. Não se declara que a suíte local com banco passou integralmente; o CI fornece o cliente PostgreSQL próprio.

## Trabalho restante

1. Revisar os 14 vínculos de development, preservando a identidade original e registrando a nova identidade e divergências reais.
2. Revalidar cobertura 20/40 e executar pointwise/batch, auditoria de ordem e A/B dentro do saldo autorizado, contando retries e uso desconhecido.
3. Calibrar o gate somente no development e executar B/C; adoção isolada do gate exige seu par próprio.
4. Revisar respostas reais e congelar modelos efetivos, prompts, políticas e critérios antes da confirmação.
5. Obter novo conjunto independente revisado e pré-registrar A/B/C e D0/D1 antes de sua abertura. A execução de grounding da #96 é uma entrega separada.

Sem essas etapas, reranker e gate ficam `incomplete`. Não se aprovam defaults, implantação ou encerramento da #91 por esta preparação; a evidência real `state_sha256` no A/B continua pendente.
