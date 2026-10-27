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


def is_direction_request(message):
    """Identify questions that explicitly ask for a place or directions."""
    text = normalize(message)

    if contains_any(text, DIRECTION_PHRASES):
        return True

    # Recognize requests such as "Mashava library location" and "Student Village map".
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

    Exact facility names and aliases score highest. Fuzzy matching is
    used only when the query contains enough meaningful words.
    """
    text = normalize(message)
    aliases = facility_aliases(facility, details)

    # Exact facility name or alias match.
    for alias in aliases:
        pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
        if re.search(pattern, text):
            return 1.0, alias

    # Remove common direction wording before fuzzy matching.
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

    if len(cleaned_words) == 1:
        threshold = 0.82
    else:
        threshold = 0.70

    if best_score >= threshold:
        return best_score, best_alias

    return 0.0, ""


def find_campus_answer(message):
    """Return structured location data for a recognized facility."""
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

    # Avoid silently choosing between two nearly equal fuzzy matches.
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

    map_query = str(
        details.get("map_query", facility)
    ).strip() or facility

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
    },
    "portal_create": {
        "create account", "new account", "create portal account",
        "register portal account", "student portal account",
    },
    "portal_access": {
        "student portal", "portal login", "log in to the portal",
        "login to the portal", "sign in to the portal",
        "access the portal",
    },
    "exam_timetable": {
        "exam timetable", "examination timetable", "exam schedule",
        "examination schedule", "timetable for exams",
        "timetable for examinations",
    },
    "results": {
        "exam results", "examination results", "results",
        "check results", "view results", "marks", "grades",
    },
    "admission": {
        "admission", "admissions", "apply for admission",
        "application for admission", "accept admission",
        "acceptance letter",
    },
    "registration": {
        "course registration", "semester registration",
        "register courses", "academic registration",
        "registration deadline", "registration process",
    },
    "fees": {
        "school fees", "tuition fees", "fee payment",
        "payment confirmation", "fees not showing",
        "payment not showing", "proof of payment",
    },
    "accommodation": {
        "accommodation", "student accommodation", "student housing",
        "student residence", "hostel", "student village",
        "residential accommodation",
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

    if contains_any(normalized, INTENT_GROUPS["portal_reset"]):
        return "portal_reset"

    if (
        contains_any(normalized, {"create account", "new account"})
        and contains_any(normalized, {"portal", "student"})
    ):
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


def faq_score(message, faq, user_intent):
    """Calculate relevance while penalizing intent mismatches."""
    text = normalize(message)
    question = normalize(faq["question"])

    if not text or not question:
        return 0.0

    if text == question:
        return 1.5

    message_words = word_set(text)
    question_words = word_set(question)

    stop_words = {
        "a", "an", "the", "is", "are", "was", "were", "do", "does",
        "did", "i", "me", "my", "we", "our", "you", "your", "how",
        "what", "where", "when", "why", "can", "could", "would",
        "should", "please", "to", "for", "of", "at", "in", "on",
        "and", "or", "find", "check", "get", "know", "gzu",
        "university", "student", "information", "tell", "about",
    }

    useful_message_words = message_words - stop_words
    useful_question_words = question_words - stop_words

    if not useful_message_words or not useful_question_words:
        overlap = 0.0
    else:
        overlap = (
            len(useful_message_words & useful_question_words)
            / len(useful_message_words)
        )

    similarity = SequenceMatcher(None, text, question).ratio()
    score = max(similarity, overlap * 0.85)

    candidate_intent = faq_intent(faq["question"])

    if user_intent and candidate_intent:
        if user_intent == candidate_intent:
            score += 0.18
        else:
            score -= 0.40

    distinctive_groups = {
        "exam_timetable": {"timetable", "schedule", "exam", "examination"},
        "results": {"results", "result", "grades", "marks"},
        "fees": {"fees", "fee", "payment", "receipt"},
        "accommodation": {
            "accommodation", "hostel", "housing", "residence"
        },
        "admission": {
            "admission", "application", "apply", "acceptance"
        },
        "registration": {
            "registration", "register", "enrolment", "enrollment"
        },
        "portal_reset": {"password", "reset", "forgot"},
        "portal_create": {"create", "account"},
        "portal_access": {"portal", "login", "access", "sign"},
        "library": {"library", "libraries", "books", "journals"},
        "academic_calendar": {"calendar", "semester", "dates"},
    }

    if candidate_intent in distinctive_groups:
        distinctive = distinctive_groups[candidate_intent]

        if not (message_words & distinctive):
            score -= 0.15

    return max(score, 0.0)


def find_faq_answer(message):
    """Return a relevant FAQ answer or safe fallback guidance."""
    text = normalize(message)

    if not text:
        return "Please type a question so I can help."

    faqs = get_faqs()

    if not faqs:
        return unknown_answer()

    for faq in faqs:
        if text == normalize(faq["question"]):
            return faq["answer"]

    user_intent = detect_intent(text)
    candidates = []

    for faq in faqs:
        score = faq_score(message, faq, user_intent)
        candidates.append((score, faq))

    candidates.sort(key=lambda item: item[0], reverse=True)

    if not candidates:
        return unknown_answer()

    best_score, best_faq = candidates[0]

    if user_intent and faq_intent(best_faq["question"]) == user_intent:
        threshold = 0.48
    else:
        threshold = 0.58

    if len(candidates) > 1:
        second_score, second_faq = candidates[1]

        if (
            best_score < 0.80
            and faq_intent(best_faq["question"])
            != faq_intent(second_faq["question"])
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
# CONVERSATIONAL AND GENERAL GZU QUESTIONS
# ---------------------------------------------------------

def is_greeting(message):
    text = normalize(message)

    return text in {
        "hi", "hello", "hey", "good morning", "good afternoon",
        "good evening", "how are you", "hi there", "hello there",
        "hey there", "good day"
    }


def is_off_topic(message):
    """Identify obvious unrelated requests without blocking GZU questions."""
    text = normalize(message)

    if not text:
        return False

    gzu_terms = {
        "gzu", "great zimbabwe", "university", "campus", "student",
        "students", "admission", "registration", "fees", "exam",
        "examination", "timetable", "results", "portal", "faculty",
        "faculties", "course", "courses", "library", "accommodation",
        "conduct", "services", "lecturer", "lecturers", "semester",
        "programme", "program", "degree", "contact", "location",
        "hostel"
    }

    if word_set(text) & gzu_terms:
        return False

    unrelated = {
        "football", "soccer", "recipe", "recipes", "movie", "movies",
        "celebrity", "celebrity gossip", "weather today", "write a poem",
        "love advice", "bitcoin price", "stock price"
    }

    return contains_any(text, unrelated)


def general_gzu_answer(message):
    """Handle high-value general questions before fuzzy FAQ matching."""
    text = normalize(message)

    if is_greeting(message):
        return (
            "Hello! Welcome to the GZU Campus Assistant. I'm here to help "
            "with Great Zimbabwe University information, including admissions, "
            "registration, fees, student services, campus locations and "
            "academic information. What would you like to know?"
        )

    identity_phrases = {
        "what is gzu", "what is great zimbabwe university",
        "explain gzu university", "explain great zimbabwe university",
        "tell me about gzu", "tell me about great zimbabwe university",
        "describe gzu", "what kind of university is gzu",
        "what is gzu university"
    }

    if contains_any(text, identity_phrases):
        return (
            "Great Zimbabwe University (GZU) is a public university in "
            "Zimbabwe. For current information about its academic programmes, "
            "admissions, campuses, university offices and announcements, "
            f"visit the official website: {GZU_WEBSITE}"
        )

    show_phrases = {
        "show me gzu", "show me gzu university",
        "show me great zimbabwe university",
        "i want to see gzu", "let me see gzu", "show gzu campus",
        "show me the university", "gzu pictures", "gzu photos",
        "show me images of gzu"
    }

    if contains_any(text, show_phrases):
        return (
            "You can explore Great Zimbabwe University's official website, "
            "which may include campus photographs, news and university "
            "information: " + GZU_WEBSITE + "\n\n"
            "I don't currently have a verified campus-photo gallery built "
            "into this assistant, so I won't show unverified or unrelated "
            "images. The gallery can be added later if suitable image files "
            "are available in the project's static folder."
        )

    generic_location_phrases = {
        "campus locations", "show me campus locations", "where is gzu",
        "where is gzu university", "where are gzu campuses",
        "show me the gzu campus", "campus location"
    }

    if contains_any(text, generic_location_phrases):
        return (
            "I can help locate a specific GZU building or facility. Please "
            "name the place you need, such as a library, administration "
            "office or student residence. For the official campus information "
            "currently published by GZU, visit " + GZU_WEBSITE +
            " Please confirm directions with university staff because some "
            "entries in this assistant's directory have not been verified."
        )

    service_phrases = {
        "student services", "what student services are available",
        "what services are available for students",
        "student support services", "support services at gzu",
        "how can student services help me"
    }

    if contains_any(text, service_phrases):
        return (
            "I can help you find information about student support and "
            "university services, but I don't have a verified, complete list "
            "of every service in the current FAQ database. Start with the "
            "official GZU website at " + GZU_WEBSITE +
            " or contact the relevant university office for current guidance."
        )

    conduct_phrases = {
        "conduct info", "conduct information", "student code of conduct",
        "student conduct rules", "university rules and regulations",
        "disciplinary rules", "student discipline", "code of conduct"
    }

    if contains_any(text, conduct_phrases):
        return (
            "For student conduct rules, disciplinary procedures and official "
            "university regulations, please consult the current GZU handbook "
            "or official university notices. I don't want to quote a rule "
            "that may be outdated. Start at " + GZU_WEBSITE +
            " and confirm any disciplinary matter with the appropriate GZU office."
        )

    contact_phrases = {
        "contact gzu", "gzu contact information", "how can i contact gzu",
        "university contact details", "contact the university"
    }

    if contains_any(text, contact_phrases):
        return (
            "For current official contact details, use the contact information "
            "published by Great Zimbabwe University: " + GZU_WEBSITE +
            " Please use the relevant faculty or office contact listed there."
        )

    return None


# ---------------------------------------------------------
# ANSWER ROUTING
# ---------------------------------------------------------

def find_answer(message):
    """
    Route questions in the correct order:
    1. Empty-message check.
    2. Specific campus facility matching.
    3. General GZU questions.
    4. Obvious off-topic questions.
    5. Unmatched direction requests.
    6. FAQ matching.
    """
    if not normalize(message):
        return {
            "response": "Please type a question so I can help.",
            "location": None,
        }

    # IMPORTANT:
    # Match a specific campus facility BEFORE general campus questions.
    # This prevents "Where is Mashava Campus Library?" from triggering
    # the general campus-locations response.

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

    # Handle greetings and general GZU questions only after checking
    # whether the user named a particular facility.
    curated = general_gzu_answer(message)

    if curated:
        return {
            "response": curated,
            "location": None,
        }

    if is_off_topic(message):
        return {
            "response": (
                "I'm the GZU Campus Assistant, designed to help with "
                "Great Zimbabwe University information. I can't reliably "
                "answer that unrelated question, but I can help with "
                "admissions, registration, fees, student services, campus "
                "locations, the academic calendar or examination information. "
                "You can also visit " + GZU_WEBSITE
            ),
            "location": None,
        }

    # Do not return an unrelated FAQ when a location could not be matched.
    if is_direction_request(message):
        return {
            "response": (
                "I couldn't confidently identify that campus location in "
                "the current directory. Please include the name of the "
                "building, campus or facility, or check the official GZU "
                f"website at {GZU_WEBSITE}. Some directory locations have "
                "not yet been verified, so confirm before travelling."
            ),
            "location": None,
        }

    return {
        "response": find_faq_answer(message),
        "location": None,
    }


# ---------------------------------------------------------
# FLASK ROUTES
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
