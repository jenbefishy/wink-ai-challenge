import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    JWT_SECRET = os.getenv("JWT_SECRET", "NO_KEY")
    PROCESSING_APP_URL = os.getenv("PROCESSING_APP_URL", "http://localhost:5000")
    PORT = int(os.getenv("PORT", 8123))
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
