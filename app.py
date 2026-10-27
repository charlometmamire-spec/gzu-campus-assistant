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

    # Also recognize short requests such as "Mashava library location"
    # and "Student Village map".
    words = word_set(text)
    return bool(words & {"location", "directions", "direction", "map", "navigate"})


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

    Exact facility names and aliases score highest. A fuzzy match is
    accepted only when it is reasonably strong and the query has enough
    meaningful words to avoid matching unrelated places.
    """
    text = normalize(message)
    aliases = facility_aliases(facility, details)

    for alias in aliases:
        pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
        if re.search(pattern, text):
            return 1.0, alias

    # Remove common direction wording before trying a fuzzy match.
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

        # A meaningful alias must have at least one shared word.
        shared = cleaned_words & alias_words
        if not shared:
            continue

        overlap = len(shared) / max(len(alias_words), 1)
        similarity = SequenceMatcher(None, cleaned_query, alias).ratio()

        # Prefer word overlap for multi-word names and fuzzy similarity
        # only when the query looks like a genuine name variation.
        score = max(overlap * 0.92, similarity * 0.82)

        if score > best_score:
            best_score = score
            best_alias = alias

    # Avoid weak matches, especially on broad aliases like "campus".
    if len(cleaned_words) == 1:
        threshold = 0.82
    else:
        threshold = 0.70

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
            # Exact matches win; longer names break close ties.
            matches.append(
                (score, len(matched_alias), facility, details)
            )

    if not matches:
        return None

    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_score, _, facility, details = matches[0]

    # If two different facilities have almost identical match scores,
    # do not silently choose one.
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

    # A configured map_query may be more specific than the display name.
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

    # Portal tasks must be distinguished before general portal questions.
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
    """Calculate a relevance score with penalties for intent mismatches."""
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
            # A strong penalty prevents timetable, fees, portal and
            # accommodation questions from borrowing each other's answers.
            score -= 0.40

    # A question with a distinctive keyword absent from the user's query
    # should not win just because the sentence structure is similar.
    distinctive_groups = {
        "exam_timetable": {"timetable", "schedule", "exam", "examination"},
        "results": {"results", "result", "grades", "marks"},
        "fees": {"fees", "fee", "payment", "receipt"},
        "accommodation": {"accommodation", "hostel", "housing", "residence"},
        "admission": {"admission", "application", "apply", "acceptance"},
        "registration": {"registration", "register", "enrolment", "enrollment"},
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

    # Exact question match first.
    for faq in faqs:
        if text == normalize(faq["question"]):
            return faq["answer"]

    user_intent = detect_intent(text)

    # Score all candidates.
    candidates = []

    for faq in faqs:
        score = faq_score(message, faq, user_intent)
        candidates.append((score, faq))

    candidates.sort(key=lambda item: item[0], reverse=True)

    if not candidates:
        return unknown_answer()

    best_score, best_faq = candidates[0]

    # Intent-specific matches may use a slightly lower threshold than
    # general fuzzy matches because their topic has already been identified.
    if user_intent and faq_intent(best_faq["question"]) == user_intent:
        threshold = 0.48
    else:
        threshold = 0.58

    # Require a clear lead over the next candidate when the scores are close.
    if len(candidates) > 1:
        second_score, second_faq = candidates[1]

        if (
            best_score < 0.80
            and faq_intent(best_faq["question"]) != faq_intent(second_faq["question"])
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

def directory_choices():
    """Return the names in the supplied directory without claiming verification."""
    directory = load_directory()
    return [
        {"name": str(name), "verified": bool(details.get("verified", False))}
        for name, details in directory.items()
        if isinstance(details, dict)
    ]


def general_gzu_answer(message):
    """Handle the main navigation topics with useful, cautious guidance."""
    text = normalize(message)
    website = GZU_WEBSITE

    # The location topic should offer real directory choices first.
    if (
        contains_any(text, {"campus locations", "campus location", "campus libraries", "campus library", "explore locations", "campus directory"})
        or text in {"where can i find the gzu campus libraries", "where can i find the campus libraries"}
    ):
        names = [item["name"] for item in directory_choices()]
        if names:
            listing = "\n".join(f"• {name}" for name in names)
            return {
                "response": (
                    "Which campus facility would you like to find? These are the places currently listed in the chatbot directory:\n\n"
                    + listing
                    + "\n\nType a place name (for example, ‘Mashava Campus Library’) and I’ll show its map search. The directory currently marks these locations as unverified, so please confirm the exact location with GZU staff before travelling."
                ),
                "location": None,
                "choices": names,
            }

    if contains_any(text, {"what student services", "student services", "student support", "services available"}):
        return {"response": (
            "GZU student services can include academic and administrative support, library and learning resources, accommodation information, and student welfare or medical support. The exact services and arrangements may differ by campus.\n\n"
            "To get help:\n1. Identify the service you need and your campus.\n2. Check the official GZU website for current contacts and announcements.\n3. If it concerns your personal student account or record, contact the responsible university office directly.\n\nOfficial website: " + website
        ), "location": None}

    if contains_any(text, {"conduct rules", "student conduct", "conduct information", "disciplinary rules", "student code of conduct"}):
        return {"response": (
            "For student conduct, use the current GZU regulations or student handbook as the authoritative source; I do not have a verified copy of every current rule in this chatbot.\n\n"
            "Suggested steps:\n1. Check the official university website for current student regulations or handbook documents.\n2. Read the section relevant to your situation.\n3. If the rule or procedure is unclear, ask your faculty office or the responsible student-affairs/administrative office before acting.\n\nOfficial website: " + website
        ), "location": None}

    if contains_any(text, {"what is great zimbabwe university", "about gzu", "tell me about gzu", "about great zimbabwe university", "show me gzu university", "explore gzu", "explore great zimbabwe university"}):
        return {"response": (
            "Great Zimbabwe University (GZU) is a public university in Zimbabwe. For the university’s current faculties, programmes, campus information, notices and official announcements, use the university’s official website.\n\n"
            "Explore GZU: " + website + "\n\nIf you tell me which programme, campus or service you want to explore, I can help you narrow down what to look for."
        ), "location": None}

    if contains_any(text, {"how can i contact gzu", "contact information", "contact gzu", "gzu contact", "university phone number", "university email"}):
        return {"response": (
            "For current GZU telephone numbers, email addresses and office contacts, use the official university contact information so you do not rely on an outdated number.\n\n"
            "1. Open the official website.\n2. Look for the Contact Us or relevant faculty/office page.\n3. Contact the office responsible for your question and include your student number only when communicating through an official, trusted channel.\n\nOfficial website: " + website
        ), "location": None}

    return None


def is_clearly_off_topic(message):
    """Avoid pretending to be a general-purpose assistant for unrelated topics."""
    text = normalize(message)
    if not text or contains_any(text, {"gzu", "great zimbabwe", "university", "campus", "student", "admission", "registration", "portal", "fees", "library", "timetable", "results", "accommodation", "course", "lecturer", "faculty", "exam"}):
        return False
    unrelated = {
        "weather", "football", "soccer", "recipe", "recipes", "horoscope",
        "stock market", "bitcoin", "celebrity gossip", "write me a poem about mars",
        "where is mars", "capital of france", "solve this unrelated puzzle",
    }
    return contains_any(text, unrelated)


def find_answer(message):
    """Route a question to a relevant topic, known location, FAQ, or honest fallback."""
    if not normalize(message):
        return {"response": "Please type a question so I can help.", "location": None}

    # Handle the dedicated navigation topics before generic FAQ matching.
    curated = general_gzu_answer(message)
    if curated:
        return curated

    # Show a location card only when a named directory place is matched.
    location = find_campus_answer(message)
    if location:
        response = f"{location['name']} — {location['campus']}. {location['description']}"
        if not location["verified"]:
            response += " The exact location has not been verified. Please confirm it with university staff before travelling."
        return {"response": response, "location": location}

    if is_direction_request(message):
        names = [item["name"] for item in directory_choices()]
        return {
            "response": (
                "I couldn't confidently match that place to the current directory. Try one of these listed names: "
                + ", ".join(names)
                + f". The entries are not verified; confirm exact locations with university staff. Official website: {GZU_WEBSITE}"
            ),
            "location": None,
            "choices": names,
        }

    if is_clearly_off_topic(message):
        return {"response": (
            "I’m the GZU Campus Assistant, so I’m designed to help with Great Zimbabwe University information, campus facilities, student processes and university services. "
            "Please ask a GZU-related question and I’ll do my best to help."
        ), "location": None}

    answer = find_faq_answer(message)
    # For an unverified/unmatched GZU question, explicitly say what is missing
    # and give a practical next step rather than presenting a guess as fact.
    return {"response": answer, "location": None}


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
