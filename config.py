import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    MAX_TOKENS = int(os.getenv("MAX_TOKENS", "8000"))
    # Plans with full file bodies need a larger completion budget
    PLAN_MAX_TOKENS = int(os.getenv("PLAN_MAX_TOKENS", "16000"))
    FILE_CONTENT_MAX_TOKENS = int(os.getenv("FILE_CONTENT_MAX_TOKENS", "12000"))
    TEMPERATURE = 0.3
    PLAN_TEMPERATURE = 0.2

    # LLM HTTP retries (429 / 5xx)
    LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "6"))
    LLM_RETRY_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY", "4"))  # seconds
    LLM_RETRY_MAX_DELAY = float(os.getenv("LLM_RETRY_MAX_DELAY", "60"))
    # Pause between successive LLM calls to reduce rate-limit hits
    LLM_CALL_GAP = float(os.getenv("LLM_CALL_GAP", "1.5"))
    
    # Agent behaviour
    ENABLE_GIT = os.getenv("ENABLE_GIT", "true").lower() == "true"
    ENABLE_VALIDATION = os.getenv("ENABLE_VALIDATION", "true").lower() == "true"
    # Live server/HTTP checks. Soft-pass on infra failures (MongoDB, EPERM).
    # Set false to only run syntax checks (fastest / most reliable for demos).
    ENABLE_SERVER_VALIDATION = os.getenv("ENABLE_SERVER_VALIDATION", "true").lower() == "true"
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
    
    # Context budgets
    MAX_TREE_CHARS = 4000
    MAX_FILE_CONTEXT = 25000
    MAX_PLAN_CONTEXT = int(os.getenv("MAX_PLAN_CONTEXT", "20000"))