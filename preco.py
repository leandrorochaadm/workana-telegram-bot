#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Calculadora de preço, régua de pagamento, líquido e piso da skill proposta-freela.

Fonte da verdade dos números que vencem. O SKILL.md aponta para cá.
Não estima horas nem telas: isso é julgamento sobre a vaga e entra como argumento.

Uso:
    uv run scripts/preco.py --horas 90 --telas 8
    uv run scripts/preco.py --horas 90 --telas 8 --orcamento-cliente 9000
    uv run scripts/preco.py --horas 90 --telas 8 --fator-estimativa 1.3
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Números que vencem — fonte única da verdade (última conferência: ago/2026)
# --------------------------------------------------------------------------

# Private in the bot's public repo: the caller sets both from secrets before calcula()
VALOR_HORA = 0.0  # R$, interno, nunca vai para o cliente
GORDURA = 0.0  # sobre a estimativa; hoje sem gordura (decisão de ago/2026)
MINIMO_PROJETO = 0.0  # corte de triagem
SIMPLES = 0.06  # Anexo III por Fator R, primeira faixa

# Levantamento (Fase B), em horas: faixa única, ponto médio 8 h (conferência: set/2026)
#
# As 8 h cobrem as duas fases de requisitos (escopo de pré-venda e requisitos
# detalhados), a análise técnica e o handoff para o desenvolvimento. **Não cobrem o
# desenho das telas**: o wireframe tem linha própria na conta, sai por
# HORAS_WIREFRAME_POR_TELA e é entregue na primeira semana de desenvolvimento.
#
# A faixa não olha o número de telas: o que a Fase B produz é o entendimento do
# negócio e das regras, e isso não cresce na proporção das telas — o que cresce com
# elas é o desenho, que agora é bloco separado.
LEVANTAMENTO_FAIXA = (6, 10)

# Desenho das telas, em horas por tela: o mock em preto e branco que o CLIENTE
# recebe para ver o básico de cada tela. Fica FORA da Fase B e dentro do
# desenvolvimento: é entrega da primeira semana de dev, não dos dias iniciais.
HORAS_DESENHO_POR_TELA = 1

# Wireframe de planejamento, em horas por tela: o rascunho INTERNO, que ninguém
# além do usuário vê. Não é entregável, e por isso não aparece no texto da
# proposta — mas é trabalho, acontece dentro da fase 1 e por isso entra na conta e
# no tempo dela. Meia hora por tela, arredondada para cima no total do bloco.
HORAS_WIREFRAME_POR_TELA = 0.5

# --------------------------------------------------------------------------
# A régua: uma parcela por fase
#
# É a única régua. A fase 1 é o levantamento (a Fase B) e dura DIAS_FASE_B dias
# úteis; as demais são semanas de desenvolvimento de esforço igual, e a última
# termina com o app publicado. As entregas de desenvolvimento caem na sexta-feira,
# no fim da tarde; a da fase 1, no fim do segundo dia útil.
#
# A entrada é max(ENTRADA_MINIMA, 1/fases) e o resto se divide por igual. Até
# 1/ENTRADA_MINIMA fases a fatia de uma fase é a maior das duas e as parcelas
# saem todas iguais; daí em diante a entrada assenta na mínima. Em exatamente 5
# fases as duas contas dão o mesmo número, então a fronteira não tem degrau.
# --------------------------------------------------------------------------

HORAS_POR_SEMANA = 30.0  # horas disponíveis por semana (conferência: ago/2026)
HORAS_POR_DIA = HORAS_POR_SEMANA / 5  # dia útil de trabalho, para o prazo da Fase B
DIAS_FASE_B = 2  # a fase 1 dura dois dias úteis, não uma semana
ENTRADA_MINIMA = 0.20  # abaixo de 1/ENTRADA_MINIMA semanas a fatia de uma semana é maior e manda
PASSO_PARCELA = 10.0  # as parcelas saem redondas; a sobra vai toda para a entrada
SEMANAS_PROJETO_LONGO = 12
DIA_DA_ENTREGA = "sexta-feira, no fim da tarde"
ORDINAL_MASCULINO = {
    1: "primeiro", 2: "segundo", 3: "terceiro", 4: "quarto", 5: "quinto",
    6: "sexto", 7: "sétimo", 8: "oitavo", 9: "nono", 10: "décimo",
}

# Fator de estimativa: correção do histórico real, medida por
# `scripts/produtividade.py calibrar`. 1.0 = estimativa crua, sem calibragem.
# Multiplica só as horas de desenvolvimento: a Fase B tem faixa própria, e se ela
# estiver curta o conserto é subir LEVANTAMENTO_*, não aplicar fator aqui.
FATOR_ESTIMATIVA_PADRAO = 1.0

# Fração da gordura que sobrevive no piso de negociação. 0.0 = o piso é a
# estimativa crua: cortar mais que a gordura é trabalhar de graça no imprevisto
# que sempre aparece. Com GORDURA em 0.0 o piso coincide com o preço: não há
# folga para ceder, e todo desconto sai do escopo.
GORDURA_NO_PISO = 0.0

ENTREGA_FASE_B = (
    "o projeto escrito: requisitos, regras de negócio, análise técnica e o handoff "
    "para o desenvolvimento"
)
ENTREGA_PRIMEIRA_DEV = (
    "o desenho das telas e o primeiro bloco de desenvolvimento (preencher no analise.md)"
)
ENTREGA_DEV = "bloco de desenvolvimento (preencher no analise.md)"
ENTREGA_FINAL = "último bloco e a publicação nas lojas"
ENTREGA_FINAL_COM_DESENHO = "o desenho das telas, o desenvolvimento e a publicação nas lojas"


# --------------------------------------------------------------------------
# Cálculo
# --------------------------------------------------------------------------


def entrega_da_fase_1(dias: int) -> str:
    """Quando a fase 1 entrega. Sai dos dias dela, que variam com o wireframe.

    Era uma constante fixa em "segundo dia útil" enquanto a fase 1 tinha duração
    fixa. Com o wireframe de planejamento dentro dela a duração passou a crescer com
    as telas, e uma constante aqui prometeria a entrega para o dia errado.
    """
    ordinal = ORDINAL_MASCULINO.get(dias)
    return f"no fim do {ordinal} dia útil" if ordinal else f"no fim do {dias}º dia útil"


