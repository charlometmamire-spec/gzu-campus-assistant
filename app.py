from pathlib import Path
import json
import re
import sqlite3
import time
import os
import urllib.request
import urllib.error
from html.parser import HTMLParser
from difflib import SequenceMatcher
from urllib.parse import quote_plus

from flask import Flask, jsonify, render_template, request, send_from_directory

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
# TEXT MATCHING HELPERS
# ---------------------------------------------------------

def normalize(text):
    text = str(text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def word_set(text):
    return set(normalize(text).split())


def contains_any(text, phrases):
    normalized = normalize(text)
    for phrase in phrases:
        phrase = normalize(phrase)
        if phrase and re.search(
            r"(?<!\w)" + re.escape(phrase) + r"(?!\w)",
            normalized,
        ):
            return True
    return False


# ---------------------------------------------------------
# EXISTING FAQ DATABASE
# ---------------------------------------------------------

def get_faqs():
    """Read the existing FAQs without modifying the database."""
    if not DB_PATH.exists():
        app.logger.warning("FAQ database not found: %s", DB_PATH)
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
            previous = unique.get(key)

            if previous is None or len(answer) > len(previous["answer"]):
                unique[key] = {
                    "question": question,
                    "answer": answer,
                }

        return list(unique.values())

    except sqlite3.Error:
        app.logger.exception("Could not read the FAQ database.")
        return []


def log_chat(message, answer):
    """Log a conversation if the existing chat_logs table is available."""
    if not DB_PATH.exists():
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
        # Logging must never stop the chatbot from answering.
        app.logger.exception("Could not save chat log.")


# ---------------------------------------------------------
# CAMPUS DIRECTORY AND MAP MATCHING
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
    "where", "location", "directions", "direction",
    "map", "locate", "find", "reach", "navigate", "campus",
}


def load_directory():
    """Read the existing campus_directory.json without changing it."""
    if not DIRECTORY_PATH.exists():
        app.logger.warning("Campus directory not found: %s", DIRECTORY_PATH)
        return {}

    try:
        data = json.loads(DIRECTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        app.logger.exception("Could not read campus directory.")
        return {}


def directory_choices():
    directory = load_directory()
    choices = []

    for name, details in directory.items():
        if isinstance(details, dict):
            choices.append({
                "name": str(name),
                "verified": bool(details.get("verified", False)),
            })

    return choices


def is_direction_request(message):
    text = normalize(message)

    if contains_any(text, DIRECTION_PHRASES):
        return True

    return bool(
        word_set(text)
        & {"location", "directions", "direction", "map", "navigate"}
    )


def facility_aliases(facility, details):
    aliases = [facility]
    configured = details.get("aliases", [])

    if isinstance(configured, list):
        aliases.extend(item for item in configured if isinstance(item, str))

    cleaned = []
    seen = set()

    for alias in aliases:
        value = normalize(alias)
        if value and value not in seen:
            cleaned.append(value)
            seen.add(value)

    return cleaned


def facility_match_score(message, facility, details):
    text = normalize(message)
    aliases = facility_aliases(facility, details)

    # Exact names and aliases always win.
    for alias in aliases:
        if re.search(
            r"(?<!\w)" + re.escape(alias) + r"(?!\w)",
            text,
        ):
            return 1.0, alias

    # Remove direction wording before attempting a cautious fuzzy match.
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
    """
    Return a location card for a named directory entry.

    A student may enter just 'TLC', or ask 'Where is TLC?'.
    General questions are not treated as place searches unless a directory
    name or alias is actually matched.
    """
    directory = load_directory()
    normalized_message = normalize(message)
    direction_request = is_direction_request(message)

    # Allow a name-only search when it exactly matches a directory entry
    # or one of that entry's aliases.
    if not direction_request:
        exact_match = any(
            normalized_message in facility_aliases(name, details)
            for name, details in directory.items()
            if isinstance(details, dict)
        )
        if not exact_match:
            return None

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

    # Avoid silently selecting one of two similarly matched locations.
    if len(matches) > 1:
        second_score, _, second_facility, _ = matches[1]
        if (
            best_score < 1.0
            and second_facility != facility
            and abs(best_score - second_score) < 0.06
        ):
            return None

    campus = str(
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
        "campus": campus,
        "description": description,
        "map_query": map_query,
        "map_url": map_url,
        "verified": verified,
    }


# ---------------------------------------------------------
# QUESTION INTENT MATCHING
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
        "exam results", "examination results", "check results",
        "view results", "marks", "grades", "results",
    },
    "admission": {
        "admission", "admissions", "apply for admission",
        "application for admission", "accept admission",
        "acceptance letter",
    },
    "registration": {
        "course registration", "semester registration",
        "register courses", "register for courses",
        "register for the semester", "academic registration",
        "registration deadline", "registration process",
        "how do i register", "how can i register",
        "how to register", "register at gzu",
        "registration", "enrol", "enroll", "enrolment", "enrollment",
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
    normalized = normalize(text)

    # More specific portal actions must be identified before general topics.
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

    if useful_message_words and useful_question_words:
        overlap = (
            len(useful_message_words & useful_question_words)
            / len(useful_message_words)
        )
    else:
        overlap = 0.0

    similarity = SequenceMatcher(None, text, question).ratio()
    score = max(similarity, overlap * 0.85)
    candidate_intent = faq_intent(faq["question"])

    if user_intent and candidate_intent:
        if user_intent == candidate_intent:
            score += 0.18
        else:
            # Prevent registration questions from borrowing fee or timetable
            # answers merely because their wording is similar.
            score -= 0.40

    distinctive_groups = {
        "exam_timetable": {"timetable", "schedule", "exam", "examination"},
        "results": {"results", "result", "grades", "marks"},
        "fees": {"fees", "fee", "payment", "receipt"},
        "accommodation": {"accommodation", "hostel", "housing", "residence"},
        "admission": {"admission", "application", "apply", "acceptance"},
        "registration": {
            "registration", "register", "enrolment", "enrollment",
            "enrol", "enroll",
        },
        "portal_reset": {"password", "reset", "forgot"},
        "portal_create": {"create", "account"},
        "portal_access": {"portal", "login", "access", "sign"},
        "library": {"library", "libraries", "books", "journals"},
        "academic_calendar": {"calendar", "semester", "dates"},
    }

    if candidate_intent in distinctive_groups:
        if not (message_words & distinctive_groups[candidate_intent]):
            score -= 0.15

    return max(score, 0.0)


def unknown_answer():
    return (
        "I couldn't find a sufficiently reliable answer in the current "
        "GZU FAQ database. Please check the official Great Zimbabwe "
        "University website at "
        f"{GZU_WEBSITE} "
        "or contact the relevant university office."
    )


def find_faq_answer(message):
    """Use the existing FAQ content, with exact and intent-aware matching."""
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
    candidates = [
        (faq_score(message, faq, user_intent), faq)
        for faq in faqs
    ]
    candidates.sort(key=lambda item: item[0], reverse=True)

    if not candidates:
        return unknown_answer()

    best_score, best_faq = candidates[0]
    best_intent = faq_intent(best_faq["question"])

    threshold = (
        0.48
        if user_intent and best_intent == user_intent
        else 0.58
    )

    if len(candidates) > 1:
        second_score, second_faq = candidates[1]
        second_intent = faq_intent(second_faq["question"])

        if (
            best_score < 0.80
            and best_intent != second_intent
            and abs(best_score - second_score) < 0.07
        ):
            return unknown_answer()

    if best_score >= threshold:
        return best_faq["answer"]

    return unknown_answer()


# ---------------------------------------------------------
# CURATED CAMPUS TOPICS
# ---------------------------------------------------------

def general_gzu_answer(message):
    text = normalize(message)
    website = GZU_WEBSITE

    # Compass Location: show the live directory count and selectable places.
    if (
        contains_any(text, {
            "compass location", "compass navigation",
            "campus locations", "campus location",
            "campus libraries", "explore locations",
            "campus directory", "campus wayfinder",
        })
        or text in {
            "where can i find the gzu campus libraries",
            "where can i find the campus libraries",
        }
    ):
        choices = directory_choices()
        names = [item["name"] for item in choices]

        if names:
            return {
                "response": (
                    "Hey there! 👋 Welcome to GZU Compass Navigation!\n\n"
                    "I’m here to help you find your way around Great Zimbabwe "
                    "University. Whether you’re looking for TLC, a library, "
                    "a student facility or another campus location, I’ll help "
                    "you find it.\n\n"
                    "Which place would you like to visit?\n\n"
                    f"There are {len(names)} places currently listed in our "
                    "campus directory. Choose a place below, or type its name "
                    "in the chat (for example, TLC). I’ll show you a map "
                    "preview, available details and a Google Maps button.\n\n"
                    "Locations marked unverified should be confirmed with "
                    "university staff before travelling."
                ),
                "location": None,
                "choices": names,
                "directory_count": len(names),
            }

        return {
            "response": (
                "Hey there! 👋 Welcome to GZU Compass Navigation!\n\n"
                "I’m ready to help you find a campus location, but the campus "
                "directory could not be loaded right now. Please try again "
                "shortly or check with GZU staff."
            ),
            "location": None,
            "choices": [],
            "directory_count": 0,
        }

    if contains_any(text, {
        "what student services", "student services", "student support",
        "services available",
    }):
        return {
            "response": (
                "GZU student services can include academic and administrative "
                "support, library and learning resources, accommodation "
                "information, and student welfare or medical support. The "
                "exact services and arrangements may differ by campus.\n\n"
                "To get help:\n"
                "1. Identify the service you need and your campus.\n"
                "2. Check the official GZU website for current contacts and "
                "announcements.\n"
                "3. For a personal student account or record, contact the "
                "responsible university office directly.\n\n"
                f"Official website: {website}"
            ),
            "location": None,
        }

    if contains_any(text, {
        "conduct rules", "student conduct", "conduct information",
        "disciplinary rules", "student code of conduct",
    }):
        return {
            "response": (
                "For student conduct, use the current GZU regulations or "
                "student handbook as the authoritative source. I do not have "
                "a verified copy of every current rule in this chatbot.\n\n"
                "Suggested steps:\n"
                "1. Check the official university website for current student "
                "regulations or handbook documents.\n"
                "2. Read the section relevant to your situation.\n"
                "3. If a rule or procedure is unclear, ask your faculty office "
                "or the responsible student-affairs/administrative office.\n\n"
                f"Official website: {website}"
            ),
            "location": None,
        }

    if contains_any(text, {
        "what is great zimbabwe university", "about gzu",
        "tell me about gzu", "about great zimbabwe university",
        "show me gzu university", "explore gzu",
        "explore great zimbabwe university",
    }):
        return {
            "response": (
                "Great Zimbabwe University (GZU) is a public university in "
                "Zimbabwe. For current faculties, programmes, campus "
                "information, notices and official announcements, use the "
                "university's official website.\n\n"
                f"Explore GZU: {website}\n\n"
                "If you tell me which programme, campus or service you want "
                "to explore, I can help you narrow down what to look for."
            ),
            "location": None,
        }

    if contains_any(text, {
        "how can i contact gzu", "contact information", "contact gzu",
        "gzu contact", "university phone number", "university email",
    }):
        return {
            "response": (
                "For current GZU telephone numbers, email addresses and "
                "office contacts, use the official university contact "
                "information so you do not rely on an outdated number.\n\n"
                "1. Open the official website.\n"
                "2. Look for the Contact Us page or the relevant faculty or "
                "office page.\n"
                "3. Contact the office responsible for your question. Share "
                "personal student information only through an official, "
                "trusted channel.\n\n"
                f"Official website: {website}"
            ),
            "location": None,
        }

    return None


def is_clearly_off_topic(message):
    text = normalize(message)

    if not text:
        return False

    university_terms = {
        "gzu", "great zimbabwe", "university", "campus", "student",
        "admission", "registration", "register", "portal", "fees",
        "library", "timetable", "results", "accommodation", "course",
        "lecturer", "faculty", "exam", "enrol", "enroll",
    }

    if contains_any(text, university_terms):
        return False

    unrelated = {
        "weather", "football", "soccer", "recipe", "recipes",
        "horoscope", "stock market", "bitcoin", "celebrity gossip",
        "write me a poem about mars", "where is mars",
        "capital of france", "solve this unrelated puzzle",
    }

    return contains_any(text, unrelated)


# ---------------------------------------------------------
# OPTIONAL GEMINI RESPONSE POLISHING
# ---------------------------------------------------------
# The deterministic FAQ/location routing remains the source of truth.
# Gemini is only used to make an existing verified answer clearer; it is
# instructed not to add facts. Without a key or on quota/network errors,
# the original answer is returned unchanged.

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
# Google restricted Gemini 2.5 access for projects without prior usage (Sep 2026).
# Try a current supported model if a configured/legacy model returns 404.
GEMINI_FALLBACK_MODELS = ("gemini-3.5-flash-lite", "gemini-3.6-flash")

def _gemini_model_candidates():
    ordered = [GEMINI_MODEL] + list(GEMINI_FALLBACK_MODELS)
    return list(dict.fromkeys(model for model in ordered if model))

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
AI_PROVIDER = os.environ.get("AI_PROVIDER", "gemini").strip().lower()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b").strip()


def _ai_provider_ready():
    return bool(GROQ_API_KEY) or bool(GEMINI_API_KEY)


TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()


def _tavily_search_context(question, official_first=False):
    """Fast bounded Tavily search; graceful no-key and failure behavior."""
    if not TAVILY_API_KEY:
        return "", []
    query = str(question).strip()
    if official_first:
        query += " Great Zimbabwe University official"
    body = {"query": query, "search_depth": "basic", "max_results": 4,
            "include_answer": False, "include_raw_content": False}
    if official_first:
        body["include_domains"] = ["gzu.ac.zw"]
        body["include_domains_mode"] = "prefer"
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + TAVILY_API_KEY},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=4.5) as response:
            raw = json.loads(response.read().decode("utf-8"))
        snippets, sources = [], []
        for item in raw.get("results", [])[:4]:
            title = str(item.get("title", "Web result"))[:200]
            url = str(item.get("url", ""))[:1000]
            content = re.sub(r"\s+", " ", str(item.get("content", ""))).strip()[:900]
            if content and url.startswith("https://"):
                snippets.append(f"Title: {title}\nURL: {url}\nExtract: {content}")
                sources.append((title, url))
        return "\n\n".join(snippets)[:3500], sources
    except Exception as exc:
        app.logger.warning("Tavily search unavailable (%s); continuing without web context", type(exc).__name__)
        return "", []



