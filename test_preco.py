#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Testes de fronteira da calculadora. É nas bordas que a régua muda.

Uso: uv run test_preco.py
"""

from __future__ import annotations

import pathlib
import unittest

import preco
from preco import (
    ENTRADA_MINIMA,
    GORDURA,
    PASSO_PARCELA,
    arredonda_para_cima,
    calcula,
    eventos_das_fases,
    numero_de_fases,
    horas_das_fases,
    horas_levantamento,
    recebido_antes_da_fase,
    regua_das_fases,
)


# Fictional rates: the real ones are private and come from secrets
TEST_HOURLY_RATE = 50.0
TEST_MIN_PROJECT = 3_000.0
_real_rates: tuple[float, float] = (0.0, 0.0)


class TestRatesStayPrivate(unittest.TestCase):
    def test_source_keeps_rates_zeroed(self):
        # The bot's repo is public: the real rates come from secrets, never from here
        source = (pathlib.Path(__file__).parent / "preco.py").read_text()
        self.assertIn("\nVALOR_HORA = 0.0 ", source)
        self.assertIn("\nMINIMO_PROJETO = 0.0 ", source)


def setUpModule() -> None:
    global _real_rates
    _real_rates = (preco.VALOR_HORA, preco.MINIMO_PROJETO)
    preco.VALOR_HORA, preco.MINIMO_PROJETO = TEST_HOURLY_RATE, TEST_MIN_PROJECT


def tearDownModule() -> None:
    preco.VALOR_HORA, preco.MINIMO_PROJETO = _real_rates


def alerta_contem(r, trecho: str) -> bool:
    return any(trecho.lower() in a.lower() for a in r.alertas)


class TestArredondamento(unittest.TestCase):
    def test_multiplo_de_cem_ate_dez_mil(self):
        self.assertEqual(arredonda_para_cima(4_780), 4_800)
        self.assertEqual(arredonda_para_cima(4_800), 4_800)
        self.assertEqual(arredonda_para_cima(9_901), 10_000)

    def test_multiplo_de_quinhentos_acima_de_dez_mil(self):
        self.assertEqual(arredonda_para_cima(10_000), 10_000)
        self.assertEqual(arredonda_para_cima(10_001), 10_500)


class TestLevantamento(unittest.TestCase):
    """8 h em 2 dias, e só requisitos, análise técnica e handoff: o desenho saiu daqui."""

    def test_ponto_medio_da_faixa(self):
        horas, faixa = horas_levantamento()
        self.assertEqual(faixa, (6, 10))
        self.assertEqual(horas, 8)

    def test_nao_depende_do_numero_de_telas(self):
        # Regressão: a Fase B saía do número de telas porque incluía os wireframes.
        # Com o desenho fora dela, o que sobrou é conversa de negócio e não escala
        # com tela nenhuma.
        for telas in (3, 8, 11, 25):
            self.assertEqual(calcula(horas_dev=90, telas=telas).horas_fase_b, 8)

    def test_cabe_nos_dois_dias_uteis(self):
        horas, faixa = horas_levantamento()
        self.assertLessEqual(faixa[1], preco.DIAS_FASE_B * preco.HORAS_POR_DIA)
        self.assertLessEqual(horas, preco.DIAS_FASE_B * preco.HORAS_POR_DIA)


class TestDesenhoEWireframe(unittest.TestCase):
    """Dois blocos distintos: o mock que o cliente recebe e o rascunho interno.

    O desenho (1 h por tela) é entregue e mora no desenvolvimento; o wireframe de
    planejamento (0,5 h por tela) é interno e mora na fase 1, junto do levantamento.
    """

    def test_uma_hora_de_desenho_e_meia_de_wireframe_por_tela(self):
        r = calcula(horas_dev=90, telas=8)
        self.assertEqual(r.horas_desenho, 8)
        self.assertEqual(r.horas_wireframe, 4)
        self.assertEqual(r.horas_execucao, 98)  # dev + desenho
        self.assertEqual(r.horas_fase_1, 12)  # Fase B + wireframe
        self.assertEqual(r.horas_total, 98 + 12)

    def test_wireframe_arredonda_o_bloco_para_cima(self):
        # 9 telas dão 4,5 h, e hora de fase é inteira.
        self.assertEqual(preco.horas_wireframe(9), 5)
        self.assertEqual(preco.horas_wireframe(8), 4)

    def test_a_fase_1_cresce_em_dias_quando_o_wireframe_nao_cabe(self):
        # 8 h de Fase B + 0,5 h × telas contra 6 h por dia útil.
        cabe = calcula(horas_dev=90, telas=8)  # 12 h → 2 dias
        estoura = calcula(horas_dev=90, telas=16)  # 16 h → 3 dias
        self.assertEqual(cabe.dias_fase_1, 2)
        self.assertEqual(estoura.dias_fase_1, 3)
        self.assertTrue(alerta_contem(estoura, "não"))
        self.assertIn("3 dias de levantamento", preco.markdown(estoura))
        self.assertIn("no fim do terceiro dia útil", preco.markdown(estoura))

    def test_fase_b_a_mao_tambem_estica_a_fase_1(self):
        # O caminho manual é o menos exercitado e o único que faz a Fase B sozinha
        # estourar os dois dias: 20 h de levantamento não cabem em 12 h.
        r = calcula(horas_dev=150, telas=8, horas_fase_b_manual=20)
        self.assertEqual(r.horas_fase_1, 24)  # 20 h + 4 h de wireframe
        self.assertEqual(r.dias_fase_1, 4)
        self.assertTrue(alerta_contem(r, "ficou com 4 dias úteis"))
        self.assertIn("4 dias de levantamento", preco.markdown(r))

    def test_fase_b_a_mao_dentro_dos_dias_nao_estica(self):
        r = calcula(horas_dev=150, telas=8, horas_fase_b_manual=8)
        self.assertEqual(r.dias_fase_1, preco.DIAS_FASE_B)
        self.assertFalse(alerta_contem(r, "dias úteis, não"))

    def test_a_fase_1_nunca_desce_do_piso_de_dois_dias(self):
        r = calcula(horas_dev=40, telas=1)  # 8,5 h → caberia em 2 dias justos
        self.assertEqual(r.dias_fase_1, preco.DIAS_FASE_B)

    def test_o_desenho_entra_no_calendario_de_desenvolvimento(self):
        # 28 h de dev cabem em 1 semana; com 20 telas o desenho empurra para 2.
        poucas = calcula(horas_dev=28, telas=2)
        muitas = calcula(horas_dev=28, telas=20)
        self.assertEqual(poucas.fases, 2)
        self.assertEqual(muitas.fases, 3)

    def test_o_wireframe_nao_entra_no_calendario_de_dev(self):
        # Ele é hora da fase 1: não pode empurrar semana de desenvolvimento.
        # 28 h de dev + 2 h de desenho fecham exatamente uma semana; a 1 h de
        # wireframe das 2 telas fica na fase 1 e não abre uma segunda semana.
        r = calcula(horas_dev=28, telas=2)
        self.assertEqual(r.horas_execucao, 30)
        self.assertEqual(r.fases, 2)

    def test_a_primeira_semana_de_dev_entrega_o_desenho(self):
        md = preco.markdown(calcula(horas_dev=150, telas=8))
        self.assertIn("o desenho das telas e o primeiro bloco", md)

    def test_nenhum_dos_dois_recebe_fator_de_estimativa(self):
        cru = calcula(horas_dev=90, telas=8)
        com_fator = calcula(horas_dev=90, telas=8, fator_estimativa=1.5)
        self.assertEqual(cru.horas_desenho, com_fator.horas_desenho)
        self.assertEqual(cru.horas_wireframe, com_fator.horas_wireframe)

    def test_desenho_que_nao_cabe_na_primeira_semana_alerta(self):
        r = calcula(horas_dev=30, telas=40, fases_manual=4)
        self.assertGreater(r.horas_desenho, r.cronograma[1].horas)
        self.assertTrue(alerta_contem(r, "não cabe nela inteiro"))


class TestLiquido(unittest.TestCase):
    """Sem plataforma no meio, o imposto é o único desconto entre bruto e líquido."""

    def test_o_unico_desconto_e_o_simples(self):
        for dev in (30, 90, 150, 280, 500):
            r = calcula(horas_dev=dev, telas=8)
            self.assertAlmostEqual(r.imposto, r.preco * preco.SIMPLES, places=2)
            self.assertAlmostEqual(r.liquido, r.preco - r.imposto, places=2)

    def test_liquido_e_noventa_e_quatro_por_cento_do_bruto(self):
        r = calcula(horas_dev=90, telas=8)
        self.assertAlmostEqual(r.liquido, r.preco * 0.94, places=2)

    def test_nao_sobrou_comissao_nem_spread_no_resultado(self):
        # Regressão do fim do canal de plataforma: se um destes campos voltar,
        # é sinal de que a conta da comissão voltou junto.
        r = calcula(horas_dev=90, telas=8)
        for campo in ("comissao_total", "comissao_efetiva", "spread", "canal"):
            self.assertFalse(hasattr(r, campo), campo)


class TestCorteDeTriagem(unittest.TestCase):
    def test_abaixo_do_minimo_dispara_descarte(self):
        # 10 h de dev + 3 h de desenho + 8 h de Fase B + 2 h de wireframe = 23 h
        r = calcula(horas_dev=10, telas=3)
        self.assertLess(r.preco, preco.MINIMO_PROJETO)
        self.assertTrue(alerta_contem(r, "ABAIXO DO MÍNIMO:"))

    def test_exatamente_no_minimo_nao_descarta(self):
        # 47 h de dev + 3 h de desenho + 8 h de Fase B + 2 h de wireframe = 60 h,
        # × R$ 50 = R$ 3.000 exatos, o mínimo fictício dos testes.
        r = calcula(horas_dev=47, telas=3)
        self.assertEqual(r.preco, TEST_MIN_PROJECT)
        self.assertFalse(alerta_contem(r, "ABAIXO DO MÍNIMO:"))

    def test_a_fase_b_nao_encolhe_perto_do_minimo(self):
        # A versão enxuta saiu em set/2026: nenhum preço recalcula a Fase B para
        # baixo, e nada é declarado ao cliente como teto de horas.
        for dev in (39, 41, 45):
            r = calcula(horas_dev=dev, telas=6)
            self.assertEqual(r.horas_fase_b, 8, f"{dev} h")
            self.assertFalse(alerta_contem(r, "enxut"), f"{dev} h")

    def test_um_real_abaixo_do_minimo_descarta(self):
        # 45 h + 3 h + 8 h + 2 h = 58 h × R$ 50 = R$ 2.900, um degrau abaixo do mínimo
        r = calcula(horas_dev=45, telas=3)
        self.assertEqual(r.preco, 2_900.0)
        self.assertTrue(alerta_contem(r, "ABAIXO DO MÍNIMO:"))

    def test_projeto_grande_mantem_a_fase_b_cheia(self):
        r = calcula(horas_dev=140, telas=8)
        self.assertEqual(r.horas_fase_b, 8)
        self.assertGreater(r.preco, 5_000)


class TestFasesSemanais(unittest.TestCase):
    """Uma fase por semana: a 1ª é o desenho, as outras são semanas de dev."""

    def test_semana_do_desenho_mais_as_semanas_de_dev(self):
        self.assertEqual(numero_de_fases(30), 2)
        self.assertEqual(numero_de_fases(60), 3)
        self.assertEqual(numero_de_fases(150), 6)

    def test_semana_comecada_conta_inteira(self):
        # Arredonda para cima de propósito: sobra folga na fase em vez de faltar.
        self.assertEqual(numero_de_fases(31), 3)
        self.assertEqual(numero_de_fases(1), 2)
        self.assertEqual(numero_de_fases(121), 6)

    def test_nunca_ha_projeto_de_uma_fase_so(self):
        # O desenho e o desenvolvimento nunca cabem na mesma entrega.
        for horas in range(1, 200):
            self.assertGreaterEqual(numero_de_fases(horas), 2)

    def test_horas_somam_o_total_e_a_primeira_e_a_fase_b(self):
        horas = horas_das_fases(150, 16, 5)
        self.assertEqual(sum(horas), 166)
        self.assertEqual(horas[0], 16)
        self.assertEqual(len(horas), 5)

    def test_esforco_das_fases_de_dev_e_igual(self):
        # A hora de sobra da divisão é espalhada, nunca empilhada numa fase só.
        for dev in (60, 150, 151, 280, 333, 500):
            horas = horas_das_fases(dev, 8, numero_de_fases(dev))
            self.assertLessEqual(max(horas[1:]) - min(horas[1:]), 1, f"{dev} h")

    def test_a_hora_de_sobra_vai_para_o_fim(self):
        # Ordem importa: com a sobra nas primeiras fases o esforço acumulado
        # passaria a reta que a régua paga, e a semana ficaria descoberta.
        horas = horas_das_fases(20, 12, 12)
        self.assertEqual(horas[1], min(horas[1:]))
        self.assertEqual(horas[-1], max(horas[1:]))

    def test_acumulado_nunca_passa_a_reta_do_esforco(self):
        for dev in range(20, 400, 11):
            for fases in (numero_de_fases(dev), 3, 9):
                horas = horas_das_fases(dev, 16, fases)
                total = sum(horas)
                dev_total = total - horas[0]
                acumulado = horas[0]
                for i, h in enumerate(horas[1:], start=1):
                    acumulado += h
                    reta = horas[0] + dev_total * i / (fases - 1)
                    self.assertLessEqual(acumulado, reta + 1e-9, f"{dev} h, {fases} fases")

    def test_nenhuma_fase_de_dev_passa_a_semana_de_trabalho(self):
        for dev in range(20, 600, 7):
            horas = horas_das_fases(dev, 8, numero_de_fases(dev))
            self.assertLessEqual(max(horas[1:]), preco.HORAS_POR_SEMANA, f"{dev} h")


class TestReguaSemanal(unittest.TestCase):
    def test_uma_parcela_por_fase(self):
        for fases in range(2, 30):
            self.assertEqual(len(regua_das_fases(fases)), fases)
            self.assertEqual(len(eventos_das_fases(fases)), fases)

    def test_toda_regua_soma_cem_por_cento(self):
        for fases in range(2, 30):
            self.assertAlmostEqual(sum(regua_das_fases(fases)), 1.0, places=9)

    def test_as_parcelas_depois_da_entrada_saem_sempre_iguais(self):
        # O esforço das semanas de dev é igual, então a parcela delas também é.
        for fases in range(3, 30):
            demais = regua_das_fases(fases)[1:]
            self.assertEqual(len(set(round(p, 9) for p in demais)), 1, f"{fases} fases")

    def test_em_projeto_curto_as_parcelas_sao_todas_iguais(self):
        # Enquanto a fatia de uma semana for maior que a entrada mínima, ela manda
        # e a régua inteira fica plana. Hoje isso vale até 5 semanas.
        for fases in range(2, int(1 / ENTRADA_MINIMA) + 1):
            regua = regua_das_fases(fases)
            self.assertEqual(len(set(round(p, 9) for p in regua)), 1, f"{fases} fases")
            self.assertAlmostEqual(regua[0], 1 / fases, places=9)

    def test_acima_disso_a_entrada_assenta_na_minima(self):
        for fases in range(int(1 / ENTRADA_MINIMA) + 1, 30):
            regua = regua_das_fases(fases)
            self.assertAlmostEqual(regua[0], ENTRADA_MINIMA, places=9)
            self.assertLess(regua[1], ENTRADA_MINIMA)

    def test_a_fronteira_das_cinco_semanas_nao_tem_degrau(self):
        # Em 5 semanas as duas contas dão o mesmo número, então a regra não salta.
        fronteira = int(1 / ENTRADA_MINIMA)
        self.assertAlmostEqual(regua_das_fases(fronteira)[0], ENTRADA_MINIMA, places=9)
        self.assertAlmostEqual(regua_das_fases(fronteira)[-1], ENTRADA_MINIMA, places=9)

    def test_entrada_nunca_fica_abaixo_da_fatia_de_uma_semana(self):
        # É essa desigualdade que sustenta a régua inteira: sem ela, o recebido
        # atrasa em relação ao entregue já na segunda semana.
        for fases in range(2, 40):
            self.assertGreaterEqual(regua_das_fases(fases)[0], 1 / fases - 1e-9)

    def test_primeiro_evento_e_a_assinatura_e_o_ultimo_o_aceite(self):
        eventos = eventos_das_fases(8)
        self.assertIn("assinatura", eventos[0])
        self.assertIn("aceite final", eventos[-1])

    def test_a_aprovacao_do_levantamento_libera_a_primeira_parcela(self):
        # Ordem de 01/09/2026: a fase 1 acontece antes do contrato, e é a
        # aprovação dela que abre a assinatura e a entrada.
        eventos = eventos_das_fases(8)
        self.assertIn("projeto escrito", eventos[0])
        self.assertIn("fase 1", eventos[0])
        self.assertIn("inicia o prazo", eventos[0])

    def test_toda_entrega_tem_parcela(self):
        # Com a fase 1 entregue antes da entrada, as 8 entregas de um projeto de 8
        # fases pareiam com as 8 parcelas: não sobra mais entrega sem par.
        eventos = eventos_das_fases(8)
        self.assertEqual(len(eventos), 8)
        for n in range(2, 8):
            self.assertTrue(any(f"entrega da fase {n}" in e for e in eventos), f"fase {n}")


class TestInvarianteDoPrejuizo(unittest.TestCase):
    """O motivo de a régua existir: o recebido nunca atrasa em relação ao entregue."""

    def test_a_fase_1_e_sempre_trabalho_em_risco(self):
        # Consequência aceita em 01/09/2026: o levantamento é entregue antes de
        # existir contrato, então ele nunca está pago quando é feito.
        for dev in range(20, 600, 13):
            for telas in (5, 12):
                r = calcula(horas_dev=dev, telas=telas)
                self.assertEqual(r.cronograma[0].recebido_antes, 0.0, f"{dev} h")
                self.assertGreater(r.cronograma[0].risco, 0.0, f"{dev} h")

    def test_em_projeto_longo_a_primeira_fase_de_dev_continua_coberta(self):
        # A entrada ainda compra folga, mas só onde há semanas para diluí-la: de
        # 200 h de dev para cima o buraco só abre na reta final. Abaixo disso ele
        # começa já na fase 2, e é por isso que projeto curto é o que mais pede
        # atenção às Checagens.
        for dev in range(200, 600, 13):
            for telas in (5, 12):
                r = calcula(horas_dev=dev, telas=telas)
                self.assertEqual(r.cronograma[1].risco, 0.0, f"{dev} h, {telas} telas")

    def test_em_projeto_curto_o_buraco_comeca_cedo(self):
        # Regressão declarada: com poucas fases não há como diluir, e a fase 2 já
        # entrega mais do que recebeu. O teto continua sendo uma parcela.
        r = calcula(horas_dev=60, telas=8)
        self.assertGreater(r.cronograma[1].risco, 0.0)
        self.assertLessEqual(max(s.risco for s in r.cronograma), r.parcelas[-1].valor)

    def test_o_prejuizo_maximo_do_projeto_e_a_ultima_parcela(self):
        # A promessa da régua, em uma linha: perca o cliente na pior hora possível
        # e o buraco é de no máximo uma parcela.
        for dev in range(20, 600, 11):
            for telas in (4, 9, 18):
                r = calcula(horas_dev=dev, telas=telas)
                pior = max(s.risco for s in r.cronograma)
                self.assertLessEqual(pior, r.parcelas[-1].valor, f"{dev} h, {telas} telas: {pior}")

    def test_o_risco_da_ultima_fase_e_exatamente_a_ultima_parcela(self):
        for dev in (60, 150, 280, 400):
            r = calcula(horas_dev=dev, telas=10)
            self.assertAlmostEqual(r.cronograma[-1].risco, r.parcelas[-1].valor, places=2)

    def test_o_trabalho_nao_pago_da_reta_final_e_sempre_declarado(self):
        # Não é mais reprovação e sim medida: quando alguma fase de dev fica
        # descoberta, o número precisa aparecer nas Checagens antes de a proposta
        # sair. E o teto continua sendo uma parcela.
        for dev in range(20, 600, 29):
            for telas in (3, 8, 20):
                for fases in (None, 3, 12):
                    r = calcula(horas_dev=dev, telas=telas, fases_manual=fases)
                    meio = [s for s in r.cronograma if s.risco > 0 and 1 < s.indice < r.fases]
                    if meio:
                        self.assertTrue(
                            alerta_contem(r, "Trabalho não pago"), f"{dev} h, {telas} telas"
                        )
                    self.assertTrue(alerta_contem(r, "Fase 1 descoberta"), f"{dev} h")

    def test_levantamento_pesado_sobe_a_entrada(self):
        # 3 h de dev com 2 telas é vaga implausível, mas a régua não pode depender
        # de plausibilidade: a entrada sobe para pelo menos cobrir a entrega que
        # ela paga. A fase 1 continua descoberta enquanto é feita, por desenho.
        r = calcula(horas_dev=3, telas=2)
        self.assertGreater(r.parcelas[0].percentual, 0.5)
        self.assertGreaterEqual(r.parcelas[0].percentual, r.cronograma[0].entregue_ate)
        self.assertTrue(alerta_contem(r, "virou o piso da entrada"))

    def test_alerta_do_levantamento_so_dispara_quando_ele_manda(self):
        # Regressão: o alerta chegou a disparar em todo projeto curto, culpando a
        # fase do levantamento por uma alta da entrada que vinha de outro lugar.
        for dev in (35, 56, 150, 280):
            r = calcula(horas_dev=dev, telas=9)
            self.assertFalse(alerta_contem(r, "virou o piso da entrada"), f"{dev} h")

    def test_motivo_diz_quando_as_parcelas_cabem_iguais(self):
        iguais = calcula(horas_dev=100, telas=8)
        self.assertIn("iguais", iguais.motivo_regua)
        entrada_maior = calcula(horas_dev=280, telas=14)
        self.assertIn("entrada de", entrada_maior.motivo_regua)

    def test_motivo_da_regua_cita_o_mesmo_numero_da_tabela(self):
        # O motivo saía da régua nominal e a tabela da efetiva: dois percentuais
        # diferentes para a mesma parcela, no mesmo documento.
        for dev in (56, 150, 280, 520):
            r = calcula(horas_dev=dev, telas=9)
            self.assertIn(preco.pct_auto(r.parcelas[0].percentual), r.motivo_regua, f"{dev} h")

    def test_ultima_fase_descoberta_e_declarada(self):
        r = calcula(horas_dev=280, telas=14)
        self.assertTrue(alerta_contem(r, "Última fase descoberta"))

    def test_recebido_antes_da_fase_conta_a_parcela_da_propria_entrega(self):
        regua = regua_das_fases(8)
        # A fase 1 começa sem nada recebido: ela vem antes do contrato.
        self.assertAlmostEqual(recebido_antes_da_fase(regua, 1), 0.0, places=9)
        self.assertAlmostEqual(recebido_antes_da_fase(regua, 2), regua[0], places=9)
        # A última parcela só sai no aceite, então a última semana começa sem ela.
        self.assertAlmostEqual(recebido_antes_da_fase(regua, 8), sum(regua[:7]), places=9)


class TestParcelas(unittest.TestCase):
    def test_parcelas_somam_o_preco(self):
        for dev in (30, 90, 150, 280, 500):
            r = calcula(horas_dev=dev, telas=8)
            self.assertAlmostEqual(sum(p.valor for p in r.parcelas), r.preco, places=2)

    def test_parcelas_semanais_saem_redondas(self):
        for dev in (60, 90, 150, 280, 333, 500):
            r = calcula(horas_dev=dev, telas=9)
            for p in r.parcelas[1:]:
                self.assertEqual(p.valor % PASSO_PARCELA, 0.0, f"{dev} h: {p.valor}")

    def test_a_sobra_do_arredondamento_vai_para_a_entrada(self):
        r = calcula(horas_dev=60, telas=6)
        self.assertGreaterEqual(r.parcelas[0].valor, r.parcelas[1].valor)
        self.assertGreaterEqual(r.parcelas[0].percentual, ENTRADA_MINIMA)

    def test_parcelas_semanais_sao_todas_do_mesmo_valor(self):
        r = calcula(horas_dev=280, telas=14)
        self.assertEqual(len({p.valor for p in r.parcelas[1:]}), 1)


class TestEntradaNaoTemPiso(unittest.TestCase):
    """Não existe mais piso desejável de 1ª parcela: ele conferia a barreira dos US$ 300.

    A régua sai do número de semanas e não se redesenha por causa do valor da
    entrada. O calendário e a Fase B vão à mão aqui porque a régua semanal não
    produz entrada pequena sozinha: projeto com 6 fases já custa caro demais.
    """

    def test_entrada_pequena_nao_dispara_nada(self):
        r = calcula(horas_dev=48, telas=6, fases_manual=6, horas_fase_b_manual=8)
        self.assertFalse(alerta_contem(r, "piso desejável"))

    def test_a_regua_continua_a_nominal(self):
        r = calcula(horas_dev=150, telas=6, fases_manual=6, horas_fase_b_manual=8)
        self.assertEqual(len(r.parcelas), 6)
        self.assertAlmostEqual(r.parcelas[0].percentual, regua_das_fases(6)[0], delta=0.01)


class TestGuardas(unittest.TestCase):
    """O que o script se recusa a calcular, em vez de devolver número errado."""

    def test_menos_de_duas_fases_e_erro_explicito(self):
        for fases in (0, 1):
            with self.assertRaises(ValueError) as ctx:
                calcula(horas_dev=100, telas=8, fases_manual=fases)
            self.assertIn("2 fases", str(ctx.exception))

    def test_fase_sem_hora_nenhuma_e_erro_explicito(self):
        # --semanas alto demais espalha as horas até sobrar entrega vazia. O
        # calendário derivado das horas nunca chega lá; o informado à mão chega.
        with self.assertRaises(ValueError) as ctx:
            calcula(horas_dev=1, telas=1, fases_manual=6)
        self.assertIn("entrega vazia", str(ctx.exception))

    def test_calendario_derivado_nunca_produz_fase_vazia(self):
        for dev in range(1, 600):
            horas = horas_das_fases(dev, 8, numero_de_fases(dev))
            self.assertGreater(min(horas[1:]), 0, f"{dev} h")

    def test_regua_irregular_e_recusada(self):
        # calcula_parcelas toma regua[1] como o valor de toda parcela semanal.
        # Com uma régua tipo 30/30/40 ela devolveria 40/30/30 sem avisar.
        with self.assertRaises(ValueError):
            preco.calcula_parcelas(10_000, (0.30, 0.30, 0.40), ["a", "b", "c"])

    def test_regua_das_fases_passa_pela_guarda(self):
        for fases in range(2, 20):
            regua = preco.regua_das_fases(fases)
            parcelas = preco.calcula_parcelas(30_000, regua, ["x"] * fases)
            self.assertEqual(len(parcelas), fases)


class TestCalendario(unittest.TestCase):
    """O calendário conta só hora de trabalho. O tempo do cliente fica de fora."""

    def test_prazo_e_o_numero_de_fases(self):
        # 150 h de dev + 8 h de desenho = 158 h → 6 semanas, mais a fase 1.
        r = calcula(horas_dev=150, telas=8)
        self.assertEqual(r.fases, 7)
        self.assertEqual(len(r.cronograma), 7)
        self.assertEqual(len(r.parcelas), 7)
        self.assertEqual(preco.prazo_texto(r.fases), "2 dias de levantamento + 6 semanas de desenvolvimento")

    def test_nao_soma_janela_de_aprovacao_do_cliente(self):
        # Regressão: a versão anterior somava 1,5 semana fixa de leitura do cliente,
        # contando duas vezes um período que o prazo de dev já exclui, porque ele só
        # começa a correr na aprovação dos requisitos.
        r = calcula(horas_dev=150, telas=8)
        self.assertEqual(r.fases, 1 + 6)

    def test_projeto_pequeno_da_duas_fases_e_meio_a_meio(self):
        r = calcula(horas_dev=24, telas=6)
        self.assertEqual(r.fases, 2)
        self.assertAlmostEqual(r.parcelas[0].percentual, 0.5, places=2)

    def test_duas_fases_com_fase_b_dominante_desiguala_a_entrada(self):
        # Projeto minúsculo: a fatia do levantamento passa da metade e vira o piso
        # da entrada. É o único jeito de uma régua de 2 fases não ser 50/50, e com
        # a Fase B em 8 h só acontece bem abaixo do corte de triagem — o cenário
        # existe para a régua não depender de plausibilidade.
        r = calcula(horas_dev=3, telas=2)
        self.assertEqual(r.fases, 2)
        self.assertGreater(r.parcelas[0].percentual, 0.5)
        self.assertTrue(alerta_contem(r, "virou o piso da entrada"))

    def test_semanas_a_mao_esticam_o_calendario(self):
        r = calcula(horas_dev=150, telas=8, fases_manual=7)
        self.assertEqual(r.fases, 7)
        self.assertEqual(len(r.parcelas), 7)
        self.assertFalse(alerta_contem(r, "acima das"))

    def test_semanas_a_mao_comprimindo_alertam(self):
        r = calcula(horas_dev=150, telas=8, fases_manual=3)
        self.assertTrue(alerta_contem(r, "acima das"))

    def test_alerta_da_fatia_nomeia_o_calendario_esticado_como_causa(self):
        # Regressão: o alerta acusava "estimativa de dev curta" mesmo quando quem
        # tinha encolhido a fase de dev era o --semanas informado à mão.
        r = calcula(horas_dev=20, telas=5, fases_manual=8)
        self.assertTrue(alerta_contem(r, "esticado à mão com --semanas"))
        self.assertFalse(alerta_contem(r, "estimativa de dev curta"))

    def test_fase_b_a_mao_fora_da_faixa_pede_registro(self):
        r = calcula(horas_dev=150, telas=8, horas_fase_b_manual=40)
        self.assertTrue(alerta_contem(r, "acima da faixa de referência"))

    def test_fase_b_a_mao_dentro_da_faixa_nao_alerta(self):
        r = calcula(horas_dev=150, telas=8, horas_fase_b_manual=10)
        self.assertFalse(alerta_contem(r, "faixa de referência"))

    def test_projeto_longo_alerta(self):
        r = calcula(horas_dev=520, telas=20)
        self.assertGreater(r.fases - 1, preco.SEMANAS_PROJETO_LONGO)
        self.assertTrue(alerta_contem(r, "primeira metade do escopo"))

    def test_limiar_do_projeto_longo_conta_semanas_de_dev(self):
        # A constante nomeia semanas, e a fase 1 não é uma: com 12 semanas de dev o
        # projeto está no limite e não alerta; com 13, alerta. Regressão do dia em
        # que `fases` (1 + semanas) passou a ser comparado com a constante.
        no_limite = calcula(horas_dev=12 * 30 - 8, telas=8)
        acima = calcula(horas_dev=12 * 30 + 1, telas=8)
        self.assertEqual(no_limite.fases - 1, preco.SEMANAS_PROJETO_LONGO)
        self.assertFalse(alerta_contem(no_limite, "primeira metade do escopo"))
        self.assertEqual(acima.fases - 1, preco.SEMANAS_PROJETO_LONGO + 1)
        self.assertTrue(alerta_contem(acima, "primeira metade do escopo"))


class TestPisoDeNegociacao(unittest.TestCase):
    def test_piso_e_a_estimativa_sem_gordura(self):
        r = calcula(horas_dev=90, telas=8)
        self.assertEqual(r.piso, arredonda_para_cima(r.horas_total * preco.VALOR_HORA))
        self.assertAlmostEqual(r.preco / r.piso, 1 + GORDURA, delta=0.03)

    def test_parcelas_do_piso_somam_o_piso(self):
        # O piso usa a régua nominal, não a efetiva do preço: misturar os dois
        # arredondamentos fazia a entrada do piso não bater com o percentual.
        r = calcula(horas_dev=150, telas=8)
        regua = preco.regua_das_fases(r.fases, r.horas_fase_b / r.horas_total)
        parcelas = preco.calcula_parcelas(r.piso, regua, ["x"] * r.fases)
        self.assertAlmostEqual(sum(p.valor for p in parcelas), r.piso, places=2)

    def test_piso_e_medido_no_liquido(self):
        r = calcula(horas_dev=90, telas=8)
        self.assertLess(r.piso_liquido, r.piso)
        self.assertAlmostEqual(r.piso_hora_efetiva, r.piso_liquido / r.horas_total, places=6)
        self.assertLess(r.piso_hora_efetiva, preco.VALOR_HORA)

    def test_orcamento_do_cliente_abaixo_do_piso(self):
        r = calcula(horas_dev=90, telas=8, orcamento_cliente=3_000)
        self.assertTrue(alerta_contem(r, "abaixo do piso de negociação"))

    def test_orcamento_entre_piso_e_preco(self):
        r = calcula(horas_dev=90, telas=8)
        if GORDURA:
            meio = (r.piso + r.preco) / 2
            r2 = calcula(horas_dev=90, telas=8, orcamento_cliente=meio)
            self.assertTrue(alerta_contem(r2, "entre o piso e o preço"))
        else:
            # Sem gordura o piso é o próprio preço: não existe faixa de
            # negociação, e qualquer orçamento menor cai no alerta do piso.
            self.assertEqual(r.piso, r.preco)
            r2 = calcula(horas_dev=90, telas=8, orcamento_cliente=r.preco - 100)
            self.assertTrue(alerta_contem(r2, "abaixo do piso de negociação"))

    def test_orcamento_acima_do_preco_nao_alerta(self):
        r = calcula(horas_dev=90, telas=8, orcamento_cliente=50_000)
        self.assertFalse(alerta_contem(r, "orçamento do cliente"))


class TestFatorEstimativa(unittest.TestCase):
    def test_fator_um_nao_muda_nada_nem_alerta(self):
        cru = calcula(horas_dev=90, telas=8)
        com_fator = calcula(horas_dev=90, telas=8, fator_estimativa=1.0)
        self.assertEqual(cru.preco, com_fator.preco)
        self.assertEqual(cru.horas_dev, com_fator.horas_dev)
        self.assertFalse(alerta_contem(cru, "fator de estimativa"))

    def test_fator_maior_sobe_horas_e_preco(self):
        r = calcula(horas_dev=90, telas=8, fator_estimativa=1.3)
        self.assertEqual(r.horas_dev, 117)  # ceil(90 × 1,3)
        self.assertEqual(r.horas_dev_informado, 90)
        self.assertGreater(r.preco, calcula(horas_dev=90, telas=8).preco)
        self.assertTrue(alerta_contem(r, "fator de estimativa"))

    def test_fator_nao_toca_na_fase_b(self):
        cru = calcula(horas_dev=90, telas=8)
        com_fator = calcula(horas_dev=90, telas=8, fator_estimativa=1.5)
        self.assertEqual(cru.horas_fase_b, com_fator.horas_fase_b)

    def test_fator_arredonda_horas_para_cima(self):
        r = calcula(horas_dev=90, telas=8, fator_estimativa=1.11)
        self.assertEqual(r.horas_dev, 100)  # ceil(99,9)

    def test_fator_entra_no_calendario_e_nas_parcelas(self):
        cru = calcula(horas_dev=90, telas=8)
        com_fator = calcula(horas_dev=90, telas=8, fator_estimativa=1.4)
        self.assertGreater(com_fator.fases, cru.fases)
        self.assertGreater(len(com_fator.parcelas), len(cru.parcelas))

    def test_markdown_mostra_a_origem_das_horas(self):
        md = preco.markdown(calcula(horas_dev=90, telas=8, fator_estimativa=1.3))
        self.assertIn("fator 1,30", md)
        self.assertIn("90 h", md)


class TestSaida(unittest.TestCase):
    def test_markdown_traz_todas_as_secoes(self):
        md = preco.markdown(calcula(horas_dev=90, telas=8))
        for trecho in (
            "A conta do preço",
            "As fases",
            "Régua de pagamento",
            "A conferência do prejuízo",
            "O líquido",
            "Piso de negociação",
            "Checagens",
        ):
            self.assertIn(trecho, md)

    def test_markdown_nao_cita_plataforma_nenhuma(self):
        md = preco.markdown(calcula(horas_dev=90, telas=8))
        for proibido in ("Workana", "Comissão", "Spread", "Canal", "dólar"):
            self.assertNotIn(proibido, md)

    def test_uma_fase_de_dev_nao_promete_duas_entregas(self):
        # Regressão: com uma única fase de desenvolvimento o parágrafo dizia "a
        # primeira delas traz o desenho das telas; a última termina com o app
        # publicado", e a tabela logo abaixo mostrava uma entrega só.
        md = preco.markdown(calcula(horas_dev=24, telas=6))
        self.assertIn("A de desenvolvimento é uma semana", md)
        self.assertNotIn("A primeira delas", md)

    def test_markdown_traz_a_sexta_e_o_nao_portao(self):
        md = preco.markdown(calcula(horas_dev=150, telas=8))
        self.assertIn("sexta-feira", md)
        self.assertIn("não é portão de aprovação", md)
        self.assertIn("7 fases", md)

    def test_markdown_diz_que_toda_entrega_tem_parcela(self):
        md = preco.markdown(calcula(horas_dev=280, telas=14))
        self.assertIn("toda entrega tem parcela", md)

    def test_duas_fases_desiguais_nao_dizem_metade(self):
        # Regressão: o parágrafo dizia "metade na assinatura e metade no aceite"
        # com a tabela logo acima mostrando 56,6% e 43,4%. Essa saída é colada no
        # analise.md e vira a Cláusula 2ª do contrato.
        r = calcula(horas_dev=3, telas=2)
        md = preco.markdown(r)
        self.assertNotIn("metade na assinatura", md)
        self.assertIn(preco.pct_auto(r.parcelas[0].percentual) + " na assinatura", md)
        self.assertIn(preco.pct_auto(r.parcelas[1].percentual) + " no aceite", md)

    def test_motivo_da_regua_nunca_diz_uma_semanas(self):
        md = preco.markdown(calcula(horas_dev=3, telas=2))
        self.assertNotIn("pelas 1 semanas", md)
        self.assertIn("1 semana de desenvolvimento", md)

    def test_projeto_de_duas_fases_nao_promete_parcela_da_semana_seguinte(self):
        # Com duas fases nenhuma entrega financia a semana seguinte: a entrada
        # paga a fase 1 e o aceite paga a fase 2.
        md = preco.markdown(calcula(horas_dev=24, telas=4))
        self.assertIn("São dois marcos", md)
        self.assertNotIn("libera a parcela da **fase seguinte**", md)

    def test_frase_da_regua_entra_no_markdown(self):
        md = preco.markdown(calcula(horas_dev=365, telas=19))
        self.assertIn("As duas frases que vão no texto da proposta", md)
        self.assertIn("não paga tudo de uma vez", md)
        self.assertIn("só é cobrada depois que você recebe", md)

    def test_frase_da_regua_cita_os_mesmos_valores_da_tabela(self):
        # O ponto inteiro de ela sair do script: desde 18/08/2026 esses valores
        # vão no texto de venda, então têm de ser os mesmos da Cláusula 2ª.
        for dev in (35, 150, 365, 520):
            r = calcula(horas_dev=dev, telas=12)
            frase = preco.frase_da_regua(r)
            self.assertIn(preco.brl0(r.parcelas[-1].valor), frase, f"{dev} h")
            self.assertIn(preco.brl0(r.parcelas[0].valor), frase, f"{dev} h")

    def test_frase_da_regua_promete_desembolso_parcelado(self):
        # Sem custódia no meio, o desembolso do cliente é mesmo parcelado: o
        # sujeito é "não paga", e "não me paga" era redação do canal antigo.
        for dev in (35, 150, 365):
            r = calcula(horas_dev=dev, telas=9)
            self.assertIn("não paga tudo de uma vez", preco.frase_da_regua(r))
            self.assertNotIn("não me paga", preco.frase_da_regua(r))
            self.assertIn("só é cobrada depois", preco.frase_da_cobranca(r))

    def test_a_cobranca_nao_conta_dias(self):
        # Desde 01/09/2026 a frase ancora na aprovação, não na duração da fase 1:
        # o prazo dela varia com o wireframe, e um número de dias colado aqui
        # mentiria justamente nos projetos de muitas telas.
        frase = preco.frase_da_cobranca(calcula(horas_dev=150, telas=8))
        self.assertNotIn("primeiros dias", frase)
        self.assertEqual(preco.extenso_masculino(2), "dois")
        self.assertEqual(preco.extenso_masculino(7), "7")

    def test_frase_da_cobranca_sempre_nomeia_a_entrada(self):
        # Dizer só "cobrada depois da entrega" continua falso na 1ª parcela: ela é
        # a entrada, paga na assinatura. Sem a oração da entrada a varredura
        # reprova (checa_cobranca_sem_entrada).
        for dev in (35, 150, 365):
            frase = preco.frase_da_cobranca(calcula(horas_dev=dev, telas=9))
            self.assertIn("paga a entrada", frase, f"{dev} h")
            self.assertIn("anexado ao contrato", frase, f"{dev} h")

    def test_a_cobranca_diz_o_que_a_entrada_compra(self):
        # Correção de 26/08/2026: "dali em diante" marcava um degrau e deixava a
        # entrada, por eliminação, como a parcela paga sem contrapartida. A semana
        # do desenho é a primeira entrega da régua, e a frase tem de dizer isso.
        for dev in (35, 150, 365):
            frase = preco.frase_da_cobranca(calcula(horas_dev=dev, telas=9))
            self.assertNotIn("dali em diante", frase, f"{dev} h")
            self.assertIn("projeto escrito", frase, f"{dev} h")

    def test_a_mecanica_nao_se_repete_na_frase_da_regua(self):
        # A mecânica é dita uma vez, no movimento 4. Repeti-la no movimento 5 é o
        # defeito mais provável de um texto de 700 palavras.
        for dev in (35, 150, 365):
            frase = preco.frase_da_regua(calcula(horas_dev=dev, telas=9))
            self.assertNotIn("cobrada depois", frase)

    def test_a_cobranca_nunca_promete_deposito_por_fase(self):
        # "Você deposita o valor da fase antes de ela começar" soa a cuidado com o
        # cliente, e por isso escapa da revisão. A régua é de cobrança, não de depósito.
        frase = preco.frase_da_cobranca(calcula(horas_dev=150, telas=9))
        self.assertNotIn("deposita", frase)
        self.assertNotIn("antes de ela começar", frase)

    def test_frase_da_regua_sempre_nomeia_a_entrada(self):
        # Correção de 18/08/2026: "cinco parcelas iguais" deixava o cliente sem
        # saber quanto ele desembolsa para começar.
        iguais = calcula(horas_dev=150, telas=8)  # 5 fases, régua plana
        entrada_maior = calcula(horas_dev=365, telas=19)  # 11 fases
        for r in (iguais, entrada_maior):
            self.assertIn("entrada de", preco.frase_da_regua(r))
            self.assertNotIn("parcelas iguais", preco.frase_da_regua(r))

    def test_as_frases_nunca_citam_plataforma_nem_retencao(self):
        r = calcula(horas_dev=150, telas=8)
        for frase in (preco.frase_da_regua(r), preco.frase_da_cobranca(r)):
            self.assertNotIn("plataforma", frase)
            self.assertNotIn("retid", frase)

    def test_quantidade_de_parcelas_sai_por_extenso(self):
        self.assertEqual(preco.extenso(10), "dez")
        self.assertEqual(preco.extenso(2), "duas")
        self.assertEqual(preco.extenso(23), "23")

    def test_percentual_redondo_sai_sem_casa_decimal(self):
        self.assertEqual(preco.pct_auto(0.20), "20%")
        self.assertEqual(preco.pct_auto(0.1143), "11,4%")

    def test_formatacao_em_padrao_brasileiro(self):
        self.assertEqual(preco.brl(12_345.6), "R$ 12 345,60")
        self.assertEqual(preco.brl0(9_000), "R$ 9 000")
        self.assertEqual(preco.brl0(870), "R$ 870")
        self.assertEqual(preco.brl(1_234_567), "R$ 1 234 567,00")


class TestChecagens(unittest.TestCase):
    """checagens() só pode depender do Resultado — é o que a torna extraível."""

    def test_e_deterministica_e_nao_guarda_estado(self):
        r = calcula(horas_dev=150, telas=8)
        self.assertEqual(preco.checagens(r), preco.checagens(r))

    def test_o_que_calcula_pendura_e_o_que_checagens_devolve(self):
        # O alerta que nasce no meio da conta (o fator de estimativa) fica na
        # frente; o resto tem de ser exatamente a saída de checagens().
        r = calcula(horas_dev=520, telas=20, orcamento_cliente=1_000)
        avisos = preco.checagens(r)
        self.assertEqual(r.alertas[-len(avisos):], avisos)

    def test_alertas_do_meio_da_conta_sobrevivem_a_extracao(self):
        r = calcula(horas_dev=90, telas=8, fator_estimativa=1.3)
        self.assertTrue(alerta_contem(r, "fator de estimativa"))
        self.assertNotIn(
            "fator de estimativa",
            " ".join(preco.checagens(r)).lower(),
            "o alerta do fator não pode ser recalculado por checagens()",
        )


@unittest.skipUnless(
    (pathlib.Path(__file__).resolve().parent.parent / "SKILL.md").exists(),
    "SKILL.md só existe na pasta da skill, não na cópia do bot",
)
class TestDocumentacao(unittest.TestCase):
    """As tabelas de percentual que vivem em .md são cópias do que o script calcula.

    O próprio SKILL.md diz que escrever o valor nos dois lugares é como eles
    divergem. Elas ficam lá porque conferir de relance vale mais que a duplicação
    — mas só com estes testes segurando o outro lado.
    """

    RAIZ = pathlib.Path(__file__).resolve().parent.parent

    def tabela(self, arquivo: str, cabecalho: str) -> list[list[str]]:
        texto = (self.RAIZ / arquivo).read_text()
        self.assertIn(cabecalho, texto, f"{arquivo}: tabela sumiu ou foi renomeada")
        bloco = texto[texto.index(cabecalho) :]
        bloco = bloco[: bloco.index("\n\n")]
        return [
            [c.strip() for c in linha.strip("|").split("|")]
            for linha in bloco.splitlines()[2:]
        ]

    def test_tabela_da_regua_no_skill_confere_com_o_script(self):
        for fases, entrada, demais in self.tabela("SKILL.md", "| Fases | Entrada |"):
            regua = regua_das_fases(int(fases))
            self.assertEqual(preco.pct_auto(regua[0]), entrada, f"{fases} fases, entrada")
            self.assertEqual(preco.pct_auto(regua[-1]), demais, f"{fases} fases, demais")


class TestConstantes(unittest.TestCase):
    def test_a_fronteira_das_parcelas_iguais_sai_da_entrada_minima(self):
        # 1/ENTRADA_MINIMA = 5 semanas. Não existe constante própria para essa
        # fronteira justamente para ela não divergir da entrada.
        fronteira = int(1 / ENTRADA_MINIMA)
        self.assertEqual(len(set(round(p, 9) for p in regua_das_fases(fronteira))), 1)
        self.assertGreater(len(set(round(p, 9) for p in regua_das_fases(fronteira + 1))), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
