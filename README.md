# Workana Telegram Bot

Monitora projetos novos na Workana (buscas por "aplicativo" e "app" das últimas 24h, em
português, clientes da América do Sul, Central e Caribe — ver `WORKANA_URLS` no `bot.py`),
filtra por palavras-chave no título, faz uma triagem com o Claude e, para cada vaga aprovada,
gera uma proposta com a skill `proposta-freela`. A proposta fica guardada no repositório
privado `leandrorochaadm/proposta`, e o Telegram recebe um aviso com preço, prazo e o link
dela (ver [Propostas com a skill](#propostas-com-a-skill)).

Este repositório é público e só tem código, testes e workflows. O estado do bot, o critério da
triagem e as propostas ficam no `proposta`; as regras de proposta e de preço ficam na skill, no
repositório privado `leandrorochaadm/dotfiles`.

## O caminho de uma vaga

O bot roda em dois workflows do GitHub Actions:

| Workflow | Disparo | Limite | O que faz |
|----------|---------|--------|-----------|
| `monitor.yml` | Cloudflare Worker a cada 15 min (e cron reserva de hora em hora) | 14 min | Lê a Workana, filtra pelas palavras-chave, lê a descrição, faz a triagem e põe a vaga aprovada na fila. Se houver fila, dispara o `proposal.yml` |
| `proposal.yml` | O `monitor.yml` (ou à mão) | 30 min | Gera **uma** proposta da fila, publica no `proposta` e manda o aviso no Telegram |

1. O `monitor.yml` acha uma vaga nova cujo título casa com `KEYWORDS` e a põe na fila
   (`proposta/data/pending.json`).
2. Ele abre a página da vaga, lê a descrição e faz a [triagem](#triagem). Aprovada, a vaga
   espera o `proposal.yml`; recusada, não chega no Telegram.
3. O `proposal.yml` pega a vaga mais antiga da fila, gera a proposta com a skill e grava uma
   pasta em `proposta/generated/`.
4. O bot faz commit e push dessa pasta no `proposta` e só então manda o aviso no Telegram, com o
   link do `proposta.md`.

Os dois workflows nunca rodam ao mesmo tempo (grupo de `concurrency` `proposta-repo`), porque
os dois gravam o `proposta/data/pending.json`. Enquanto uma proposta é gerada, o monitor espera.
Por isso o aviso chega alguns minutos depois de a vaga aparecer na Workana, e sai uma vaga por
execução do `proposal.yml`: o monitor dispara o `proposal.yml` de novo enquanto houver fila.

## Execução no GitHub Actions

Configuração no repositório (Settings → Secrets and variables → Actions):
- Secrets:
  - `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`: bot e chat que recebem os avisos.
  - `CLAUDE_CODE_OAUTH_TOKEN`: triagem e proposta (ver [Token do Claude Code](#token-do-claude-code)).
  - `PROPOSTA_DEPLOY_KEY` e `DOTFILES_DEPLOY_KEY`: acesso aos repositórios privados (ver
    [Deploy keys](#deploy-keys)).
- O relatório de uso do Claude (`usage.yml`) vai para o bot [@leandro_claude_notify_bot](https://t.me/leandro_claude_notify_bot): secret `USAGE_TELEGRAM_TOKEN` (token do BotFather) e, se o chat for outro, `USAGE_TELEGRAM_CHAT_ID` (sem ele, usa o `TELEGRAM_CHAT_ID`)
- Variables: `KEYWORDS` (ex: `aplicativo,app`) e, opcional, `EXCLUDE_KEYWORDS` (ex: `jogo,wordpress,no code`). Só o `monitor.yml` usa as duas.

Para trocar as palavras-chave:

```
gh variable set KEYWORDS --body "aplicativo,app,flutter"
```

### Palavras bloqueadas

`EXCLUDE_KEYWORDS` lista palavras que descartam a vaga, separadas por vírgula. Para criar ou trocar:

```
gh variable set EXCLUDE_KEYWORDS --body "jogo,game,manutenção,wordpress,no code,low code"
```

Como o filtro funciona:
- Olha só o **título** da vaga, não a descrição.
- Vence o `KEYWORDS`: "App de jogo" é descartado mesmo tendo "app".
- Vale também para vagas que já estavam na fila (`proposta/data/pending.json`).
- A vaga descartada vai para o `proposta/data/seen.json`: não chega no Telegram nem gasta proposta.
- Mesma regra do `KEYWORDS`: palavra inteira, sem diferenciar maiúscula de minúscula,
  e o plural com "s" também conta (`jogo` barra "jogos", mas não "joguinho").
- Um espaço na palavra também pega hífen ou as palavras juntas: `no code` barra "no-code" e "nocode".
  Isso vale para o `KEYWORDS` também.
- Variável vazia ou ausente: nada é filtrado.

### Agendamento com Cloudflare Worker

O Worker dispara o `monitor.yml` e o `usage.yml` (relatório de uso) deste repositório; o
`proposal.yml` é disparado pelo próprio monitor. Os horários de cada workflow ficam na lista
`JOBS` em `scheduler/src/index.js`, escritos em horário de Brasília (o cron da Cloudflare só
roda a cada 15 min em UTC; o código decide quem dispara).

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

## Repositórios privados

| Pasta no runner | Repositório | Acesso | Secret | Usado por |
|-----------------|-------------|--------|--------|-----------|
| `proposta/` | `leandrorochaadm/proposta` | escrita | `PROPOSTA_DEPLOY_KEY` | os dois workflows |
| `dotfiles/` (só `.claude/skills/proposta-freela`) | `leandrorochaadm/dotfiles` | só leitura | `DOTFILES_DEPLOY_KEY` | `proposal.yml` |

O `proposta` guarda:
- `data/seen.json`: vagas já processadas, para não avisar duas vezes.
- `data/pending.json`: a fila, com a etapa de cada vaga (`triage`, `proposal` ou `alert`).
- `data/rejected.json`: vagas recusadas na triagem (título, link e data), para ajustar o critério.
- `prompts/fit_prompt.md`: o critério da triagem.
- `generated/`: uma pasta por proposta, `AAAA-MM-DD-hh-mm-workana-<título>` (horário de
  Brasília), com `proposta.md` e `analise.md`, ao lado das propostas feitas à mão.

Os workflows fazem commit no `proposta` a cada execução: `chore: update seen projects` (monitor),
`chore: update proposal queue` (proposta) e um `chore: add workana proposal <pasta>` por
proposta. Rode `git pull` no seu clone do `proposta` antes de editar qualquer arquivo dele.

Sem o `proposta`, o bot não roda: ele para com `Pasta proposta/data não encontrada` em vez de
avisar de novo todas as vagas. As pastas `proposta/`, `dotfiles/` e `temp/` estão no
`.gitignore` deste repositório, para nunca subirem aqui por engano.

### Deploy keys

Cada workflow entra nos repositórios privados com uma deploy key (chave SSH que vale para um
repositório só): a do `proposta` pode escrever, a do `dotfiles` só lê. A chave privada fica no
secret deste repositório; a pública, em Settings → Deploy keys do repositório privado.

Para trocar a chave do `proposta` (vazou ou foi apagada):

```bash
dir="$(mktemp -d)"
ssh-keygen -q -t ed25519 -N "" -C "workana-telegram-bot actions proposta" -f "$dir/proposta_key"
gh repo deploy-key add "$dir/proposta_key.pub" --allow-write -R leandrorochaadm/proposta -t "workana-telegram-bot actions"
gh secret set PROPOSTA_DEPLOY_KEY -R leandrorochaadm/workana-telegram-bot < "$dir/proposta_key"
rm -rf "$dir"
```

Para trocar a chave do `dotfiles` (sem `--allow-write`: ela só lê):

```bash
dir="$(mktemp -d)"
ssh-keygen -q -t ed25519 -N "" -C "workana-telegram-bot actions dotfiles" -f "$dir/dotfiles_key"
gh repo deploy-key add "$dir/dotfiles_key.pub" -R leandrorochaadm/dotfiles -t "workana-telegram-bot actions"
gh secret set DOTFILES_DEPLOY_KEY -R leandrorochaadm/workana-telegram-bot < "$dir/dotfiles_key"
rm -rf "$dir"
```

Depois, apague a chave antiga. As duas têm o mesmo título:
`gh repo deploy-key list -R <repositório>` mostra o id e a data de criação, e a antiga é a de
data mais velha. Apague com `gh repo deploy-key delete <id> -R <repositório>`. Se apagar a nova
por engano, o checkout do próximo run falha: gere outra chave com o bloco acima.

## Triagem

Antes da proposta, o Haiku (`claude-haiku-4-5-20251001`) responde só sim ou não: a vaga vale
uma proposta? O critério fica em `proposta/prompts/fit_prompt.md`, no repositório privado. Para
mudar, edite esse arquivo no seu clone do `proposta` (com `git pull` antes) e faça push: o
próximo `monitor.yml` já usa.

- A vaga recusada vai para o `proposta/data/seen.json` e para o `proposta/data/rejected.json`
  e nunca é tentada de novo. Não chega no Telegram nem vira proposta.
- Se a triagem falhar, a vaga segue como aprovada.
- A triagem roda nos primeiros 5 min de cada execução do monitor (`TRIAGE_DEADLINE_SECONDS`);
  o que sobrar fica na fila para a próxima execução.
- Se a página da vaga não abrir, ela é tentada de novo nas próximas execuções. Depois de
  `MAX_JOB_ATTEMPTS` (3) falhas, o aviso sai sem proposta.
- Sem `CLAUDE_CODE_OAUTH_TOKEN`, sem o comando `claude` ou sem o `fit_prompt.md`, o monitor
  manda só o aviso da vaga (título e link), sem triagem e sem proposta, e o log diz o que falta.

## Propostas com a skill

A proposta é escrita por um agente do Claude Code (`claude-sonnet-5-5`, esforço médio) que
segue a skill `proposta-freela`, a mesma usada à mão, lida do `dotfiles` como está. Este
repositório não guarda nenhuma regra de proposta nem de preço: mudou a skill e deu push no
`dotfiles`, o próximo `proposal.yml` já usa (ver [Editar a skill](#editar-a-skill)).

- O `proposal.yml` baixa só a pasta da skill do `dotfiles` e cria o link
  `~/.claude/skills/proposta-freela`, porque a skill roda os scripts dela por esse caminho.
- O agente segue os Passos 1 a 4 da skill sem perguntar nada: o que a skill mandaria perguntar,
  ele assume e marca como `assumido` no `analise.md`. Ele grava `proposta.md` e `analise.md` na
  pasta da vaga e devolve preço e prazo para o aviso.
- O agente só lê a skill e só escreve na pasta da vaga: não acessa a rede, não roda `git` e não
  recebe os tokens do Telegram. O texto da vaga é tratado como dado, nunca como instrução. Quem
  faz o commit é o bot.
- Uma proposta por execução, com até 20 min para o agente (`PROPOSAL_TIMEOUT_SECONDS`). Se ele
  falhar ou passar do tempo, a pasta é apagada e a vaga continua na fila. Depois de
  `MAX_JOB_ATTEMPTS` (3) falhas, o aviso sai sem proposta, com `⚠️ Proposta não gerada`.
- Se o checkout da skill falhar, o `proposal.yml` conta uma falha na vaga e termina vermelho
  (o GitHub manda e-mail).

O aviso no Telegram é uma mensagem só:

```text
🆕 Título da vaga
https://www.workana.com/job/...

💰 Preço: R$ 6 000
⏱ Prazo: 1 dia + 3 semanas
📄 Proposta   (link para generated/<pasta>/proposta.md no GitHub)
```

- O link só abre com login na conta dona do `proposta` (repositório privado), também no celular.
- Se o push da proposta falhar, no lugar do link vem `⚠️ Proposta gravada, mas não subiu para o
  GitHub.`. O fim do workflow tenta o push de novo.
- Cada envio ao Telegram é tentado até `TELEGRAM_SEND_ATTEMPTS` (3) vezes. Se o aviso não sair,
  a vaga fica na fila com a proposta pronta, e o próximo `proposal.yml` manda só o aviso, sem
  gerar outra.

### Revise antes de colar na Workana

A skill foi escrita para cliente direto e pode oferecer chamada, contato, e-mail, telefone ou
link. A Workana proíbe isso e pode suspender a conta. Abra o `proposta.md`, tire esses trechos
e só então cole a proposta na Workana. O `analise.md` mostra a conta do preço e as suposições
marcadas como `assumido`.

### Editar a skill

O `proposal.yml` usa a skill que está no GitHub, no `dotfiles`, e não a da sua máquina. Para
mudar a skill:

1. Edite os arquivos em `~/.claude/skills/proposta-freela/`.
2. Copie a mudança para o clone do `dotfiles`: `~/dotfiles/sync-configs.py backup`.
3. Faça commit e push no `dotfiles`:
   ```bash
   git -C ~/dotfiles add .claude/skills/proposta-freela
   git -C ~/dotfiles commit -m "chore(skill): update proposta-freela"
   git -C ~/dotfiles push
   ```

Sem o push, o Actions continua com a versão antiga. Mudar o nome dos Passos 1 a 4 ou o caminho
dos scripts da skill pode quebrar o bot: as vagas passam a chegar sem proposta. Depois de uma
mudança grande, dispare o `monitor.yml` e confira o próximo `proposal.yml`.

### Token do Claude Code

A triagem e a proposta usam o Claude Code em modo não interativo (`claude -p`), cobrado na
assinatura Max, não na API. Cada proposta consome a mesma cota de uso do plano. Para gerar o
token (vale cerca de 1 ano):

```
claude setup-token
gh secret set CLAUDE_CODE_OAUTH_TOKEN
```

Quando o token vencer, o log do passo "Run bot" do `proposal.yml` mostra `Falha na proposta de
…: claude saiu com código 1 (… api_error_status=401)`. É só repetir os dois comandos acima.

Para testar um token antes de salvar no secret:

```
./check_token.py <token>
```

- **Token válido:** mostra `Token válido.` e sai com código 0.
- **Token recusado (401):** mostra `Token recusado (401): expirou ou foi revogado. Gere outro
  com claude setup-token.` e sai com código 1.
- **Outro erro:** mostra o código de saída e o detalhe do erro, e sai com código 1.

O script roda o `claude` isolado do mesmo jeito que a triagem do bot (sem `ANTHROPIC_API_KEY`,
sem ferramentas, sem configurações locais), então o resultado é o mesmo que o bot terá. A
chamada usa o modelo `haiku` com um prompt mínimo, que gasta muito pouco da cota do plano.

O token passado como parâmetro fica salvo no histórico do shell. Para evitar isso no zsh, ligue
`setopt HIST_IGNORE_SPACE` e comece a linha com um espaço.

Os dois workflows instalam uma versão fixa do Claude Code (`@anthropic-ai/claude-code@2.1.282`),
porque o bot depende das flags, das regras de permissão e do JSON de saída dessa versão. Para
atualizar, troque a versão no `monitor.yml` e no `proposal.yml`, dispare o `monitor.yml` e
confira se a próxima proposta chega.

O secret `ANTHROPIC_API_KEY` não é mais usado e pode ser apagado:
`gh secret delete ANTHROPIC_API_KEY`.

## Rodar os testes

```
uv run --with pytest --with requests --with pydantic --with playwright==1.63.0 \
  pytest -q test_bot.py test_usage_report.py
```

Os testes montam um `proposta/` e uma skill falsos numa pasta temporária: não chamam o
Telegram, o Claude nem os repositórios privados.

## Disparar manualmente

O bot roda só no GitHub Actions, que monta os checkouts do `proposta` e da skill. Para rodar
fora do horário, use a aba Actions (botão "Run workflow") ou:

```
gh workflow run monitor.yml       # lê a Workana; se houver fila, dispara o proposal.yml
gh workflow run proposal.yml      # gera a próxima proposta da fila
gh run list --workflow proposal.yml --limit 5
gh run watch                      # acompanha um run em andamento
```

Sem vaga na fila, o `proposal.yml` termina sem gerar nada.

Para pausar o bot, desligue os dois workflows; para voltar, troque `disable` por `enable`:

```
gh workflow disable monitor.yml
gh workflow disable proposal.yml
```

Com o `monitor.yml` desligado, os disparos do Worker recebem erro: é esperado.

## Como funciona

- Usa Playwright (Chromium headless) porque a Workana está atrás de Cloudflare
  e bloqueia requisições HTTP simples (curl/requests puro).
- Percorre até 5 páginas de cada busca (7 vagas por página), abrindo cada página numa sessão
  limpa do navegador, porque a Cloudflare bloqueia o segundo carregamento na mesma sessão.
- No `monitor.yml`, o navegador sai pelo Cloudflare WARP em modo proxy (`BROWSER_PROXY`),
  porque a Cloudflare da Workana bloqueia os IPs de datacenter dos runners. Se o WARP não
  conectar, o bot roda sem proxy. Quando a listagem dá timeout, o print e o HTML da página
  ficam no artefato `debug-page` da execução.
- O `proposal.yml` não abre navegador: usa a descrição que o monitor guardou na fila.
- O estado fica em `proposta/data/`: os IDs das vagas já processadas (para não notificar duas
  vezes), a fila e as vagas que a triagem recusou.
- O `monitor.yml` roda a cada 15 minutos, disparado pelo Cloudflare Worker.
- Os logs do Actions são públicos: o bot nunca imprime o texto da proposta, e um erro do `git`
  aparece só com o código de saída.
