# Workana Telegram Bot

Monitora projetos novos na Workana (categoria TI e Programação, ordenados por mais recentes),
filtra por palavras-chave no título e avisa no Telegram.

## Execução no GitHub Actions (principal)

O workflow `.github/workflows/monitor.yml` roda a cada 15 minutos (horário aproximado:
o GitHub pode atrasar execuções agendadas) e também pode ser disparado manualmente na aba Actions.

Configuração no repositório (Settings → Secrets and variables → Actions):
- Secrets: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
- Variable: `KEYWORDS` (ex: `aplicativo,app`)

Para trocar as palavras-chave:

```
gh variable set KEYWORDS --body "aplicativo,app,flutter"
```

A cada execução o workflow faz commit do `seen.json`. Rode `git pull` antes de editar localmente.

Não ative o launchd junto com o GitHub Actions: cada um tem seu próprio `seen.json`
e você receberia avisos repetidos.

## Setup local

1. Copie `.env.example` para `.env` e preencha:
   - `TELEGRAM_TOKEN`: token do bot (criado via @BotFather)
   - `TELEGRAM_CHAT_ID`: chat_id de destino
   - `KEYWORDS`: palavras separadas por vírgula (ex: `aplicativo,app`)

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
uv run --with pytest --with playwright==1.63.0 --with requests pytest -q
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

## Como funciona

- Usa Playwright (Chromium headless) porque a Workana está atrás de Cloudflare
  e bloqueia requisições HTTP simples (curl/requests puro).
- Mantém `seen.json` com os IDs de projetos já processados, para não notificar duas vezes.
- Roda a cada 15 minutos via GitHub Actions (ou launchd, se rodar localmente).