def horas_levantamento() -> tuple[int, tuple[int, int]]:
    """Horas da Fase B. Devolve (horas usadas, faixa).

    Não recebe o número de telas: a Fase B é conversa de negócio e regra, e o que
    cresce com a quantidade de telas é o desenho, que saiu daqui e virou
    `horas_wireframe`. Dentro da faixa, escolha pelo que a vaga descreve e informe
    à mão com `--horas-levantamento`; sem sinal, o ponto médio.

    Não existe mais versão enxuta. Ela encolhia a Fase B de 16 h para 12 h quando o
    projeto caía logo acima do mínimo; com a Fase B em 8 h ela moveria quase nada e
    custava o trecho mais ramificado do cálculo. Removida em set/2026.
    """
    faixa = LEVANTAMENTO_FAIXA
    return round((faixa[0] + faixa[1]) / 2), faixa


def horas_desenho(telas: int) -> int:
    """Horas do mock que o cliente recebe. Fora da Fase B, dentro do desenvolvimento.

    O desenho é a parte do antigo "desenho do projeto" que cresce com o número de
    telas, e é por isso que ele tem linha própria: somado à Fase B, ele fazia os
    dias iniciais crescerem sem que o entendimento do negócio crescesse junto.
    """
    return telas * HORAS_DESENHO_POR_TELA


def horas_wireframe(telas: int) -> int:
    """Horas do rascunho interno de planejamento. Dentro da fase 1.

    Arredonda o total para cima porque as horas de fase são inteiras: 9 telas dão
    4,5 h, que viram 5. Não confundir com `horas_desenho` — este bloco não é
    entregável e nunca é citado ao cliente.
    """
    return math.ceil(telas * HORAS_WIREFRAME_POR_TELA)


def dias_da_fase_1(horas_fase_1: int) -> int:
    """Quantos dias úteis a fase 1 ocupa. Nunca menos que DIAS_FASE_B.

    A fase 1 deixou de ter duração fixa quando o wireframe de planejamento entrou
    nela: 8 h de levantamento cabem em dois dias, mas 8 h mais meia hora por tela
    não cabem em app grande. O prazo sai das horas para não prometer dois dias onde
    são três — e o piso existe porque levantamento não se faz em meio dia, mesmo em
    app de duas telas.
    """
    return max(DIAS_FASE_B, math.ceil(horas_fase_1 / HORAS_POR_DIA))


def arredonda_para_cima(valor: float) -> float:
    """Número redondo: múltiplo de R$ 100 até R$ 10 mil, de R$ 500 acima."""
    passo = 100.0 if valor < 10_000 else 500.0
    return math.ceil(valor / passo) * passo


def numero_de_fases(horas_execucao: int) -> int:
    """Quantas fases o projeto tem. A 1ª é o levantamento; as outras, uma por semana.


    O desenvolvimento — que inclui o desenho das telas — se divide em semanas cheias
    de HORAS_POR_SEMANA e arredonda para cima: sobra folga em cada fase em vez de
    faltar, e é essa folga que faz a entrega de sexta-feira sobreviver à semana em
    que algo dá errado. A fase 1 não é uma semana: são DIAS_FASE_B dias úteis, e é
    por isso que o prazo do projeto se escreve "2 dias + N semanas".
    """
    return 1 + math.ceil(horas_execucao / HORAS_POR_SEMANA)


def prazo_texto(fases: int, dias_fase_1: int = DIAS_FASE_B) -> str:
    """O prazo como ele vai no analise.md: os dias da fase 1 + as semanas de dev."""
    semanas = fases - 1
    unidade_semana = "semana" if semanas == 1 else "semanas"
    unidade_dia = "dia" if dias_fase_1 == 1 else "dias"
    return (
        f"{dias_fase_1} {unidade_dia} de levantamento + {semanas} {unidade_semana} "
        "de desenvolvimento"
    )


def horas_das_fases(horas_execucao: int, horas_fase_1: int, fases: int) -> list[int]:
    """Horas de cada fase. A 1ª é a fase do levantamento; as de dev saem iguais.

    `horas_fase_1` é a Fase B mais o wireframe de planejamento, que acontece dentro
    dela. `horas_execucao` traz o desenho das telas somado ao desenvolvimento: o
    mock é entrega da primeira semana de dev, não dos dias do levantamento.

    O resto da divisão vai nas ÚLTIMAS fases, uma hora em cada, e a ordem
    importa: assim o esforço acumulado sobe o mais devagar possível contra a reta
    que a régua paga. Empilhado nas primeiras, o buraco da reta final abriria no
    meio do projeto.
    """
    dev_fases = fases - 1
    base, resto = divmod(horas_execucao, dev_fases)
    horas = [horas_fase_1]
    horas += [base + (1 if i >= dev_fases - resto else 0) for i in range(dev_fases)]
    return horas


def regua_das_fases(fases: int, fatia_do_levantamento: float = 0.0) -> tuple[float, ...]:
    """Percentual de cada parcela. Uma parcela por fase.

    Duas regras:

    1. **A entrada** é o maior entre os 20% da regra, a fatia de uma fase
       (1/fases) e a fatia de esforço da fase do levantamento. Esse terceiro piso é
       a trava que sustenta a régua inteira: com `entrada >= levantamento/total` o
       recebido nunca atrasa em relação ao entregue depois da entrada, e sem ele um
       projeto de levantamento pesado e pouco desenvolvimento continuaria descoberto
       mesmo já tendo cobrado a entrada. Na prática ele quase nunca é o maior dos
       três — a Fase B são 8 h contra dezenas de desenvolvimento.
    2. **O resto se divide por igual**, porque o esforço das semanas de
       desenvolvimento é igual. Uma semana de dev vale um pouco mais que a parcela
       que a acompanha, e quem cobria essa diferença era a folga da entrada; desde
       01/09/2026 a entrada é paga uma casa depois e a folga acabou na reta final.
       Ver a conferência do prejuízo.
    """
    if fases <= 1:
        return (1.0,)
    entrada = max(ENTRADA_MINIMA, 1 / fases, fatia_do_levantamento)
    resto = (1.0 - entrada) / (fases - 1)
    return (entrada,) + tuple([resto] * (fases - 1))