class _GZUTimetableLinkParser(HTMLParser):
    """Collect download anchors from the official GZU timetable page."""
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []
    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            attrs = dict(attrs)
            self._href = attrs.get("href")
            self._text = []
    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)
    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None
            self._text = []


def _official_gzu_timetable_documents():
    """Fetch live timetable download links from GZU, validating each link.

    The official page is the source of truth. Never return dead links or
    substitute an older timetable when the current file is unavailable.
    """
    page_url = "https://www.gzu.ac.zw/exam-timetable/"
    try:
        req = urllib.request.Request(page_url, headers={"User-Agent": "GZU-Campus-Assistant/1.0"})
        with urllib.request.urlopen(req, timeout=8) as response:
            html = response.read().decode("utf-8", errors="replace")
        parser = _GZUTimetableLinkParser()
        parser.feed(html)
        candidates = []
        for href, label in parser.links:
            url = urllib.parse.urljoin(page_url, href)
            host = urllib.parse.urlparse(url).netloc.lower()
            path = urllib.parse.urlparse(url).path.lower()
            label_norm = normalize(label)
            # The November 2026 timetable page contains a dedicated set of
            # school-level XLSX files. Only accept those spreadsheets; never
            # let unrelated site-wide PDFs (newsletter, handbook, calendar,
            # annual report, etc.) enter the examination extraction pipeline.
            filename = path.rsplit("/", 1)[-1]
            november_2026_xlsx = (
                path.endswith(".xlsx") and "/2026/11/" in path and any(token in filename for token in (
                    "school-of", "julius-nyerere", "simon-mazorodze",
                    "robert-mugabe", "gary-magadzire", "herbert-chitepo",
                    "munhumutapa"))
            )
            if host.endswith("gzu.ac.zw") and november_2026_xlsx:
                candidates.append((label or filename.replace("-", " "), url))
        # GZU's timetable page links to WordPress download landing pages by school.
        # Follow those official pages to discover the actual attachment URL.
        expanded = list(candidates)
        for title, landing in candidates[:10]:
            landing_path = urllib.parse.urlparse(landing).path.lower()
            if not landing_path.endswith((".pdf", ".xlsx", ".xls", ".docx")) and "/download/" in landing_path:
                try:
                    req2 = urllib.request.Request(landing, headers={"User-Agent": "Mozilla/5.0 GZU-Campus-Assistant/1.0"})
                    with urllib.request.urlopen(req2, timeout=6) as resp2:
                        landing_html = resp2.read().decode("utf-8", errors="replace")
                    parser2 = _GZUTimetableLinkParser(); parser2.feed(landing_html)
                    for href2, label2 in parser2.links:
                        file_url = urllib.parse.urljoin(landing, href2)
                        file_host = urllib.parse.urlparse(file_url).netloc.lower()
                        file_path = urllib.parse.urlparse(file_url).path.lower()
                        if file_host.endswith("gzu.ac.zw") and file_path.endswith((".pdf", ".xlsx", ".xls", ".docx")):
                            expanded.append((label2 or title, file_url))
                except Exception as exc:
                    app.logger.info("GZU timetable landing page unavailable (%s)", type(exc).__name__)
        verified = []
        seen = set()
        for title, url in expanded[:24]:
            if url in seen:
                continue
            seen.add(url)
            try:
                # GET rather than HEAD: WordPress/CDN file hosts often reject HEAD.
                check = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 GZU-Campus-Assistant/1.0", "Range": "bytes=0-1023"})
                with urllib.request.urlopen(check, timeout=7) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    final_url = resp.geturl()
                    final_host = urllib.parse.urlparse(final_url).netloc.lower()
                    final_path = urllib.parse.urlparse(final_url).path.lower()
                    file_like = ("pdf" in ctype or "spreadsheet" in ctype or "ms-excel" in ctype or
                                 final_path.endswith((".pdf", ".xlsx", ".xls", ".docx")))
                    if 200 <= resp.status < 300 and final_host.endswith("gzu.ac.zw") and file_like:
                        verified.append({"title": title[:180], "url": final_url})
            except Exception as exc:
                app.logger.info("GZU timetable download unavailable (%s)", type(exc).__name__)
        return verified, page_url
    except Exception as exc:
        app.logger.warning("Official GZU timetable page fetch failed (%s)", type(exc).__name__)
        return [], page_url


