
from pathlib import Path
import json
import re
import sqlite3
from difflib import SequenceMatcher

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

def normalize(text):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()

def get_faqs():
    if not DB_PATH.exists():
        return []
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT question, answer FROM faqs"
        ).fetchall()
    return [dict(row) for row in rows]

def find_campus_answer(message):
    if not DIRECTORY_PATH.exists():
        return None

    text = normalize(message)
    location_words = (
        "where", "location", "locate", "find", "directions",
        "map", "how do i get", "how can i get"
    )
    if not any(word in text for word in location_words):
        return None

    directory = json.loads(DIRECTORY_PATH.read_text(encoding="utf-8"))
    for campus, details in directory.items():
        for alias in details.get("aliases", []):
            if normalize(alias) in text:
                map_url = (
                    "https://www.google.com/maps/search/?api=1&query="
                    + request_quote(details.get("map_query", campus))
                )
                return (
                    f"LOCATION: {alias.title()}\\n"
                    f"CAMPUS: {campus}\\n"
                    f"DETAILS: {details['description']}\\n"
                    f"MAP SEARCH: {map_url}\\n"
                    "STATUS: Exact facility location may need confirmation."
                )
    return None

def request_quote(value):
    from urllib.parse import quote_plus
    return quote_plus(value)

def find_answer(message):
    text = normalize(message)
    if not text:
        return "Please type a question so I can help."

    campus_answer = find_campus_answer(message)
    if campus_answer:
        return campus_answer

    faqs = get_faqs()
    best_answer = None
    best_score = 0

    for faq in faqs:
        question = faq.get("question", "")
        answer = faq.get("answer", "")
        normalized_question = normalize(question)

        if text == normalized_question:
            return answer

        similarity = SequenceMatcher(None, text, normalized_question).ratio()
        message_words = set(text.split())
        question_words = set(normalized_question.split())
        overlap = len(message_words & question_words) / max(len(message_words), 1)
        score = max(similarity, overlap * 0.8)

        if score > best_score:
            best_score = score
            best_answer = answer

    if best_answer and best_score >= 0.35:
        return best_answer

    return (
        "I couldn't find a reliable answer to that question in my current "
        "campus FAQ database. Please check the official Great Zimbabwe "
        "University website at https://www.gzu.ac.zw/ or contact the "
        "relevant university office."
    )

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "message": "GZU Campus Assistant is running"})

@app.route("/manifest.json")
def manifest():
    return send_from_directory(
        BASE_DIR / "static", "manifest.json",
        mimetype="application/manifest+json"
    )

@app.route("/service-worker.js")
def service_worker():
    response = send_from_directory(
        BASE_DIR / "static", "service-worker.js",
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
        return jsonify({"response": "Please enter a question."}), 400

    answer = find_answer(message)

    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO chat_logs (user_message, bot_response) VALUES (?, ?)",
                (message, answer)
            )
    except sqlite3.Error as exc:
        app.logger.warning("Chat logging failed: %s", exc)

    return jsonify({"response": answer})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
