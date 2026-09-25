#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Testes da varredura. Cada caso positivo é um erro que já aconteceu de verdade.

Uso: uv run scripts/test_varredura.py
"""

from __future__ import annotations

import unittest

import preco
from varredura import (
    ALERTA,
    ERRO,
    checa_cadencia_sem_ancora,
    checa_pendencias_de_envio,
    checa_citacao_reaproveitada,
    checa_cobertura_da_vaga,
    checa_rastreabilidade,
    checa_compromisso_precoce,
    checa_exclusividade,
    checa_formatacao,
    checa_cobranca_sem_entrada,
    checa_status,
    checa_tamanho,
    checa_acessos,
    checa_titulos_duplicados,
    checa_unidade_de_divisao,
    checa_valores_batem,
    conta_palavras,
    relatorio,
    tem_emoji,
    valores_citados,
    varre,
)

# O parágrafo do movimento 4 na forma correta, que nenhuma checagem pode acusar.
MOVIMENTO_4_BOM = (
    "Sobre como corre: os dois primeiros dias são para fechar o projeto no papel. Depois que você "
    "aprovar eu começo a montar, em fases. Tem uma entrada na assinatura: ela reserva a agenda e "
    "paga a entrada: ela paga o projeto escrito que você acabou de aprovar e reserva a agenda. Cada parcela seguinte só é cobrada "
    "depois que você recebe a entrega da fase. "
    "Dentro da fase, toda semana você recebe uma versão nova para instalar no celular."
)


class TestExclusividade(unittest.TestCase):
    def test_pega_a_frase_que_escapou_em_agosto(self):
        a = checa_exclusividade("Nessas seis semanas eu trabalho só no seu projeto.")
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0].nivel, ERRO)

    def test_pega_variantes(self):
        for frase in (
            "tenho dedicação exclusiva ao que a gente combinar",
            "trabalho sem dividir com outro cliente",
            "pego um projeto por vez",
        ):
            self.assertTrue(checa_exclusividade(frase), frase)

    def test_frase_com_dois_padroes_gera_um_achado_so(self):
        texto = "Tenho dedicação exclusiva e trabalho só no seu projeto."
        self.assertEqual(len(checa_exclusividade(texto)), 1)

    def test_frase_de_agenda_nao_e_exclusividade(self):
        self.assertEqual(checa_exclusividade("Respondo em até duas horas no horário comercial."), [])


class TestCobranca(unittest.TestCase):
    def test_sem_entrada_reprova(self):
        a = checa_cobranca_sem_entrada(
            "Cada parte só é cobrada depois que você recebe a entrega daquela fase."
        )
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0].nivel, ERRO)

    def test_a_redacao_antiga_do_escrow_tambem_reprova(self):
        # O canal de plataforma acabou, mas texto reaproveitado de proposta velha
        # ainda chega com "liberada": a trava continua valendo para ele.
        a = checa_cobranca_sem_entrada(
            "Cada parte só é liberada depois que você recebe a entrega."
        )
        self.assertEqual(len(a), 1)

    def test_com_entrada_no_mesmo_paragrafo_passa(self):
        self.assertEqual(checa_cobranca_sem_entrada(MOVIMENTO_4_BOM), [])

    def test_entrada_em_outro_paragrafo_nao_salva(self):
        texto = "Tem uma entrada na assinatura.\n\nCada parte só é cobrada depois que você recebe a entrega."
        self.assertEqual(len(checa_cobranca_sem_entrada(texto)), 1)


class TestCadencia(unittest.TestCase):
    def test_sem_ancora_reprova(self):
        a = checa_cadencia_sem_ancora("Toda semana você recebe uma versão nova para instalar no celular.")
        self.assertEqual(len(a), 1)

    def test_dentro_da_fase_passa(self):
        self.assertEqual(checa_cadencia_sem_ancora(MOVIMENTO_4_BOM), [])

    def test_depois_que_voce_aprovar_tambem_ancora(self):
        texto = "Depois que você aprovar, toda sexta você recebe uma versão nova."
        self.assertEqual(checa_cadencia_sem_ancora(texto), [])

    def test_reporta_todas_as_ocorrencias(self):
        """Parar na primeira obrigaria o usuário a corrigir e rodar de novo por ocorrência."""
        texto = "Toda semana você recebe uma versão. Depois, toda sexta você recebe o resumo."
        self.assertEqual(len(checa_cadencia_sem_ancora(texto)), 2)


class TestAcessos(unittest.TestCase):
    """19/08/2026: titularidade e acesso são coisas diferentes.

    A titularidade é do cliente desde o começo e pode ser dita. O que só passa no
    fim da entrega, ou no encerramento do contrato, é o acesso: senha, chave,
    credencial. E essa mecânica não entra na proposta, é conversa do levantamento.
    """

    def test_titularidade_desde_o_comeco_passa(self):
        texto = "O app, o código e as contas ficam no seu nome desde o começo, então você não fica preso a mim depois."
        self.assertEqual(checa_acessos(texto), [])

    def test_acesso_antecipado_reprova(self):
        a = checa_acessos("Eu te passo os acessos e as senhas desde o começo, é tudo seu.")
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0].nivel, ERRO)
        self.assertEqual(a[0].regra, "acesso antecipado")

    def test_acesso_durante_o_desenvolvimento_reprova(self):
        a = checa_acessos("As credenciais ficam com você durante o projeto inteiro.")
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0].nivel, ERRO)

    def test_mecanica_dos_acessos_e_alerta_nao_erro(self):
        a = checa_acessos("As senhas e as chaves são transferidas no aceite final.")
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0].nivel, ALERTA)

    def test_acesso_como_funcionalidade_do_app_passa(self):
        """Falso positivo que a regra precisa evitar: acesso é palavra de produto também."""
        for t in (
            "Cada vendedor tem acesso só aos pedidos dele.",
            "O dono da loja entra com a senha dele e vê tudo.",
        ):
            self.assertEqual(checa_acessos(t), [], t)


class TestQuebraDeLinha(unittest.TestCase):
    """19/08/2026: o arquivo da proposta vem quebrado em 80 colunas.

    A quebra caindo no meio de uma âncora de várias palavras salvava o texto de
    toda trava. Foi assim que o exemplo bom do tom-e-exemplos passou a acusar
    cadência sem âncora depois de um simples reflow do parágrafo.
    """

    def test_ancora_partida_pela_quebra_de_linha_ainda_trava(self):
        texto = "Dentro\nda fase, toda semana você recebe uma versão nova."
        self.assertEqual([a for a in varre(texto, None) if a.regra == "cadência cobrindo a fase 1"], [])

    def test_frase_proibida_partida_pela_quebra_ainda_reprova(self):
        texto = "Nessas seis semanas eu trabalho só\nno seu projeto."
        regras = {a.regra for a in varre(texto, None) if a.nivel == ERRO}
        self.assertIn("exclusividade", regras)

    def test_paragrafos_continuam_separados(self):
        """Desdobrar não pode juntar parágrafos: a cobrança vale por parágrafo."""
        texto = "Tem uma entrada na assinatura.\n\nCada parte só é liberada depois que você recebe a entrega."
        regras = {a.regra for a in varre(texto, None) if a.nivel == ERRO}
        self.assertIn("cobrança sem a entrada", regras)


class TestFormatacao(unittest.TestCase):
    def test_travessao_reprova(self):
        a = checa_formatacao("Pelo escopo que está escrito — o app simples —, fica em R$ 28.000.")
        self.assertTrue(any(x.regra == "travessão" for x in a))

    def test_emoji_reprova(self):
        self.assertTrue(any(x.regra == "emoji" for x in checa_formatacao("Bora começar 🚀")))

    def test_link_vira_alerta_e_nao_erro(self):
        # Em cliente direto não há plataforma para proibir link. O que sobra é a
        # decisão de ago/2026: a prova é o vídeo em anexo, então o endereço no
        # texto pede decisão, não barra a entrega.
        a = checa_formatacao("Veja em https://exemplo.com")
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0].nivel, ALERTA)

    def test_endereco_nao_barra_a_entrega(self):
        achados = varre("Veja em https://exemplo.com " + "palavra " * 620, None)
        self.assertFalse([x for x in achados if x.nivel == ERRO and "endereço" in x.regra])

    def test_texto_limpo_passa(self):
        self.assertEqual(checa_formatacao(MOVIMENTO_4_BOM), [])

    def test_marca_registrada_nao_e_emoji(self):
        """™, © e ® são categoria So mas aparecem em texto legítimo."""
        self.assertEqual(tem_emoji("Claro Pay™ e Monetizze®"), [])
        self.assertEqual(checa_formatacao("Trabalhei no Claro Pay™."), [])


class TestPalavrasProibidas(unittest.TestCase):
    def test_taxa_e_whatsapp_reprovam(self):
        for t in (
            "Sem taxa escondida.",
            "As taxas da loja ficam com você.",
            "Me chama no WhatsApp.",
            "Te mando pelo whats app.",
            "Manda um zap.",
            "A sua comissão cai sozinha.",
            "Define as comissões da plataforma.",
            "Paga com Pix ou cartão.",
            "Recebe PIX na hora.",
            "Se tiver, eu ligo a plataforma nela.",
            "Dá para ligar o app no sistema.",
        ):
            a = checa_formatacao(t)
            self.assertTrue(any(x.nivel == ERRO and x.regra.startswith("palavra proibida") for x in a), t)

    def test_palavra_parecida_passa(self):
        self.assertEqual(checa_formatacao("O valor é taxativo, e o what de cada tela está no projeto."), [])
        self.assertEqual(checa_formatacao("A tela tem pixels bem definidos."), [])
        self.assertEqual(checa_formatacao("A ligação com o seu caso é direta."), [])


class TestConviteParaConversaNaoEMaisTrava(unittest.TestCase):
    """A trava do convite existia por causa do filtro da plataforma, e saiu com ela.

    Em cliente direto oferecer uma conversa é a coisa certa a fazer, não um risco:
    não há filtro para barrar a mensagem, e a chamada é justamente o argumento
    contra a agência, que nunca põe o desenvolvedor na frente do cliente.
    """

    def test_conversar_por_video_passa(self):
        self.assertEqual(checa_formatacao("Se ficar mais fácil, dá para conversar por vídeo."), [])

    def test_fala_comigo_direto_passa(self):
        self.assertEqual(
            checa_formatacao("Você fala comigo direto, do primeiro orçamento à última entrega."), []
        )

    def test_call_e_reuniao_passam(self):
        for t in (
            "Podemos fazer uma call rápida.",
            "Dá para marcar uma reunião essa semana.",
            "Se quiser entrar em contato, estou por aqui.",
        ):
            self.assertEqual(checa_formatacao(t), [], t)

    def test_video_em_anexo_passa(self):
        t = "Deixei um vídeo curto em anexo, dá para ver funcionando em dois minutos."
        self.assertEqual(checa_formatacao(t), [])

    def test_frase_de_quem_responde_passa(self):
        t = "Do primeiro orçamento à última entrega, quem responde aqui é quem escreve o código."
        self.assertEqual(checa_formatacao(t), [])


class TestUnidadeDeDivisao(unittest.TestCase):
    def test_numero_de_telas_reprova(self):
        self.assertTrue(checa_unidade_de_divisao("Faço as 8 telas em seis semanas."))

    def test_por_extenso_tambem(self):
        self.assertTrue(checa_unidade_de_divisao("São sete telas ao todo."))

    def test_semanas_nao_sao_unidade_proibida(self):
        self.assertEqual(checa_unidade_de_divisao("São seis semanas de desenvolvimento."), [])

    def test_duas_horas_de_resposta_nao_e_unidade(self):
        """A promessa de atendimento não é número que o cliente divide pelo preço."""
        texto = "Respondo em até duas horas no horário comercial."
        self.assertEqual(checa_unidade_de_divisao(texto), [])


class TestCompromissoPrecoce(unittest.TestCase):
    def test_fecho_em_alerta(self):
        a = checa_compromisso_precoce("Pelo que está escrito, fecho em R$ 28.000.")
        self.assertEqual(a[0].nivel, ALERTA)

    def test_ancorado_no_escopo_passa(self):
        self.assertEqual(checa_compromisso_precoce("Pelo escopo que está escrito na vaga, fica em R$ 28.000."), [])


class TestTamanho(unittest.TestCase):
    def test_curto_demais(self):
        self.assertEqual(checa_tamanho("palavra " * 300)[0].nivel, ALERTA)

    def test_longo_demais(self):
        self.assertEqual(checa_tamanho("palavra " * 800)[0].nivel, ALERTA)

    def test_na_faixa_passa(self):
        self.assertEqual(checa_tamanho("palavra " * 650), [])


class TestAnalise(unittest.TestCase):
    def test_enviada_sem_data_reprova(self):
        texto = "- **Data de envio:**\n- **Status:** enviada\n"
        self.assertEqual(len(checa_status(texto)), 1)

    def test_enviada_com_data_passa(self):
        texto = "- **Data de envio:** 2026-08-19\n- **Status:** enviada\n"
        self.assertEqual(checa_status(texto), [])

    def test_enviada_sem_o_campo_de_data_reprova(self):
        """Campo ausente é sinal mais forte que campo vazio, e era o único que escapava."""
        a = checa_status("- **Status:** enviada\n")
        self.assertEqual(len(a), 1)
        self.assertIn("não existe campo", a[0].detalhe)

    def test_rascunho_passa(self):
        self.assertEqual(checa_status("- **Data de envio:**\n- **Status:** rascunho\n"), [])

    def test_titulo_duplicado(self):
        texto = "## A conta do preço\n\ntexto\n\n## A conta do preço\n\noutro\n"
        a = checa_titulos_duplicados(texto)
        self.assertEqual(len(a), 1)
        self.assertIn("2 vezes", a[0].detalhe)

    def test_titulos_unicos_passam(self):
        self.assertEqual(checa_titulos_duplicados("## Um\n\n## Dois\n"), [])


class TestPendenciasDeEnvio(unittest.TestCase):
    def test_pj_em_aberto_alerta(self):
        texto = "- **Cliente é PJ ou pessoa física:** não informado."
        self.assertEqual(len(checa_pendencias_de_envio(texto)), 1)

    def test_pj_confirmado_nao_alerta(self):
        """A janela de 120 caracteres com DOTALL não pode colar dois campos distintos."""
        texto = (
            "- **Cliente é PJ ou pessoa física:** PJ, confirmado na tela do projeto.\n"
            "- **Orçamento informado pelo cliente:** não informado\n"
        )
        self.assertEqual(checa_pendencias_de_envio(texto), [])


class TestRelatorio(unittest.TestCase):
    def test_sem_achados_lembra_da_leitura_humana(self):
        saida = relatorio([], 650)
        self.assertIn("650 palavras", saida)
        self.assertIn("leitura humana", saida)

    def test_separa_erros_de_alertas(self):
        achados = varre("Eu trabalho só no seu projeto. " + "palavra " * 300, None)
        saida = relatorio(achados, 330)
        self.assertIn("Erros, que barram a entrega", saida)
        self.assertIn("Alertas, que pedem decisão", saida)


class TestValores(unittest.TestCase):
    def test_valor_orfao_reprova(self):
        proposta = "entrada de R$ 5.620 e mais seis de R$ 3.730"
        analise = "| 1 | R$ 5.620,00 |"
        a = checa_valores_batem(proposta, analise)
        self.assertEqual(len(a), 1)
        self.assertIn("3730", a[0].detalhe)

    def test_todos_presentes_passa(self):
        proposta = "entrada de R$ 5.620 e mais seis de R$ 3.730"
        analise = "R$ 5.620,00 e R$ 3.730,00"
        self.assertEqual(checa_valores_batem(proposta, analise), [])

    def test_normaliza_centavos_e_pontos(self):
        self.assertEqual(valores_citados("R$ 5.620,00"), valores_citados("R$ 5620"))
        self.assertEqual(valores_citados("R$ 5 620,00"), valores_citados("R$ 5620"))

    def test_dolar_tambem_e_conferido(self):
        a = checa_valores_batem("as contas custam US$ 25 e US$ 99", "US$ 25 uma vez")
        self.assertEqual(len(a), 1)
        self.assertIn("US$ 99", a[0].detalhe)

    def test_moeda_diferente_nao_se_valida(self):
        """R$ 99 no texto não pode ser validado por um US$ 99 do analise."""
        self.assertTrue(checa_valores_batem("fica em R$ 99", "custa US$ 99"))


class TestIntegracao(unittest.TestCase):
    def test_proposta_boa_nao_gera_erro(self):
        proposta = MOVIMENTO_4_BOM + "\n\nO app, o código e as contas ficam no seu nome desde o começo."
        achados = varre(proposta, None)
        self.assertEqual([a for a in achados if a.nivel == ERRO], [])

    def test_proposta_de_agosto_gera_os_tres_erros(self):
        """O texto que passou pela skill em ago/2026, com os defeitos reais."""
        proposta = (
            "Depois que você aprovar eu começo a montar, em fases. Cada parte só é cobrada "
            "depois que você recebe a entrega da fase. Toda semana "
            "você recebe uma versão nova para instalar no celular. Nesse período eu trabalho só no seu "
            "projeto.\n\nEu te passo os acessos e as senhas desde o começo."
        )
        regras = {a.regra for a in varre(proposta, None) if a.nivel == ERRO}
        self.assertIn("exclusividade", regras)
        self.assertIn("cobrança sem a entrada", regras)
        self.assertIn("cadência cobrindo a fase 1", regras)
        self.assertIn("acesso antecipado", regras)


class TestVarreComAnalise(unittest.TestCase):
    def test_checagens_do_analise_entram_no_resultado(self):
        proposta = MOVIMENTO_4_BOM + " O valor é de R$ 9.000."
        analise = "- **Data de envio:**\n- **Status:** enviada\n\n## Um\n\n## Um\n"
        regras = {a.regra for a in varre(proposta, analise)}
        self.assertIn("status sem data de envio", regras)
        self.assertIn("título duplicado", regras)
        self.assertIn("valor que o script não devolveu", regras)

    def test_sem_analise_nao_roda_as_checagens_dele(self):
        regras = {a.regra for a in varre(MOVIMENTO_4_BOM, None)}
        self.assertNotIn("status sem data de envio", regras)


class TestUtilidades(unittest.TestCase):
    def test_conta_palavras(self):
        self.assertEqual(conta_palavras("  uma  duas   três "), 3)
        self.assertEqual(conta_palavras("fecho em R$ 12 000 e mais onze de R$ 870"), 10)


# --------------------------------------------------------------------------
# Rastreabilidade: regra de ouro 15
# --------------------------------------------------------------------------

VAGA_DELIVERY = """### Texto original da vaga (íntegra)

