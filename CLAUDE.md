# CLAUDE.md

## Horário

Vale a regra global: todo horário em UTC-3 (Brasília). Particularidades deste projeto:

- O cron do Worker (`scheduler/wrangler.jsonc`) e o do `monitor.yml` estão em UTC.
- Os `shouldRun` da lista `JOBS` em `scheduler/src/index.js` já recebem hora de Brasília:
  escreva os horários direto em UTC-3, sem converter.
- `gh run list` e os nomes dos logs do wrangler mostram UTC: subtraia 3h antes de informar.

## Dotfiles

- **Nunca** mexer no repositório `~/dotfiles` (nem editar, nem commitar, nem rodar scripts dele).
  Isso inclui a cópia da skill `proposta-freela` que mora lá e que o `proposal.yml` baixa: se a
  mudança pedida estiver nela, avise e deixe a alteração para mim.
- A cópia local `~/.claude/skills/proposta-freela` é outra pasta, fora do `dotfiles`, e pode
  ser alterada quando eu pedir.
