
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

    except sqlite3.Error:
        app.logger.exception("Could not read FAQs.")
        return []


def log_chat(message, answer):
    """Save a chat exchange without interrupting the response if logging fails."""
    if not DB_PATH.exists():
        app.logger.warning("Cannot log chat: database does not exist.")
        return

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


# ---------------------------------------------------------
# CAMPUS DIRECTORY AND LOCATION MATCHING
# ---------------------------------------------------------

DIRECTION_PHRASES = (
    "where is",
    "where are",
    "where can i find",
    "where do i find",
    "how do i get to",
    "how can i get to",
    "how do i reach",
    "directions to",
    "direction to",
    "take me to",
    "show me the way to",
    "location of",
    "map to",
    "find the location of",
    "how do i locate",
    "locate",
    "navigate to",
)

LOCATION_WORDS = {
    "where", "location", "directions", "direction", "map",
    "locate", "find", "reach", "navigate", "campus",
}

# Phrases that usually mean the user wants information, not a physical place.
INFORMATION_PHRASES = {
    "information", "details", "application", "apply",
    "requirements", "requirement", "process", "procedure",
    "how to apply", "how do i apply", "how can i apply",
    "booking", "book accommodation", "accommodation fees",
    "accommodation application", "accommodation information",
    "registration", "registration process", "registration deadline",
    "fees", "fee payment", "payment", "payment confirmation",
    "exam timetable", "examination timetable", "exam schedule",
    "exam results", "examination results", "academic calendar",
    "student portal", "portal password", "reset password",
    "library resources", "digital library", "books", "journals",
}


