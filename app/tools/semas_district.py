"""상권 진단 도구(2026-09-29) — 소상공인시장진흥공단 상가(상권)정보 오픈API(data.go.kr, 서비스 B553077).

구(시군구) 단위로 업종 대분류별 점포 수를 세어 "이 동네에 이 업종이 얼마나 몰려 있는지"를 돌려준다.
상권 분석 담당자가 판단 전에 실제 숫자를 이 도구로 받는다.

**시군구코드는 아직 확인 전이다**(data/config/changwon_areas.json 의 districts[].signgu_code 가 null).
실제 인증키를 받으면 `storeListInDong` 대신 구 이름으로 먼저 `storeZoneInAdmi`(행정구역상권조회)나 여러 코드
후보를 시도해 실제 응답이 오는 코드를 찾아 채운다(문서만 믿지 않고 라이브로 확인 — 2026-09-28 DART 때와 같은 원칙).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
LEGACY_CONFIG = ROOT / "data" / "config" / "changwon_areas.json"   # 2026-09-29 첫 버전 — 지역이 창원 하나뿐이던 시절
REGIONS_DIR = ROOT / "data" / "config" / "regions"                # 2026-09-29 일반화: 지역마다 data/config/regions/<region_id>.json
CACHE_DIR = ROOT / "data" / "cache" / "semas"
CACHE_DAYS = 7
BASE = "http://apis.data.go.kr/B553077/api/open/sdsc2"


def _config(region_id: str = "changwon") -> dict:
    """지역 설정(구 목록·시군구코드). region_id 를 안 주면 창원(기존 회사와 호환) — 새 지역은 반드시 region_id 를 준다."""
    p = REGIONS_DIR / f"{region_id}.json"
    if not p.is_file() and region_id == "changwon" and LEGACY_CONFIG.is_file():
        p = LEGACY_CONFIG   # 옛 경로도 당분간 계속 읽는다(리팩터링 전 실행 기록과의 호환)
    if not p.is_file():
        raise FileNotFoundError(f"지역 설정을 찾을 수 없습니다: {region_id} ({p})")
    return json.loads(p.read_text(encoding="utf-8"))


def _key() -> str:
    k = os.getenv("SEMAS_API_KEY")
    if not k:
        raise RuntimeError("SEMAS_API_KEY 가 .env 에 없습니다.")
    return k


PAGE_SIZE = 1000   # 실측(2026-09-29): numOfRows 상한이 1000 근처라 그 이상은 페이지를 나눠 받아야 한다
MAX_STORES = 30000   # 안전판 — 구 하나가 이보다 많이 나오면 뭔가 잘못 짚은 것(코드 오류 등)


def _get(path: str, params: dict, page_no: int = 1, num_of_rows: int = 10) -> dict:
    q = urlencode({"serviceKey": _key(), "type": "json", "numOfRows": num_of_rows, "pageNo": page_no, **params})
    url = f"{BASE}/{path}?{q}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"SEMAS API 오류({e.code}): {e.read()[:200]}") from e
    # 실측(2026-09-29): type=json 응답은 XML 과 달리 response 로 한 번 더 안 감싼다({"header","body"} 가 최상위).
    header = data.get("header") or {}
    if header.get("resultCode") not in ("00", "0"):
        raise RuntimeError(f"SEMAS API 오류: {header.get('resultMsg')}")
    body = data.get("body") or {}
    items = body.get("items")
    items = [] if not items else (items if isinstance(items, list) else [items])
    return {"items": items, "total": int(body.get("totalCount") or len(items)), "stdr_ym": body.get("stdrYm")}


# 캐시에는 진단·검색에 쓰는 항목만 남긴다(2026-09-30): 주소·우편번호 등을 빼면 크기가 1/5 정도로 줄어 지역이 늘어도 부담이 적다.
SLIM_KEYS = ("bizesNm", "bldNm", "indsLclsCd", "indsLclsNm", "indsMclsCd", "indsMclsNm", "indsSclsCd", "indsSclsNm",
             "signguCd", "signguNm", "adongCd", "adongNm", "ldongCd", "ldongNm", "lon", "lat")
_MEM: dict[str, tuple[float, list, str | None]] = {}   # 시군구코드 → (캐시 파일 수정시각, 점포, 기준월) — 요청마다 큰 파일을 다시 읽지 않게


def _slim(it: dict) -> dict:
    return {k: it.get(k) for k in SLIM_KEYS}


def _write_cache(cache: Path, items: list, stdr_ym) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"items": items, "stdr_ym": stdr_ym}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def stores_in_district(signgu_code: str) -> tuple[list[dict], str | None]:
    """시군구 코드로 전체 상가업소 목록(페이지를 다 모아서, 캐시 7일). 실측용 원본 — district_health()·지역 트리가 집계에 쓴다."""
    cache = CACHE_DIR / f"{signgu_code}.json"
    if cache.is_file() and time.time() - cache.stat().st_mtime < CACHE_DAYS * 86400:
        mt = cache.stat().st_mtime
        hit = _MEM.get(signgu_code)
        if hit and hit[0] == mt:
            return hit[1], hit[2]
        cached = json.loads(cache.read_text(encoding="utf-8"))
        items = cached["items"]
        if items and "rdnmAdr" in items[0]:   # 예전(전체 항목) 캐시는 한 번 가볍게 바꿔 다시 저장한다
            items = [_slim(i) for i in items]
            _write_cache(cache, items, cached["stdr_ym"])
            mt = cache.stat().st_mtime
        _MEM[signgu_code] = (mt, items, cached["stdr_ym"])
        return items, cached["stdr_ym"]
    first = _get("storeListInDong", {"divId": "signguCd", "key": signgu_code}, page_no=1, num_of_rows=PAGE_SIZE)
    total, stdr_ym = min(first["total"], MAX_STORES), first["stdr_ym"]
    items = list(first["items"])
    page = 2
    while len(items) < total:
        got = _get("storeListInDong", {"divId": "signguCd", "key": signgu_code}, page_no=page, num_of_rows=PAGE_SIZE)
        if not got["items"]:
            break
        items.extend(got["items"])
        page += 1
        time.sleep(0.1)
    items = [_slim(i) for i in items]
    _write_cache(cache, items, stdr_ym)
    _MEM[signgu_code] = (cache.stat().st_mtime, items, stdr_ym)
    return items, stdr_ym


MIN_AREA_STORES = 50   # 동 점포가 이보다 적으면 비율이 흔들려 구(시) 전체로 돌아간다


def find_upjong(name: str, items: list[dict]) -> tuple[str, str] | None:
    """업종 이름(대·중·소분류)을 (분류 수준의 점포 키, 이름)으로 찾는다. 대분류 → 중분류 → 소분류 순으로 이름이 같은 것."""
    for key in ("indsLclsNm", "indsMclsNm", "indsSclsNm"):
        if any(it.get(key) == name for it in items[:5000]) or any(it.get(key) == name for it in items):
            return key, name
    return None


def _mix(picked: list[dict], key: str, n: int) -> list[dict]:
    c = Counter(it.get(key) or "기타" for it in picked)
    total = sum(c.values()) or 1
    return [{"name": k, "count": v, "share_pct": round(v / total * 100, 1)} for k, v in c.most_common(n)]


def district_health(district_name: str, region_id: str = "changwon", area: str | None = None, upjong: str | None = None) -> dict:
    """구(district_name)의 업종 대분류별 점포 수 분포. {"district","total","by_upjong":[{"name","count","share"}],...}
    area(후보 동네)가 실제 행정동·법정동 이름이면 그 동만 센다. upjong(대·중·소분류 이름)이 있으면 그 업종의 집중도(focus)도 준다.
    region_id 를 생략하면 창원(기존 호출과 호환). 여러 지역을 쓸 때는 region_id 를 준다."""
    cfg = _config(region_id)
    row = next((d for d in cfg["districts"] if d["name"] == district_name), None)
    if row is None:
        return {"error": f"모르는 구 이름입니다: {district_name}"}
    if not row.get("signgu_code"):
        return {"error": f"{district_name}의 시군구코드가 아직 확인되지 않았습니다(SEMAS_API_KEY 발급 후 라이브로 채울 것)."}
    try:
        items, stdr_ym = stores_in_district(row["signgu_code"])
    except Exception as e:
        return {"error": f"SEMAS 자료를 받지 못했습니다: {e}"}
    if not items:
        return {"error": f"{district_name} 자료가 비어 있습니다."}
    scope, picked = "구 단위", items
    if area:   # 후보 동네가 실제 동(읍·면) 이름이면 그 동의 점포만 센다 — 구·시 전체 평균이 아니라 그 동네의 업종 구성
        in_area = [it for it in items if area in (it.get("adongNm"), it.get("ldongNm"))]
        if len(in_area) >= MIN_AREA_STORES:
            scope, picked = "동 단위", in_area
    mix = _mix(picked, "indsLclsNm", 10)
    total = len(picked)
    out = {"district": district_name, "scope": scope, **({"area": area, "district_total_stores": len(items)} if scope == "동 단위" else {}),
           "total_stores": total, "by_upjong": mix, "by_middle": _mix(picked, "indsMclsNm", 8)}
    if upjong:   # 의뢰 업종: 이 범위에서의 점포 수·비중과 시(구) 전체 비중(집중도 비교), 세부 업종 상위
        hit = find_upjong(upjong, items)
        if hit is None:
            out["focus"] = {"upjong": upjong, "error": "이 지역 자료에 없는 업종 이름입니다"}
        else:
            key, nm = hit
            mine = [it for it in picked if it.get(key) == nm]
            whole = sum(1 for it in items if it.get(key) == nm)
            detail_key = {"indsLclsNm": "indsMclsNm", "indsMclsNm": "indsSclsNm", "indsSclsNm": "indsSclsNm"}[key]
            share = round(len(mine) / total * 100, 1) if total else 0.0
            base = round(whole / len(items) * 100, 1)
            out["focus"] = {"upjong": nm, "count": len(mine), "share_pct": share, "wide_share_pct": base,
                            "vs_wide": round(share / base, 2) if base else None, "detail": _mix(mine, detail_key, 5)}
    out["note"] = (("동 단위: 후보 동(읍·면)의 점포만 센 값. " if scope == "동 단위" else "구 단위: 후보 동네가 동 이름과 맞지 않아(예: 공원·시설 일원) 구(시) 전체를 센 값. ")
                   + "값은 SEMAS(소상공인시장진흥공단) 상가업소정보 원자료를 이 도구가 직접 센 것(모델 계산 아님). 최신 스냅샷 점포 수일 뿐 정확한 기준월은 이 API가 안정적으로 안 준다. 폐업률·개업률·시계열은 이 API에 없다(점포 수 한 시점뿐).")
    return out
