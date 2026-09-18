# Sabidão — orientações do projeto

## Contexto e idioma

Este repositório contém um assistente de suporte em Python para Discord, com PostgreSQL e pgvector.

- Consulte este arquivo antes de tomar decisões sobre o código.
- Use português na documentação, nas respostas ao usuário e nas descrições de alterações.
- Preserve identificadores, comandos, APIs e nomes de arquivos quando necessário para manter a compatibilidade.
- Descreva o comportamento da versão do repositório em que está trabalhando; não documente recursos existentes apenas em alterações locais ainda não publicadas.

## Qualidade e revisão

Antes de apresentar ou publicar código, revise e corrija internamente:

1. **Código idiomático:** siga as convenções do Python e dos componentes utilizados.
2. **Ausência de redundância:** elimine código morto, repetições e etapas desnecessárias.
3. **Eficiência:** evite consultas repetidas, iterações desnecessárias e operações individuais quando houver uma solução adequada em lote.
4. **Clareza:** não acrescente validações defensivas sem necessidade.
5. **Consistência:** mantenha os padrões de nomes e organização do projeto.
6. **Escopo mínimo:** implemente somente o necessário, sem abstrações prematuras.

## Cuidados com o repositório

- Preserve alterações do usuário que não façam parte da tarefa.
- Não publique credenciais, dados brutos de clientes ou artefatos temporários.
- Mantenha os caminhos usados pelos inicializadores e pelo Docker.
- Execute verificações proporcionais à mudança e informe limitações reais.