def load_directory():
    """Load the campus directory safely."""
    if not DIRECTORY_PATH.exists():
        app.logger.warning("Campus directory not found at %s", DIRECTORY_PATH)
        return {}

    try:
        data = json.loads(DIRECTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        app.logger.exception("Could not read campus directory.")
        return {}


def is_information_request(message):
    """Identify questions about processes or information rather than locations."""
    text = normalize(message)

    if contains_any(text, INFORMATION_PHRASES):
        return True

    # Generic phrases such as "where can I find information about..."
    # are requests for information, not necessarily requests for directions.
    if (
        contains_any(
            text,
            {
                "where can i find information",
                "where do i find information",
                "where can i get information",
                "where do i get information",
                "where can i find details",
                "where do i find details",
            },
        )
    ):
        return True

    return False


def is_direction_request(message):
    """Identify questions that explicitly ask for a place or directions."""
    text = normalize(message)

    # Do not route ordinary information questions to location handling.
    if is_information_request(text):
        return False

    if contains_any(text, DIRECTION_PHRASES):
        return True

    # Also recognize short requests such as "Mashava library location"
    # and "Student Village map".
    words = word_set(text)
    return bool(
        words & {"location", "directions", "direction", "map", "navigate"}
    )


def facility_aliases(facility, details):
    """Return clean aliases from the directory entry."""
    aliases = [facility]
    configured_aliases = details.get("aliases", [])

    if isinstance(configured_aliases, list):
        aliases.extend(
            alias for alias in configured_aliases
            if isinstance(alias, str)
        )

    cleaned = []
    seen = set()

    for alias in aliases:
        alias_text = normalize(alias)
        if alias_text and alias_text not in seen:
            cleaned.append(alias_text)
            seen.add(alias_text)

    return cleaned


def facility_match_score(message, facility, details):
    """
    Score a directory entry against a location question.

    Exact facility names and aliases score highest. Fuzzy matches are
    accepted only when reasonably strong.
    """
    text = normalize(message)
    aliases = facility_aliases(facility, details)

    for alias in aliases:
        pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
        if re.search(pattern, text):
            return 1.0, alias

    cleaned = text
    for phrase in sorted(DIRECTION_PHRASES, key=len, reverse=True):
        cleaned = re.sub(
            r"(?<!\w)" + re.escape(phrase) + r"(?!\w)",
            " ",
            cleaned,
        )

    cleaned_words = word_set(cleaned) - LOCATION_WORDS
    if not cleaned_words:
        return 0.0, ""

    cleaned_query = " ".join(sorted(cleaned_words))
    best_score = 0.0
    best_alias = ""

    for alias in aliases:
        alias_words = word_set(alias)
        if not alias_words:
            continue

        shared = cleaned_words & alias_words
        if not shared:
            continue

        overlap = len(shared) / max(len(alias_words), 1)
        similarity = SequenceMatcher(None, cleaned_query, alias).ratio()
        score = max(overlap * 0.92, similarity * 0.82)

        if score > best_score:
            best_score = score
            best_alias = alias

    threshold = 0.82 if len(cleaned_words) == 1 else 0.70

    if best_score >= threshold:
        return best_score, best_alias

    return 0.0, ""


def find_campus_answer(message):
    """Return structured location data only for a recognized place request."""
    if not is_direction_request(message):
        return None

    directory = load_directory()
    matches = []

    for facility, details in directory.items():
        if not isinstance(details, dict):
            continue

        score, matched_alias = facility_match_score(
            message, facility, details
        )

        if score > 0:
            matches.append(
                (score, len(matched_alias), facility, details)
            )

    if not matches:
        return None

    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_score, _, facility, details = matches[0]

    if len(matches) > 1:
        second_score, _, second_facility, _ = matches[1]
        if (
            best_score < 1.0
            and second_facility != facility
            and abs(best_score - second_score) < 0.06
        ):
            return None

    campus_name = str(
        details.get("campus", "Great Zimbabwe University")
    ).strip()

    description = str(
        details.get(
            "description",
            "Please confirm the exact location with university staff.",
        )
    ).strip()

    verified = bool(details.get("verified", False))
    map_query = str(details.get("map_query", facility)).strip() or facility

    map_url = (
        "https://www.google.com/maps/search/?api=1&query="
        + quote_plus(map_query)
    )

    return {
        "name": facility,
        "campus": campus_name,
        "description": description,
        "map_query": map_query,
        "map_url": map_url,
        "verified": verified,
    }


# ---------------------------------------------------------
# FAQ INTENT DETECTION
# ---------------------------------------------------------

INTENT_GROUPS = {
    "portal_reset": {
        "reset password", "forgot password", "forgotten password",
        "change password", "password reset", "cannot log in",
        "can't log in", "cannot login", "can't login",
        "forgot my password", "reset my password",
    },
    "portal_create": {
        "create account", "new account", "create portal account",
        "register portal account", "student portal account",
        "create a student account", "create a portal account",
    },
    "portal_access": {
        "student portal", "portal login", "log in to the portal",
        "login to the portal", "sign in to the portal",
        "access the portal", "portal access",
    },
    "exam_timetable": {
        "exam timetable", "examination timetable", "exam schedule",
        "examination schedule", "timetable for exams",
        "timetable for examinations", "exam dates", "examination dates",
        "when are my exams", "when do exams start",
    },
    "results": {
        "exam results", "examination results", "results",
        "check results", "view results", "marks", "grades",
        "semester results", "academic results",
    },
    "admission": {
        "admission", "admissions", "apply for admission",
        "application for admission", "accept admission",
        "acceptance letter", "admission application",
    },
    "registration": {
        "course registration", "semester registration",
        "register courses", "academic registration",
        "registration deadline", "registration process",
        "register for courses", "module registration",
    },
    "fees": {
        "school fees", "tuition fees", "fee payment",
        "payment confirmation", "fees not showing",
        "payment not showing", "proof of payment",
        "pay fees", "fee structure",
    },
    "accommodation": {
        "accommodation", "student accommodation", "student housing",
        "student residence", "hostel", "student village",
        "residential accommodation", "accommodation application",
    },
    "library": {
        "library", "libraries", "library resources",
        "books", "journals", "digital library",
    },
    "academic_calendar": {
        "academic calendar", "university calendar",
        "semester dates", "term dates",
    },
}


def detect_intent(text):
    """Identify a specific FAQ intent, preferring more specific phrases."""
    normalized = normalize(text)

    # Handle specific portal tasks before general portal questions.
    if contains_any(normalized, INTENT_GROUPS["portal_reset"]):
        return "portal_reset"

    if contains_any(normalized, INTENT_GROUPS["portal_create"]):
        return "portal_create"

    if contains_any(normalized, INTENT_GROUPS["exam_timetable"]):
        return "exam_timetable"

    if contains_any(normalized, INTENT_GROUPS["academic_calendar"]):
        return "academic_calendar"

    if contains_any(normalized, INTENT_GROUPS["admission"]):
        return "admission"

    if contains_any(normalized, INTENT_GROUPS["registration"]):
        return "registration"

    if contains_any(normalized, INTENT_GROUPS["fees"]):
        return "fees"

    if contains_any(normalized, INTENT_GROUPS["accommodation"]):
        return "accommodation"

    if contains_any(normalized, INTENT_GROUPS["library"]):
        return "library"

    if contains_any(normalized, INTENT_GROUPS["results"]):
        return "results"

    if contains_any(normalized, INTENT_GROUPS["portal_access"]):
        return "portal_access"

    return None


def faq_intent(question):
    return detect_intent(question)


STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "do", "does",
    "did", "i", "me", "my", "we", "our", "you", "your", "how",
    "what", "where", "when", "why", "can", "could", "would",
    "should", "please", "to", "for", "of", "at", "in", "on",
    "and", "or", "find", "check", "get", "know", "gzu",
    "university", "student", "information", "tell", "about",
    "access", "view", "see", "want", "need", "help", "with",
    "me", "myself", "am", "it", "this", "that", "from",
}


