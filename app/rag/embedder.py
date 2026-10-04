"""로컬 임베딩 모델(bge-m3). 문서 조각과 질문을 숫자 벡터로 바꾼다. 공급사(Claude/OpenAI/Gemini)와 무관하다.

- 처음 쓸 때 한 번만 불러온다(약 2GB, 메모리에 상주). 처음 실행 때는 모델 파일을 내려받아 data/models 에 저장한다.
- GPU 없이 CPU로 돈다. 이 PC(i5-8500)에서 측정한 값: 조각(≤700자) 하나에 약 0.7초, 질문 하나에 약 0.13초.
  → 100쪽 PDF(약 400조각) 처리에 약 4.7분. (기준: 5분 넘으면 가벼운 모델로 교체)
- 모델을 바꾸면(EMBED_MODEL) 벡터 차원과 의미 공간이 달라져서 이미 만든 색인은 쓸 수 없다 → 문서를 다시 처리해야 한다.
"""

import hashlib
import logging
import os
import sqlite3
import threading
from pathlib import Path

import numpy as np

from app.config import DATA_DIR

log = logging.getLogger("agent_town.embedder")

# 모델 파일(약 2GB)은 캐릭터 데이터와 무관하게 공용이다. 테스트가 데이터 폴더를 따로 써도 다시 내려받지 않도록 위치를 따로 지정할 수 있다.
MODELS_DIR = Path(os.getenv("AGENT_TOWN_MODELS") or DATA_DIR / "models")
EMBED_MODEL = os.getenv("EMBED_MODEL") or "BAAI/bge-m3"
MAX_SEQ_LENGTH = 512     # 조각 하나가 넘기지 않는 길이. 줄이면 빠르지만 긴 조각의 뒷부분이 잘린다
BATCH_SIZE = 32   # 2026-09-22: 8→32 — 한 번에 더 많은 조각을 모델에 넣어(배치) CPU 추론 오버헤드를 줄인다(worker.py 의 STEP 과 맞춤)

# 조각 글 해시(+모델)로 벡터를 캐시한다(2026-09-22 추가) — 같은 글(문서를 다시 처리하거나, 여러 문서에 같은 문구가
# 있을 때)은 모델을 다시 안 돌리고 저장해 둔 벡터를 그대로 쓴다. 캐릭터와 무관한 공용 캐시(내용+모델이 같으면 벡터도 같다).
_CACHE_PATH = Path(os.getenv("AGENT_TOWN_EMBED_CACHE") or DATA_DIR / "embed_cache.sqlite")
_cache_conn: sqlite3.Connection | None = None

# 원격 색인 장비(2026-09-27, 젯슨 Orin Nano): 설정돼 있으면 벡터 계산을 거기로 보낸다. 같은 모델·같은 설정(정규화·접두어 없음·512)이라
# 벡터가 사실상 같다(기준값 검사 최대 차이 0.00016). 젯슨을 고른 상태에서 장비가 응답하지 않으면 PC 로 넘어가지 않고 색인이 멈춘다.
# 장비 쪽 설치: docs/젯슨_색인장비_설치지시서_2026-09-27.md
# 기본은 이 PC(2026-10-04). .env 의 EMBED_REMOTE_URL 주소는 보관만 하고, 화면에서 [젯슨] 을 눌러야 젯슨으로 계산한다
# (젯슨이 없는 PC 에서 색인이 멈추지 않게). 토큰·리랭커 기본값도 .env 의 EMBED_REMOTE_TOKEN·EMBED_REMOTE_RERANK.
# 화면(뉴스 수집 컴퓨터 → 색인 장비)에서 바꾸면 data/config/index_device.json 에 남고 그쪽이 우선한다(재시작 없이 다음 계산부터).
# 토큰은 화면 설정에 없으면 .env 것을 쓰고, 화면에서 저장할 때 .env 의 토큰을 파일로 옮겨 적지 않는다(비밀값을 데이터 폴더에 남기지 않게).
DEVICE_PATH = Path(os.getenv("AGENT_TOWN_INDEX_DEVICE") or DATA_DIR / "config" / "index_device.json")
REMOTE_BATCH = 64
remote = {"url": "", "state": "unknown", "message": "", "last_ok": None}   # 주소가 없으면 화면이 off 로 보인다


def _env_device() -> dict:
    return {"url": (os.getenv("EMBED_REMOTE_URL") or "").strip().rstrip("/"), "token": (os.getenv("EMBED_REMOTE_TOKEN") or "").strip(),
            "rerank": (os.getenv("EMBED_REMOTE_RERANK") or "").strip() == "1"}