class _OfficialTimetableTableParser(HTMLParser):
    """Extract HTML tables from the official timetable page."""
    def __init__(self):
        super().__init__(); self.tables=[]; self.table=None; self.row=None; self.cell=None
    def handle_starttag(self, tag, attrs):
        t=tag.lower()
        if t=="table": self.table=[]
        elif t=="tr" and self.table is not None: self.row=[]
        elif t in ("td","th") and self.row is not None: self.cell=[]
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        t=tag.lower()
        if t in ("td","th") and self.cell is not None:
            self.row.append(re.sub(r"\s+"," ","".join(self.cell)).strip()); self.cell=None
        elif t=="tr" and self.row is not None:
            if any(self.row): self.table.append(self.row)
            self.row=None
        elif t=="table" and self.table is not None:
            if self.table: self.tables.append(self.table)
            self.table=None


def _extract_timetable_tables_from_official_page():
    """Extract candidate schedule tables from GZU's official HTML without Tavily."""
    url="https://www.gzu.ac.zw/exam-timetable/"
    try:
        req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 GZU-Campus-Assistant/1.0"})
        with urllib.request.urlopen(req,timeout=9) as response:
            html=response.read().decode("utf-8",errors="replace")
        parser=_OfficialTimetableTableParser(); parser.feed(html)
        good=[]
        for table in parser.tables:
            joined=" ".join(" ".join(row) for row in table)
            lower=joined.lower()
            has_time=bool(re.search(r"\b(?:0?[89]|1[0-9]|2[0-3]):[0-5][0-9]\b",lower))
            has_date=bool(re.search(r"\b(?:\d{1,2}[-/]\w{3,9}[-/]\d{2,4}|\w{3,9}\s+\d{1,2},?\s+\d{4})\b",lower))
            has_course=bool(re.search(r"\b[A-Z]{2,}[A-Z0-9]{2,}\b",joined))
            if len(table)>=3 and (has_time or has_date) and has_course: good.append(table)
        if not good:return ""
        return "\n\n".join("\n".join(" | ".join(cell.replace("|","/") for cell in row) for row in table[:220]) for table in good[:6])[:16000]
    except Exception as exc:
        app.logger.info("Official timetable HTML table extraction unavailable (%s)",type(exc).__name__)
        return ""


