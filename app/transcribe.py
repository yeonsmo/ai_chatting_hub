"""회의 녹음 전사(자체 호스팅 STT).

faster-whisper로 서버에서 직접 음성을 텍스트로 변환한다.
- 외부 API 키 불필요, 오디오가 서버 밖으로 나가지 않음(사내 데이터 보호).
- 모델은 최초 사용 시 1회 로드/다운로드되고 이후 프로세스 캐시로 재사용.
- 로드 실패(자원/네트워크 부족 등) 시 명확한 예외를 던져 전사 기능만 실패시키고
  나머지 앱 동작에는 영향을 주지 않는다(지연 로딩).
"""
import asyncio
import threading

from app.config import settings

_model = None
_lock = threading.Lock()


class TranscribeUnavailable(RuntimeError):
    """음성인식 모델을 불러오지 못했을 때(자원/네트워크 등)."""


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
                _model = WhisperModel(
                    settings.whisper_model,
                    device=settings.whisper_device,
                    compute_type=settings.whisper_compute,
                )
            except Exception as e:  # noqa: BLE001 - 어떤 로드 실패든 사용자에게 명확히 전달
                raise TranscribeUnavailable(
                    "음성인식 모델을 불러오지 못했습니다. 서버 자원/인터넷(모델 다운로드)을 확인하세요. "
                    f"({type(e).__name__})"
                ) from e
    return _model


def transcribe_sync(path: str) -> str:
    """오디오 파일 → 텍스트(블로킹). 호출자는 스레드풀에서 실행할 것."""
    model = _get_model()
    segments, _info = model.transcribe(
        path,
        language=settings.whisper_language or None,
        vad_filter=True,               # 무음 구간 제거로 잡음/환청 감소
        beam_size=1,
    )
    lines = [seg.text.strip() for seg in segments if seg.text and seg.text.strip()]
    return "\n".join(lines).strip()


async def transcribe_async(path: str) -> str:
    """이벤트 루프를 막지 않도록 스레드풀에서 전사 실행."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, transcribe_sync, path)
