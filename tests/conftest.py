"""Pin the settings the tests are written for, so a COST_SCALE / SENTIMENT_MODEL / CLAIM_NLP left in the shell,
a .env export or the Windows environment cannot change the expected decisions. Runs before any test module imports intelligence."""
import os

os.environ["COST_SCALE"] = "1.0"
os.environ["CLAIM_NLP"] = "rules"
for _k in ("SENTIMENT_MODEL", "LLM_API_KEY", "DATABASE_URL"):
    os.environ.pop(_k, None)