def _extract_official_timetable_file(url, title="GZU timetable"):
    """Download and extract readable table text from an official timetable file."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 GZU-Campus-Assistant/1.0"})
        with urllib.request.urlopen(req, timeout=12) as response:
            data = response.read(8 * 1024 * 1024 + 1)
            ctype = (response.headers.get("Content-Type") or "").lower()
            final_url = response.geturl()
        if len(data) > 8 * 1024 * 1024:
            return ""
        path = urllib.parse.urlparse(final_url).path.lower()
        if path.endswith((".xlsx", ".xlsm")) or "spreadsheet" in ctype or "excel" in ctype:
            try:
                from openpyxl import load_workbook
                import io
                wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
                parts = []
                for ws in wb.worksheets[:8]:
                    rows = []
                    for row in ws.iter_rows(values_only=True):
                        vals = [str(v).strip() if v is not None else "" for v in row]
                        if any(vals): rows.append(vals[:12])
                        if len(rows) >= 180: break
                    if rows:
                        parts.append("SHEET: " + ws.title + "\n" + "\n".join(" | ".join(r).rstrip(" |") for r in rows))
                return "\n\n".join(parts)[:14000]
            except Exception as exc:
                app.logger.info("Timetable spreadsheet extraction failed (%s)", type(exc).__name__)
                return ""
        if path.endswith(".pdf") or "pdf" in ctype:
            try:
                from pypdf import PdfReader
                import io
                reader = PdfReader(io.BytesIO(data))
                parts = []
                for page in reader.pages[:35]:
                    txt = page.extract_text() or ""
                    if txt.strip(): parts.append(txt.strip())
                return "\n\n".join(parts)[:14000]
            except Exception as exc:
                app.logger.info("Timetable PDF extraction failed (%s)", type(exc).__name__)
                return ""
        return ""
    except Exception as exc:
        app.logger.info("Official timetable download/extraction unavailable (%s)", type(exc).__name__)
        return ""


def _tavily_media_search(question, mode="images"):
    """Retrieve image URLs or document links only when explicitly requested."""
    query = str(question).strip()
    is_timetable_request = mode != "images" and any(term in normalize(query) for term in ("exam timetable", "examination timetable", "exam time table", "timetable"))
    if is_timetable_request:
        live_docs, official_page = _official_gzu_timetable_documents()
        if live_docs:
            return {"images": [], "documents": live_docs, "official_page": official_page, "verified_live": True}
        # Do not fall through to general search for an old or third-party timetable.
        return {"images": [], "documents": [], "official_page": official_page, "verified_live": False}
    if not TAVILY_API_KEY:
        return {"images": [], "documents": [], "official_page": "https://www.gzu.ac.zw/exam-timetable/" if is_timetable_request else None}
    body = {"query": query, "search_depth": "basic", "max_results": 5,
            "include_answer": False, "include_raw_content": False}
    if mode == "images":
        body["include_images"] = True
    else:
        # Timetables are high-stakes/time-sensitive: prefer only GZU's official domain.
        is_timetable = any(term in normalize(query) for term in ("exam timetable", "examination timetable", "exam time table", "timetable"))
        body["query"] += " official Great Zimbabwe University PDF document timetable handbook notice"
        if is_timetable:
            body["include_domains"] = ["gzu.ac.zw"]
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + TAVILY_API_KEY},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=6) as response:
            raw = json.loads(response.read().decode("utf-8"))
        found = {"images": [], "documents": []}
        if mode == "images":
            seen = set()
            for item in raw.get("images", [])[:8]:
                url = item.get("url") if isinstance(item, dict) else item
                if isinstance(url, str) and url.startswith("https://") and url not in seen:
                    seen.add(url)
                    found["images"].append({"url": url, "description": str(item.get("description", "Relevant image"))[:180] if isinstance(item, dict) else "Relevant image"})
        else:
            seen = set()
            for item in raw.get("results", []):
                url = str(item.get("url", ""))
                title = str(item.get("title", "Official document"))[:180]
                is_timetable = any(term in normalize(query) for term in ("exam timetable", "examination timetable", "exam time table", "timetable"))
                official = url.lower().split("/", 3)[2].endswith("gzu.ac.zw") if url.startswith("https://") else False
                looks_pdf = url.lower().split("?", 1)[0].endswith(".pdf") or "pdf" in str(item.get("content", "")).lower()
                acceptable = looks_pdf and (official if is_timetable else True)
                if url.startswith("https://") and acceptable:
                    if url not in seen:
                        seen.add(url)
                        found["documents"].append({"url": url, "title": title})
                if len(found["documents"]) >= 5:
                    break
        return found
    except Exception as exc:
        app.logger.warning("Tavily media search unavailable (%s)", type(exc).__name__)
        return {"images": [], "documents": []}


def _media_intent(question):
    q = normalize(question)
    # Normalize common typing errors before intent detection. In particular,
    # "immages of SKZ" must still reach the image-gallery pipeline.
    q = re.sub(r"\bimm+ages?\b", "images", q)
    q = re.sub(r"\bim+ages?\b", "images", q)
    q = re.sub(r"\bpictuers?\b", "pictures", q)
    q = re.sub(r"\bphots?\b", "photos", q)
    # Match natural requests with singular/plural and campus/member qualifiers,
    # not just a brittle list of exact phrases.
    has_media_noun = bool(re.search(r"\b(pic|pics|picture|pictures|photo|photos|image|images|photograph|photographs)\b", q))
    asks_to_show = bool(re.search(r"\b(show|find|get|display|send|give|fetch|look up|search)\b", q))
    # Many users phrase an image request without an imperative: "images of SKZ",
    # "pics of a dog", "photos of Eiffel Tower". Treat those as image intent too.
    subject_after_media = bool(re.search(
        r"\b(?:pics?|pictures?|images?|photos?|photographs?)\b\s+(?:of|for|showing)\s+\w", q))
    media_subject_first = bool(re.search(
        r"\b(?:show|find|get|display|send|give|fetch|look up|search)\b.*\b(?:pics?|pictures?|images?|photos?)\b", q))
    # Also catch subject-first forms like "ENHYPEN images", "SKZ pics",
    # "GZU photos" without requiring a verb or the word "of".
    subject_then_media = bool(re.search(
        r"\b[a-z0-9][a-z0-9 .&'’-]{0,60}\s+\b(?:pics?|pictures?|images?|photos?|photographs?)\b", q))
    image_intent = (has_media_noun and asks_to_show) or subject_after_media or media_subject_first or subject_then_media or any(x in q for x in (
        "show gzu", "gzu pics", "gzu pictures", "gzu photos", "campus pics",
        "campus pictures", "pictures of gzu", "photos of gzu", "pics of gzu",
        "show me the pics", "show the pics", "show me the pictures",
        "show the photos", "images of gzu", "campus image gallery"))
    document_intent = any(x in q for x in (
        "find pdf", "find the pdf", "search pdf", "download pdf",
        "get the document", "find document", "show document",
        "exam timetable pdf", "timetable document", "download timetable",
        "find timetable", "get timetable", "retrieve timetable",
        "extract timetable", "extract time table", "extract exam timetable"))
    if image_intent:
        return "images"
    if document_intent or bool(re.search(r"\b(extract|show|find|get|download|retrieve)\b.*\b(exam|examination|timetable|time table|time tabe|schedule)\b", q)):
        return "documents"
    return ""

def _append_source_links(answer, sources):
    if not answer or not sources:
        return answer
    seen, lines = set(), []
    for title, url in sources[:4]:
        if url not in seen:
            seen.add(url)
            lines.append(f"- {title}: {url}")
    return _clean_model_markdown(answer.rstrip()) + "\n\nSources (Tavily):\n" + "\n".join(lines)


def _groq_post(payload, timeout=15, use_browser_search=False):
    """Groq GPT-OSS-120B with selected generation settings and SSE streaming."""
    if not GROQ_API_KEY:
        app.logger.warning("AI_PROVIDER is groq but GROQ_API_KEY is not configured")
        return None
    parts = payload.get("contents", [{}])[0].get("parts", [])
    prompt = "\n".join(str(part.get("text", "")) for part in parts if part.get("text"))
    prompt = prompt.encode("ascii", "backslashreplace").decode("ascii")
    generation = payload.get("generationConfig", {})
    body = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": generation.get("temperature", 1),
        "max_completion_tokens": generation.get("maxOutputTokens", 2048),
        "top_p": 1,
        "reasoning_effort": "medium",
        "stream": True,
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + GROQ_API_KEY,
            # Groq's edge protection can reject urllib's default Python UA
            # with HTTP 403 / error 1010. Identify this app explicitly.
            "User-Agent": "Mozilla/5.0 (compatible; GZU-Campus-Assistant/2.0; +https://www.gzu.ac.zw/)",
        },
        method="POST")
    try:
        chunks = []
        with urllib.request.urlopen(req, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                    delta = event.get("choices", [{}])[0].get("delta", {})
                    if delta.get("content"):
                        chunks.append(delta["content"])
                except (json.JSONDecodeError, IndexError, AttributeError):
                    pass
        answer = "".join(chunks).strip()
        return {"candidates": [{"content": {"parts": [{"text": answer}]}}]} if answer else {"candidates": []}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:350]
        if exc.code == 403 and "1010" in detail:
            app.logger.warning(
                "Groq edge protection blocked this request (HTTP 403 / code 1010). "
                "The request includes an explicit app User-Agent; using configured backup."
            )
        else:
            app.logger.warning("Groq API returned HTTP %s: %s", exc.code, detail)
        raise
    except (TimeoutError, urllib.error.URLError) as exc:
        app.logger.warning("Groq connection failed (%s)", type(exc).__name__)
        raise


def _question_needs_web_search(payload):
    """Conservatively detect requests needing current/web-grounded evidence."""
    parts = payload.get("contents", [{}])[0].get("parts", [])
    prompt = " ".join(str(part.get("text", "")) for part in parts if part.get("text")).lower()
    markers = (
        "current", "latest", "today", "this week", "this month", "deadline",
        "timetable", "exam date", "notice", "who is the dean", "dean of",
        "staff member", "vice chancellor", "director", "contact details",
        "recent", "news", "on the website", "gzu.ac.zw", "verify",
        "search the web", "search online", "university official",
    )
    return any(marker in prompt for marker in markers)


def _gemini_post(payload, timeout=15, use_browser_search=None):
    """Groq primary with Gemini backup (or reverse if configured otherwise)."""
    groq_first = AI_PROVIDER == "groq"
    if groq_first and GROQ_API_KEY:
        try:
            return _groq_post(payload, timeout=timeout,
                             use_browser_search=(_question_needs_web_search(payload) if use_browser_search is None else use_browser_search))
        except Exception as exc:
            app.logger.warning("Primary Groq failed; attempting Gemini backup (%s)", type(exc).__name__)

    if not GEMINI_API_KEY:
        if not groq_first and GROQ_API_KEY:
            try:
                return _groq_post(payload, timeout=timeout,
                                 use_browser_search=_question_needs_web_search(payload))
            except Exception as exc:
                app.logger.warning("Groq backup failed (%s)", type(exc).__name__)
        app.logger.warning("Gemini backup unavailable: GEMINI_API_KEY is not configured")
        return None

    last_error = None
    for model in _gemini_model_candidates():
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + quote_plus(model) + ":generateContent"
        )
        req = urllib.request.Request(
            endpoint, data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            method="POST",
        )
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 404:
                    app.logger.warning("Gemini model %s returned 404; trying fallback model", model)
                    break
                if exc.code in (429, 500, 502, 503, 504) and attempt == 0:
                    app.logger.warning("Gemini returned HTTP %s; retrying once", exc.code)
                    time.sleep(0.6)
                    continue
                break
            except (TimeoutError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.6)
                    continue
                break

    if not groq_first and GROQ_API_KEY:
        try:
            app.logger.warning("Primary Gemini failed; attempting Groq backup")
            return _groq_post(payload, timeout=timeout,
                             use_browser_search=_question_needs_web_search(payload))
        except Exception as backup_exc:
            app.logger.warning("Groq backup failed (%s)", type(backup_exc).__name__)
    if last_error:
        raise last_error
    return None


def polish_with_gemini(question, grounded_answer):
    if not _ai_provider_ready() or not grounded_answer:
        return grounded_answer

    # Never send campus-location responses to the generative model. This keeps
    # the carefully working location feature entirely untouched.
    if isinstance(grounded_answer, dict) or not isinstance(grounded_answer, str):
        return grounded_answer

    if grounded_answer == unknown_answer():
        return grounded_answer


    payload = {
        "contents": [{"parts": [{"text": (
            "You are the GZU Campus Assistant. Rewrite the supplied verified "
            "university answer in clear, concise, friendly language with neat "
            "formatting. Do not add, infer, change, or omit factual details, "
            "dates, percentages, requirements, or URLs. If uncertain, preserve "
            "the wording. Return only the improved answer.\n\n"
            "Student question: " + question + "\n\nVerified answer/context: "
            + grounded_answer
        )}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 500}
    }

    try:
        result = _gemini_post(payload, timeout=8) or {}
        candidates = result.get("candidates", [])
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        polished = "\n".join(
            str(part.get("text", "")).strip() for part in parts
            if part.get("text")
        ).strip()
        return polished if polished else grounded_answer
    except Exception as exc:
        app.logger.info("%s polishing unavailable; using verified answer: %s", AI_PROVIDER, exc)
        return grounded_answer


# ---------------------------------------------------------
# LIVE OFFICIAL GZU WEBSITE LOOKUP
# ---------------------------------------------------------
from html.parser import HTMLParser
import threading
import time

# Only pages on GZU-controlled official domains are queried. Pages are selected
# by topic rather than making a slow request to every page for every question.
OFFICIAL_GZU_PAGES = {
    "home": "https://www.gzu.ac.zw/",
    "notices": "https://www.gzu.ac.zw/category/notices/",
    "admissions": "https://www.gzu.ac.zw/admissions/",
    "registration": "https://www.gzu.ac.zw/registration/",
    "fees": "https://www.gzu.ac.zw/fees-structure/",
    "examinations": "https://www.gzu.ac.zw/examinations/",
    "programmes": "https://www.gzu.ac.zw/undergraduate-programmes/",
    "documents": "https://www.gzu.ac.zw/documents/",
    "contacts": "https://www.gzu.ac.zw/contact-us-2/",
    "medical_school": "https://smsmhs.gzu.ac.zw/",
    "medical_dean": "https://smsmhs.gzu.ac.zw/about/",
    "accepted_students": "https://elearning.gzu.ac.zw/accepted/",
}

class _VisibleTextParser(HTMLParser):
    """Extract readable page text while correctly ignoring nested hidden areas."""
    HIDDEN = {"script", "style", "nav", "footer", "header", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_stack = []

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self.hidden_stack.append(tag)
        if not self.hidden_stack and tag in {"p", "li", "h1", "h2", "h3", "h4", "br", "tr"}:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if not self.hidden_stack and tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.HIDDEN:
            # HTML is imperfect; remove the matching hidden element and any
            # malformed nested entries above it, without decrementing on an
            # unrelated closing tag.
            for index in range(len(self.hidden_stack) - 1, -1, -1):
                if self.hidden_stack[index] == tag:
                    del self.hidden_stack[index:]
                    break
        elif not self.hidden_stack and tag in {"p", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden_stack and data.strip():
            self.parts.append(data.strip())


_OFFICIAL_PAGE_CACHE = {}
_OFFICIAL_CACHE_LOCK = threading.Lock()
_OFFICIAL_CACHE_TTL = 900
_OFFICIAL_FETCH_TIMEOUT = 5
_OFFICIAL_MAX_BYTES = 1_200_000


def _official_pages_for_question(question):
    q = normalize(question)
    selected = ["notices"]
    rules = [
        ("fees", ("fee", "fees", "tuition", "payment", "pay", "deposit", "balance", "levy", "invoice")),
        ("admissions", ("admission", "apply", "application", "applicant", "entry requirements", "enrol", "enroll", "intake")),
        ("registration", ("registration", "register", "semester", "module", "course registration", "defer", "withdraw")),
        ("examinations", ("exam", "examination", "timetable", "result", "results", "supplementary", "special exam")),
        ("medical_dean", ("dean of medicine", "medical school dean", "dean medicine", "dean medical", "professor mufunda", "j mufunda", "simon mazorodze")),
        ("medical_school", ("medical school", "medicine school", "school of medicine", "medical and health sciences")),
        ("programmes", ("programme", "programmes", "program", "degree", "diploma", "study", "faculty", "department")),
        ("documents", ("handbook", "regulation", "policy", "form", "forms", "document", "prospectus")),
        ("contacts", ("contact", "phone", "email", "office", "address", "Mashava", "campus")),
        ("accepted_students", ("accepted", "new student", "newly admitted", "portal", "online registration", "e-learning", "elearning")),
    ]
    for page, terms in rules:
        if any(normalize(term) in q for term in terms):
            selected.append(page)
    if "dean" in q and any(term in q for term in ("medicine", "medical", "health", "mazorodze")):
        selected.insert(0, "medical_dean")
    if "admissions" in selected:
        selected.extend(["programmes", "documents"])
    if len(selected) == 1:
        selected.extend(["home", "documents"])
    # Preserve order and cap outbound requests to three official pages.
    return list(dict.fromkeys(selected))[:3]


def _fetch_official_page(page_key):
    now = time.time()
    with _OFFICIAL_CACHE_LOCK:
        cached = _OFFICIAL_PAGE_CACHE.get(page_key)
        if cached and now - cached[0] < _OFFICIAL_CACHE_TTL:
            return cached[1], cached[2]
    url = OFFICIAL_GZU_PAGES[page_key]
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; GZU-Campus-Assistant/2.0)",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=_OFFICIAL_FETCH_TIMEOUT) as response:
            final_url = response.geturl()
            if not final_url.startswith(("https://www.gzu.ac.zw/", "https://gzu.ac.zw/", "https://elearning.gzu.ac.zw/", "https://smsmhs.gzu.ac.zw/")):
                return "", url
            content_type = response.headers.get("Content-Type", "").lower()
            if "html" not in content_type:
                return "", url
            raw = response.read(_OFFICIAL_MAX_BYTES).decode("utf-8", "replace")
        parser = _VisibleTextParser()
        parser.feed(raw)
        page_text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
        if page_text:
            with _OFFICIAL_CACHE_LOCK:
                _OFFICIAL_PAGE_CACHE[page_key] = (now, page_text, final_url)
        return page_text, final_url
    except Exception as exc:
        app.logger.info("Official GZU page unavailable (%s): %s", page_key, exc)
        return "", url


def retrieve_official_gzu_context(question, max_pages=3):
    """Return concise, relevance-ranked excerpts from selected official pages."""
    qwords = {w for w in normalize(question).split() if len(w) > 2}
    if not qwords:
        return ""
    collected = []
    for page_key in _official_pages_for_question(question)[:max_pages]:
        page_text, url = _fetch_official_page(page_key)
        if not page_text:
            continue
        sentences = re.split(r"(?<=[.!?])\s+", page_text)
        ranked = []
        for sentence in sentences:
            normalized_words = set(normalize(sentence).split())
            overlap = len(qwords & normalized_words)
            if overlap:
                ranked.append((overlap, sentence[:650]))
        ranked.sort(key=lambda item: item[0], reverse=True)
        excerpt = " ".join(sentence for _, sentence in ranked[:10])[:4200]
        if not excerpt and len(page_text) > 100:
            excerpt = page_text[:1200]
        if excerpt:
            collected.append((sum(score for score, _ in ranked[:10]), url, excerpt))
    collected.sort(key=lambda item: item[0], reverse=True)
    return "\n\n".join(
        f"Official source: {url}\nPage extracts: {excerpt}"
        for _, url, excerpt in collected
    )[:10500]


def _official_medical_dean_answer(question):
    """Answer medical-dean queries from the School's own official profile."""
    q = normalize(question)
    dean_terms = ("dean", "head of medicine", "medical school head")
    medical_terms = ("medicine", "medical", "health", "mazorodze")
    if not any(term in q for term in dean_terms) or not any(term in q for term in medical_terms):
        return None
    page_text, page_url = _fetch_official_page("medical_dean")
    # Only return the personnel details when the official page itself confirms
    # the profile; otherwise fall through to the normal official-source flow.
    normalized_page = normalize(page_text)
    if "professor j mufunda" not in normalized_page and "pro j mufunda" not in normalized_page:
        return None
    return {
        "response": (
            "The Dean of Great Zimbabwe University's Simon Mazorodze School of "
            "Medical and Health Sciences is **Professor J. Mufunda**.\n\n"
            "**Official GZU contact details:**\n"
            "- Email: jmufunda@gzu.ac.zw\n"
            "- Telephone: +263 771 452 475\n\n"
            "Official profile: https://smsmhs.gzu.ac.zw/about/"
        ),
        "location": None,
        "source": "official_gzu_medical_school_profile",
    }