def eventos_das_fases(fases: int) -> list[str]:
    """Evento que libera cada parcela.

    **A ordem mudou em 01/09/2026.** A fase 1, o projeto escrito, acontece antes do
    contrato e é apresentada ao cliente. É a aprovação dela que abre a assinatura: o
    documento aprovado vira anexo do contrato e o cliente assina os dois junto com a
    entrada. Então a parcela 1 paga a fase 1 já entregue e financia a fase 2, e daí
    em diante cada entrega libera a parcela da fase seguinte, com o dinheiro
    chegando antes do trabalho. A última parcela é a exceção de sempre: fica presa
    ao aceite final, com o app publicado.

    O efeito colateral bom é que toda entrega passou a ter parcela: com a fase 1
    entregue antes da entrada, não sobra mais entrega sem par. O efeito colateral
    caro está em `avisos()`: a fase 1 é trabalhada sem nada recebido.
    """
    assinatura = (
        "aprovação do projeto escrito e assinatura do contrato, com ele anexado "
        "(paga a fase 1, já entregue, e financia a fase 2; é também o evento que "
        "inicia o prazo de desenvolvimento)"
    )
    if fases <= 1:
        return ["aprovação do projeto escrito e assinatura do contrato, com ele anexado"]
    eventos = [assinatura]
    for parcela in range(2, fases):
        eventos.append(f"entrega da fase {parcela}")
    eventos.append("aceite final, com o app publicado")
    return eventos


def recebido_antes_da_fase(regua: tuple[float, ...], fase: int) -> float:
    """Fração do preço já liberada quando a fase começa.

    Desde 01/09/2026 a parcela 1 sai na aprovação da fase 1, e a parcela k sai na
    entrega da fase k — menos a última, que só sai no aceite. Então antes da fase k
    já entraram k-1 parcelas, e **antes da fase 1 não entrou nada**: o levantamento
    é trabalhado antes de existir contrato.
    """
    return sum(regua[: max(0, fase - 1)])


@dataclass
class Parcela:
    indice: int
    percentual: float
    valor: float
    evento: str


@dataclass
class Fase:
    """Uma fase: quanto dura, o que fica pronto, o que custa e o que está pago.

    A fase 1 dura ao menos DIAS_FASE_B dias úteis — mais, se o wireframe de
    planejamento não couber neles; as demais, uma semana cada. É a única assimetria
    do calendário, e ela existe porque o levantamento é conversa e escrita, não uma
    semana de trabalho.
    """

    indice: int
    horas: int
    duracao: str
    entrega: str
    recebido_antes: float  # fração do preço já liberada quando a fase começa
    entregue_ate: float  # fração do esforço total entregue no fim dela
    risco: float  # R$ de esforço descoberto se o cliente sumir nesta fase


@dataclass
class Resultado:
    horas_dev: int
    horas_dev_informado: int
    fator_estimativa: float
    telas: int
    fases: int
    cronograma: list[Fase]
    horas_desenho: int
    horas_wireframe: int
    horas_execucao: int
    horas_fase_b: int
    horas_fase_1: int
    dias_fase_1: int
    faixa_fase_b: tuple[int, int]
    horas_total: int
    subtotal: float
    com_gordura: float
    preco: float
    motivo_regua: str
    parcelas: list[Parcela]
    imposto: float
    liquido: float
    hora_efetiva: float
    piso: float
    piso_liquido: float
    piso_hora_efetiva: float
    orcamento_cliente: float | None
    semanas_manuais: bool = False
    fase_b_manual: bool = False
    alertas: list[str] = field(default_factory=list)


def calcula_parcelas(
    preco: float,
    regua: tuple[float, ...],
    eventos: list[str],
) -> list[Parcela]:
    """Parcelas em valor redondo, com o percentual medido no valor real.

    As parcelas de fase descem para o múltiplo de PASSO_PARCELA e a sobra toda
    vai para a entrada. Cliente não lê "R$ 3.166,67" como preço pensado, lê como
    conta de dividir mal feita — e a entrada é o lugar certo da sobra, porque é
    ela que já carrega a folga da régua.
    """
    if len(regua) == 1:
        valores = [round(preco, 2)]
    else:
        # A conta abaixo toma regua[1] como o valor de TODA parcela de fase. Vale
        # porque a régua tem sempre essa forma; com outra distribuição ela
        # devolveria silenciosamente uma régua diferente da pedida.
        if len(set(round(x, 9) for x in regua[1:])) != 1:
            raise ValueError(
                "régua fora da forma (entrada, fase, fase, ...): "
                f"{regua}. calcula_parcelas não distribui percentuais irregulares."
            )
        por_fase = math.floor(preco * regua[1] / PASSO_PARCELA) * PASSO_PARCELA
        valores = [round(preco - por_fase * (len(regua) - 1), 2)]
        valores += [float(por_fase)] * (len(regua) - 1)

    parcelas: list[Parcela] = []
    for i, valor in enumerate(valores):
        parcelas.append(
            Parcela(
                indice=i + 1,
                percentual=valor / preco if preco else 0.0,
                valor=valor,
                evento=eventos[i],
            )
        )
    return parcelas


def monta_cronograma(
    horas: list[int], regua: tuple[float, ...], preco: float, dias_fase_1: int = DIAS_FASE_B
) -> list[Fase]:
    total = sum(horas)
    acumulado = 0
    cronograma: list[Fase] = []
    for i, h in enumerate(horas):
        fase = i + 1
        acumulado += h
        entregue = acumulado / total if total else 0.0
        recebido = recebido_antes_da_fase(regua, fase)
        if fase == 1:
            unidade = "dia" if dias_fase_1 == 1 else "dias"
            entrega, duracao = ENTREGA_FASE_B, f"{dias_fase_1} {unidade}"
        else:
            duracao = "1 semana"
            if fase == 2 and fase == len(horas):
                # Projeto de duas fases: a única semana de dev desenha e publica.
                entrega = ENTREGA_FINAL_COM_DESENHO
            elif fase == len(horas):
                entrega = ENTREGA_FINAL
            elif fase == 2:
                entrega = ENTREGA_PRIMEIRA_DEV
            else:
                entrega = ENTREGA_DEV
        cronograma.append(
            Fase(
                indice=fase,
                horas=h,
                duracao=duracao,
                entrega=entrega,
                recebido_antes=recebido,
                entregue_ate=entregue,
                risco=round(max(0.0, entregue - recebido) * preco, 2),
            )
        )
    return cronograma


def liquido_de(preco: float) -> tuple[float, float]:
    """Devolve (imposto, líquido). Simples sobre o bruto (cenário conservador).

    Em cliente direto não há comissão de plataforma nem spread cambial: o único
    desconto entre o bruto e o líquido é o imposto.
    """
    imposto = round(preco * SIMPLES, 2)
    return imposto, round(preco - imposto, 2)


