#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Varredura mecânica da proposta pronta, antes de entregar ao usuário.

Existe porque trava escrita em prosa não trava. As regras desta skill estão
distribuídas em milhares de linhas de SKILL.md e references/, e a evidência de
que isso não basta é concreta: em agosto de 2026 uma proposta prometeu
exclusividade fora das semanas de desenvolvimento com a regra escrita, e cobriu
com cadência semanal uma fase 1 que não tem build.

Aqui ficam só as regras **decidíveis por texto**. Julgamento — o gancho, o teste
do aliado, se as três perguntas são boas — continua sendo leitura humana, e é
justamente para sobrar atenção para elas que o resto virou código.

Uso:
    uv run scripts/varredura.py --pasta caminho/da/proposta
    uv run scripts/varredura.py --proposta proposta.md --analise analise.md

Sai com código 1 se houver qualquer ERRO, 0 se só houver alertas.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import unicodedata
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Limites que vencem — espelham SKILL.md e references/tom-e-exemplos.md
# --------------------------------------------------------------------------

PALAVRAS_MIN = 600
PALAVRAS_MAX = 700

# Frases que prometem exclusividade. Decisão do usuário, ago/2026: não se
# promete e nem se cita. Frase de agenda ("período integral") não é exclusividade
# e não cai aqui — mas a disponibilidade em horas saiu do texto por outro motivo,
# a unidade de divisão, e quem pega isso é checa_unidade_de_divisao.
EXCLUSIVIDADE = [
    r"s[óo] no seu projeto",
    r"s[óo] ao seu projeto",
    r"dedica[çc][ãa]o exclusiva",
    r"exclusivamente no seu",
    r"sem dividir com outro",
    r"n[ãa]o pego outro (projeto|cliente)",
    r"um (projeto|cliente) por vez",
]

# Conectores e construções que denunciam IA (varredura 2 do SKILL.md).
CONECTORES_PROIBIDOS = [
    r"\bal[ée]m disso\b",
    r"\bportanto\b",
    r"\bdessa forma\b",
    r"\bdesta forma\b",
    r"\bem suma\b",
    r"\bvale ressaltar\b",
    r"\bé importante (notar|destacar|ressaltar)\b",
    r"\bn[ãa]o apenas\b.{0,40}\bmas tamb[ée]m\b",
    r"\bno mundo de hoje\b",
    r"\bsolu[çc][ãa]o robusta\b",
]

# Palavras vetadas no texto de venda. Decisão do usuário, set/2026: "taxa",
# "WhatsApp", "comissão", "Pix" e o verbo "ligar" não aparecem na proposta, em
# nenhuma forma. "Ligação" (no sentido de relação) fica de fora da trava.
# É ERRO, não ALERTA.
PALAVRAS_PROIBIDAS = [
    (r"\btaxas?\b", "taxa"),
    (r"\bwhats\s*app\b|\bwhats\b|\bzap\b", "WhatsApp"),
    (r"\bcomiss\w*", "comissão"),
    (r"\bpix\b", "Pix"),
    (r"\blig(ar|o|ue|uei|amos|ando|aremos|arei)\b", "ligar"),
]

# Endereços no texto de venda. Em cliente direto não há proibição de plataforma
# nenhuma, mas a decisão de ago/2026 é que a prova do portfólio continua sendo o
# **vídeo em anexo**, não um link: o cliente PME não clica em endereço dentro de
# um texto de proposta, e o link concorre com o anexo em vez de somar. Por isso é
# ALERTA e não ERRO — se um dia a decisão mudar, é aqui que ela muda.
ENDERECO = [
    (r"https?://", "endereço de site"),
    (r"\bwww\.", "endereço de site"),
    (r"\b(github|behance)\b", "repositório ou portfólio de terceiro"),
]

ERRO = "ERRO"
ALERTA = "ALERTA"


@dataclass
class Achado:
    nivel: str
    regra: str
    detalhe: str
    trecho: str = ""


# --------------------------------------------------------------------------
# Utilidades de texto
# --------------------------------------------------------------------------


def paragrafos(texto: str) -> list[str]:
    """Blocos separados por linha em branco, que é a unidade em que as travas valem."""
    return [p.strip() for p in re.split(r"\n\s*\n", texto) if p.strip()]