def answer_from_official_context_fallback(question, context):
    """Present relevant official extracts when Gemini is unavailable.

    This keeps live GZU retrieval useful without making the API key a
    single point of failure. It does not invent or paraphrase facts.
    """
    if not context:
        return ""

    question_words = {word for word in normalize(question).split() if len(word) > 2}
    blocks = re.split(r"\n\n(?=Official source: )", context)
    ranked_items = []
    source_urls = []

    for block in blocks:
        source_match = re.search(r"Official source:\s*(https://[^\s]+)", block)
        if not source_match:
            continue
        source_url = source_match.group(1).rstrip(".,)")
        source_urls.append(source_url)
        extract = block.split("Page extracts:", 1)[-1].strip()
        for sentence in re.split(r"(?<=[.!?])\s+", extract):
            sentence = sentence.strip()
            if len(sentence) < 25:
                continue
            sentence_words = set(normalize(sentence).split())
            overlap = len(question_words & sentence_words)
            if overlap:
                ranked_items.append((overlap, sentence))

    # Keep only the strongest, unique source statements. Do not fabricate an
    # answer if the official pages did not return relevant evidence.
    ranked_items.sort(key=lambda item: item[0], reverse=True)
    selected = []
    seen = set()
    for _, sentence in ranked_items:
        key = normalize(sentence)
        if key and key not in seen:
            selected.append(sentence)
            seen.add(key)
        if len(selected) >= 6:
            break

    if not selected:
        return ""

    answer = (
        "I found these relevant details on the official GZU website. "
        "I am showing the page information directly because the AI answer "
        "service is unavailable right now:\n\n"
        + "\n".join(f"- {sentence}" for sentence in selected)
    )

    # Explicitly surface the known high-risk case where official fee pages
    # may publish different deposit percentages. Do not choose one silently.
    if ("fee" in question_words or "fees" in question_words or "tuition" in question_words):
        joined = " ".join(selected)
        percentages = set(re.findall(r"\b(\d{1,3})\s*%", joined))
        if "60" in percentages and "75" in percentages:
            answer += (
                "\n\nImportant: the official extracts show conflicting "
                "registration deposit percentages (60% and 75%). Please "
                "confirm the current requirement with the GZU Bursar or "
                "Accounts Office before making payment."
            )

    unique_sources = list(dict.fromkeys(source_urls))
    if unique_sources:
        answer += "\n\nOfficial source(s):\n" + "\n".join(unique_sources[:4])
    answer += (
        "\n\nPlease check the latest official notice or contact the "
        "relevant university office for changes to fees, deadlines or "
        "personal student-account matters."
    )
    return answer


