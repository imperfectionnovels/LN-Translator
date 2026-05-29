import os
import tempfile
from pathlib import Path

# Override DB_PATH BEFORE any backend module is imported, so backend.config
# picks it up at import time.
_TEST_DB = Path(tempfile.mkdtemp(prefix="cnt-tests-")) / "test.db"
os.environ["DB_PATH"] = str(_TEST_DB)

# Same idea for the LLM response cache: redirect into a fresh temp dir so
# tests can't pollute (or read from) the real data/llm_cache.
_TEST_CACHE = Path(tempfile.mkdtemp(prefix="cnt-tests-cache-"))
os.environ["LLM_CACHE_ROOT"] = str(_TEST_CACHE)

# H1: the app enforces a Host-header allowlist (TrustedHostMiddleware). Starlette's
# TestClient sends "Host: testserver", so add it to the allowlist for the suite.
os.environ["LN_TRANSLATOR_ALLOWED_HOSTS"] = "127.0.0.1,localhost,testserver"
