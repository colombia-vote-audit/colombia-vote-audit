from cva.sample import doc_category, era_of, text_quality


def test_doc_category():
    assert doc_category("Acta de Plenaria") == "acta_plenaria"
    assert doc_category("Acta de Congreso Pleno") == "acta_plenaria"
    assert doc_category("Acta de Comisión") == "acta_comision"
    assert doc_category("Acta de Audiencia Pública") == "acta_other"
    assert doc_category("Informe de Conciliación  al  Proyecto de Ley") == "conciliacion"
    assert doc_category("Ponencia para Primer Debate al Proyecto de Ley") == "ponencia"
    assert doc_category("Proyecto de Ley") == "proyecto"
    assert doc_category("Leyes Sancionadas") == "ley_texto"
    assert doc_category("") == "unlabeled"
    assert doc_category("Sentencia de Constitucionalidad") == "other"


def test_era_of():
    assert era_of(2000) == "2000-2003"
    assert era_of(2026) == "2023-2030"
    assert era_of(12) is None


def test_text_quality():
    spanish = (
        "El proyecto de ley que se aprobó en la plenaria del Senado por los congresistas " * 20
    )
    assert text_quality(spanish, 1)[1] == "ok"
    garbled = "*ൺർൾඍൺൽൾඅ&ඈඇ඀උൾඌඈ 6(1$'2<&È0$5$ $UWtFXOR/H\\GH ,035(17$1$&,21$/ " * 20
    assert text_quality(garbled, 1)[1] == "garbled"
    assert text_quality("   ", 3)[1] == "no_text"