def answer_from_official_context(question, context):
    if not _ai_provider_ready() or not context:
        return ""
    web_context, web_sources = _tavily_search_context(question, official_first=True)
    combined_context = str(context)
    if web_context:
        combined_context += "\n\nSupplementary Tavily search results (official GZU sources preferred):\n" + web_context
    prompt = (
        "You are the GZU Campus Assistant. Answer the student's question "
        "using ONLY the official Great Zimbabwe University context below. "
        "Be clear, concise, and helpful. Do not invent deadlines, fees, "
        "requirements, contacts, or policies. If the context does not answer "
        "the question, say what is missing and direct the student to the "
        "relevant official GZU office or website.\n\n"
        "Student question: " + str(question) + "\n\n"
        "Official GZU context:\n" + combined_context
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 700},
    }
    try:
        result = _gemini_post(payload, timeout=15, use_browser_search=True) or {}
        candidates = result.get("candidates", [])
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        answer = "\n".join(
            part.get("text", "").strip() for part in parts if part.get("text")
        ).strip()
        return _append_source_links(answer, web_sources)
    except Exception as exc:
        app.logger.warning("Official-site %s answer unavailable (%s)", AI_PROVIDER, type(exc).__name__)
        return ""

def answer_with_tavily_context(question, context, sources):
    """Answer from retrieved web snippets when the static official-page index misses."""
    if not context or not _ai_provider_ready():
        return ""
    prompt = (
        "You are the GZU Campus Assistant. Answer the user's question using the "
        "following search excerpts. Prioritize official gzu.ac.zw pages for university "
        "facts. For timetable questions, identify and link the actual official PDF or "
        "document if it appears in results; do not merely describe the exam period. "
        "Never invent exam dates, venues, or times. If only a general exams page is "
        "found and no timetable document is present, say that clearly. For staff questions, "
        "distinguish a Faculty Dean from a Department Chair/HOD; do not call a chair "
        "the dean. If the user's wording is ambiguous, explain the distinction and "
        "state only names supported by the official results. Keep dates and policies qualified.\n\n"
        "Question: " + str(question) + "\n\nSearch results:\n" + str(context)
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.1, "maxOutputTokens": 700}}
    try:
        result = _gemini_post(payload, timeout=10) or {}
        candidates = result.get("candidates", [])
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        answer = "\n".join(str(part.get("text", "")).strip() for part in parts if part.get("text")).strip()
        return _append_source_links(answer, sources)
    except Exception as exc:
        app.logger.warning("Tavily-grounded answer unavailable (%s)", type(exc).__name__)
        return ""


def answer_with_general_gemini(question):
    """Answer non-GZU questions using the configured AI provider, safely."""
    if not _ai_provider_ready():
        return ""
    prompt = (
        "You are a friendly, helpful general-purpose AI assistant embedded "
        "in the GZU Campus Assistant. Answer general academic and everyday "
        "questions from your knowledge, even when GZU's website has no definition. "
        "Expand common student abbreviations (for example, chem = chemistry) "
        "when the intent is clear. Only use GZU-specific evidence for questions "
        "about GZU programmes, people, policies, notices, fees, or schedules. "
        "For questions such as 'Dean of Computer Science', carefully determine whether "
        "the institution has a faculty Dean or a department Chair/HOD; do not invent "
        "a separate dean for a department if sources identify only a faculty dean. "
        "Answer accurately and in plain language. For uncertain or time-sensitive facts, say "
        "so and avoid pretending to have live information. For medical, "
        "legal, financial, or safety-critical matters, give cautious general "
        "information and recommend a qualified professional when appropriate.\n\n"
        "User question: " + str(question)
    )
    wants_web = any(marker in normalize(question) for marker in (
        "search online", "search the web", "look up online", "latest", "current news",
        "recent news", "today", "this week", "verify online"))
    web_context, web_sources = _tavily_search_context(question, official_first=False) if wants_web else ("", [])
    if web_context:
        prompt += "\n\nTavily web search evidence (cite these sources; note uncertainty):\n" + web_context
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 700},
    }
    try:
        result = _gemini_post(payload, timeout=12) or {}
        candidates = result.get("candidates", [])
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        answer = "\n".join(str(part.get("text", "")).strip() for part in parts if part.get("text")).strip()
        return _append_source_links(answer, web_sources)
    except Exception as exc:
        app.logger.warning("General %s response unavailable (%s)", AI_PROVIDER, type(exc).__name__)
        return ""

# ---------------------------------------------------------
# QUESTION INTENT ROUTING
# ---------------------------------------------------------

def _is_gzu_specific_question(message):
    """Only consult campus sources when the question is actually about GZU."""
    q = normalize(message)
    explicit = (
        "gzu", "great zimbabwe university", "at the university", "on campus",
        "our campus", "my university", "university of", "faculty of",
        "school of", "department of", "programme offered", "degree offered",
        "lecturer at", "staff at", "who is the dean", "dean of", "vice chancellor",
        "gzu fees", "gzu timetable", "gzu exam", "gzu notice", "gzu news",
        "registration at", "admission to gzu", "apply to gzu",
    )
    dynamic_gzu = (
        "exam timetable", "examination timetable", "exam time table", "exam timetable",
        "semester timetable", "class timetable", "registration deadline",
        "latest notice", "latest news", "current dean", "department chair",
        "chairperson", "lecturer", "staff directory", "current fees",
        "fees for", "contact for", "school of",
    )
    # Timetable/staff/fee terms imply the institution in this campus-assistant app.
    return any(x in q for x in explicit) or any(x in q for x in dynamic_gzu)


def _clean_model_markdown(text):
    """Normalize AI's over-escaped Markdown while preserving URLs and code."""
    if not isinstance(text, str):
        return text
    # The model sometimes emits literal backslashes before Markdown syntax.
    text = re.sub(r"\\([*_`~\[\]()>])", r"\1", text)
    # Remove escaped whitespace artifacts from generated markdown lists.
    text = re.sub(r"(?m)^\\\*\s+", "- ", text)
    # Repair the frequent malformed link: [label]\([url](url))
    text = re.sub(r"\[([^\]]+)\]\\?\(\[?(https?://[^\s\]\)]+)\]?\)", r"[\1](\2)", text)
    # Normalize duplicate wrappers around bare URLs without escaping them.
    text = re.sub(r"\\?\[(https?://[^\]\s]+)\\?\]", r"\1", text)
    text = text.replace(r"\&", "&")
    return text.strip()