> O projeto consiste no desenvolvimento de um aplicativo de delivery completo, com foco
> principal na otimização da logística de entrega. O sistema deverá conectar estabelecimentos
> a motoboys, priorizando a atribuição de entregas aos profissionais mais próximos.
> O aplicativo deverá contemplar interfaces para o usuário final (cliente), para os
> estabelecimentos parceiros e para os motoboys.

---
"""


def _analise(linhas: str) -> str:
    return VAGA_DELIVERY + "\n## Rastreabilidade: cada item contra a linha da vaga\n\n" + (
        "| No orçamento | Linha da vaga que pede |\n|---|---|\n" + linhas
    ) + "\n\n**Considerado e deixado de fora, por não ter linha correspondente:**\n"


class TesteRastreabilidade(unittest.TestCase):
    def test_citacao_literal_passa(self):
        a = _analise('| App do motoboy | "interfaces (...) para os motoboys" |\n')
        self.assertEqual(checa_rastreabilidade(a), [])

    def test_custo_de_projeto_e_excecao_declarada(self):
        a = _analise("| Setup e testes | **exceção declarada:** custo de projeto |\n")
        self.assertEqual(checa_rastreabilidade(a), [])

    def test_linha_sem_citacao_nenhuma_reprova(self):
        a = _analise("| Programa de fidelidade | todo delivery precisa fidelizar |\n")
        r = checa_rastreabilidade(a)
        self.assertEqual([x.nivel for x in r].count(ERRO), 1)
        self.assertEqual(r[0].regra, "item de orçamento sem citação da vaga")

    def test_citacao_que_nao_existe_na_vaga_reprova(self):
        """A paráfrase disfarçada de citação, que é o modo mais comum de inflar escopo."""
        a = _analise('| Chat interno | "conversa entre cliente e motoboy" |\n')
        r = [x for x in checa_rastreabilidade(a) if x.nivel == ERRO]
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].regra, "citação que não está na vaga")

    def test_inferencia_vira_alerta_mesmo_com_citacao_boa(self):
        a = _analise('| Push | decorre de "priorizando a atribuição de entregas" |\n')
        r = checa_rastreabilidade(a)
        self.assertEqual([x.nivel for x in r], [ALERTA])
        self.assertEqual(r[0].regra, "escopo justificado por inferência")

    def test_o_caso_do_mapa_nao_e_pego_pela_maquina(self):
        """Registro deliberado do limite: a citação era verdadeira e o item era inventado.

        Rastreamento no mapa entrou numa proposta de delivery em 19/08/2026 justificado por
        'otimização da logística de entrega', que está mesmo na vaga. Nenhuma checagem de texto
        decide isso: é a varredura 11, humana. O teste existe para ninguém confiar demais aqui.
        """
        a = _analise('| Rastreamento ao vivo no mapa | "otimização da logística de entrega" |\n')
        self.assertEqual(checa_rastreabilidade(a), [])

    def test_sem_secao_de_vaga_nao_acusa(self):
        a = "## Rastreabilidade\n\n| No orçamento | Linha |\n|---|---|\n| X | sem aspas |\n"
        self.assertEqual(checa_rastreabilidade(a), [])


COBERTURA_BOA = """## O que a vaga pediu, e onde cada pedido foi

