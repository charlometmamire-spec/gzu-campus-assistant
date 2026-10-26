
from pathlib import Path
import json
import re
import sqlite3
from difflib import SequenceMatcher
from urllib.parse import quote_plus

from flask import Flask, jsonify, render_template, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "campus_chatbot.db"
DIRECTORY_PATH = BASE_DIR / "campus_directory.json"

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
    static_url_path="/static"
)

# --------------------------------------------------
# TEXT HELPERS
# --------------------------------------------------

def normalize(text):
    """Convert text to a consistent form for matching."""
    text = str(text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def request_quote(value):
    """Safely encode a Google Maps search query."""
    return quote_plus(str(value))


# --------------------------------------------------
# DATABASE
# --------------------------------------------------

def get_faqs():
    """Read FAQ questions and answers from SQLite."""
    if not DB_PATH.exists():
        app.logger.warning("FAQ database was not found: %s", DB_PATH)
        return []

    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT question, answer FROM faqs"
            ).fetchall()

        # Ignore empty entries and duplicate questions.
        unique_faqs = {}
        for row in rows:
            question = str(row["question"] or "").strip()
            answer = str(row["answer"] or "").strip()

            if question and answer:
                unique_faqs[normalize(question)] = {
                    "question": question,
                    "answer": answer
                }

        return list(unique_faqs.values())

    except sqlite3.Error as exc:
        app.logger.error("Could not read FAQs: %s", exc)
        return []


def log_chat(message, answer):
    """Save a conversation entry without interrupting the response."""
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO chat_logs (user_message, bot_response)
                VALUES (?, ?)
                """,
                (message, answer)
            )
    except sqlite3.Error as exc:
        app.logger.warning("Chat logging failed: %s", exc)


# --------------------------------------------------
# CAMPUS LOCATIONS
# --------------------------------------------------

def find_campus_answer(message):
    """Find a matching facility and return its map search link."""
    if not DIRECTORY_PATH.exists():
        return None

    text = normalize(message)

    location_words = (
        "where", "location", "locate", "find", "directions",
        "map", "how do i get", "how can i get", "take me to"
    )

    # Only process questions that appear to ask for a location.
    if not any(word in text for word in location_words):
        return None

    try:
        directory = json.loads(
            DIRECTORY_PATH.read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError) as exc:
        app.logger.warning("Could not read campus directory: %s", exc)
        return None

    matches = []

    for facility, details in directory.items():
        for alias in details.get("aliases", []):
            normalized_alias = normalize(alias)

            if not normalized_alias:
                continue

            # Match complete phrases rather than accidental substrings.
            pattern = (
                r"(?<!\w)"
                + re.escape(normalized_alias)
                + r"(?!\w)"
            )

            if re.search(pattern, text):
                matches.append(
                    (len(normalized_alias), facility, alias, details)
                )

    if not matches:
        return None

    # Prefer the longest, most specific facility name.
    matches.sort(key=lambda item: item[0], reverse=True)

    _, facility, alias, details = matches[0]

    # These facilities belong to Mashava Campus in this directory.
    mashava_facilities = {
        "tlc",
        "mashava campus library",
        "student village",
        "medical centre",
        "dining hall",
        "chemberi hall",
        "clubhouse"
    }

    if facility.lower() in mashava_facilities:
        campus_name = "Mashava Campus"
    else:
        # For other entries, use a parent campus if supplied.
        campus_name = details.get("campus", facility)

    map_query = details.get("map_query", facility)
    map_url = (
        "https://www.google.com/maps/search/?api=1&query="
        + request_quote(map_query)
    )

    description = details.get(
        "description",
        "Please confirm the exact location with university staff."
    )

    if details.get("verified", False):
        status = "Location information is marked as verified in the directory."
    else:
        status = (
            "The exact facility location has not been verified. "
            "Please confirm with university staff."
        )

    return (
        f"LOCATION: {facility}\n"
        f"CAMPUS: {campus_name}\n"
        f"DETAILS: {description}\n"
        f"MAP SEARCH: {map_url}\n"
        f"STATUS: {status}"
    )


# --------------------------------------------------
# FAQ ANSWER MATCHING
# --------------------------------------------------

def find_faq_answer(message):
    """Return an answer only when an FAQ match is sufficiently relevant."""
    text = normalize(message)

    if not text:
        return "Please type a question so I can help."

    faqs = get_faqs()

    if not faqs:
        return (
            "I couldn't access the campus FAQ information right now. "
            "Please check the official GZU website: https://www.gzu.ac.zw/"
        )

    message_words = set(text.split())
    candidates = []

    # Exact match first.
    for faq in faqs:
        normalized_question = normalize(faq["question"])

        if text == normalized_question:
            return faq["answer"]

    # Then compare similar questions.
    for faq in faqs:
        normalized_question = normalize(faq["question"])
        question_words = set(normalized_question.split())

        if not question_words:
            continue

        similarity = SequenceMatcher(
            None, text, normalized_question
        ).ratio()

        overlap = (
            len(message_words & question_words)
            / max(len(message_words), 1)
        )

        # Penalise matches where the important words differ.
        score = max(similarity, overlap * 0.75)

        # A small bonus for a question whose words closely cover
        # the words used by the student.
        if message_words and message_words.issubset(question_words):
            score += 0.05

        candidates.append((score, faq))

    candidates.sort(key=lambda item: item[0], reverse=True)

    if not candidates:
        return unknown_answer()

    best_score, best_faq = candidates[0]

    # Require a stronger match than the old 0.35 threshold.
    if best_score >= 0.55:
        return best_faq["answer"]

    return unknown_answer()


def unknown_answer():
    return (
        "I couldn't find a sufficiently reliable answer in the current "
        "GZU FAQ database. Please check the official Great Zimbabwe "
        "University website at https://www.gzu.ac.zw/ or contact the "
        "relevant university office."
    )


def find_answer(message):
    """Choose the most appropriate answer source."""
    text = normalize(message)

    if not text:
        return "Please type a question so I can help."

    location_answer = find_campus_answer(message)

    if location_answer:
        return location_answer

    return find_faq_answer(message)


# --------------------------------------------------
# WEB ROUTES
# --------------------------------------------------

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "message": "GZU Campus Assistant is running"
    })


@app.route("/manifest.json")
def manifest():
    return send_from_directory(
        BASE_DIR / "static",
        "manifest.json",
        mimetype="application/manifest+json"
    )


@app.route("/service-worker.js")
def service_worker():
    response = send_from_directory(
        BASE_DIR / "static",
        "service-worker.js",
        mimetype="application/javascript"
    )
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()

    if not message:
        return jsonify({
            "response": "Please enter a question."
        }), 400

    answer = find_answer(message)
    log_chat(message, answer)

    return jsonify({"response": answer})


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )

