import os
import re
import io
import json
import base64
import asyncio
import logging
import tempfile
import subprocess
import sys
from pathlib import Path
from typing import Optional, AsyncGenerator
from contextlib import asynccontextmanager

import litellm
import psycopg2
from psycopg2.extras import RealDictCursor

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Optional document and search libraries
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from docx import Document
except ImportError:
    Document = None

try:
    from openpyxl import load_workbook
except ImportError:
    load_workbook = None

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None


# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

PORT = int(os.getenv("PORT", "8000"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("super-agent")

litellm.drop_params = True
litellm.set_verbose = False

MODEL_CHAIN = [
    {
        "model": "gemini/gemini-3.8-flash",
        "api_key": GEMINI_API_KEY,
        "api_base": None,
    },
    {
        "model": "openrouter/openrouter/free",
        "api_key": OPENROUTER_API_KEY,
        "api_base": "https://openrouter.ai/api/v1",
    },
    {
        "model": "openrouter/google/gemma-3-27b-it:free",
        "api_key": OPENROUTER_API_KEY,
        "api_base": "https://openrouter.ai/api/v1",
    },
]

SYSTEM_PROMPT = """
Sen Super-Agent nomli aqlli, foydali va samimiy sun'iy intellekt yordamchisisan.

Foydalanuvchi bilan tabiiy, ravon va do'stona o'zbek tilida gaplash.
Foydalanuvchi boshqa tilda yozsa, o'sha tilda javob ber.
Javoblaringni tushunarli, aniq va foydali qil.
Kerak bo'lganda Markdown, sarlavhalar, qalin yozuv va ro'yxatlardan foydalan.
Savolga to'g'ridan-to'g'ri javob ber.
Bilmagan ma'lumotingni to'qib chiqarma.
Rasm noaniq yoki xira bo'lsa, ko'rinmayotgan tafsilotlarni taxmin qilma.
Kod so'ralganda ishlaydigan, to'liq kod yoz.
Hisob-kitoblarda natijani tekshir.
Foydalanuvchiga hurmat bilan va tabiiy ohangda murojaat qil.
"""


# ---------------------------------------------------------
# DATABASE
# ---------------------------------------------------------

def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL sozlanmagan.")
    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=8,
        cursor_factory=RealDictCursor,
        sslmode=os.getenv("DB_SSLMODE", "prefer"),
    )


def initialize_database():
    if not DATABASE_URL:
        logger.warning("DATABASE_URL mavjud emas. Xotira bazasi o'chirilgan.")
        return