DISTINCTIVE_GROUPS = {
    "exam_timetable": {
        "timetable", "schedule", "exam", "examination", "dates",
    },
    "results": {
        "results", "result", "grades", "marks",
    },
    "fees": {
        "fees", "fee", "payment", "receipt", "tuition",
    },
    "accommodation": {
        "accommodation", "hostel", "housing", "residence",
        "village",
    },
    "admission": {
        "admission", "application", "apply", "acceptance",
    },
    "registration": {
        "registration", "register", "enrolment", "enrollment",
        "courses", "modules",
    },
    "portal_reset": {
        "password", "reset", "forgot", "login", "log",
    },
    "portal_create": {
        "create", "account", "register",
    },
    "portal_access": {
        "portal", "login", "access", "sign",
    },
    "library": {
        "library", "libraries", "books", "journals",
    },
    "academic_calendar": {
        "calendar", "semester", "dates", "term",
    },
}


def faq_score(message, faq, user_intent):
    """Score FAQ relevance while strongly preferring the same topic."""
    text = normalize(message)
    question = normalize(faq["question"])

    if not text or not question:
        return 0.0

    if text == question:
        return 2.0

    message_words = word_set(text) - STOP_WORDS
    question_words = word_set(question) - STOP_WORDS

    if not message_words or not question_words:
        overlap = 0.0
        question_coverage = 0.0
    else:
        # How much of the user's meaningful query is represented?
        overlap = (
            len(message_words & question_words)
            / len(message_words)
        )

        # How much of the FAQ's meaningful wording is represented?
        question_coverage = (
            len(message_words & question_words)
            / len(question_words)
        )

    similarity = SequenceMatcher(None, text, question).ratio()

    # Token overlap is more useful than sentence structure alone.
    score = max(
        similarity * 0.75,
        overlap * 0.85,
        question_coverage * 0.65,
    )

    candidate_intent = faq_intent(faq["question"])

    if user_intent:
        if candidate_intent == user_intent:
            score += 0.25
        elif candidate_intent is not None:
            score -= 0.45

    if candidate_intent in DISTINCTIVE_GROUPS:
        distinctive = DISTINCTIVE_GROUPS[candidate_intent]
        if not (word_set(text) & distinctive):
            score -= 0.18

    return max(score, 0.0)