def desdobra(texto: str) -> str:
    """Junta as linhas de cada parágrafo, mantendo a separação entre parágrafos.

    Toda trava desta varredura é uma expressão de várias palavras, e o arquivo da
    proposta vem quebrado em 80 colunas. Sem esta normalização, "Dentro\nda fase"
    não casa com "dentro da fase" e a trava passa batido: a quebra de linha cai
    justamente no meio da âncora com a frequência de uma a cada dez palavras.
    Descoberto em 19/08/2026, ao rodar a varredura no exemplo bom do
    `references/tom-e-exemplos.md` depois de reescrever um parágrafo.

    Só vale para a proposta. O `analise.md` tem tabela, e tabela precisa da linha.
    """
    return "\n\n".join(re.sub(r"[^\S\n]*\n[^\S\n]*", " ", p) for p in paragrafos(texto))


def conta_palavras(texto: str) -> int:
    # "R$ 12 000" é um número só: o espaço de milhar não pode virar palavra a mais.
    texto = re.sub(r"(?<=\d) (?=\d{3}\b)", "", texto)
    return len([p for p in re.split(r"\s+", texto.strip()) if p])


# Símbolos da categoria "So" que não são emoji e podem aparecer num texto legítimo.
SIMBOLOS_PERMITIDOS = {"™", "©", "®", "℠", "°", "№"}

# Blocos Unicode de emoji de fato. A categoria "So" sozinha é ampla demais e
# reprovaria "Claro Pay™" com a mensagem errada.
FAIXAS_EMOJI = ((0x1F300, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF), (0xFE0F, 0xFE0F))


def tem_emoji(texto: str) -> list[str]:
    achados = []
    for ch in texto:
        if ch in SIMBOLOS_PERMITIDOS:
            continue
        ponto = ord(ch)
        if any(ini <= ponto <= fim for ini, fim in FAIXAS_EMOJI):
            achados.append(ch)
    return achados


def valores_citados(texto: str) -> set[str]:
    """Todo valor em R$ ou US$, normalizado com a moeda junto, para comparar entre arquivos.

    A moeda fica no valor de propósito: R$ 99 e US$ 99 são coisas diferentes e não
    podem se validar mutuamente.
    """
    limpos = set()
    for moeda, bruto in re.findall(r"(R\$|US\$)\s?([\d][\d.,\s]*)", texto):
        v = re.sub(r"[.\s]", "", bruto).rstrip(",")
        if v.endswith(",00"):
            v = v[:-3]
        if v:
            limpos.add(f"{moeda} {v}")
    return limpos


def contexto(paragrafo: str, padrao: str, janela: int = 70) -> str:
    m = re.search(padrao, paragrafo, re.IGNORECASE)
    if not m:
        return ""
    ini = max(0, m.start() - janela)
    fim = min(len(paragrafo), m.end() + janela)
    return ("..." if ini else "") + paragrafo[ini:fim].replace("\n", " ") + ("..." if fim < len(paragrafo) else "")


# --------------------------------------------------------------------------
# Checagens da proposta
# --------------------------------------------------------------------------


def checa_exclusividade(texto: str) -> list[Achado]:
    trechos = [contexto(texto, pad) for pad in EXCLUSIVIDADE if re.search(pad, texto, re.IGNORECASE)]
    if not trechos:
        return []
    return [
        Achado(
            ERRO,
            "exclusividade",
            "A proposta promete exclusividade. Decisão de ago/2026: não se promete e nem se cita. "
            "Corte a frase; a resposta em até duas horas no bloco sobre mim continua valendo.",
            " / ".join(dict.fromkeys(trechos)),
        )
    ]


def checa_cobranca_sem_entrada(texto: str) -> list[Achado]:
    """A 1ª parcela é a entrada: dizer só 'cobrada depois da entrega' é falso.

    A entrada mudou de lugar em 01/09/2026 — hoje ela é paga na assinatura, que só
    acontece depois de o cliente aprovar o projeto escrito —, mas a checagem é a
    mesma: sem nomear a entrada, a frase do pagamento mente na primeira cobrança.
    """
    alvo = (
        r"(s[óo] [ée] (cobrad|liberad)|(cobrad|liberad)[ao] depois|sai por entrega|s[óo] sai depois)"
    )
    out = []
    for p in paragrafos(texto):
        if re.search(alvo, p, re.IGNORECASE) and not re.search(r"\bentrada\b|\bsinal\b", p, re.IGNORECASE):
            out.append(
                Achado(
                    ERRO,
                    "cobrança sem a entrada",
                    "A frase do pagamento não nomeia a entrada da assinatura, então fica falsa na 1ª parcela "
                    "(o SKILL.md manda: nenhuma linha de código antes dessa parcela). Use a frase literal do "
                    "fim do movimento 4 em references/tom-e-exemplos.md.",
                    contexto(p, alvo),
                )
            )
    return out


