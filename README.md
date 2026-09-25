# Workana Telegram Bot

Monitora projetos novos na Workana (buscas por "aplicativo" e "app" das últimas 24h, em
português, clientes da América do Sul, Central e Caribe — ver `WORKANA_URLS` no `bot.py`),
filtra por palavras-chave no título e avisa no Telegram. Opcionalmente, lê a descrição de cada
vaga e gera uma proposta com o Claude (ver [Propostas com o Claude](#propostas-com-o-claude)).

## Execução no GitHub Actions (principal)

O workflow `.github/workflows/monitor.yml` é disparado a cada 15 minutos por um Cloudflare
Worker (pasta `scheduler/`), porque o agendamento nativo do GitHub atrasa ou pula execuções.
Como reserva, o próprio workflow roda de hora em hora (minuto 41), caso o Worker pare.
Também pode ser disparado manualmente na aba Actions.

Configuração no repositório (Settings → Secrets and variables → Actions):
- Secrets: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (e, para as propostas, `CLAUDE_CODE_OAUTH_TOKEN` e `PROMPT_KEY`)
- Variable: `KEYWORDS` (ex: `aplicativo,app`)

Para trocar as palavras-chave:

```
gh variable set KEYWORDS --body "aplicativo,app,flutter"
```

A cada execução o workflow faz commit do `seen.json` e do `pending.json`. Rode `git pull` antes de editar localmente.

Não ative o launchd junto com o GitHub Actions: cada um tem seu próprio `seen.json`
e você receberia avisos repetidos.

### Agendamento com Cloudflare Worker

O Worker dispara também o [telegram-vagas-gupy-bot](https://github.com/leandrorochaadm/telegram-vagas-gupy-bot).
Os horários de cada bot ficam na lista `JOBS` em `scheduler/src/index.js`, escritos em horário
de Brasília (o cron da Cloudflare só roda a cada 15 min em UTC; o código decide quem dispara).

1. Crie um token no GitHub (Settings → Developer settings → Fine-grained tokens) com acesso
   aos repositórios da lista `JOBS` e permissão **Actions: Read and write**.
2. Dentro de `scheduler/`:
   ```
   npm install
   npx wrangler secret put GITHUB_TOKEN   # cole o token quando pedir
   npm run deploy
   ```
3. Logs: `npx wrangler tail` (dentro de `scheduler/`).

O token expira (no máximo em 1 ano). Ao renovar, rode `npx wrangler secret put GITHUB_TOKEN` de novo.

## Propostas com o Claude

Para cada vaga que bate com as palavras-chave, o bot abre a página, lê a descrição e pede ao
Claude (`claude-opus-5-5`) uma proposta. Chegam duas mensagens: uma com título, link, preço,
prazo, piso de negociação e observações; outra só com o texto da proposta, para copiar.

- A vaga só é avisada junto com a proposta. Se passar do limite de `MAX_PROPOSALS_PER_RUN` (5)
  propostas ou do tempo da execução, ou se a geração falhar, ela fica em `pending.json` e é
  tentada de novo na próxima execução, mesmo que já tenha saído da lista da Workana.
- Depois de `MAX_PROPOSAL_ATTEMPTS` (3) falhas, a vaga chega sem proposta, com um aviso.
- Sem `CLAUDE_CODE_OAUTH_TOKEN`, sem `PROMPT_KEY` ou sem o comando `claude` no `PATH`, o bot
  envia só o aviso da vaga, sem proposta.

A proposta é gerada pelo Claude Code em modo não interativo (`claude -p`), cobrado na
assinatura Max, não na API. Cada proposta consome a mesma cota de uso do plano. Para gerar o
token (vale cerca de 1 ano):

```
claude setup-token
gh secret set CLAUDE_CODE_OAUTH_TOKEN
```

Quando o token vencer, o log do Actions mostra `Falha na proposta de …: claude saiu com
código 1 (… api_error_status=401)`. É só repetir os dois comandos acima.

O workflow instala uma versão fixa do Claude Code (`@anthropic-ai/claude-code@2.1.282` no
`monitor.yml`), porque o bot depende das flags e do JSON de saída dessa versão. Para atualizar,
troque a versão ali e dispare o workflow manualmente para conferir se a proposta chega.

O secret `ANTHROPIC_API_KEY` não é mais usado e pode ser apagado:
`gh secret delete ANTHROPIC_API_KEY`.

O prompt tem regras de preço privadas e o repositório é público, por isso só a versão
criptografada (`proposal_prompt.enc`) é commitada. O texto puro fica em `proposal_prompt.md`,
que está no `.gitignore`. Para alterar o prompt:

```
# edite proposal_prompt.md, depois:
PROMPT_KEY=<sua chave> ./encrypt_prompt.py
git add proposal_prompt.enc && git commit -m "chore: update proposal prompt"
```

Sem `PROMPT_KEY`, o script gera uma chave nova; cadastre-a com `gh secret set PROMPT_KEY`.

A `PROMPT_KEY` fica no `.env` local e no secret do GitHub, e as duas precisam ser iguais. O
GitHub não deixa ler o valor de um secret; para garantir que ele bate com o `.env`, cadastre de
novo a partir dele:

```
grep '^PROMPT_KEY=' .env | cut -d= -f2- | gh secret set PROMPT_KEY
```

Se perder a chave, rode `./encrypt_prompt.py` sem `PROMPT_KEY` definida: ele gera uma chave nova
e criptografa o `proposal_prompt.md` de novo. Depois, guarde a chave no `.env`, cadastre-a com
`gh secret set PROMPT_KEY` e commite o `proposal_prompt.enc` atualizado.

Os logs do Actions são públicos: o bot nunca imprime o texto da proposta.

## Setup local

1. Copie `.env.example` para `.env` e preencha:
   - `TELEGRAM_TOKEN`: token do bot (criado via @BotFather)
   - `TELEGRAM_CHAT_ID`: chat_id de destino
   - `KEYWORDS`: palavras separadas por vírgula (ex: `aplicativo,app`)
   - `CLAUDE_CODE_OAUTH_TOKEN` e `PROMPT_KEY`: opcionais, para gerar propostas (exige o
     Claude Code instalado: `npm install -g @anthropic-ai/claude-code@2.1.282`)

2. Instale o Chromium do Playwright (uma vez só):
   ```
   uv run --with playwright==1.63.0 playwright install chromium
   ```

## Rodar manualmente

```
./bot.py
```

## Testes

```
uv run --with pytest --with playwright==1.63.0 --with requests --with pydantic --with cryptography pytest -q
```

## Agendamento local (launchd, macOS, alternativa ao GitHub Actions)

Ver `com.workana.telegrambot.plist`. Para instalar:

```
cp com.workana.telegrambot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.workana.telegrambot.plist
```

Para parar:

```
launchctl unload ~/Library/LaunchAgents/com.workana.telegrambot.plist
```

Logs em `~/Library/Logs/workana-telegram-bot.log` e `.err.log` (fora do HD externo,
porque o launchd não consegue gravar logs em `/Volumes/...`).

O launchd roda com um `PATH` mínimo, sem `~/.local/bin` nem o `npm` global, então não acha o
comando `claude` e segue sem propostas ("Claude Code não instalado" no `.err.log`).

## Como funciona

- Usa Playwright (Chromium headless) porque a Workana está atrás de Cloudflare
  e bloqueia requisições HTTP simples (curl/requests puro).
- Percorre até 5 páginas de cada busca (7 vagas por página), abrindo cada página numa sessão
  limpa do navegador, porque a Cloudflare bloqueia o segundo carregamento na mesma sessão.
- Mantém `seen.json` com os IDs de projetos já processados, para não notificar duas vezes, e
  `pending.json` com as vagas que ainda esperam proposta.
- Roda a cada 15 minutos via GitHub Actions, disparado pelo Cloudflare Worker (ou launchd, se rodar localmente).