def find_faq_answer(message):
    """Return a relevant FAQ answer or safe fallback guidance."""
    text = normalize(message)

    if not text:
        return "Please type a question so I can help."

    faqs = get_faqs()

    if not faqs:
        return unknown_answer()

    # Exact question match first.
    for faq in faqs:
        if text == normalize(faq["question"]):
            return faq["answer"]

    user_intent = detect_intent(text)

    # If this is a recognized topic and the database contains FAQs
    # for that topic, compare only those FAQs. This reduces the chance
    # of returning a similar-sounding answer from a different topic.
    if user_intent:
        same_intent_faqs = [
            faq for faq in faqs
            if faq_intent(faq["question"]) == user_intent
        ]

        if same_intent_faqs:
            faqs_to_score = same_intent_faqs
        else:
            faqs_to_score = faqs
    else:
        faqs_to_score = faqs

    candidates = [
        (faq_score(message, faq, user_intent), faq)
        for faq in faqs_to_score
    ]
    candidates.sort(key=lambda item: item[0], reverse=True)

    if not candidates:
        return unknown_answer()

    best_score, best_faq = candidates[0]
    best_intent = faq_intent(best_faq["question"])

    if user_intent and best_intent == user_intent:
        # A recognized topic can use a lower threshold because candidates
        # have already been restricted to the same FAQ topic when possible.
        threshold = 0.38
    else:
        threshold = 0.58

    # Avoid returning an uncertain answer when two different topics
    # are effectively tied.
    if len(candidates) > 1:
        second_score, second_faq = candidates[1]
        second_intent = faq_intent(second_faq["question"])

        if (
            best_score < 0.75
            and best_intent != second_intent
            and abs(best_score - second_score) < 0.07
        ):
            return unknown_answer()

    if best_score >= threshold:
        return best_faq["answer"]

    return unknown_answer()


def unknown_answer():
    return (
        "I couldn't find a sufficiently reliable answer in the current "
        "GZU FAQ database. Please check the official Great Zimbabwe "
        "University website at "
        f"{GZU_WEBSITE} "
        "or contact the relevant university office."
    )


# ---------------------------------------------------------
# ANSWER ROUTING
# ---------------------------------------------------------

def find_answer(message):
    """Return response text and optional structured location details."""
    if not normalize(message):
        return {
            "response": "Please type a question so I can help.",
            "location": None,
        }

    # First, handle questions clearly asking for information about
    # a known FAQ topic. For example, "Where can I find accommodation
    # information?" should not automatically become a Student Village card.
    if is_information_request(message):
        return {
            "response": find_faq_answer(message),
            "location": None,
        }

    # Physical directions can return a structured location card.
    location = find_campus_answer(message)

    if location:
        response = (
            f"{location['name']} — {location['campus']}. "
            f"{location['description']}"
        )

        if not location["verified"]:
            response += (
                " The exact location has not been verified. Please confirm "
                "it with university staff before travelling."
            )

        return {
            "response": response,
            "location": location,
        }

    # If the user clearly asks for directions but no known facility
    # could be matched, do not return an unrelated FAQ.
    if is_direction_request(message):
        return {
            "response": (
                "I couldn't confidently identify that campus location in "
                "the current directory. Please check the official GZU "
                f"website at {GZU_WEBSITE} or confirm the facility name "
                "with university staff."
            ),
            "location": None,
        }

    return {
        "response": find_faq_answer(message),
        "location": None,
    }


# ---------------------------------------------------------
# ROUTES
# ---------------------------------------------------------

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
        return jsonify({
            "response": "Please enter a question.",
            "location": None,
        }), 400

    result = find_answer(message)
    log_chat(message, result["response"])

    return jsonify(result)


# ---------------------------------------------------------
# LOCAL DEVELOPMENT ENTRY POINT
# ---------------------------------------------------------

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
    )