def checa_cadencia_sem_ancora(texto: str) -> list[Achado]:
    """'Toda semana uma versão nova' precisa começar depois da aprovação: a fase 1 não tem build."""
    alvo = r"toda (semana|sexta)[^.]{0,80}(recebe|entrego|mando|sai)"
    ancoras = r"dentro da fase|depois que voc[êe] aprovar|depois de aprovar|a partir da[íi]|depois da aprova[çc][ãa]o"
    # Por frase, não por parágrafo: uma menção à aprovação em outra frase do mesmo
    # bloco não ancora a promessa da cadência, e foi assim que ela escapou.
    out = []
    for p in re.split(r"(?<=[.!?])\s+", texto):
        if re.search(alvo, p, re.IGNORECASE) and not re.search(ancoras, p, re.IGNORECASE):
            out.append(
                Achado(
                    ERRO,
                    "cadência cobrindo a fase 1",
                    "A promessa de versão semanal não está ancorada depois da aprovação, então cobre a "
                    "fase 1, que são os dois dias do levantamento e não tem app instalável. Escreva 'Dentro da "
                    "fase, toda semana...'.",
                    contexto(p, alvo),
                )
            )
    return out


# Titularidade e acesso são coisas diferentes, e a confusão entre as duas gerou a
# regra errada até 19/08/2026. **A titularidade é do cliente desde o começo** — o
# código, as contas de loja e o app publicado nascem no nome dele — e isso é fato
# do contrato, é o argumento mais forte do movimento 5 e **pode ser dito**. O que
# só acontece no fim é a **entrega dos acessos**: senhas, chaves e credenciais
# passam para a mão dele quando a entrega termina ou o contrato é encerrado, e
# isso existe para não haver brecha de segurança no meio do desenvolvimento.
#
# Nada dessa mecânica entra na proposta. Ela é assunto do levantamento de
# requisitos, com calma e em detalhe; no cortejo ela responde uma pergunta que o
# cliente ainda não fez e planta desconfiança onde havia só o possessivo.
ACESSO = r"(acessos?|senhas?|credenciais|chave de assinatura|chaves das lojas|keystore)"
ENTREGA_DE_ACESSO = (
    r"(passo|entrego|repasso|transfiro|libero|entregar|passar|transferir|"
    r"transferid\w+|entregues?|repassad\w+|liberad\w+|ficam com voc[êe])"
)
ACESSO_ANTECIPADO = r"desde o come[çc]o|desde o in[íi]cio|desde j[áa]|desde agora|a qualquer momento|durante o (projeto|desenvolvimento)"
ACESSO_NO_FIM = r"aceite final|[úu]ltima parcela|fim do projeto|no final|cancelamento|rescis[ãa]o|encerrar? o contrato|encerramento"


def checa_acessos(texto: str) -> list[Achado]:
    """Os acessos não se prometem no texto: nem antecipados, nem explicados.

    Duas coisas diferentes, e por isso dois níveis:

    - **Erro:** prometer acesso, senha ou chave na mão do cliente antes do fim.
      Não é o combinado e abre brecha de segurança no meio do desenvolvimento.
    - **Alerta:** explicar quando os acessos passam. É verdade e está no contrato,
      mas é conversa do levantamento de requisitos, não da proposta.

    O que **não** é acusado aqui: "o app, o código e as contas ficam no seu nome
    desde o começo". Titularidade é do cliente desde o primeiro dia, é fato do
    contrato e é o que faz o preço parecer justo.
    """
    out = []
    for frase in re.split(r"(?<=[.!?])\s+", texto):
        if not re.search(ACESSO, frase, re.IGNORECASE):
            continue
        if not re.search(ENTREGA_DE_ACESSO, frase, re.IGNORECASE):
            continue
        if re.search(ACESSO_ANTECIPADO, frase, re.IGNORECASE):
            out.append(
                Achado(
                    ERRO,
                    "acesso antecipado",
                    "O texto promete acesso, senha ou chave na mão do cliente antes do fim da entrega. "
                    "A titularidade é dele desde o começo, mas os acessos passam no fim da entrega ou no "
                    "encerramento do contrato. Corte a frase: o possessivo já faz o trabalho.",
                    contexto(frase, ACESSO),
                )
            )
        elif re.search(ACESSO_NO_FIM, frase, re.IGNORECASE):
            out.append(
                Achado(
                    ALERTA,
                    "mecânica dos acessos no texto de venda",
                    "O texto explica quando os acessos são entregues. É verdade e está no contrato, mas "
                    "responde uma pergunta que o cliente ainda não fez, e no cortejo isso esfria. "
                    "Guarde para o levantamento de requisitos e registre a dívida no analise.md.",
                    contexto(frase, ACESSO),
                )
            )
    return out


