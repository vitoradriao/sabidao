# Como contribuir com o Sabidão

Consulte os [primeiros passos](docs/primeiros-passos.md), a [arquitetura](docs/arquitetura.md) e as [orientações do projeto](AGENTS.md).

Use **português** na documentação, nas explicações e nas mensagens de alteração. Preserve os nomes de arquivos, comandos, variáveis e APIs quando a tradução prejudicar a compatibilidade.

## Organização

- Mantenha os pontos de entrada Python na raiz: os inicializadores e serviços Docker dependem desses caminhos.
- Coloque guias de manutenção em `docs/` e inclua seus links no [índice](docs/README.md).
- Reserve `documentos/` para as fontes consultadas pelo assistente.
- Coloque ferramentas em `scripts/`, alterações de banco em `sql/` e testes em `tests/`.
- Mantenha cenários revisados em `evaluation/datasets/`; relatórios gerados ficam em `evaluation/reports/`.
- Não inclua credenciais, ambientes virtuais, logs, arquivos temporários ou cópias de segurança nos commits.

As regras de exclusão do Git não removem arquivos já versionados. Confira o conteúdo da alteração antes de publicar, inclusive quando ele estiver dentro de uma pasta ignorada.

## Validação

Com as dependências instaladas e o ambiente virtual ativo, execute a suíte publicada, baseada em `unittest`:

```sh
python -m unittest discover -s tests -p "test_*.py"
```

A [avaliação das respostas](evaluation/README.md) é um fluxo separado: utiliza os serviços configurados de banco e IA. A opção `--dry-run` não elimina essas chamadas; ela evita a persistência dos resultados da avaliação no banco e ainda gera um relatório local.

Para mudanças apenas na documentação, confira os links, os comandos, as referências a arquivos e a correspondência com o código publicado. Revise a aparência dos diagramas e imagens quando houver mudanças visuais.

## Preparar uma alteração

Faça alterações focadas, preserve trabalhos não relacionados e acompanhe o padrão do código existente. Revise redundâncias, consultas desnecessárias e abstrações que não ajudam a resolver o problema.

Descreva o problema, o resultado e a validação realizada. Em mudanças de esquema ou recuperação de documentos, explique a ordem de aplicação e como retornar à configuração anterior.