def checagens(r: Resultado) -> list[str]:
    """Tudo que o script quer avisar depois de a conta fechar.

    Vive fora de `calcula` porque é redação, não cálculo: um terço daquela função
    era texto de alerta, e misturar os dois esconde a conta. A ordem daqui é a
    ordem em que os avisos saem no `analise.md`, do mais estrutural ao mais
    circunstancial.
    """
    avisos: list[str] = []

    if r.preco < MINIMO_PROJETO:
        avisos.append(
            f"ABAIXO DO MÍNIMO: {brl0(r.preco)} < {brl0(MINIMO_PROJETO)}. "
            "Recomende não se candidatar — o overhead de proposta, alinhamento e revisão come a margem."
        )

    fatia_do_levantamento = r.horas_fase_b / r.horas_total if r.horas_total else 0.0
    if fatia_do_levantamento > max(ENTRADA_MINIMA, 1 / r.fases) + 1e-9:
        if r.semanas_manuais:
            causa = (
                "A causa aqui é o calendário esticado à mão com --semanas: ele encolhe a fase "
                "de desenvolvimento sem encolher a Fase B."
            )
        elif r.fase_b_manual:
            causa = (
                "A causa aqui é a Fase B informada à mão com --horas-levantamento, acima da "
                "faixa de referência."
            )
        else:
            causa = (
                "Confira as horas: com a Fase B em 8 h, ela só passa desse peso quando a "
                "estimativa de dev está curta demais."
            )
        avisos.append(
            f"A fase do levantamento vale {pct1(fatia_do_levantamento)} do esforço e virou o piso "
            f"da entrada: com menos que isso a entrada não cobriria nem a entrega que ela paga. {causa}"
        )

    if r.fase_b_manual and not (r.faixa_fase_b[0] <= r.horas_fase_b <= r.faixa_fase_b[1]):
        lado = "abaixo" if r.horas_fase_b < r.faixa_fase_b[0] else "acima"
        avisos.append(
            f"Fase B informada à mão em {r.horas_fase_b} h, {lado} da faixa de referência de "
            f"{r.faixa_fase_b[0]} a {r.faixa_fase_b[1]} h. É decisão sua e o "
            "script obedece — registre o motivo nas premissas do analise.md, senão o desvio some "
            "da conta e o `realizado.json` compara com uma estimativa que ninguém sabe de onde veio."
        )

    if r.dias_fase_1 > DIAS_FASE_B:
        avisos.append(
            f"A fase 1 ficou com {r.dias_fase_1} dias úteis, não {DIAS_FASE_B}: são "
            f"{r.horas_fase_b} h de levantamento mais {r.horas_wireframe} h de wireframe das "
            f"{r.telas} telas, e isso não cabe em {DIAS_FASE_B * HORAS_POR_DIA:.0f} h. **O prazo "
            "da proposta e do contrato usa esse número**, não os dois dias de sempre."
        )

    horas_dev_por_fase = max(s.horas for s in r.cronograma[1:])
    if horas_dev_por_fase > HORAS_POR_SEMANA:
        avisos.append(
            f"Fase de desenvolvimento com {horas_dev_por_fase} h, acima das "
            f"{HORAS_POR_SEMANA:.0f} h disponíveis na semana. Com --semanas informado "
            "à mão você comprimiu o calendário: ou sobem as semanas, ou a entrega de sexta não fecha."
        )

    if r.horas_desenho > r.cronograma[1].horas:
        avisos.append(
            f"O desenho das {r.telas} telas são {r.horas_desenho} h e a primeira semana de "
            f"desenvolvimento tem {r.cronograma[1].horas} h: o mock não cabe nela inteiro e "
            "vai transbordar para a semana seguinte. Diga isso na coluna do que fica pronto do "
            "analise.md, senão a proposta promete o desenho de todas as telas para uma data que "
            "não fecha."
        )

    # Até 31/08/2026 esta conferência era um invariante: em nenhuma semana o esforço
    # já entregue passava o dinheiro já recebido. A ordem nova tirou uma parcela de
    # folga da régua inteira, e o usuário decidiu em 01/09/2026 carregar esse risco
    # em vez de subir a entrada. Então aqui a checagem virou medida, não reprovação:
    # ela existe para o número aparecer antes de a proposta sair, nunca depois.
    descobertas = [s for s in r.cronograma if s.risco > 0 and 1 < s.indice < r.fases]
    if descobertas:
        piores = ", ".join(f"fase {s.indice} ({brl0(s.risco)})" for s in descobertas)
        pior = max(s.risco for s in descobertas)
        avisos.append(
            f"Trabalho não pago nas fases de desenvolvimento — {piores}. É esperado desde "
            "01/09/2026: a entrada saiu da assinatura e passou para a aprovação do projeto "
            "escrito, então a régua andou uma casa e perdeu a parcela de folga que cobria a "
            f"reta final. Decisão do usuário: mantém-se a entrada e carrega-se o risco, que "
            f"aqui chega a {brl0(pior)} antes da última fase. Se esse número incomodar neste "
            "projeto, o remédio é subir a entrada, não cortar o levantamento."
        )

    risco_fase_1 = r.cronograma[0].risco
    if risco_fase_1 > 0:
        avisos.append(
            f"Fase 1 descoberta por {brl0(risco_fase_1)}, ou {pct1(risco_fase_1 / r.preco)} do "
            "projeto — é o levantamento, e é deliberado desde 01/09/2026: o projeto escrito é "
            "apresentado antes do contrato, e é a aprovação dele que abre a assinatura. O que "
            "protege esse trabalho não é parcela, é o documento não sair da sua mão: o cliente "
            "lê, ajusta e aprova, e só recebe o arquivo anexado ao contrato que ele assinou."
        )

    risco_final = r.cronograma[-1].risco
    if risco_final > 0:
        avisos.append(
            f"Última fase descoberta por {brl0(risco_final)}, ou {pct1(risco_final / r.preco)} do "
            "projeto — é a parcela final, e é deliberado: ela fica presa ao aceite com o app "
            "publicado. Se o cliente sumir nessa semana, a Cláusula 6.6 do contrato segura o "
            "código e as chaves das lojas, então ele não leva o app. Fora ela e a fase 1, todas "
            "as fases estão pagas antes de começar."
        )

    # O limiar conta SEMANAS DE DESENVOLVIMENTO, que é o que a constante nomeia.
    # Comparar com r.fases inflaria o corte em uma semana desde que a fase 1 deixou
    # de ocupar uma: fases = 1 + semanas de dev.
    if r.fases - 1 > SEMANAS_PROJETO_LONGO:
        avisos.append(
            f"Projeto de {r.fases - 1} semanas de desenvolvimento, acima de "
            f"{SEMANAS_PROJETO_LONGO}: são {r.fases} entregas e "
            f"{r.fases} parcelas, e isso é muita cobrança para um cliente novo. Considere propor só a "
            "primeira metade do escopo agora. Ele também trava a agenda por meses."
        )

    if r.piso < MINIMO_PROJETO:
        avisos.append(
            f"Piso de negociação ({brl0(r.piso)}) abaixo do mínimo de projeto "
            f"({brl0(MINIMO_PROJETO)}): não há espaço real de desconto — corte escopo, não preço."
        )

    if r.orcamento_cliente is not None:
        if r.orcamento_cliente < r.piso:
            avisos.append(
                f"Orçamento do cliente ({brl0(r.orcamento_cliente)}) abaixo do piso de negociação "
                f"({brl0(r.piso)}): só fecha cortando escopo de produto — tela, funcionalidade, integração. "
                "O levantamento não é item cortável; cortar tela corta o desenho dela junto."
            )
        elif r.orcamento_cliente < r.preco:
            avisos.append(
                f"Orçamento do cliente ({brl0(r.orcamento_cliente)}) entre o piso e o preço: "
                "cabe negociação sem desmontar o escopo."
            )

    return avisos