def checa_formatacao(texto: str) -> list[Achado]:
    out = []
    emojis = tem_emoji(texto)
    if emojis:
        out.append(Achado(ERRO, "emoji", f"A proposta tem emoji: {' '.join(emojis[:5])}. Nenhum é permitido."))

    for sinal, nome in (("—", "travessão"), ("–", "meia risca")):
        if sinal in texto:
            out.append(
                Achado(
                    ERRO,
                    nome,
                    f"O texto usa {nome} ('{sinal}'), que é a pontuação que mais denuncia IA. "
                    "Troque por vírgula, dois-pontos ou ponto.",
                    contexto(texto, re.escape(sinal)),
                )
            )

    if "**" in texto or re.search(r"(?<!\*)\*(?!\*)\w", texto):
        out.append(Achado(ERRO, "negrito", "A proposta tem formatação em negrito ou itálico. Só a numeração das três perguntas é permitida."))

    for pad in CONECTORES_PROIBIDOS:
        if re.search(pad, texto, re.IGNORECASE):
            out.append(Achado(ERRO, "conector de IA", "Conector da lista proibida.", contexto(texto, pad)))

    for pad, nome in PALAVRAS_PROIBIDAS:
        if re.search(pad, texto, re.IGNORECASE):
            out.append(
                Achado(
                    ERRO,
                    f"palavra proibida ({nome})",
                    f"A palavra '{nome}' não entra na proposta, em nenhuma forma. Reescreva o trecho sem ela.",
                    contexto(texto, pad),
                )
            )

    for pad, nome in ENDERECO:
        if re.search(pad, texto, re.IGNORECASE):
            out.append(
                Achado(
                    ALERTA,
                    f"endereço no texto ({nome})",
                    "A prova do portfólio é o vídeo em anexo, não um link: o cliente PME não clica em "
                    "endereço no meio de uma proposta, e o link concorre com o anexo. Descreva o "
                    "trabalho e remeta ao vídeo, ou confirme que quer abrir exceção.",
                    contexto(texto, pad),
                )
            )
    return out


def checa_tamanho(texto: str) -> list[Achado]:
    n = conta_palavras(texto)
    if n < PALAVRAS_MIN:
        return [
            Achado(
                ALERTA,
                "abaixo de 600 palavras",
                f"{n} palavras. Abaixo da faixa alguma coisa foi cortada, quase sempre o bloco sobre mim "
                "ou o movimento 4. Exceção: vaga vaga demais para orçar.",
            )
        ]
    if n > PALAVRAS_MAX:
        return [
            Achado(
                ALERTA,
                "acima de 700 palavras",
                f"{n} palavras. Acima da faixa o material verdadeiro acabou e o texto virou narrativa.",
            )
        ]
    return []


def checa_compromisso_precoce(texto: str) -> list[Achado]:
    """'Fecho em R$ X' compromete valor antes das respostas que mudam o escopo."""
    pad = r"\bfecho em\b|\bfica fechado em\b|\bpre[çc]o final\b"
    if re.search(pad, texto, re.IGNORECASE):
        return [
            Achado(
                ALERTA,
                "compromisso antes das perguntas",
                "O número vem com verbo de compromisso fechado, e as três perguntas ainda podem mudar o "
                "escopo. Ancore em 'pelo escopo que está escrito na vaga'.",
                contexto(texto, pad),
            )
        ]
    return []