def _file_device() -> dict | None:
    """화면에서 저장한 설정(없거나 못 읽으면 None)."""
    import json
    if not DEVICE_PATH.is_file():
        return None
    try:
        return json.loads(DEVICE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("색인 장비 설정 파일을 읽지 못했습니다: %s", DEVICE_PATH, exc_info=True)
        return None


def device_config() -> dict:
    """{"url", "saved_url", "use_local", "token", "rerank"} — 화면 설정이 있으면 그것, 비어 있는 항목은 .env 값.
    rerank=True 면 검색 순위 매기기(리랭커)도 장비의 /rerank 로 보낸다(2026-09-29, 젯슨에 /rerank 를 설치한 뒤 켠다)."""
    env, d = _env_device(), _file_device()
    if d is None:
        # 화면에서 고른 적이 없으면 이 PC 로 계산한다. .env 주소는 saved_url 로 보관만 해 두어 [젯슨] 을 누르면 바로 쓴다.
        return {"url": "", "saved_url": env["url"], "use_local": True, "token": env["token"], "rerank": env["rerank"]}
    saved = str(d.get("url") or "").rstrip("/") or env["url"]
    local = bool(d.get("use_local", True))   # 켜 두면 주소는 보관만 하고 이 PC 로 계산한다(화면의 장비 스위치). 키가 없으면 이 PC
    return {"url": "" if local else saved, "saved_url": saved, "use_local": local,
            "token": str(d.get("token") or "") or env["token"],
            "rerank": bool(d["rerank"]) if "rerank" in d else env["rerank"]}


def _file_token() -> str:
    """화면 설정 파일에 직접 저장된 토큰만(.env 토큰은 포함하지 않는다)."""
    return str((_file_device() or {}).get("token") or "")


def set_use_local(local: bool) -> dict:
    """장비 스위치: True 면 이 PC 로, False 면 저장해 둔 젯슨 주소로 계산한다(주소·토큰은 지우지 않는다)."""
    import json
    cur = device_config()
    cfg = {"url": cur["saved_url"], "token": _file_token(), "rerank": cur["rerank"], "use_local": bool(local)}
    DEVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEVICE_PATH.with_name(DEVICE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DEVICE_PATH)
    active = "" if local else cfg["url"]
    remote.update(url=active, state="unknown" if active else "off", message="", last_ok=None)
    return cfg


def save_device_config(url: str, token: str | None, rerank: bool | None = None) -> dict:
    """url 을 비우면 원격 장비를 끈다(이 PC 로 계산). token·rerank 가 None 이면 지금 값을 그대로 둔다."""
    import json
    cur = device_config()
    cfg = {"url": url.strip().rstrip("/"), "token": _file_token() if token is None else token.strip(),
           "rerank": cur["rerank"] if rerank is None else bool(rerank), "use_local": False}
    DEVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEVICE_PATH.with_name(DEVICE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DEVICE_PATH)
    remote.update(url=cfg["url"], state="unknown" if cfg["url"] else "off", message="", last_ok=None)
    return cfg


def device_health(url: str, token: str) -> dict:
    """장비에 /health 를 물어 모델·벡터 길이가 이 PC 와 같은지 확인한다(설정 화면의 '연결 시험')."""
    import json
    import urllib.request
    req = urllib.request.Request(f"{url.rstrip('/')}/health", headers={"X-Embed-Token": token})
    with urllib.request.urlopen(req, timeout=10) as r:
        h = json.loads(r.read())
    ok = h.get("model") == EMBED_MODEL and h.get("dim") == 1024 and h.get("max_seq_length") == MAX_SEQ_LENGTH
    return {"ok": ok, "health": h, "problem": None if ok else f"장비 설정이 이 PC 와 다릅니다(모델 {h.get('model')}, 벡터 {h.get('dim')}, 길이 {h.get('max_seq_length')})"}

# 화면에 보여 줄 상태: idle(아직 안 씀) / loading(불러오는 중) / ready / error
state = {"state": "idle", "message": ""}

_model = None
_lock = threading.RLock()   # 불러오기와 추론을 한 번에 하나씩 (질문 임베딩과 문서 처리가 같은 모델을 쓴다)


def _cached() -> bool:
    return (MODELS_DIR / ("models--" + EMBED_MODEL.replace("/", "--"))).is_dir()


def get_model():
    global _model
    with _lock:
        if _model is not None:
            return _model
        cached = _cached()
        state.update(
            state="loading",
            message="임베딩 모델을 불러오는 중입니다" if cached else "임베딩 모델을 내려받는 중입니다 (처음 한 번, 약 2GB, 수 분~십여 분)",
        )
        try:
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            import torch
            from sentence_transformers import SentenceTransformer

            torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))   # 한 코어는 서버가 쓰도록 남긴다
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            try:
                m = SentenceTransformer(EMBED_MODEL, cache_folder=str(MODELS_DIR), device="cpu", local_files_only=cached)
            except Exception:
                if not cached:
                    raise
                log.warning("저장된 모델을 못 읽어 다시 내려받습니다", exc_info=True)   # 내려받다 만 캐시일 수 있다
                m = SentenceTransformer(EMBED_MODEL, cache_folder=str(MODELS_DIR), device="cpu")
            m.max_seq_length = MAX_SEQ_LENGTH
            _model = m
            state.update(state="ready", message="")
            log.info("임베딩 모델 준비: %s (차원 %s)", EMBED_MODEL, m.get_sentence_embedding_dimension())
            return _model
        except Exception as e:
            state.update(state="error", message=f"임베딩 모델을 불러오지 못했습니다: {type(e).__name__}: {e}")
            raise


