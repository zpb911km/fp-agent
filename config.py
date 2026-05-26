import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE_URL = os.getenv("OPENAI_API_BASE_URL")
MODEL_NAME = os.getenv("MODEL_NAME", "qwen3.5-flash")

if not OPENAI_API_KEY or not OPENAI_API_BASE_URL:
    raise ValueError("OPENAI_API_KEY and OPENAI_API_BASE_URL must be set")

# 死循环检测
MAX_ITERATIONS = 50
SIMILAR_RESPONSE_THRESHOLD = 3
MAX_CONTEXT_TOKENS = 8000

# 路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(PROJECT_ROOT, "prompts")
SKILLS_DIR = os.path.join(PROJECT_ROOT, "skills")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")

# LLM
TEMPERATURE = 0.8
MAX_TOKENS = 32768
