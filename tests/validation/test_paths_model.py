from validation.model import build_calc
from validation.paths import extract, render


def test_extract_paths():
    d = {"a": {"b": [{"k": 1, "v": 5}, {"k": 2, "v": 7}]}}
    assert extract(d, "a.b[0].v") == 5
    assert extract(d, "a.b[k=2].v") == 7
    assert extract(d, "a.b[*].v") == [5, 7]
    assert extract(d, "a.b[*].v|sum") == 12
    assert extract(d, "a.b|len") == 2
    assert extract(d, "a.zzz") is None


def test_render_templates():
    ctx = {"n": 3, "calc": {"x": 4}}
    assert render("{{ n + 1 }}", ctx) == 4
    assert render("x-{{ n }}", ctx) == "x-3"
    assert render({"a": ["{{ calc.x * 2 }}"]}, ctx) == {"a": [8]}


def test_calc_matches_spec_numbers():
    c = build_calc()
    assert 41 <= c["cap30_lo"] <= c["cap30_hi"] <= 45
    assert c["cap50_lo"] == 64 or c["cap50_hi"] == 64
    assert c["over30"] > c["cap30_hi"]
    assert c["ratio_b"] < c["eta"] < c["ratio_c"]
    assert c["eta_ok"]
