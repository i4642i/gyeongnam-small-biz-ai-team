"""후처리 훅 실행기. 서버가 별도 파이썬 프로세스로 띄운다: python -I -X utf8 hook_runner.py <훅 파일> <함수 이름>

표준 입력으로 검사할 JSON 을 받고, 훅 함수 `fn(data: dict) -> list[str]` 를 부른 뒤, 결과를 「@@RESULT@@ + JSON 한 줄」로 표준 출력에 낸다.
입력이 {"__hook_payload__": 1, "data": …, "config": …} 이면 data 로 훅을 부르고, config(표준 기준값)는 훅 모듈의 PLATFORM_CONFIG 에 넣는다.
(훅 코드가 print 를 해도 결과와 섞이지 않게 표지를 쓴다.) 오류 메시지 목록이 비어 있으면 통과다.
서버 프로세스와 분리되어 있어서 훅이 멈추거나 죽어도 서버는 영향을 받지 않는다(시간 제한은 서버가 건다).
"""

import importlib.util
import json
import sys
import traceback

MARK = "@@RESULT@@"

sys.dont_write_bytecode = True   # 훅 폴더에 __pycache__ 를 남기지 않는다


def main() -> None:
    path, func = sys.argv[1], sys.argv[2]
    data = json.load(sys.stdin)
    config = {}
    if isinstance(data, dict) and data.get("__hook_payload__"):   # 서버가 데이터와 표준 기준값을 함께 보낸 경우(2026-09-26~)
        data, config = data.get("data"), data.get("config") or {}
    try:
        spec = importlib.util.spec_from_file_location("task_hook", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["task_hook"] = module   # @dataclass 등은 자기 모듈을 sys.modules 에서 찾는다(등록하지 않으면 AttributeError)
        spec.loader.exec_module(module)
        module.PLATFORM_CONFIG = config     # 훅은 기준값을 숫자로 적지 않고 이 값에서 읽는다(표준 v1.1 §7)
        fn = getattr(module, func)
        result = fn(data)
        if not isinstance(result, (list, tuple)) or not all(isinstance(x, str) for x in result):
            out = {"error": f"훅 함수 {func}() 는 오류 메시지(글) 목록을 돌려줘야 합니다(지금: {type(result).__name__})."}
        else:
            out = {"errors": list(result)}
    except Exception as e:   # 훅 코드의 오류: 어디서 났는지 알 수 있게 마지막 줄들을 함께 보낸다
        tb = traceback.format_exc().strip().splitlines()
        out = {"error": f"{type(e).__name__}: {e}", "trace": tb[-6:]}
    sys.stdout.write("\n" + MARK + json.dumps(out, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
