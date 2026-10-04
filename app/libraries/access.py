"""직원 → 회사 컴퓨터 찾기. 직원은 한 회사의 한 책상에만 앉으므로(app/offices/store.assign), 그 회사에 놓인 컴퓨터를 쓴다."""

from app.libraries import store
from app.offices import store as offices


def office_of(agent_id: str) -> dict | None:
    for office in offices.list_all():
        if agent_id in office["assignments"].values():
            return office
    return None


def computers_for(agent_id: str, region_id: str | None = None) -> list[dict]:
    """이 직원이 쓸 수 있는 컴퓨터(앉은 회사에 놓인 것). 사라진 컴퓨터 id 는 건너뛴다.
    region_id 를 주면(의뢰 지역이 있는 실행) 그 지역 전용 뉴스 컴퓨터만 돌려준다 — 다른 지역 기사가 섞이지 않게."""
    office = office_of(agent_id)
    out = []
    for lid in (office or {}).get("libraries") or []:
        try:
            out.append(store.get(lid))
        except store.LibraryNotFound:
            continue
    if region_id:
        from app import regions
        want = (regions.all_regions().get(region_id) or {}).get("news_computer")
        if want:
            out = [c for c in out if c["id"] == want]
    return out