def checa_unidade_de_divisao(texto: str) -> list[Achado]:
    """Número de tela, hora ou funcionalidade dá ao cliente uma unidade para dividir o preço."""
    pad = r"\b(\d+|uma|duas|tr[êe]s|quatro|cinco|seis|sete|oito|nove|dez)\s+(telas?|horas?|funcionalidades?)\b"
    # "respondo em até duas horas" é promessa de atendimento, não unidade de
    # divisão: o cliente não divide o preço por ela.
    isento = r"respond|resposta|hor[áa]rio comercial|prazo de retorno"
    out = []
    for frase in re.split(r"(?<=[.!?])\s+", texto):
        m = re.search(pad, frase, re.IGNORECASE)
        if not m or re.search(isento, frase, re.IGNORECASE):
            continue
        out.append(
            Achado(
                ERRO,
                "unidade de divisão",
                "O texto dá ao cliente um número de tela, hora ou funcionalidade para dividir pelo preço. "
                "Esses números vivem na estimativa e no Anexo I, nunca na proposta.",
                contexto(frase, pad),
            )
        )
    return out


# --------------------------------------------------------------------------
# Checagens do analise.md
# --------------------------------------------------------------------------


def checa_status(texto: str) -> list[Achado]:
    m = re.search(r"^-?[^\S\n]*\*\*Status:\*\*[^\S\n]*(.*)$", texto, re.MULTILINE)
    if not m:
        return []
    status = m.group(1).strip().lower()
    if not status.startswith("enviada"):
        return []
    data = re.search(r"^-?[^\S\n]*\*\*Data de envio:\*\*[^\S\n]*(.*)$", texto, re.MULTILINE)
    if data is None:
        motivo = "e não existe campo 'Data de envio' no arquivo"
    elif not data.group(1).strip():
        motivo = "e a data de envio está em branco"
    else:
        return []
    return [
        Achado(
            ERRO,
            "status sem data de envio",
            f"O status diz 'enviada' {motivo}. Enquanto a proposta não sair, o status é 'rascunho'.",
        )
    ]


def checa_titulos_duplicados(texto: str) -> list[Achado]:
    titulos = re.findall(r"^(#{2,3} .+)$", texto, re.MULTILINE)
    vistos: dict[str, int] = {}
    for t in titulos:
        vistos[t.strip()] = vistos.get(t.strip(), 0) + 1
    return [
        Achado(
            ERRO,
            "título duplicado",
            f"'{t}' aparece {n} vezes. Quase sempre é a saída do preco.py colada sob um título de mesmo "
            "nome: o container é '## Lastro da estimativa' e a saída entra abaixo com os títulos dela.",
        )
        for t, n in vistos.items()
        if n > 1
    ]


def checa_pendencias_de_envio(texto: str) -> list[Achado]:
    """Linha a linha, não por janela: campos vizinhos não podem contaminar um ao outro."""
    for linha in texto.splitlines():
        if re.search(r"pessoa f[íi]sica", linha, re.IGNORECASE) and re.search(
            r"n[ãa]o informado", linha, re.IGNORECASE
        ):
            return [
                Achado(
                    ALERTA,
                    "PJ ou pessoa física em aberto",
                    "Se o cliente for pessoa física, o contrato padrão não serve: exige CNPJ para sustentar "
                    "o teto de indenização, e precisa de outro modelo.",
                    linha.strip()[:140],
                )
            ]
    return []


ELISAO = "(...)"

# Justificativas que não são citação: quem escreve uma delas está inferindo.
MARCAS_DE_INFERENCIA = (
    "decorre",
    "pressupõe",
    "pressupoe",
    "é natural",
    "e natural",
    "está implícito",
    "esta implicito",
    "subentendido",
    "todo app",
    "todo aplicativo",
    "óbvio",
    "obvio",
)


def _normaliza(texto: str) -> str:
    """Minúsculas, aspas retas e espaço colapsado, para comparar citação com a vaga."""
    t = texto.lower()
    for esquerda, direita in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'")):
        t = t.replace(esquerda, direita)
    return re.sub(r"\s+", " ", t).strip()


