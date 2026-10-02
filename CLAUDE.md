# CLAUDE.md

## Horário

Vale a regra global: todo horário em UTC-3 (Brasília). Particularidades deste projeto:

- O cron do Worker (`scheduler/wrangler.jsonc`) e o do `monitor.yml` estão em UTC.
- Os `shouldRun` da lista `JOBS` em `scheduler/src/index.js` já recebem hora de Brasília:
  escreva os horários direto em UTC-3, sem converter.
- `gh run list` e os nomes dos logs do wrangler mostram UTC: subtraia 3h antes de informar.
