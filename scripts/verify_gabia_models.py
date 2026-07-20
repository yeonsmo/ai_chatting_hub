#!/usr/bin/env python3
"""가비아 AI Hub에서 '실제 사용 가능한' 모델을 즉석에서 검증하는 독립 스크립트.

앱을 배포하지 않고도, 가비아에 접속 가능한 아무 PC에서 바로 실행해
/v1/models 목록을 받고 각 모델을 최소 요청으로 호출해 본다.
(이 스크립트는 앱의 admin '가비아 모델 자동 검증·등록' 기능과 동일한 로직을
 커맨드라인으로 옮긴 것 — 결과 목록을 DEFAULT_MODEL_ROUTES 시드에 반영할 때 유용.)

사용법:
    GABIA_API_KEY=발급받은키 python scripts/verify_gabia_models.py
    # 옵션
    GABIA_API_KEY=... AI_HUB_BASE_URL=https://ai-hub.gabia.com \
        MAX_PROBE=200 CONCURRENCY=4 python scripts/verify_gabia_models.py

필요 패키지: openai (requirements.txt에 이미 포함).
출력: 사용가능/실패 모델 목록 + 시드에 붙여넣기 좋은 튜플 스니펫.
"""
import asyncio
import os
import sys

try:
    from openai import AsyncOpenAI
    import openai
except ImportError:
    sys.exit("openai 패키지가 필요합니다:  pip install openai")

BASE_URL = os.environ.get("AI_HUB_BASE_URL", "https://ai-hub.gabia.com").rstrip("/")
API_KEY = os.environ.get("GABIA_API_KEY", "").strip()
MAX_PROBE = int(os.environ.get("MAX_PROBE", "200"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "4"))
TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "30"))

# 대화 모델이 아닌 게 명백한 것(임베딩/음성/이미지/리랭커 등)은 검증 없이 건너뜀
NON_CHAT_HINTS = ("embedding", "embed", "rerank", "reranker", "whisper", "tts", "audio",
                  "moderation", "image", "vision-ocr", "dall-e", "stable-diffusion",
                  "flux", "clip", "guard", "bge", "gte")


def looks_non_chat(mid: str) -> bool:
    m = mid.lower()
    return any(h in m for h in NON_CHAT_HINTS)


async def probe(client, mid: str) -> tuple[str, bool, str]:
    msgs = [{"role": "user", "content": "connectivity check — reply with 'ok'."}]

    async def _call():
        try:
            return await client.chat.completions.create(model=mid, messages=msgs, max_completion_tokens=16)
        except openai.BadRequestError as e:
            if "max_completion_tokens" not in str(e):
                raise
            return await client.chat.completions.create(model=mid, messages=msgs, max_tokens=16)

    try:
        resp = await asyncio.wait_for(_call(), timeout=TIMEOUT)
        choice = resp.choices[0] if getattr(resp, "choices", None) else None
        msg = getattr(choice, "message", None) if choice else None
        reply = ((getattr(msg, "content", "") or "").strip().replace("\n", " ")) if msg else ""
        return mid, True, (reply[:40] or "응답 수신")
    except asyncio.TimeoutError:
        return mid, False, f"시간 초과({int(TIMEOUT)}s)"
    except Exception as e:  # noqa: BLE001
        detail = ""
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:
                detail = ""
        code = getattr(e, "status_code", "")
        return mid, False, (f"HTTP {code}: {detail}" if detail else str(e))[:160]


async def main():
    if not API_KEY:
        sys.exit("환경변수 GABIA_API_KEY를 설정하세요.")
    client = AsyncOpenAI(api_key=API_KEY, base_url=f"{BASE_URL}/v1", timeout=TIMEOUT)
    try:
        print(f"[1/3] /v1/models 조회 → {BASE_URL}/v1/models")
        raw = set()
        page = await client.models.list()
        while page is not None:                      # 페이지네이션까지 따라가 전체 수집
            for m in (getattr(page, "data", None) or []):
                if getattr(m, "id", None):
                    raw.add(str(m.id))
            has_next = getattr(page, "has_next_page", None)
            try:
                if callable(has_next) and has_next():
                    page = await page.get_next_page()
                    continue
            except Exception:
                pass
            break
        ids = sorted(raw)
        print(f"      노출 모델 {len(ids)}개")

        candidates = [m for m in ids if not looks_non_chat(m)][:MAX_PROBE]
        skipped = [m for m in ids if looks_non_chat(m)]
        print(f"[2/3] 대화 모델 후보 {len(candidates)}개 검증(동시 {CONCURRENCY}) · 비대화 제외 {len(skipped)}개")

        sem = asyncio.Semaphore(CONCURRENCY)

        async def _one(mid):
            async with sem:
                return await probe(client, mid)

        results = await asyncio.gather(*[_one(m) for m in candidates], return_exceptions=True)
    finally:
        await client.close()
    results = [r if not isinstance(r, Exception) else ("?", False, f"검증 오류: {r}") for r in results]
    working = sorted([(m, n) for m, ok, n in results if ok])
    failed = sorted([(m, n) for m, ok, n in results if not ok])

    print(f"\n[3/3] 결과: 사용 가능 {len(working)} · 실패 {len(failed)}\n")
    print("── 사용 가능 ──")
    for m, note in working:
        print(f"  ✅ {m:<40} {note}")
    print("\n── 실패(권한/미지원/비대화 등) ──")
    for m, note in failed:
        print(f"  ❌ {m:<40} {note}")
    if skipped:
        print("\n── 비대화(사전 제외) ──")
        print("  " + ", ".join(skipped))

    print("\n── DEFAULT_MODEL_ROUTES 시드용 스니펫(사용 가능분) ──")
    for i, (m, _) in enumerate(working):
        key = m.replace(".", "-")
        print(f'    ("{key}", "{m}", "gabia", "{m}", "자동검증됨", {100 + i}),')


if __name__ == "__main__":
    asyncio.run(main())
