# Workana Telegram Bot

Monitora projetos novos na Workana (categoria TI e Programação, ordenados por mais recentes),
filtra por palavras-chave no título e avisa no Telegram.

## Setup

1. Copie `.env.example` para `.env` e preencha:
   - `TELEGRAM_TOKEN`: token do bot (criado via @BotFather)
   - `TELEGRAM_CHAT_ID`: chat_id de destino
   - `KEYWORDS`: palavras separadas por vírgula (ex: `aplicativo,app`)

2. Instale o Chromium do Playwright (uma vez só):
   ```
   uv run --with playwright playwright install chromium
   ```

## Rodar manualmente

```
./bot.py
```

## Testes

```
uv run --with pytest --with playwright --with requests pytest -q
```

## Agendamento (launchd, macOS)

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
- Roda a cada 15 minutos via launchd.
