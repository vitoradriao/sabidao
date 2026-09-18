# Documentação do Sabidão

[Voltar à apresentação](../README.md)

Encontre o procedimento pelo que você precisa fazer. Estes guias descrevem o projeto; os documentos de suporte consultados pelo bot ficam em `documentos/`.

| Quero… | Guia |
| --- | --- |
| Preparar uma máquina e iniciar o bot | [Primeiros passos](primeiros-passos.md) |
| Entender os componentes e o fluxo de dados | [Arquitetura](arquitetura.md) |
| Executar os serviços em contêineres | [Docker](../GUIA_DOCKER.md) |
| Indexar ou atualizar documentos | [Operação da base](operacao.md) |
| Preparar o banco ou mudar a dimensão dos vetores | [SQL e migrações](../sql/README.md) |
| Medir a qualidade das respostas | [Avaliação](../evaluation/README.md) |
| Alterar e validar o projeto | [Como contribuir](../CONTRIBUTING.md) |

## Referências

- [Modelo de configuração](../.env.example): variáveis disponíveis para o ambiente.
- [Notas de retorno ao Claude](../ROLLBACK_PARA_CLAUDE.md): procedimento histórico; confira a compatibilidade com a configuração atual antes de usá-lo.
- [Orientações para assistentes de programação](../AGENTS.md): idioma e critérios de qualidade do projeto.

O nome do guia Docker na raiz foi mantido para preservar os links existentes.
