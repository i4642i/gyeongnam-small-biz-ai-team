"""상권 진단 도구(app/tools/semas_district.py) — 집계 로직은 네트워크 없이 시험한다."""

from app.tools import semas_district as sd


def test_district_health_unknown_name():
    r = sd.district_health("없는구")
    assert "error" in r


def test_district_health_missing_code(monkeypatch, tmp_path):
    cfg = {"districts": [{"name": "테스트구", "signgu_code": None}], "known_areas": [], "aliases": {}}
    monkeypatch.setattr(sd, "_config", lambda region_id="changwon": cfg)
    r = sd.district_health("테스트구")
    assert "error" in r and "시군구코드" in r["error"]


def test_district_health_aggregates_by_upjong(monkeypatch):
    cfg = {"districts": [{"name": "테스트구", "signgu_code": "99999"}], "known_areas": [], "aliases": {}}
    monkeypatch.setattr(sd, "_config", lambda region_id="changwon": cfg)
    items = [{"indsLclsNm": "음식"}] * 6 + [{"indsLclsNm": "소매"}] * 3 + [{"indsLclsNm": "교육"}] * 1
    monkeypatch.setattr(sd, "stores_in_district", lambda code: (items, None))
    r = sd.district_health("테스트구")
    assert r["total_stores"] == 10
    assert r["by_upjong"][0] == {"name": "음식", "count": 6, "share_pct": 60.0}