# ---------------------------------------------------------
# ANSWER ROUTING
# ---------------------------------------------------------

def _is_greeting(message):
    text = normalize(message).strip(" !.,?\t")
    return text in {"hi", "hello", "hey", "hiya", "hie", "hie there", "mhoro", "good morning", "good afternoon", "good evening", "greetings", "how are you", "whats up", "what is up"} or bool(re.match(r"^(hi|hello|hey|hie|hiya|mhoro)\b.*\b(how are you|how r u|how are u|whats up|what is up)\b", text))


def _greeting_response(message):
    text = normalize(message)
    if "good morning" in text:
        greeting = "Good morning"
    elif "good afternoon" in text:
        greeting = "Good afternoon"
    elif "good evening" in text:
        greeting = "Good evening"
    else:
        greeting = "Hello"
    return {"response": f"{greeting}! 👋 Welcome to Great Zimbabwe University. I’m your GZU Campus Assistant. I can help with admissions, programmes, registration, fees, examinations, student services and campus directions. What would you like to know?", "location": None}


def _admissions_basics(message):
    q = normalize(message)
    admission_terms = ("admission", "apply", "application", "entry requirements", "how do i get in", "how to join")
    if not any(term in q for term in admission_terms):
        return None
    return {"response": "To apply to Great Zimbabwe University:\n\n1. Visit the official GZU website: https://www.gzu.ac.zw/\n2. Open the Admissions / Apply Online section, or use the official application portal: https://elearning.gzu.ac.zw/apply/login/\n3. Create an applicant account and complete the application form.\n4. Choose the programme and intake you wish to apply for.\n5. Follow the portal instructions for required supporting documents and application-fee payment.\n6. Review and submit your application, then monitor the portal and official notices for updates.\n\nEntry requirements, application fees and deadlines can vary by programme and intake. Check the current official admissions notice before applying. If you need help, contact GZU through https://www.gzu.ac.zw/contact-us-2/.\n\nOfficial sources: https://www.gzu.ac.zw/admissions/ and https://www.gzu.ac.zw/category/notices/", "location": None, "source": "official_gzu_admissions"}


