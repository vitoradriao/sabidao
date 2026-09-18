# Microsoft Teams

[Documentação](docs/README.md) / Microsoft Teams

O bot do Teams compartilha a base de conhecimento e a lógica de consulta do Discord. A operação de produção fica no servidor Linux; a máquina Windows é utilizada para edição, verificações locais e preparação das atualizações.

## Configurar o aplicativo

Use `.env.example` como referência.

| Finalidade | Variáveis |
| --- | --- |
| Identificação no Azure | `TEAMS_APP_ID`, `TEAMS_APP_PASSWORD`, `TEAMS_TENANT_ID` |
| Execução | `TEAMS_ADMIN_IDS`, `TEAMS_PORT` |
| Nome do aplicativo | `TEAMS_MANIFEST_SHORT_NAME`, `TEAMS_MANIFEST_FULL_NAME` |
| Descrições | `TEAMS_MANIFEST_SHORT_DESCRIPTION`, `TEAMS_MANIFEST_FULL_DESCRIPTION` |
| Desenvolvedor | `TEAMS_MANIFEST_DEVELOPER_NAME`, `TEAMS_MANIFEST_DEVELOPER_WEBSITE_URL` |
| Políticas | `TEAMS_MANIFEST_DEVELOPER_PRIVACY_URL`, `TEAMS_MANIFEST_DEVELOPER_TERMS_URL` |
| Aparência | `TEAMS_MANIFEST_ACCENT_COLOR` no formato `#RRGGBB` |

Configure também o banco e os serviços de IA conforme os [primeiros passos](docs/primeiros-passos.md).

O `TEAMS_APP_ID` é utilizado pelo bot e pelos campos `manifest.id` e `bots[].botId` do manifesto gerado.

## Gerar o pacote

No Windows, após instalar as dependências:

```powershell
.\gerar_manifest_teams.bat
```

Ou com o ambiente Python ativo:

```sh
python scripts/build_teams_package.py
```

Com Docker:

```sh
docker compose --profile tools run --rm teams_package
```

A fonte versionada é `teams_manifest/manifest.template.json`. Os arquivos para publicação são gerados em:

- `teams_manifest/build/manifest.json`
- `teams_manifest/build/bot-azure.zip`

Use esse pacote gerado para publicar. Arquivos históricos diretamente em `teams_manifest/` não são a saída desse fluxo.

## Executar no servidor

Com o ambiente Python do servidor ativo:

```sh
python scripts/check_teams_runtime.py
python bot_teams.py
```

A verificação inicial confere as dependências instaladas. Para uma implantação em contêiner:

```sh
docker compose up -d teams_bot
```

O inicializador Windows `iniciar_teams.bat` continua disponível para uma execução local configurada intencionalmente. Na operação normal, as atualizações chegam ao servidor Linux por `git pull` e são aplicadas com reinicialização do processo ou reconstrução do contêiner.

## Conectar o Teams

1. Publique um endereço HTTPS no servidor Linux ou em seu proxy reverso, encaminhando ao serviço Teams.
2. No Azure Bot, configure o endpoint de mensagens como `https://<host-publico>/api/messages`.
3. Importe `teams_manifest/build/bot-azure.zip` no Teams.
4. Confira `/api/health` e envie uma pergunta para validar a consulta e a geração da resposta.

Esse ambiente utiliza o endpoint público do servidor Linux. Não use um túnel ngrok nessa implantação. Se o HTTPS estiver publicado em uma porta diferente da padrão, inclua essa porta pública na URL.

Para consultar logs, portas e persistência, veja o [guia Docker](GUIA_DOCKER.md).
