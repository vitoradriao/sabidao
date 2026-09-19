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

Use Python 3.11, como na imagem Docker e no instalador Windows. `requirements.in` lista as dependências diretas de produção; `requirements.txt` fixa as versões diretas e transitivas usadas pelo bot. `requirements-dev.in` acrescenta a ferramenta de atualização dos locks, e `requirements-dev.txt` fixa o ambiente de desenvolvimento e CI. O Docker e o instalador continuam instalando `requirements.txt`.

Em um ambiente virtual limpo, instale as dependências de desenvolvimento e execute a suíte publicada, baseada em `unittest`:

```sh
python -m pip install -r requirements-dev.txt
python -m pip check
python -m unittest discover -s tests -p "test_*.py"
```

Para atualizar as versões, edite os arquivos `.in` e gere novamente os dois arquivos `.txt` em Python 3.11:

```sh
python -m pip install pip-tools==7.5.3
pip-compile --no-strip-extras --output-file=requirements.txt requirements.in
pip-compile --no-strip-extras --constraint=requirements.txt --output-file=requirements-dev.txt requirements-dev.in
```

Revise as versões alteradas e valide uma instalação em ambiente limpo antes de enviar o PR. O workflow de CI roda a suíte sem credenciais de IA e, em um job separado, aplica o esquema SQL em PostgreSQL/pgvector e executa os testes de integração habilitados por `RUN_DB_INTEGRATION_TESTS=1` e a fixture de busca híbrida.

A [avaliação das respostas](evaluation/README.md) é um fluxo separado: utiliza os serviços configurados de banco e IA. A opção `--dry-run` não elimina essas chamadas; ela evita a persistência dos resultados da avaliação no banco e ainda gera um relatório local.

Para mudanças apenas na documentação, confira os links, os comandos, as referências a arquivos e a correspondência com o código publicado. Revise a aparência dos diagramas e imagens quando houver mudanças visuais.

## Preparar uma alteração

Faça alterações focadas, preserve trabalhos não relacionados e acompanhe o padrão do código existente. Revise redundâncias, consultas desnecessárias e abstrações que não ajudam a resolver o problema.

Descreva o problema, o resultado e a validação realizada. Em mudanças de esquema ou recuperação de documentos, explique a ordem de aplicação e como retornar à configuração anterior.
