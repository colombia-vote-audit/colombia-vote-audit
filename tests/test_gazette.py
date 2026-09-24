from cva.gazette import expand_year, normalize_number, parse_refs


def test_parse_single_and_multiple_refs():
    assert parse_refs("1739/22") == [(2022, "1739")]
    assert parse_refs("1283/22, 1309/22, 1616/22") == [
        (2022, "1283"),
        (2022, "1309"),
        (2022, "1616"),
    ]


def test_parse_refs_across_years_and_centuries():
    assert parse_refs("947/12, 292/13") == [(2012, "947"), (2013, "292")]
    assert parse_refs("625/99") == [(1999, "625")]
    assert parse_refs("12/2004") == [(2004, "12")]


def test_parse_refs_ignores_placeholders_and_duplicates():
    assert parse_refs(None) == []
    assert parse_refs("") == []
    assert parse_refs("S/N") == []
    assert parse_refs("1199/22, 1199/22") == [(2022, "1199")]


def test_normalize_number():
    assert normalize_number("09") == "9"
    assert normalize_number("242b") == "242B"
    assert normalize_number("0") == "0"
    assert parse_refs("09/13") == [(2013, "9")]


def test_expand_year():
    assert expand_year("98") == 1998
    assert expand_year("02") == 2002
    assert expand_year("2026") == 2026
