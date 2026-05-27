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
