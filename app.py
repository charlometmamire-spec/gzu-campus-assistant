
from pathlib import Path
import json
import re
import sqlite3
from difflib import SequenceMatcher
from urllib.parse import quote_plus

from flask import (
    Flask, jsonify, render_template, request, send_from_directory
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "campus_chatbot.db"
DIRECTORY_PATH = BASE_DIR / "campus_directory.json"

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
    static_url_path="/static",
)


def normalize(text):
    text = str(text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def get_faqs():
    """Read FAQs and avoid letting a duplicate overwrite a fuller answer."""
    if not DB_PATH.exists():
        app.logger.warning("FAQ database not found.")
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

    except sqlite3.Error:
        app.logger.exception("Could not read FAQs.")
        return []


def log_chat(message, answer):
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO chat_logs (user_message, bot_response)
                VALUES (?, ?)
                """,
                (message, answer),
            )
    except sqlite3.Error:
        app.logger.exception("Could not save chat log.")


def load_directory():
    if not DIRECTORY_PATH.exists():
        return {}

    try:
        data = json.loads(DIRECTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        app.logger.exception("Could not read campus directory.")
        return {}


def find_campus_answer(message):
    """Return structured location data only for clear direction requests."""
    text = normalize(message)

    direction_phrases = (
        "where is", "where can i find", "directions to",
        "how do i get to", "how can i get to",
        "take me to", "locate", "location of",
        "show me the way to", "map to",
    )

    if not any(phrase in text for phrase in direction_phrases):
        return None

    directory = load_directory()
    matches = []

    for facility, details in directory.items():
        if not isinstance(details, dict):
            continue

        aliases = [facility] + details.get("aliases", [])

        for alias in aliases:
            alias_text = normalize(alias)
            if not alias_text:
                continue

            pattern = r"(?<!\w)" + re.escape(alias_text) + r"(?!\w)"

            if re.search(pattern, text):
                matches.append((len(alias_text), facility, details))
                break

    if not matches:
        return None

    # Prefer the longest matching facility name or alias.
    matches.sort(key=lambda item: item[0], reverse=True)
    _, facility, details = matches[0]

    campus_name = details.get("campus", "Great Zimbabwe University")
    description = details.get(
        "description",
        "Please confirm the exact location with university staff.",
    )
    map_query = details.get("map_query", facility)

    map_url = (
        "https://www.google.com/maps/search/?api=1&query="
        + quote_plus(str(map_query))
    )

    return {
        "name": facility,
        "campus": campus_name,
        "description": description,
        "map_url": map_url,
        "verified": bool(details.get("verified", False)),
    }


def unknown_answer():
    return (
        "I couldn't find a sufficiently reliable answer in the current "
        "GZU FAQ database. Please check the official Great Zimbabwe "
        "University website at https://www.gzu.ac.zw/ or contact the "
        "relevant university office."
    )


# Terms that identify different question types.
INTENT_TERMS = {
    "admission": {
        "admission", "apply", "application", "accepted", "acceptance",
        "admissions", "entry",
    },
    "registration": {
        "register", "registration", "semester", "course", "courses",
        "enrol", "enrollment", "enrolment",
    },
    "portal": {
        "portal", "login", "log", "password", "account", "access",
        "reset", "forgot",
    },
    "results": {
        "result", "results", "grade", "grades", "marks",
    },
    "timetable": {
        "timetable", "schedule", "exam", "examination", "exams",
    },
    "fees": {
        "fee", "fees", "payment", "paid", "pay", "receipt",
    },
    "accommodation": {
        "accommodation", "hostel", "housing", "residence", "lodging",
    },
    "library": {
        "library", "resources", "books", "journals",
    },
}


def detect_intent(text):
    words = set(normalize(text).split())

    # More specific phrases take priority over broad shared words.
    if words & {"password", "forgot", "reset"} and words & {"portal", "account", "login", "password"}:
        return "portal_reset"

    if words & {"create", "new"} and words & {"account", "portal"}:
        return "portal_create"

    if words & {"access", "login", "log", "sign"} and words & {"portal", "account"}:
        return "portal_access"

    if words & {"timetable", "schedule"} and words & {"exam", "examination", "exams"}:
        return "exam_timetable"

    if words & {"result", "results", "grade", "grades", "marks"}:
        return "results"

    if words & {"admission", "admissions", "apply", "application", "accepted"}:
        return "admission"

    if words & {"register", "registration", "semester", "course", "courses", "enrol", "enrollment", "enrolment"}:
        return "registration"

    if words & {"fee", "fees", "payment", "paid", "pay", "receipt"}:
        return "fees"

    if words & {"accommodation", "hostel", "housing", "residence", "lodging"}:
        return "accommodation"

    if words & {"library", "resources", "books", "journals"}:
        return "library"

    return None


def faq_intent(question):
    text = normalize(question)
    return detect_intent(text)


def find_faq_answer(message):
    text = normalize(message)

    if not text:
        return unknown_answer()

    faqs = get_faqs()

    if not faqs:
        return unknown_answer()

    # First, accept an exact question match.
    for faq in faqs:
        if text == normalize(faq["question"]):
            return faq["answer"]

    user_intent = detect_intent(text)

    words = set(text.split())
    stop_words = {
        "a", "an", "the", "is", "are", "was", "were", "do", "does",
        "did", "i", "me", "my", "we", "our", "you", "your", "how",
        "what", "where", "when", "can", "could", "would", "should",
        "please", "to", "for", "of", "at", "in", "on", "and", "or",
        "find", "check", "get", "know", "gzu", "university",
    }
    useful_words = words - stop_words

    candidates = []

    for faq in faqs:
        question = normalize(faq["question"])
        question_words = set(question.split())
        useful_question_words = question_words - stop_words

        if not useful_question_words:
            continue

        similarity = SequenceMatcher(None, text, question).ratio()

        overlap = (
            len(useful_words & useful_question_words)
            / max(len(useful_words), 1)
        )

        score = max(similarity, overlap * 0.85)

        # Do not match an unrelated question just because it shares
        # a broad word such as "how", "check", or "student".
        candidate_intent = faq_intent(question)

        if user_intent and candidate_intent:
            if user_intent != candidate_intent:
                score -= 0.35
            else:
                score += 0.15

        candidates.append((score, faq))

    if not candidates:
        return unknown_answer()

    candidates.sort(key=lambda item: item[0], reverse=True)

    best_score, best_faq = candidates[0]

    # Require a reasonable match rather than confidently returning
    # an unrelated FAQ.
    if best_score >= 0.55:
        return best_faq["answer"]

    return unknown_answer()


def find_answer(message):
    if not normalize(message):
        return {
            "response": "Please type a question so I can help.",
            "location": None,
        }

    location = find_campus_answer(message)

    if location:
        response = (
            f"{location['name']} — {location['campus']}. "
            f"{location['description']}"
        )

        if not location["verified"]:
            response += (
                " The exact location has not been verified, so please "
                "confirm it with university staff before travelling."
            )

        return {
            "response": response,
            "location": location,
        }

    return {
        "response": find_faq_answer(message),
        "location": None,
    }


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "message": "GZU Campus Assistant is running",
    })


@app.route("/manifest.json")
def manifest():
    return send_from_directory(
        BASE_DIR / "static",
        "manifest.json",
        mimetype="application/manifest+json",
    )


@app.route("/service-worker.js")
def service_worker():
    response = send_from_directory(
        BASE_DIR / "static",
        "service-worker.js",
        mimetype="application/javascript",
    )
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()

    if not message:
        return jsonify({"response": "Please enter a question."}), 400

    result = find_answer(message)
    log_chat(message, result["response"])

    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