def calcula(
    horas_dev: int,
    telas: int,
    orcamento_cliente: float | None = None,
    fases_manual: int | None = None,
    horas_fase_b_manual: int | None = None,
    fator_estimativa: float = FATOR_ESTIMATIVA_PADRAO,
) -> Resultado:
    alertas: list[str] = []

    horas_dev_informado = horas_dev
    if fator_estimativa != 1.0:
        horas_dev = math.ceil(horas_dev * fator_estimativa)
        alertas.append(
            f"Fator de estimativa {fator_estimativa} aplicado: {horas_dev_informado} h "
            f"estimadas viraram {horas_dev} h de desenvolvimento na conta. Origem do fator: "
            "`produtividade.py calibrar`. A Fase B, o wireframe e o desenho das telas não "
            "recebem fator."
        )

    h_desenho = horas_desenho(telas)
    h_wireframe = horas_wireframe(telas)
    horas_execucao = horas_dev + h_desenho

    h_fase_b, faixa = horas_levantamento()
    if horas_fase_b_manual is not None:
        h_fase_b = horas_fase_b_manual
    h_fase_1 = h_fase_b + h_wireframe
    dias_1 = dias_da_fase_1(h_fase_1)
    horas_total = horas_execucao + h_fase_1
    subtotal = horas_total * VALOR_HORA
    com_gordura = subtotal * (1 + GORDURA)
    preco = arredonda_para_cima(com_gordura)

    fases = fases_manual if fases_manual is not None else numero_de_fases(horas_execucao)
    if fases < 2:
        raise ValueError(
            f"fases={fases}: o desenho e o desenvolvimento nunca cabem na mesma entrega, "
            "então todo projeto tem no mínimo 2 fases."
        )
    horas = horas_das_fases(horas_execucao, h_fase_1, fases)
    if min(horas[1:]) <= 0:
        raise ValueError(
            f"fases={fases} para {horas_execucao} h de desenvolvimento: sobra fase sem hora "
            "nenhuma, e entrega vazia não é entrega. O calendário derivado das horas nunca "
            "faz isso — reduza --semanas."
        )
    fatia_do_levantamento = h_fase_1 / horas_total if horas_total else 0.0
    regua = regua_das_fases(fases, fatia_do_levantamento)
    eventos = eventos_das_fases(fases)
    parcelas = calcula_parcelas(preco, regua, eventos)
    regua_nominal = regua
    # Daqui para baixo vale a régua EFETIVA, medida nos valores já arredondados: é
    # ela que sai na tabela, e o motivo tem de citar o mesmo número que a tabela.
    # Ela não vai para o Resultado: Parcela.percentual já é essa mesma informação,
    # e guardar as duas é como elas divergem.
    regua = tuple(p.percentual for p in parcelas)
    cronograma = monta_cronograma(horas, regua, preco, dias_1)

    # Os percentuais saem no mesmo formato da tabela de parcelas: "20%" e "20,0%"
    # no mesmo documento, para a mesma parcela, é erro que o leitor nota.
    prazo = prazo_texto(fases, dias_1)
    if len(set(round(p, 3) for p in regua)) == 1:
        motivo = (
            f"projeto de {fases} fases ({prazo}): as parcelas cabem iguais, uma por fase, "
            f"{pct_auto(regua[0])} cada"
        )
    elif fases == 2:
        # Com duas fases o único piso que desiguala é o da fatia do levantamento.
        motivo = (
            f"projeto de 2 fases ({prazo}): entrada de {pct_auto(regua[0])}, porque a fase do "
            f"levantamento vale mais que a metade do esforço, e {pct_auto(regua[1])} no aceite"
        )
    else:
        motivo = (
            f"projeto de {fases} fases ({prazo}): entrada de {pct_auto(regua[0])} e o resto "
            f"dividido por igual pelas {fases - 1} entregas seguintes, {pct_auto(regua[1])} cada"
        )

    imposto, liquido = liquido_de(preco)
    hora_efetiva = liquido / horas_total if horas_total else 0.0

    # Piso de negociação: a estimativa sem gordura, medida no líquido. Com
    # GORDURA em 0.0 ele coincide com o preço cheio.
    piso = arredonda_para_cima(subtotal * (1 + GORDURA_NO_PISO))
    _, piso_liquido = liquido_de(piso)
    piso_hora_efetiva = piso_liquido / horas_total if horas_total else 0.0

    resultado = Resultado(
        horas_dev=horas_dev,
        horas_dev_informado=horas_dev_informado,
        fator_estimativa=fator_estimativa,
        telas=telas,
        fases=fases,
        cronograma=cronograma,
        horas_desenho=h_desenho,
        horas_wireframe=h_wireframe,
        horas_execucao=horas_execucao,
        horas_fase_b=h_fase_b,
        horas_fase_1=h_fase_1,
        dias_fase_1=dias_1,
        faixa_fase_b=faixa,
        horas_total=horas_total,
        subtotal=subtotal,
        com_gordura=com_gordura,
        preco=preco,
        motivo_regua=motivo,
        parcelas=parcelas,
        imposto=imposto,
        liquido=liquido,
        hora_efetiva=hora_efetiva,
        piso=piso,
        piso_liquido=piso_liquido,
        piso_hora_efetiva=piso_hora_efetiva,
        orcamento_cliente=orcamento_cliente,
        semanas_manuais=fases_manual is not None,
        fase_b_manual=horas_fase_b_manual is not None,
        alertas=alertas,
    )
    # Os dois alertas de cima nascem no meio da conta, com estado que não sobrevive
    # até aqui. O resto é conferência sobre o resultado pronto, e mora em checagens().
    resultado.alertas.extend(checagens(resultado))
    return resultado


# --------------------------------------------------------------------------
# Saída em Markdown, pronta para colar no analise.md
# --------------------------------------------------------------------------


def brl(valor: float) -> str:
    # Milhar separado por espaço e vírgula decimal, por decisão do usuário: R$ 12 000,50.
    return "R$ " + f"{valor:,.2f}".replace(",", " ").replace(".", ",")


def brl0(valor: float) -> str:
    return "R$ " + f"{valor:,.0f}".replace(",", " ")


def pct(valor: float) -> str:
    return f"{valor * 100:.0f}%"


def pct1(valor: float) -> str:
    """Percentual com uma casa, em vírgula decimal."""
    return f"{valor * 100:.1f}%".replace(".", ",")


def pct_auto(valor: float) -> str:
    """20% quando é redondo, 11,4% quando não é. Percentual com casa decimal só
    onde ela informa alguma coisa."""
    return pct(valor) if abs(valor * 100 - round(valor * 100)) < 0.05 else pct1(valor)


def num1(valor: float) -> str:
    return f"{valor:.1f}".replace(".", ",")


def num2(valor: float) -> str:
    """Duas casas: o fator de estimativa é 1,38, não 1,4."""
    return f"{valor:.2f}".replace(".", ",")


NUMERO_POR_EXTENSO = {
    1: "uma", 2: "duas", 3: "três", 4: "quatro", 5: "cinco", 6: "seis", 7: "sete",
    8: "oito", 9: "nove", 10: "dez", 11: "onze", 12: "doze", 13: "treze",
    14: "catorze", 15: "quinze", 16: "dezesseis", 17: "dezessete",
    18: "dezoito", 19: "dezenove", 20: "vinte",
}


NUMERO_POR_EXTENSO_MASCULINO = {2: "dois", 3: "três", 4: "quatro", 5: "cinco"}


def extenso_masculino(n: int) -> str:
    """Quantidade por extenso no masculino: "dois dias", não "duas dias".

    `NUMERO_POR_EXTENSO` é feminino porque nasceu contando parcelas. A fase 1 conta
    dias, e o gênero errado num texto que vai colado para o cliente é o tipo de
    defeito que só aparece depois de enviado.
    """
    return NUMERO_POR_EXTENSO_MASCULINO.get(n, str(n))


def extenso(n: int) -> str:
    """Quantidade por extenso, que é como o texto da proposta escreve.

    Acima de 20 cai no algarismo: "vinte e três parcelas" lê pior que "23", e
    projeto desse tamanho já disparou o alerta de projeto longo muito antes.
    """
    return NUMERO_POR_EXTENSO.get(n, str(n))


def frase_da_cobranca(r: Resultado) -> str:
    """A frase da cobrança, que vai no movimento 4 da proposta.

    Separada da régua em 18/08/2026, quando o movimento 4 passou a contar as fases:
    a **mecânica** do dinheiro é dita uma vez, aqui, e o movimento 5 leva só os
    **valores**. Repetir a mecânica nos dois é o defeito mais provável de um texto
    de 700 palavras.

    Em cliente direto não há custódia nem plataforma no meio: o que se promete é
    cobrança por entrega, e é verdade. Nunca escreva "o dinheiro fica retido" nem
    cite plataforma nenhuma — era a redação do canal antigo, e hoje é falsa.

    **A entrada é nomeada antes da cobrança por entrega, e isso não é estilo.** A 1ª
    parcela é cobrada na assinatura, antes de qualquer entrega: dizer só "cada parte
    é cobrada depois que você recebe" é falso justamente na primeira cobrança. Até
    19/08/2026 esta função devolvia a frase sem a oração da entrada, e a
    `varredura.py` reprovava a própria saída do script.

    **E a frase diz o que a entrada compra, desde 26/08/2026.** O "dali em diante"
    da redação anterior marcava um degrau e deixava a entrada, por eliminação, como a
    parcela paga sem contrapartida. Ver `references/tom-e-exemplos.md`, primeira
    das três frases literais no fim do movimento 4.

    **Em 01/09/2026 a ordem mudou e a frase mudou junto.** O projeto escrito é
    apresentado antes de qualquer pagamento; é a aprovação dele que abre a
    assinatura, com o documento anexado ao contrato. A entrada continua nomeada e
    continua dizendo o que compra — só que agora compra uma entrega que o cliente já
    tem aprovada na mão, que é o argumento mais forte que esta frase já teve.
    """
    return (
        "Com o projeto aprovado, ele vai anexado ao contrato, e é aí que você assina "
        "os dois e paga a entrada: ela paga o projeto escrito que você acabou de "
        "aprovar e reserva a agenda. "
        "Cada parcela seguinte só é cobrada depois que você recebe a entrega da fase."
    )


def frase_da_regua(r: Resultado) -> str:
    """A frase da régua que vai no movimento 5 da proposta, pronta para colar.

    Mora aqui, e não na cabeça de quem escreve, porque desde 18/08/2026 os valores
    das parcelas vão no texto de venda: frase montada à mão é como a proposta passa
    a divergir da Cláusula 2ª do contrato.

    **O sujeito é "não paga", e isso mudou com o fim do canal de plataforma.** Sem
    custódia no meio, o desembolso do cliente é de fato parcelado: ele paga cada
    parcela quando ela vence. A redação antiga, "não me paga", descrevia liberação
    de escrow e hoje só confunde.

    **A entrada é sempre nomeada, inclusive quando a régua é plana.** "Cinco
    parcelas iguais" deixa o cliente sem saber quanto ele desembolsa para começar,
    que é justamente a informação que a frase existe para dar. Correção de
    18/08/2026, vinda de uma proposta gerada sem o valor da entrada.

    Vão só os valores, e a mecânica não se repete aqui: ela é dita no movimento 4,
    por `frase_da_cobranca`. O evento que destrava cada parcela continua fora do
    texto: é ali que a régua vira tabela e o texto de venda vira contrato.
    """
    if r.fases < 2:
        return f"O valor é de {brl0(r.parcelas[0].valor)}, em uma parcela só."
    valores = (
        f"entrada de {brl0(r.parcelas[0].valor)} e mais {extenso(r.fases - 1)} de "
        f"{brl0(r.parcelas[1].valor)}, uma por entrega"
    )
    return f"E você não paga tudo de uma vez: {valores}."