def texto_da_vaga(analise: str) -> str:
    """O bloco citado sob 'Texto original da vaga', que é a única fonte da verdade do escopo."""
    m = re.search(
        r"^#{2,4}\s*Texto original da vaga.*?$(.*?)(?=^#{1,4}\s|\n---\s*\n|\Z)",
        analise,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return ""
    linhas = [re.sub(r"^\s*>\s?", "", ln) for ln in m.group(1).splitlines()]
    return _normaliza(" ".join(linhas))


def linhas_da_rastreabilidade(analise: str) -> list[tuple[str, str]]:
    """Pares (item, justificativa) da primeira tabela da seção, que é a do que ENTROU no orçamento."""
    m = re.search(
        r"^#{2,4}\s*Rastreabilidade.*?$(.*?)(?=^\*\*Considerado e deixado de fora|^#{1,4}\s|\n---\s*\n|\Z)",
        analise,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return []
    pares: list[tuple[str, str]] = []
    for linha in m.group(1).splitlines():
        linha = linha.strip()
        if not linha.startswith("|") or set(linha) <= set("|-: "):
            continue
        celulas = [c.strip() for c in linha.strip("|").split("|")]
        if len(celulas) < 2:
            continue
        if celulas[0].lower().startswith("no orçamento") or celulas[0].lower().startswith("no orcamento"):
            continue
        pares.append((celulas[0], celulas[1]))
    return pares


def checa_rastreabilidade(analise: str) -> list[Achado]:
    """Regra de ouro 15: nenhum item de orçamento sem citação literal da vaga que o peça.

    Cobre a citação inexistente e a linha sem citação nenhuma. **Não cobre** a citação
    verdadeira usada para justificar o item errado, que é leitura humana (varredura 11).
    """
    vaga = texto_da_vaga(analise)
    pares = linhas_da_rastreabilidade(analise)
    if not vaga or not pares:
        return []

    achados: list[Achado] = []
    for item, justificativa in pares:
        if "exceção declarada" in justificativa.lower() or "excecao declarada" in justificativa.lower():
            continue

        citacoes = re.findall(r'"([^"]{4,})"', justificativa.replace("“", '"').replace("”", '"'))
        if not citacoes:
            achados.append(
                Achado(
                    ERRO,
                    "item de orçamento sem citação da vaga",
                    f"'{_corta(item)}' não tem trecho da vaga entre aspas na coluna da direita. "
                    "Regra de ouro 15: sem linha que o peça, o item sai do orçamento e vira pergunta. "
                    "Custo de projeto é a única exceção, e vai marcado como 'exceção declarada'.",
                    _corta(justificativa),
                )
            )
            continue

        for citacao in citacoes:
            fragmentos = [f.strip() for f in _normaliza(citacao).split(ELISAO) if f.strip()]
            faltando = [f for f in fragmentos if f not in vaga]
            if faltando:
                achados.append(
                    Achado(
                        ERRO,
                        "citação que não está na vaga",
                        f"'{_corta(item)}' cita \"{_corta(citacao)}\", e isto não aparece no texto original "
                        "da vaga. A coluna da direita é citação, não paráfrase: se foi preciso reescrever "
                        "a linha para ela justificar o item, o item não está lá.",
                        _corta(faltando[0]),
                    )
                )

        marca = next((m for m in MARCAS_DE_INFERENCIA if m in justificativa.lower()), None)
        if marca:
            achados.append(
                Achado(
                    ALERTA,
                    "escopo justificado por inferência",
                    f"'{_corta(item)}' se justifica com '{marca}'. Releia: a frase citada **pede** este item, "
                    "ou só menciona o assunto dele? Frase genérica da vaga não autoriza nada dentro dela, e "
                    "quando a vaga se explica na oração seguinte, vale a explicação dela.",
                    _corta(justificativa),
                )
            )
    return achados


def _citacoes(celula: str) -> list[str]:
    """Fragmentos citados de uma célula, já normalizados e com a elisão (...) desmembrada."""
    bruto = re.findall(r'"([^"]{4,})"', celula.replace("“", '"').replace("”", '"'))
    fragmentos: list[str] = []
    for citacao in bruto:
        fragmentos += [f.strip() for f in _normaliza(citacao).split(ELISAO) if f.strip()]
    return fragmentos


def _tabela(analise: str, titulo: str, parar_em: str | None = None) -> list[tuple[str, str]]:
    fim = parar_em or r"^#{1,4}\s"
    m = re.search(
        rf"^#{{2,4}}\s*{titulo}.*?$(.*?)(?={fim}|^#{{1,4}}\s|\n---\s*\n|\Z)",
        analise,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return []
    pares: list[tuple[str, str]] = []
    for linha in m.group(1).splitlines():
        linha = linha.strip()
        if not linha.startswith("|") or set(linha) <= set("|-: "):
            continue
        celulas = [c.strip() for c in linha.strip("|").split("|")]
        if len(celulas) < 2:
            continue
        pares.append((celulas[0], celulas[1]))
    return pares


def checa_cobertura_da_vaga(analise: str) -> list[Achado]:
    """O sentido inverso da rastreabilidade: cada exigência da vaga precisa de destino.

    A tabela de rastreabilidade corre do orçamento para a vaga e trava a invenção. Esta
    corre da vaga para o orçamento e trava o esquecimento: exigência sem destino é escopo
    que ninguém orçou e que o cliente vai cobrar na entrega.
    """
    vaga = texto_da_vaga(analise)
    if not vaga:
        return []
    linhas = [
        (esq, dir_)
        for esq, dir_ in _tabela(analise, r"O que a vaga pediu")
        if not esq.lower().startswith("exig")
    ]
    if not linhas:
        return [
            Achado(
                ERRO,
                "sem a tabela de cobertura da vaga",
                "Falta a seção 'O que a vaga pediu, e onde cada pedido foi'. Ela é o sentido inverso "
                "da rastreabilidade: uma linha por exigência da vaga, dizendo onde ela foi atendida. "
                "Sem ela nada impede que um pedido do cliente simplesmente não seja orçado.",
            )
        ]

    achados: list[Achado] = []
    for exigencia, destino in linhas:
        if not destino or destino in {"", "-", "—"}:
            achados.append(
                Achado(
                    ERRO,
                    "exigência da vaga sem destino",
                    f"'{_corta(exigencia)}' não diz onde foi atendida. Toda exigência tem destino: item do "
                    "orçamento, pergunta do texto, ou fora com o motivo escrito.",
                )
            )
            continue
        for fragmento in _citacoes(exigencia):
            if fragmento not in vaga:
                achados.append(
                    Achado(
                        ERRO,
                        "exigência que não está na vaga",
                        f'A linha cita "{_corta(fragmento)}", e isto não aparece no texto original da vaga. '
                        "Esta tabela decompõe a vaga, não a reescreve.",
                        _corta(exigencia),
                    )
                )
    return achados


def checa_citacao_reaproveitada(analise: str) -> list[Achado]:
    """A mesma frase da vaga bancando dois itens do orçamento: foi assim que o mapa entrou.

    Em 19/08/2026 'otimização da logística de entrega' justificou o motor de atribuição e,
    de novo, o rastreamento no mapa. Cada linha achou a citação dela e a tabela pareceu
    cheia. Alerta, não erro: uma linha da vaga pode legitimamente comprar várias telas do
    mesmo painel.
    """
    vaga = texto_da_vaga(analise)
    linhas = linhas_da_rastreabilidade(analise)
    if not vaga or len(linhas) < 2:
        return []

    porFragmento: dict[str, list[str]] = {}
    for item, justificativa in linhas:
        if "exceção declarada" in justificativa.lower() or "excecao declarada" in justificativa.lower():
            continue
        for fragmento in _citacoes(justificativa):
            if len(fragmento) >= 20:
                porFragmento.setdefault(fragmento, []).append(item)

    achados: list[Achado] = []
    fragmentos = sorted(porFragmento, key=len, reverse=True)
    for i, fragmento in enumerate(fragmentos):
        itens = list(porFragmento[fragmento])
        for outro in fragmentos[i + 1 :]:
            if outro != fragmento and outro in fragmento:
                itens += porFragmento[outro]
        unicos = sorted(set(itens))
        if len(unicos) > 1:
            achados.append(
                Achado(
                    ALERTA,
                    "mesma linha da vaga comprando dois itens",
                    f'"{_corta(fragmento)}" justifica {len(unicos)} itens do orçamento: '
                    + "; ".join(_corta(u, 50) for u in unicos)
                    + ". Confira se a frase pede mesmo os dois, ou se um deles está pegando carona numa "
                    "citação genérica. É o padrão exato do rastreamento no mapa, em 19/08/2026.",
                )
            )
    return achados


def _corta(texto: str, limite: int = 90) -> str:
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


def checa_valores_batem(proposta: str, analise: str) -> list[Achado]:
    """Todo R$ que o cliente lê tem de existir na saída do script, sem redigitação."""
    na_proposta = valores_citados(proposta)
    no_analise = valores_citados(analise)
    orfaos = sorted(v for v in na_proposta if v not in no_analise)
    if orfaos:
        return [
            Achado(
                ERRO,
                "valor que o script não devolveu",
                "Estes valores estão na proposta e não aparecem no analise.md: "
                + ", ".join(orfaos)
                + ". Os números são os do preco.py, sem exceção (regra de ouro 9); os custos de conta "
                "de loja saem de references/ativos-do-cliente.md.",
            )
        ]
    return []


# --------------------------------------------------------------------------
# Orquestração
# --------------------------------------------------------------------------


def varre(proposta: str, analise: str | None) -> list[Achado]:
    achados: list[Achado] = []
    # As travas são expressões de várias palavras, e o arquivo vem quebrado em 80
    # colunas: sem desdobrar, a quebra de linha no meio da âncora salva o texto.
    proposta = desdobra(proposta)
    achados += checa_exclusividade(proposta)
    achados += checa_cobranca_sem_entrada(proposta)
    achados += checa_cadencia_sem_ancora(proposta)
    achados += checa_acessos(proposta)
    achados += checa_unidade_de_divisao(proposta)
    achados += checa_formatacao(proposta)
    achados += checa_compromisso_precoce(proposta)
    achados += checa_tamanho(proposta)
    if analise is not None:
        achados += checa_status(analise)
        achados += checa_titulos_duplicados(analise)
        achados += checa_pendencias_de_envio(analise)
        achados += checa_rastreabilidade(analise)
        achados += checa_cobertura_da_vaga(analise)
        achados += checa_citacao_reaproveitada(analise)
        achados += checa_valores_batem(proposta, analise)
    return achados


def relatorio(achados: list[Achado], palavras: int) -> str:
    erros = [a for a in achados if a.nivel == ERRO]
    alertas = [a for a in achados if a.nivel == ALERTA]
    linhas: list[str] = ["# Varredura da proposta", ""]
    linhas.append(f"{palavras} palavras · {len(erros)} erro(s) · {len(alertas)} alerta(s)")
    linhas.append("")

    if not achados:
        linhas.append("Nenhum achado mecânico. **Falta a leitura humana**: o gancho, o teste do aliado e a")
        linhas.append("qualidade das três perguntas não são decidíveis por texto e continuam sendo suas.")
        return "\n".join(linhas)

    for titulo, grupo in (("Erros, que barram a entrega", erros), ("Alertas, que pedem decisão", alertas)):
        if not grupo:
            continue
        linhas.append(f"## {titulo}")
        linhas.append("")
        for a in grupo:
            linhas.append(f"- **{a.regra}** — {a.detalhe}")
            if a.trecho:
                linhas.append(f"  > {a.trecho}")
        linhas.append("")

    linhas.append("A varredura só cobre o que é decidível por texto. O gancho, o teste do aliado e a")
    linhas.append("qualidade das três perguntas continuam sendo leitura sua.")
    return "\n".join(linhas)


def main() -> None:
    p = argparse.ArgumentParser(description="Varre a proposta pronta contra as travas da skill.")
    p.add_argument("--pasta", type=pathlib.Path, default=None, help="pasta com proposta.md e analise.md")
    p.add_argument("--proposta", type=pathlib.Path, default=None)
    p.add_argument("--analise", type=pathlib.Path, default=None)
    args = p.parse_args()

    if args.pasta and (args.proposta or args.analise):
        p.error("--pasta não combina com --proposta/--analise: escolha um dos dois modos")

    if args.pasta:
        caminho_proposta = args.pasta / "proposta.md"
        caminho_analise = args.pasta / "analise.md"
        if not caminho_analise.exists():
            caminho_analise = None
    else:
        caminho_proposta = args.proposta
        caminho_analise = args.analise

    if caminho_proposta is None:
        p.error("informe --pasta ou --proposta")
    if not caminho_proposta.exists():
        p.error(f"não achei {caminho_proposta}")

    proposta = caminho_proposta.read_text(encoding="utf-8")
    analise = caminho_analise.read_text(encoding="utf-8") if caminho_analise and caminho_analise.exists() else None

    achados = varre(proposta, analise)
    print(relatorio(achados, conta_palavras(proposta)))
    sys.exit(1 if any(a.nivel == ERRO for a in achados) else 0)


if __name__ == "__main__":
    main()
