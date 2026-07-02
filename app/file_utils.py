import os
import io
import uuid
from app.config import settings

# 텍스트로 취급할 확장자 (모델 컨텍스트에 내용 주입 가능)
TEXT_EXTENSIONS = {
    "txt", "md", "markdown", "csv", "tsv", "json", "yaml", "yml", "xml", "html", "htm",
    "css", "js", "ts", "jsx", "tsx", "py", "java", "c", "cpp", "cc", "h", "hpp", "cs",
    "go", "rs", "rb", "php", "kt", "swift", "sh", "bash", "zsh", "sql", "log", "ini",
    "toml", "cfg", "conf", "env", "bat", "ps1", "r", "m", "scala", "lua", "pl", "dart",
    "vue", "svelte", "tex", "rst", "properties", "gradle", "dockerfile", "makefile",
}

MAX_EXTRACT_CHARS = 200_000  # DB에 저장할 추출 텍스트 상한


def ensure_upload_dir() -> str:
    path = os.path.abspath(settings.upload_dir)
    os.makedirs(path, exist_ok=True)
    return path


def safe_ext(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    return "".join(ch for ch in ext if ch.isalnum())[:12]


def new_stored_name(filename: str) -> str:
    ext = safe_ext(filename)
    name = uuid.uuid4().hex
    return f"{name}.{ext}" if ext else name


def stored_path(stored_name: str) -> str:
    return os.path.join(ensure_upload_dir(), stored_name)


def is_text_like(filename: str, content_type: str) -> bool:
    if (content_type or "").startswith("text/"):
        return True
    if content_type in ("application/json", "application/xml", "application/javascript",
                        "application/x-yaml", "application/sql"):
        return True
    base = os.path.basename(filename).lower()
    if base in ("dockerfile", "makefile"):
        return True
    return safe_ext(filename) in TEXT_EXTENSIONS


def extract_text(data: bytes, filename: str, content_type: str) -> str | None:
    """가능한 형식이면 텍스트를 추출해 반환, 아니면 None."""
    if is_text_like(filename, content_type):
        try:
            return data.decode("utf-8", errors="replace")[:MAX_EXTRACT_CHARS]
        except Exception:
            return None

    if content_type == "application/pdf" or safe_ext(filename) == "pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = []
            total = 0
            for page in reader.pages:
                t = page.extract_text() or ""
                pages.append(t)
                total += len(t)
                if total > MAX_EXTRACT_CHARS:
                    break
            text = "\n\n".join(pages).strip()
            return text[:MAX_EXTRACT_CHARS] if text else None
        except Exception:
            return None

    return None