def markdown(r: Resultado) -> str:
    linhas: list[str] = []
    add = linhas.append

    add("## A conta do preço")
    add("")
    add(f"Telas: **{r.telas}** · Prazo: **{prazo_texto(r.fases, r.dias_fase_1)}**")
    add("")
    add("| Bloco | Horas |")
    add("|---|---|")
    rotulo_dev = "Desenvolvimento (estimativa)"
    if r.fator_estimativa != 1.0:
        rotulo_dev += f" — {r.horas_dev_informado} h × fator {num2(r.fator_estimativa)} do histórico"
    add(f"| {rotulo_dev} | {r.horas_dev} h |")
    add(
        f"| Desenho das telas (o mock que o cliente recebe) — {r.telas} × "
        f"{HORAS_DESENHO_POR_TELA} h, na 1ª semana de dev | {r.horas_desenho} h |"
    )
    add(
        f"| Levantamento (Fase B) — requisitos, análise técnica e handoff | {r.horas_fase_b} h |"
    )
    add(
        f"| Wireframe de planejamento (interno, não entregável) — {r.telas} × "
        f"{num1(HORAS_WIREFRAME_POR_TELA)} h, dentro da fase 1 | {r.horas_wireframe} h |"
    )
    add(f"| **Total** | **{r.horas_total} h** |")
    add("")
    add(
        f"Faixa de referência da Fase B: {r.faixa_fase_b[0]} a {r.faixa_fase_b[1]} h, independente "
        f"do número de telas. Com o wireframe de planejamento, a **fase 1 fecha em "
        f"{r.horas_fase_1} h e {r.dias_fase_1} dias úteis**. **O desenho que o cliente recebe não "
        "está aí dentro**: ele é bloco de desenvolvimento e é entregue na primeira semana de dev."
    )
    add("")
    add(f"- {r.horas_total} h × {brl0(VALOR_HORA)}/h = **{brl(r.subtotal)}**")
    if GORDURA:
        add(f"- Gordura de {pct(GORDURA)} = **{brl(r.com_gordura)}**")
    add(f"- Arredondado para cima: **{brl0(r.preco)}**")
    add("")

    add("## As fases")
    add("")
    horas_dev_fase = [s.horas for s in r.cronograma[1:]]
    faixa_fase = (
        f"{min(horas_dev_fase)} h" if len(set(horas_dev_fase)) == 1
        else f"{min(horas_dev_fase)} h a {max(horas_dev_fase)} h"
    )
    cabe_na_semana = max(horas_dev_fase) <= HORAS_POR_SEMANA
    dentro = (
        f"dentro das {HORAS_POR_SEMANA:.0f} h disponíveis na semana"
        if cabe_na_semana
        else f"**acima das {HORAS_POR_SEMANA:.0f} h disponíveis na semana** (ver Checagens)"
    )
    abertura = (
        f"**{r.fases} fases: {prazo_texto(r.fases, r.dias_fase_1)}.** A fase 1 é o levantamento "
        "— requisitos, análise técnica, handoff e o wireframe de planejamento —, dura "
        f"{r.dias_fase_1} dias úteis e entrega {entrega_da_fase_1(r.dias_fase_1)}. "
    )
    if len(horas_dev_fase) == 1:
        # Uma fase de dev só: falar em "a primeira delas" e "a última" prometeria
        # duas entregas onde a tabela logo abaixo mostra uma.
        add(
            abertura + f"A de desenvolvimento é uma semana de {faixa_fase}, {dentro}, com entrega "
            f"na {DIA_DA_ENTREGA}, e nela cabem o desenho das telas, o desenvolvimento e a "
            "publicação nas lojas."
        )
    else:
        add(
            abertura + f"As de desenvolvimento são semanas de {faixa_fase} cada, {dentro}, com "
            f"entrega na {DIA_DA_ENTREGA}. A primeira delas traz o desenho das telas; a última "
            "termina com o app publicado."
        )
    add("")
    add("| Fase | Duração | O que fica pronto | Horas |")
    add("|---|---|---|---|")
    for s in r.cronograma:
        add(f"| {s.indice} | {s.duracao} | {s.entrega} | {s.horas} h |")
    add("")
    add(
        "O esforço das fases de desenvolvimento é igual de propósito: é isso que impede uma "
        "entrega de valer mais do que a parcela dela. **As horas não se remanejam entre fases** "
        "— o que não coube na semana entra na seguinte, e o conteúdo de cada uma é que se ajusta. "
        "Preencha a coluna do que fica pronto no analise.md com o conteúdo real; na proposta, diga "
        "quanto tempo é cada fase e o que ele recebe em cada uma, sem citar horas."
    )
    add("")
    add(
        "**A entrega de fase não é portão de aprovação:** o cliente recebe, usa e comenta, e o "
        "trabalho segue para a semana seguinte. As únicas aprovações que travam o andamento são "
        "as do contrato, requisitos e aceite."
    )
    add("")
    add(
        "O tempo que o cliente leva para ler e aprovar o que a fase 1 entrega **não entra no "
        f"prazo de {r.fases - 1} semanas de desenvolvimento**: ele só começa a correr na "
        "aprovação, e somá-lo aqui contaria o mesmo período duas vezes."
    )
    add("")

    add("## Régua de pagamento")
    add("")
    add(f"**Uma parcela por fase** — {r.motivo_regua}.")
    add("")
    add("| # | % | Valor | Evento que libera |")
    add("|---|---|---|---|")
    for p in r.parcelas:
        add(f"| {p.indice} | {pct_auto(p.percentual)} | {brl(p.valor)} | {p.evento} |")
    add("")
    if r.fases == 2:
        p1, p2 = r.parcelas
        if abs(p1.percentual - 0.5) < 0.005:
            add(
                "São dois marcos: metade na assinatura, que só acontece depois de ele aprovar o "
                "projeto escrito, e metade no aceite, com o app publicado. A entrada paga a fase "
                "1, o projeto escrito, que a essa altura já está entregue e aprovado."
            )
        else:
            add(
                f"São dois marcos: {pct_auto(p1.percentual)} na assinatura, que só acontece "
                f"depois de ele aprovar o projeto escrito, e {pct_auto(p2.percentual)} no aceite, "
                "com o app publicado. A entrada passa da metade porque a fase do levantamento "
                "vale mais que isso do esforço (ver Checagens). A entrada paga a fase 1, o "
                "projeto escrito, que a essa altura já está entregue e aprovado."
            )
    else:
        add(
            "A entrada sai na aprovação do projeto escrito, e daí em diante cada entrega libera "
            "a parcela da **fase seguinte**, então o dinheiro chega antes do trabalho. As duas "
            "exceções são as pontas: a fase 1 é trabalhada antes de existir contrato, e a última "
            "parcela fica presa ao aceite final. Com a fase 1 entregue antes da entrada, **toda "
            "entrega tem parcela** — não sobra mais entrega sem par."
        )
    add("")
    add(
        "**As duas frases que vão no texto da proposta**, prontas para colar. A "
        "mecânica do dinheiro é dita uma vez, no movimento 4:"
    )
    add("")
    add(f"> {frase_da_cobranca(r)}")
    add("")
    add(
        "E os valores vão no movimento 5, sem repetir a mecânica. O que destrava cada "
        "parcela fica para a conversa e para a Cláusula 2ª:"
    )
    add("")
    add(f"> {frase_da_regua(r)}")
    add("")

    add("## A conferência do prejuízo")
    add("")
    add(
        "O que a régua promete desde 01/09/2026: nas primeiras fases o dinheiro recebido cobre "
        "o esforço entregue, e nas últimas ele deixa de cobrir. A causa é a ordem nova — a "
        "entrada só é paga depois de o cliente aprovar o projeto escrito, então toda a régua "
        "andou uma casa e perdeu uma parcela de folga. Duas pontas ficam descobertas por "
        "decisão: a fase 1, que é trabalhada antes de existir contrato, e a última, presa ao "
        "aceite. O maior número da coluna da direita é o teto do prejuízo do projeto."
    )
    add("")
    add("| Fase | Recebido antes de começar | Entregue no fim dela | Se ele sumir nesta fase |")
    add("|---|---|---|---|")
    for s in r.cronograma:
        risco = "sem prejuízo" if s.risco <= 0 else f"**{brl0(s.risco)}**"
        add(
            f"| {s.indice} | {pct_auto(s.recebido_antes)} ({brl0(s.recebido_antes * r.preco)}) | "
            f"{pct_auto(s.entregue_ate)} | {risco} |"
        )
    add("")

    add("## O líquido")
    add("")
    add("| Item | Valor |")
    add("|---|---|")
    add(f"| Bruto | {brl(r.preco)} |")
    add(f"| Simples Nacional ({pct(SIMPLES)}, sobre o bruto) | −{brl(r.imposto)} |")
    add(f"| **Líquido** | **{brl(r.liquido)}** |")
    add("")
    add(
        f"Valor/hora efetivo: **{brl(r.hora_efetiva)}/h** sobre {r.horas_total} h "
        f"({r.liquido / r.preco * 100:.0f}% do bruto)."
    )
    add("")

    add("## Piso de negociação")
    add("")
    explicacao_piso = (
        f"é a estimativa sem a gordura de {pct(GORDURA)}"
        if GORDURA
        else "é a própria estimativa: o preço já sai sem gordura, então não há folga a ceder"
    )
    add(
        f"**{brl0(r.piso)}** — {explicacao_piso}. "
        f"Líquido nesse piso: {brl(r.piso_liquido)}, ou {brl(r.piso_hora_efetiva)}/h efetivos."
    )
    add("")
    add(
        "Abaixo disso, corte **escopo de produto** (tela, funcionalidade, integração). "
        "O levantamento não é item cortável: é ele que a **entrada paga**, é a aprovação dele "
        "que abre a assinatura do contrato, e é ela que faz o prazo começar a correr. Cortar tela corta o "
        "desenho dela junto, uma hora por tela. **Cortar escopo encurta o calendário**, então "
        "recalcule: menos semanas é menos parcelas."
    )
    add("")

    add("## Checagens")
    add("")
    if r.alertas:
        for a in r.alertas:
            add(f"- ⚠️ {a}")
    else:
        add("- Nenhuma checagem disparada.")
    add("")
    return "\n".join(linhas)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Calcula preço, régua de pagamento, líquido e piso de uma proposta de freela.",
    )
    p.add_argument("--horas", type=int, required=True, help="horas estimadas de desenvolvimento (sem a Fase B)")
    p.add_argument("--telas", type=int, required=True, help="número de telas do app")
    p.add_argument("--orcamento-cliente", type=float, default=None, help="orçamento informado pelo cliente, se houver")
    p.add_argument(
        "--semanas",
        type=int,
        default=None,
        help=(
            "número de fases; se omitido, é 1 (o levantamento, de 2 dias) + as semanas de "
            "desenvolvimento arredondadas para cima. Informe à mão só para esticar o calendário, "
            "nunca para comprimir"
        ),
    )
    p.add_argument(
        "--horas-levantamento",
        type=int,
        default=None,
        help=f"sobrescreve as horas da Fase B (padrão: ponto médio de {LEVANTAMENTO_FAIXA})",
    )
    p.add_argument(
        "--fator-estimativa",
        type=float,
        default=FATOR_ESTIMATIVA_PADRAO,
        help="correção das horas de dev pelo histórico real; sai de `produtividade.py calibrar`",
    )
    args = p.parse_args()

    if args.horas <= 0 or args.telas <= 0:
        p.error("--horas e --telas precisam ser maiores que zero")
    if args.semanas is not None and args.semanas < 2:
        p.error("--semanas precisa ser 2 ou mais: a fase 1 é o levantamento e o desenvolvimento vem depois")
    if args.horas_levantamento is not None and args.horas_levantamento <= 0:
        p.error("--horas-levantamento precisa ser maior que zero: a Fase B acontece em todo projeto")
    if args.orcamento_cliente is not None and args.orcamento_cliente <= 0:
        p.error("--orcamento-cliente precisa ser maior que zero")
    if args.fator_estimativa <= 0:
        p.error("--fator-estimativa precisa ser maior que zero")

    r = calcula(
        horas_dev=args.horas,
        telas=args.telas,
        orcamento_cliente=args.orcamento_cliente,
        fases_manual=args.semanas,
        horas_fase_b_manual=args.horas_levantamento,
        fator_estimativa=args.fator_estimativa,
    )
    print(markdown(r))


if __name__ == "__main__":
    main()
