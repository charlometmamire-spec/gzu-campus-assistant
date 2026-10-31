from pathlib import Path
import json
import re
import sqlite3
import os
import urllib.request
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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()


def polish_with_gemini(question, grounded_answer):
    if not GEMINI_API_KEY or not grounded_answer:
        return grounded_answer

    # Never send campus-location responses to the generative model. This keeps
    # the carefully working location feature entirely untouched.
    if isinstance(grounded_answer, dict) or not isinstance(grounded_answer, str):
        return grounded_answer

    if grounded_answer == unknown_answer():
        return grounded_answer


    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + quote_plus(GEMINI_MODEL)
        + ":generateContent?key=" + quote_plus(GEMINI_API_KEY)
    )
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
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
        candidates = result.get("candidates", [])
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        polished = "\n".join(
            str(part.get("text", "")).strip() for part in parts
            if part.get("text")
        ).strip()
        return polished if polished else grounded_answer
    except Exception as exc:
        app.logger.info("Gemini polishing unavailable; using verified answer: %s", exc)
        return grounded_answer


# ---------------------------------------------------------
# LIVE OFFICIAL GZU WEBSITE LOOKUP (best-effort, cached per request)
# ---------------------------------------------------------
from html.parser import HTMLParser
import time

OFFICIAL_GZU_PAGES = [
    "https://www.gzu.ac.zw/",
    "https://www.gzu.ac.zw/category/notices/",
    "https://www.gzu.ac.zw/admissions/",
    "https://www.gzu.ac.zw/registration/",
    "https://www.gzu.ac.zw/fees-structure/",
    "https://www.gzu.ac.zw/examinations/",
    "https://www.gzu.ac.zw/undergraduate-programmes/",
    "https://www.gzu.ac.zw/documents/",
    "https://www.gzu.ac.zw/contact-us-2/",
    "https://elearning.gzu.ac.zw/accepted/",
]

class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.skip=0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header"):
            self.skip += 1
        if tag in ("p", "li", "h1", "h2", "h3", "br") and not self.skip:
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header") and self.skip:
            self.skip -= 1
    def handle_data(self, data):
        if not self.skip and data.strip(): self.parts.append(data.strip())

def retrieve_official_gzu_context(question, max_pages=4):
    """Fetch official GZU pages only. Returns short relevance-ranked context or empty."""
    qwords = {w for w in normalize(question).split() if len(w) > 2}
    if not qwords: return ""
    collected=[]
    for url in OFFICIAL_GZU_PAGES:
        try:
            req=urllib.request.Request(url, headers={"User-Agent":"GZU-Campus-Assistant/1.0 (official-site lookup)"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if "text/html" not in resp.headers.get("Content-Type", ""): continue
                raw=resp.read(900_000).decode("utf-8", "ignore")
            parser=_VisibleTextParser(); parser.feed(raw)
            text=re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
            words=set(normalize(text).split())
            score=len(qwords & words)
            if score:
                # Keep bounded evidence, avoid flooding Gemini with entire pages.
                chunks=[]
                for sentence in re.split(r"(?<=[.!?])\s+", text):
                    if len(qwords & set(normalize(sentence).split())) >= 1:
                        chunks.append(sentence[:700])
                snippet=" ".join(chunks[:8])[:3500] or text[:1800]
                collected.append((score, url, snippet))
        except Exception as exc:
            app.logger.debug("Official GZU page lookup failed for %s: %s", url, exc)
    collected.sort(key=lambda x:x[0], reverse=True)
    return "\n\n".join(f"Source: {url}\nExtract: {snippet}" for _,url,snippet in collected[:max_pages])


def answer_from_official_context(question, context):
    if not GEMINI_API_KEY or not context: return ""
    endpoint=("https://generativelanguage.googleapis.com/v1beta/models/"+
             quote_plus(GEMINI_MODEL)+":generateContent?key="+quote_plus(GEMINI_API_KEY))
    prompt=("You are the GZU Campus Assistant. Answer the student's question using ONLY "
           "the official GZU website excerpts below. Do not guess or use outside knowledge. "
           "If excerpts do not answer it, clearly say the website information found is insufficient "
           "and direct the student to the relevant official office/page. Keep answer concise. "
           "Include the source URL(s) used. Do not assume campus-specific facts apply to every campus.\n\n"
           f"Question: {question}\n\nOfficial excerpts:\n{context}")
    payload={"contents":[{"parts":[{"text":prompt}]}],
             "generationConfig":{"temperature":0.1,"maxOutputTokens":600}}
    try:
        req=urllib.request.Request(endpoint,data=json.dumps(payload).encode(),
              headers={"Content-Type":"application/json"},method="POST")
        with urllib.request.urlopen(req,timeout=12) as resp: result=json.loads(resp.read().decode())
        candidates=result.get("candidates",[])
        parts=candidates[0].get("content",{}).get("parts",[]) if candidates else []
        answer="\n".join(p.get("text","").strip() for p in parts if p.get("text")).strip()
        return answer
    except Exception as exc:
        app.logger.info("Official-site Gemini answer unavailable: %s",exc)
        return ""

# ---------------------------------------------------------
# ANSWER ROUTING
# ---------------------------------------------------------

def find_answer(message):
    if not normalize(message):
        return {
            "response": "Please type a question so I can help.",
            "location": None,
        }

    # Handle named campus topics first.
    curated = general_gzu_answer(message)
    if curated:
        return curated

    # Then check whether the user named a directory location.
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
            choices_text = ", ".join(names)
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

    if is_clearly_off_topic(message):
        return {
            "response": (
                "I’m the GZU Campus Assistant, designed to help with Great "
                "Zimbabwe University information, campus facilities, student "
                "processes and university services. Please ask a GZU-related "
                "question and I’ll do my best to help."
            ),
            "location": None,
        }

    # Prefer current official GZU website excerpts for general questions.
    # Campus-location routing above is intentionally untouched.
    live_context = retrieve_official_gzu_context(message)
    live_answer = answer_from_official_context(message, live_context)
    if live_answer:
        return {"response": live_answer, "location": None, "source": "official_gzu_website"}

    # Fall back to the stored FAQ if live site/Gemini is unavailable.
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