def find_answer(message):
    if not normalize(message):
        return {
            "response": "Please type a question so I can help.",
            "location": None,
        }

    # Handle conversational greetings naturally; these are not FAQ lookups.
    if _is_greeting(message):
        return _greeting_response(message)

    qquick = normalize(message)

    # The Compass button is an invitation to browse the directory, not a
    # request to resolve a specific place. Start a friendly guided prompt.
    compass_entry_phrases = {
        "compass location", "open compass location", "compass navigation",
        "campus locations", "campus directory", "find a campus location",
        "help me find a place", "where can i go",
    }
    if qquick in compass_entry_phrases:
        choices = directory_choices()
        names = [item["name"] for item in choices]
        count = len(names)
        if names:
            return {
                "response": (
                    "Hi there! 👋 Welcome to GZU Compass Locations.\n\n"
                    "I’m here to help you find your way around Great Zimbabwe University. "
                    f"There are currently {count} places in the campus directory.\n\n"
                    "Where would you like to go? You can select a place from the list "
                    "below or type its name, and I’ll help you with the location and directions.\n\n"
                    "Please confirm unverified locations with university staff."
                ),
                "location": None,
                "choices": names,
                "directory_count": count,
                "source": "compass_directory_welcome",
            }
        return {
            "response": (
                "Hi there! 👋 I’m ready to help you find a campus location, "
                "but the directory is temporarily unavailable. Please try again shortly."
            ),
            "location": None,
            "choices": [],
            "directory_count": 0,
            "source": "compass_directory_unavailable",
        }
    if any(term in qquick for term in ("how can i contact gzu", "contact gzu", "gzu contact details", "gzu phone number", "gzu email address")):
        return {"response": (
            "You can contact Great Zimbabwe University through its general contacts:\n\n"
            "- Telephone: +263 39 2266648 / 58 / 60 / 63\n"
            "- Mobile: +263 782 780 662\n"
            "- Email: information@gzu.ac.zw\n"
            "- Contact page: https://www.gzu.ac.zw/contact-us-2/\n\n"
            "For a specific issue (fees, admissions, examinations, or a department), "
            "tell me which office you need so I can direct you more precisely."
        ), "location": None, "source": "official_gzu_contact"}
    if qquick in {"how do i register", "how to register", "registration at gzu", "gzu registration"}:
        return {"response": (
            "GZU registration generally involves using the relevant university portal, "
            "following the current registration notice, completing required details, "
            "and meeting the published payment and document requirements.\n\n"
            "Useful official links:\n"
            "- Student portal: https://studentportal.gzu.ac.zw/\n"
            "- Official notices: https://www.gzu.ac.zw/category/notices/\n"
            "- Contact GZU: https://www.gzu.ac.zw/contact-us-2/\n\n"
            "Are you registering as a **new student** or doing **semester/course registration**? "
            "The steps differ, and I can guide you accordingly."
        ), "location": None, "source": "registration_router"}

    # Give a safe, official-source-based admissions starter answer immediately.
    admissions_answer = _admissions_basics(message)
    if admissions_answer:
        return admissions_answer

    # Campus directory/location matching is deliberately first and unchanged.
    # Keep this path isolated from the live website and language-model features.
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

        return {"response": response, "location": location}

    # If the user clearly requested directions but no location matched,
    # offer the actual directory entries instead of guessing.
    if is_direction_request(message):
        names = [item["name"] for item in directory_choices()]
        if names:
            choices_text = "\n- ".join(names)
        else:
            choices_text = "the campus directory is currently unavailable"

        return {
            "response": (
                "I couldn't confidently match that place to the current "
                f"directory. Try one of these listed names: {choices_text}.\n\n"
                "Directory entries may be unverified, so confirm the exact "
                f"location with university staff. Official website: {GZU_WEBSITE}"
            ),
            "location": None,
            "choices": names,
        }

    # Explicit media requests: search and return media metadata to the chat UI.
    media_mode = _media_intent(message)
    if media_mode:
        # GZU campus imagery is served from known official URLs without calling
        # Tavily, preserving the user's limited search credits.
        if media_mode == "images" and any(term in normalize(message) for term in ("gzu", "great zimbabwe", "campus")):
            official_images = [
                {"url": "https://www.gzu.ac.zw/wp-content/uploads/2024/02/RG-NEW-ACTUAL.jpg", "description": "Great Zimbabwe University — official campus image"},
                {"url": "https://www.gzu.ac.zw/wp-content/uploads/2025/05/Innovation-HUB-WEB-1024x576.jpg", "description": "GZU Innovation Hub — official image"},
                {"url": "https://www.gzu.ac.zw/wp-content/uploads/2024/02/IMG_7647.jpg", "description": "Great Zimbabwe University — official campus photo"},
            ]
            return {"response": "Here are official GZU-hosted images. Select an image to open it.", "location": None, "images": official_images, "source": "gzu_official_image_fallback"}
        media = _tavily_media_search(message, media_mode)
        if media_mode == "images" and media.get("images"):
            return {"response": "Here are relevant images I found online. Open an image to view it. Check its source before reuse.", "location": None, "images": media["images"], "source": "tavily_images"}
        if media_mode == "images" and not media.get("images") and any(term in normalize(message) for term in ("gzu", "great zimbabwe", "campus")):
            # Reliable official GZU-hosted imagery fallback when Tavily image search
            # is unavailable, quota-limited, or returns no image objects.
            official_images = [
                {"url": "https://www.gzu.ac.zw/wp-content/uploads/2024/02/RG-NEW-ACTUAL.jpg", "description": "Great Zimbabwe University — official campus image"},
                {"url": "https://www.gzu.ac.zw/wp-content/uploads/2025/05/Innovation-HUB-WEB-1024x576.jpg", "description": "GZU Innovation Hub — official image"},
                {"url": "https://www.gzu.ac.zw/wp-content/uploads/2024/02/IMG_7647.jpg", "description": "Great Zimbabwe University — official campus photo"},
            ]
            return {"response": "Here are official GZU-hosted images. Select an image to open it.", "location": None, "images": official_images, "source": "gzu_official_image_fallback"}
        if media_mode == "documents" and any(term in normalize(message) for term in ("exam timetable", "examination timetable", "exam time table", "timetable")):
            live_table_text = _extract_timetable_tables_from_official_page()
            if live_table_text:
                return {"response": "I extracted a timetable table directly from GZU's official page. Check whether the page labels it draft or final before relying on it.\n\n" + live_table_text + "\n\nOfficial timetable page: https://www.gzu.ac.zw/exam-timetable/", "location": None, "source": "official_gzu_timetable_html_table_extracted"}
        if media_mode == "documents" and not media.get("documents") and any(term in normalize(message) for term in ("exam timetable", "examination timetable", "exam time table", "timetable")):
            return {"response": "I checked GZU's official November 2026 examination timetable page. It currently displays the 3rd Draft / November 2026 timetable, but the school Excel download links on that page are returning 404 (file not found), so I cannot extract or verify the actual dates, times, and venues yet.\n\nOfficial timetable page: https://www.gzu.ac.zw/exam-timetable/\n\nExaminations Office: exams@gzu.ac.zw | +263 39 2266672\n\nI won't invent exam details or substitute an outdated timetable. If you upload the working Excel/PDF here, I can extract it immediately.", "location": None, "source": "official_gzu_timetable_links_unavailable"}
        if media_mode == "documents" and media.get("documents"):
            links = "\n".join(f"- [{item['title']}]({item['url']})" for item in media["documents"])
            if media.get("verified_live"):
                extracted = []
                for item in media["documents"][:4]:
                    title = str(item.get("title", "Official timetable"))
                    text = _extract_official_timetable_file(item["url"], title)
                    # A valid exam timetable must identify itself in its title or
                    # extracted body. Never present newsletters/handbooks as timetables.
                    combined = normalize(title + " " + text[:5000])
                    url_path = urllib.parse.urlparse(item["url"]).path.lower()
                    is_november_school_xlsx = (
                        url_path.endswith(".xlsx") and "/2026/11/" in url_path and any(token in url_path for token in (
                            "julius-nyerere", "school-of-natural-sciences", "simon-mazorodze",
                            "robert-mugabe", "gary-magadzire", "herbert-chitepo", "munhumutapa"))
                    )
                    # The official page itself labels these seven school XLSX files
                    # as the November 2026 final timetable. Their filenames don't
                    # repeat "exam timetable", so verify the extracted workbook
                    # has schedule-like dates/times rather than requiring that phrase.
                    has_date_rows = bool(re.search(r"\b(?:\d{1,2}[/-](?:\d{1,2}|[a-z]{3,9})[/-]\d{2,4}|(?:mon|tue|wed|thu|fri|sat|sun)\w*[, ]+\d{1,2})\b", combined))
                    has_time_rows = bool(re.search(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b|\b(?:08|09|10|11|12|13|14|15|16|17)\s*(?:00)?\b", combined))
                    looks_exam_schedule = any(term in combined for term in (
                        "exam timetable", "examination timetable", "examinations timetable",
                        "exam schedule", "examination schedule", "november 2026 examination",
                        "november 2026 exams", "final examination timetable", "draft examination timetable"))
                    looks_newsletter = any(term in normalize(title) for term in (
                        "newsletter", "annual report", "handbook", "prospectus", "magazine", "bulletin"))
                    schedule_verified = looks_exam_schedule or (is_november_school_xlsx and has_date_rows and has_time_rows)
                    if text.strip() and schedule_verified and not looks_newsletter:
                        extracted.append((title, item["url"], text))
                if extracted:
                    chunks = []
                    for title, url, text in extracted:
                        chunks.append("### " + title + "\nSource: " + url + "\n\n" + text)
                    return {"response": "I downloaded and extracted text from the currently accessible official GZU timetable file(s). Please double-check whether the document is marked draft or final.\n\n" + "\n\n".join(chunks) + "\n\nOfficial timetable page: https://www.gzu.ac.zw/exam-timetable/", "location": None, "source": "official_gzu_timetable_extracted"}
                intro = "I found official documents, but none passed verification as the November 2026 examination timetable. I rejected unrelated files (such as newsletters) rather than presenting them as exam schedules. Please use the official page or upload the correct Excel/PDF for extraction:"
                return {"response": intro + "\n\n" + links + "\n\nOfficial timetable page: https://www.gzu.ac.zw/exam-timetable/", "location": None, "source": "official_gzu_timetable_live_not_extracted"}
            return {"response": "I found these possible documents. Please verify the date and issuing office before relying on a timetable or policy:\n\n" + links, "location": None, "source": "tavily_documents"}

    medical_dean_answer = _official_medical_dean_answer(message)
    if medical_dean_answer:
        return medical_dean_answer

    curated = general_gzu_answer(message)
    if curated:
        return curated

    # Route non-location questions through the configured AI provider.
    # For GZU-specific questions, first retrieve official context and let the
    # provider formulate a complete grounded answer. General conversation is
    # sent directly to the provider (not to the website scraper).
    # General academic questions ("what is machine learning?", "chem") must
    # not be constrained by GZU's website. Campus retrieval is opt-in by intent.
    is_campus_question = _is_gzu_specific_question(message)
    qnorm = normalize(message)
    is_timetable_question = any(term in qnorm for term in (
        "timetable", "time table", "exam schedule", "examination schedule",
        "exam dates", "when are exams"))

    # Dynamic timetables/notices can be PDFs absent from our fixed page list.
    # Search Tavily first so the model can return the actual official document.
    if is_campus_question and is_timetable_question:
        web_context, web_sources = _tavily_search_context(message, official_first=True)
        if web_context:
            grounded = answer_with_tavily_context(message, web_context, web_sources)
            if grounded:
                return {"response": _clean_model_markdown(grounded), "location": None, "source": "tavily_gzu_search"}

    if is_campus_question:
        live_context = retrieve_official_gzu_context(message)
        if live_context:
            live_answer = answer_from_official_context(message, live_context)
            if live_answer:
                return {"response": _clean_model_markdown(live_answer), "location": None, "source": "official_gzu_website_ai"}

        campus_current_markers = (
            "latest notice", "latest news", "current dean", "dean of",
            "department chair", "chairperson", "lecturer", "staff", "registration deadline",
            "exam timetable", "examination timetable", "fees for", "current fees",
            "who is the", "contact for", "gzu announcement", "school of"
        )
        if any(marker in qnorm for marker in campus_current_markers):
            web_context, web_sources = _tavily_search_context(message, official_first=True)
            if web_context:
                grounded = answer_with_tavily_context(message, web_context, web_sources)
                if grounded:
                    return {"response": _clean_model_markdown(grounded), "location": None, "source": "tavily_gzu_search"}

    general_answer = answer_with_general_gemini(message)
    if general_answer:
        return {"response": _clean_model_markdown(general_answer), "location": None, "source": AI_PROVIDER + "_general"}

    # Safe, concise fallback: never expose raw scraped HTML/page fragments.
    # Provide a clean summary for the known fees-payment topic.
    q = normalize(message)
    if any(term in q for term in ("fee", "fees", "tuition", "pay university", "payment")):
        return {
            "response": (
                "GZU's published fees guidance says 60% of fees is due before "
                "registration and the remaining 40% before end-of-semester exams. "
                "For each payment, 50% of the required amount must be paid in "
                "USD; the other 50% may be paid in USD or ZWG. The exact current "
                "Computer Science fee amount was not verified in the retrieved "
                "official information. Please confirm the amount with GZU Accounts "
                "before paying. Official source: https://www.gzu.ac.zw/fees-structure/"
            ),
            "location": None,
            "source": "verified_fees_fallback",
        }

    # Last resort: use a stored FAQ answer, but do not send scraped page text.
    answer = find_faq_answer(message)
    return {"response": answer or (
        "I'm temporarily unable to reach my AI service. Please try again shortly, "
        "or check the official GZU website: https://www.gzu.ac.zw/"
    ), "location": None, "source": "safe_fallback"}

    # Fall back to the stored FAQ only when live official evidence is unavailable.
    answer = find_faq_answer(message)
    answer = polish_with_gemini(message, answer)
    return {"response": answer, "location": None}


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
# ADMIN PORTAL AND STUDENT FAQ INTEGRATION
# ---------------------------------------------------------

from admin_portal import register_admin_routes

register_admin_routes(app, DB_PATH)


@app.route("/faqs")
def student_faq():
    return render_template("student_faq.html")


# ---------------------------------------------------------
# START FLASK APPLICATION
# ---------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