| Exigência da vaga | Onde foi atendido |
|---|---|
| "aplicativo de delivery completo" | app do cliente, 9 telas |
| "priorizando a atribuição de entregas aos profissionais mais próximos" | motor de atribuição |
"""


class TesteCoberturaDaVaga(unittest.TestCase):
    def test_tabela_completa_passa(self):
        self.assertEqual(checa_cobertura_da_vaga(VAGA_DELIVERY + COBERTURA_BOA), [])

    def test_falta_da_tabela_reprova(self):
        r = checa_cobertura_da_vaga(VAGA_DELIVERY)
        self.assertEqual([x.regra for x in r], ["sem a tabela de cobertura da vaga"])

    def test_exigencia_sem_destino_reprova(self):
        """O esquecimento: o cliente pediu, ninguém orçou, e aparece na entrega."""
        a = VAGA_DELIVERY + COBERTURA_BOA + '| "interfaces (...) para os motoboys" | |\n'
        r = [x for x in checa_cobertura_da_vaga(a) if x.nivel == ERRO]
        self.assertEqual([x.regra for x in r], ["exigência da vaga sem destino"])

    def test_exigencia_reescrita_reprova(self):
        a = VAGA_DELIVERY + COBERTURA_BOA + '| "programa de fidelidade" | upsell |\n'
        r = [x for x in checa_cobertura_da_vaga(a) if x.nivel == ERRO]
        self.assertEqual([x.regra for x in r], ["exigência que não está na vaga"])

    def test_sem_vaga_nao_acusa(self):
        self.assertEqual(checa_cobertura_da_vaga(COBERTURA_BOA), [])


class TesteCitacaoReaproveitada(unittest.TestCase):
    def test_o_saque_duplo_do_mapa(self):
        """O caso real de 19/08/2026, na forma em que a máquina consegue vê-lo.

        A citação genérica é verdadeira nas duas linhas, então checa_rastreabilidade passa.
        O que denuncia é ela bancar dois itens diferentes do orçamento.
        """
        a = _analise(
            '| Motor de atribuição | "priorizando a atribuição de entregas aos profissionais mais próximos" |\n'
            '| Rastreamento no mapa | "atribuição de entregas aos profissionais mais próximos" |\n'
        )
        r = checa_citacao_reaproveitada(a)
        self.assertEqual([x.nivel for x in r], [ALERTA])
        self.assertIn("Motor de atribuição", r[0].detalhe)
        self.assertIn("Rastreamento no mapa", r[0].detalhe)

    def test_citacoes_distintas_passam(self):
        a = _analise(
            '| App do motoboy | "interfaces (...) para os motoboys" |\n'
            '| App do cliente | "interfaces para o usuário final (cliente)" |\n'
        )
        self.assertEqual(checa_citacao_reaproveitada(a), [])

    def test_excecao_declarada_fica_de_fora_da_conta(self):
        a = _analise(
            '| App do motoboy | "interfaces (...) para os motoboys" |\n'
            "| Setup e testes | **exceção declarada:** custo de projeto |\n"
        )
        self.assertEqual(checa_citacao_reaproveitada(a), [])

    def test_fragmento_curto_nao_conta(self):
        """Trecho curto repetido é ruído, não saque duplo."""
        a = _analise(
            '| Um | "aplicativo" |\n'
            '| Dois | "aplicativo" |\n'
        )
        self.assertEqual(checa_citacao_reaproveitada(a), [])



class TestFrasesDoPrecoPassamNaVarredura(unittest.TestCase):
    """O `preco.py` entrega frases "prontas para colar" e a varredura reprova o texto pronto.

    Enquanto ninguém cruzou os dois, eles divergiram: até 19/08/2026 a frase do
    pagamento saía do script sem nomear a entrada, que é exatamente o que
    `checa_cobranca_sem_entrada` reprova. Este teste é a trava que faltava.
    """

    def _resultados(self):
        for dev, telas in ((35, 4), (150, 9), (365, 19), (40, 4)):
            yield preco.calcula(horas_dev=dev, telas=telas)

    def test_a_frase_da_cobranca_nao_e_reprovada(self):
        for r in self._resultados():
            frase = preco.frase_da_cobranca(r)
            self.assertEqual(checa_cobranca_sem_entrada(frase), [], frase)
            self.assertEqual(checa_formatacao(frase), [], frase)

    def test_a_frase_da_regua_nao_e_reprovada(self):
        for r in self._resultados():
            frase = preco.frase_da_regua(r)
            self.assertEqual(checa_exclusividade(frase), [], frase)
            self.assertEqual(checa_formatacao(frase), [], frase)
            self.assertEqual(checa_unidade_de_divisao(frase), [], frase)

    def test_as_duas_juntas_passam_como_paragrafos_da_proposta(self):
        """É assim que elas chegam ao texto: dois parágrafos, movimentos 4 e 5."""
        for r in self._resultados():
            texto = preco.frase_da_cobranca(r) + "\n\n" + preco.frase_da_regua(r)
            erros = [a for a in (checa_cobranca_sem_entrada(texto) + checa_formatacao(texto)) if a.nivel == ERRO]
            self.assertEqual(erros, [], texto)


if __name__ == "__main__":
    unittest.main(verbosity=2)
