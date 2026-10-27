
    
  

from pathlib import Path
import json
import re
import sqlite3
from difflib import SequenceMatcher
from urllib.parse import quote_plus

from flask import Flask, jsonify, render_template, request, send_from_directory


# ---------------------------------------------------------
# APPLICATION CONFIGURATION
# ---------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "campus_chatbot.db"
DIRECTORY_PATH = BASE_DIR / "campus_directory.json"
GZU_WEBSITE = "https://www.gzu.ac.zw/"

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
    static_url_path="/static",
)


# ---------------------------------------------------------
# TEXT HELPERS
# ---------------------------------------------------------

def normalize(text):
    """Normalize text for matching without changing its meaning."""
    text = str(text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def word_set(text):
    return set(normalize(text).split())


def contains_any(text, phrases):
    """Check for whole-word or whole-phrase matches."""
    normalized = normalize(text)

    for phrase in phrases:
        phrase = normalize(phrase)
        if not phrase:
            continue

        pattern = r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"
        if re.search(pattern, normalized):
            return True

    return False


# ---------------------------------------------------------
# DATABASE ACCESS
# ---------------------------------------------------------

def get_faqs():
    """Read FAQs and keep the fullest answer for duplicate questions."""
    if not DB_PATH.exists():
        app.logger.warning("FAQ database not found at %s", DB_PATH)
        return []

    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT question, answer FROM faqs"
            ).fetchall()

        unique = {}

        for row in rows:
            question = str(row["question"] or "").strip()
            answer = str(row["answer"] or "").strip()

            if not question or not answer:
                continue

            key = normalize(question)
            existing = unique.get(key)

            if existing is None or len(answer) > len(existing["answer"]):
                unique[key] = {
                    "question": question,
                    "answer": answer,
                }

        return list(unique.values())

