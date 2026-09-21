# Contrato documental canônico v1

Este diretório especifica o formato editorial que documentos canônicos do
Sabidão devem seguir. A versão `1.0.0` é uma especificação: ela ainda não muda
a ingestão, o banco, a busca nem os arquivos em `documentos/`.

Os artefatos normativos são:

- `document.schema.json`: front matter de um documento Markdown;
- `manifest.schema.json`: inventário externo do corpus e de sua transição;
- `vocabulary.json`: vocabulário mínimo usado somente pelo template e pelas
  fixtures; a taxonomia definitiva permanece na issue #31;
- `template.md`: ponto de partida fora do corpus ativo;
- `fixtures/`: documentos Markdown e manifestos sintéticos usados na validação
  offline, sem integrar o corpus ativo.

Ao copiar `template.md`, gere um UUID novo para `document_id`; o UUID presente
no arquivo é apenas um valor sintético validável e não pode identificar conteúdo
real.

## Representação e codificação

Um documento canônico é um arquivo Markdown codificado em UTF-8 estrito, sem
BOM, com final de linha LF ou CRLF. Ele começa na primeira linha com `---`,
contém um único documento YAML e termina o front matter com outra linha `---`.
Depois do front matter há exatamente um H1, cujo texto é igual a `title`.

O YAML deve ser carregado em modo seguro. Aliases, anchors, tags específicas de
linguagem e chaves duplicadas são inválidos. O carregador não pode corrigir,
mesclar ou escolher silenciosamente uma chave repetida. Campos desconhecidos
são rejeitados pelos schemas (`additionalProperties: false`). O corpo técnico
permanece no Markdown; não se copiam procedimentos, tabelas ou SQL para YAML.

Cada heading H2–H6 usa uma chave editorial explícita e única:

```markdown
## Preparar o ambiente {#preparar-ambiente}
```

A chave segue `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`, não deriva de título, nome do
arquivo, número da linha ou ordem. Alterar o texto do heading não altera sua
chave. O H1 não recebe chave de seção. Headings dentro de code fences não são
seções. Todo H2–H6 do corpo deve ter uma entrada homônima em `sections`, e toda
entrada de `sections` deve existir uma única vez no corpo.

## Campos do documento

| Campo | Regra v1 |
| --- | --- |
| `schema_version` | Obrigatório e exatamente `1.0.0`. É a versão do contrato, não a revisão do conteúdo. |
| `document_id` | UUID editorial imutável e globalmente único. Sobrevive a rename e não deriva do caminho ou título. |
| `revision` | Inteiro iniciado em 1 e incrementado quando corpo ou metadado semântico muda. O mesmo ID/revisão com conteúdo diferente é conflito. |
| `title` / `language` | Título não vazio e idioma BCP 47 simples, como `pt-BR`. O H1 deve coincidir com `title`. |
| `doc_type` | Sempre `md`, preservando a semântica dos filtros atuais de formato. |
| `semantic_type` | Um de `guide`, `procedure`, `catalog`, `data_dictionary`, `troubleshooting` ou `sql_reference`. |
| `products` | Códigos do registro taxonômico. `unknown` significa produto realmente desconhecido; lista vazia é inválida. |
| `taxonomy` | Versão do registro, módulo primário, módulos secundários e modo de resposta. Citar uma tabela não muda o módulo por si só. |
| `aliases` | Nomes editoriais ou caminhos anteriores. Não são identidades e não resolvem splits. |
| `sources` | Fontes editoriais. Cada `source_id` é único no documento; `null` declara dado desconhecido nos campos que o schema permite. |
| `applicability` | Validade de produto/ERP/tempo informada pela fonte. `status: unknown` exige listas vazias e datas nulas; não significa “todas as versões”. |
| `review` | Estado da revisão humana. `reviewed` exige revisor e instante; `pending` exige ambos nulos. Não representa validade do produto. |
| `sections` | Metadados editoriais de H2–H6, indexados pela chave estável usada no corpo. |

`null` só é aceito onde o schema o declara. Campo obrigatório ausente é erro.
`unknown` é um valor de vocabulário explícito, não substitui `null` e não pode
ser inventado em campos livres. Versões, campos e enums desconhecidos falham de
forma fechada até que o contrato suportado seja atualizado.

### Tipos semânticos e corpo

O tipo escolhe um perfil de autoria, sem transportar o conteúdo para YAML:

- `procedure`: pré-requisitos, passos, resultado e exceções;
- `catalog`: identificador, significado, valores, escopo e relação entre itens;
- `data_dictionary`: tabela, campo, tipo, chave e significado;
- `troubleshooting`: sintoma, evidência, causa confirmada e ação;
- `sql_reference`: finalidade, dialeto, SQL cercado por fence e cuidados;
- `guide`: explicação orientada a uma tarefa que não se encaixa nos perfis
  anteriores.

### Fontes e localizadores

Fontes de documento têm `kind`, localizador, versão verificável e SHA-256 quando
disponível. Origem não identificada usa `kind: unknown` e campos desconhecidos
nulos, com uma nota factual. Uma referência de seção aponta para um `source_id`
declarado e usa um destes localizadores:

- `line_range` ou `page_range`, com extremos inclusivos e `start <= end`;
- `table`, `figure` ou `url_fragment`, com valor não vazio;
- `unknown`, com motivo não vazio.

Uma imagem ausente, texto ilegível ou associação incerta deve usar uma lacuna
explícita; o autor não reconstrói o conteúdo por suposição.

### Herança e metadados derivados

`products`, `taxonomy.primary_module`, `taxonomy.secondary_modules` e
`taxonomy.answer_mode` são os valores editoriais do documento. Uma seção herda
esses valores. `classification_override` substitui apenas as propriedades nele
presentes; `null` significa herança integral. Override editorial vence qualquer
heurística futura.

`entities` registra apenas entidades editoriais confirmadas. Entidades extraídas,
classificações heurísticas, scores, caminhos de heading, contagem de tokens e
resumos gerados são metadados derivados e não entram neste front matter. Uma
implementação futura deve armazená-los separadamente com a versão do extrator.

## Manifesto externo

O manifesto fica fora de `documentos/` e é a fonte explícita de seleção durante
a transição. Cada caminho relativo POSIX aparece uma vez. `format` distingue
`legacy` e `canonical`; entradas canônicas exigem `document_id`, enquanto legado
pode usar `null`. `ingestion` determina inclusão ou exclusão. Toda exclusão traz
decisor, instante e justificativa, sem usar o nome do diretório como decisão
editorial implícita.

Uma entrada `superseded` é excluída e aponta para um ou mais sucessores
canônicos existentes no mesmo manifesto. `section_keys` pode restringir a parte
coberta pelo sucessor; lista vazia significa o documento inteiro. Não se escolhe
um sucessor arbitrariamente. `batch_id` sempre resolve para `batches` e permite
promoção e rollback por lote.

Além do JSON Schema, a validação semântica rejeita:

- caminhos, IDs de lote, `source_id` ou `document_id` duplicados;
- referências de fonte, lote ou sucessor inexistentes;
- localizador cujo fim precede o início;
- códigos fora da versão de vocabulário declarada;
- `document_id` do front matter diferente do manifesto;
- colisão de uma chave de seção no corpo.

## Identidades e evolução

São identidades distintas e não intercambiáveis:

1. `schema_version`: contrato dos metadados;
2. `document_id` + `revision`: identidade e revisão editorial;
3. versões de taxonomia, parser e renderização: interpretação/projeção;
4. `processing_hash`: fingerprint da projeção processada;
5. identidade global de embeddings: provider, modelo, dimensão e
   pré-processamento.

`processing_hash`, versão do parser/renderizador e identidade de embeddings não
são campos editoriais do documento. O histórico editorial fica no Git; a revisão
ativa é uma projeção, não um log de eventos.

Na linha v1, patch corrige texto ou restrições sem mudar instâncias válidas;
minor pode adicionar capacidade opcional mantendo compatibilidade; mudança que
altera significado, obrigatoriedade ou remove valores exige major. O validador
aceita somente versões conhecidas e nunca presume compatibilidade futura.

## Validação das fixtures

As fixtures são validadas sem banco, LLM, embeddings ou acesso à rede:

```powershell
py -m unittest tests.test_canonical_document_contract
```

Esse teste é uma verificação do contrato e não é o parser de ingestão previsto
na próxima etapa da issue #32. Ele confere o front matter e o corpo das fixtures,
incluindo H1, chaves H2–H6, correspondência de IDs entre documento e manifesto e
referências de sucessão. Nenhum arquivo em `documentos/` é lido ou alterado.