def _cache() -> sqlite3.Connection:
    global _cache_conn
    if _cache_conn is None:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _cache_conn = sqlite3.connect(_CACHE_PATH, check_same_thread=False)
        _cache_conn.execute("CREATE TABLE IF NOT EXISTS vectors (hash TEXT NOT NULL, model TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY (hash, model))")
    return _cache_conn


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed(texts: list[str]) -> np.ndarray:
    """텍스트 목록 → (개수, 차원) float32 배열. 길이가 1이 되도록 정규화되어 있어 내적이 곧 코사인 유사도다.
    글 해시(+모델)로 캐시를 먼저 보고, 없는 것만 모델에 돌린다."""
    with _lock:
        hashes = [_hash(t) for t in texts]
        conn = _cache()
        found: dict[str, np.ndarray] = {}
        # SQLite 는 한 번에 바인딩할 수 있는 변수 수(기본 999)가 있어 큰 문서는 나눠 조회한다
        for i in range(0, len(hashes), 900):
            batch = hashes[i:i + 900]
            rows = conn.execute(
                f"SELECT hash, vector FROM vectors WHERE model=? AND hash IN ({','.join('?' * len(batch))})",
                [EMBED_MODEL, *batch],
            ).fetchall()
            for h, blob in rows:
                found[h] = np.frombuffer(blob, dtype="float32")

        missing = [(i, t) for i, (t, h) in enumerate(zip(texts, hashes)) if h not in found]
        if missing:
            dev = device_config()
            remote["url"] = dev["url"]
            if not dev["url"]:
                remote.update(state="off", message="")
            new_vecs = None
            if dev["url"]:
                # 젯슨이 설정돼 있으면 PC CPU 로 몰래 넘어가지 않는다(2026-09-28 사용자 지시: "CPU로 색인 하지 마").
                # 잠깐씩 기다리며 다시 시도하고, 끝내 안 되면 오류로 멈춘다 — 문서는 실패로 표시되고 로그에 남는다.
                import time as _time
                for wait in (0, 3, 10, 30):
                    if wait:
                        _time.sleep(wait)
                    new_vecs = _remote_encode([t for _, t in missing], dev)
                    if new_vecs is not None:
                        break
                if new_vecs is None:
                    raise RuntimeError(f"색인 장비(젯슨 {dev['url']})가 응답하지 않습니다 — PC CPU 로는 계산하지 않습니다. "
                                       f"젯슨 전원·IP 를 확인하세요(마지막 오류: {remote.get('message', '')}).")
            if new_vecs is None:   # 색인 장비 주소가 비어 있을 때만 PC 로 계산한다
                model = get_model()
                new_vecs = model.encode(
                    [t for _, t in missing], batch_size=BATCH_SIZE, normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True,
                ).astype("float32")
            rows_to_insert = []
            for (i, _), v in zip(missing, new_vecs):
                found[hashes[i]] = v
                rows_to_insert.append((hashes[i], EMBED_MODEL, v.tobytes()))
            conn.executemany("INSERT OR REPLACE INTO vectors (hash, model, vector) VALUES (?, ?, ?)", rows_to_insert)
            conn.commit()

        return np.stack([found[h] for h in hashes])


def _remote_encode(texts: list[str], dev: dict) -> np.ndarray | None:
    """원격 색인 장비로 계산한다. 실패하면 None(부르는 쪽이 다시 시도하고, 끝내 안 되면 오류 — PC 로 넘어가지 않는다)."""
    import json
    import time
    import urllib.request
    out = []
    try:
        for i in range(0, len(texts), REMOTE_BATCH):
            req = urllib.request.Request(f"{dev['url']}/embed", data=json.dumps({"texts": texts[i:i + REMOTE_BATCH]}).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "X-Embed-Token": dev["token"]})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
            if data.get("model") != EMBED_MODEL or data.get("dim") != 1024:
                raise ValueError(f"장비의 모델이 다릅니다: {data.get('model')} / {data.get('dim')}")
            out.extend(data["vectors"])
        remote.update(state="ok", message="", last_ok=time.time())
        return np.asarray(out, dtype="float32")
    except Exception as e:
        remote.update(state="error", message=f"{type(e).__name__}: {e}"[:200])
        log.warning("원격 색인 장비 실패 — 다시 시도합니다(PC CPU 로는 계산하지 않음): %s", e)
        return None


def embed_query(text: str) -> np.ndarray:
    return embed([text])[0]
