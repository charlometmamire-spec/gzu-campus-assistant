GZU CAMPUS ASSISTANT — RELEASE PACKAGE
=====================================

This package is intended to replace the project files in the existing GitHub
repository, then let the connected Render service deploy from GitHub.
It does not upload anything to GitHub or change Render settings for you.

BEFORE DEPLOYING
----------------
1. Download/keep a backup of the current GitHub repository.
2. Extract this ZIP. Open the folder named gzu-campus-assistant-main.
3. Upload/replace the files and folders inside that folder in the ROOT of the
   existing GitHub repository. Keep the same repository and Render service.
4. Do not delete the existing campus_chatbot.db or campus_directory.json.
   The supplied copies are included for a complete package, but if the live
   repository has newer records, keep those live copies instead.
5. Commit the changes. Render should deploy automatically from GitHub.
6. Do not put GEMINI_API_KEY in any file or in GitHub.

RENDER SETTINGS
---------------
AI PROVIDER ORDER
------------------
- Primary AI: Groq (AI_PROVIDER=groq; GROQ_API_KEY; GROQ_MODEL defaults to
  openai/gpt-oss-120b). Keep Groq as the primary provider.
- Secondary AI: Gemini (GEMINI_API_KEY; GEMINI_MODEL optional). Gemini is only
  called if Groq fails. Keep the existing Gemini key private.
- Web search: Tavily (TAVILY_API_KEY). This is a search service, NOT an AI
  provider. It supplies ranked snippets and source URLs to Groq/Gemini for
  questions needing current information.

TAVILY FREE TIER
-----------------
Tavily Researcher currently includes 1,000 search credits per month and does
not require a credit card (verify current terms at https://www.tavily.com/pricing).
The code uses basic search (1 credit per query), max 4 results, and a 4-second
network timeout. If TAVILY_API_KEY is absent, search is skipped and the rest of
the assistant continues normally. When credits are exhausted, Tavily requests
may stop; the app does not automatically upgrade or invoke paid billing.

RENDER ENVIRONMENT
------------------
Keep existing GROQ_API_KEY and GEMINI_API_KEY as-is. Add TAVILY_API_KEY only
after you create a free Tavily account and copy its key into Render. Do not
paste keys into chat, source code, README files, or GitHub. Keep the Render
start command unchanged: gunicorn app:app --bind 0.0.0.0:$PORT

AFTER DEPLOYMENT: CHECK THESE URLS
---------------------------------
https://gzu-campus-assistant.onrender.com/health
https://gzu-campus-assistant.onrender.com/manifest.json
https://gzu-campus-assistant.onrender.com/service-worker.js

Then test the chat with:
- a greeting (e.g. Hello)
- a fee/payment question
- an admissions question
- an examination/results question
- a Mashava-specific question
- a campus directory place name and a directions question
- an unrelated question

LIVE OFFICIAL INFORMATION
-------------------------
For GZU-specific questions, the app selects relevant pages from the official
GZU website (www.gzu.ac.zw) and the official GZU e-learning domain
(elearning.gzu.ac.zw), extracts page text, and adds a bounded Tavily search
for current questions (e.g. latest notices or exam timetables). Groq answers
first; Gemini is the backup. The model is instructed to ground its answer in
retrieved evidence. The notices page is included to help surface current notices.
Retrieved page text is cached in memory for 15 minutes to reduce repeated
requests. This is live website retrieval, not a guarantee that the GZU site is
reachable or that every answer is present on the site. If Gemini is unavailable but official page text was retrieved, the app now
shows relevant source extracts directly (with their official URLs) instead of
discarding the live evidence. If the website itself is unreachable or returns
no relevant text, the existing FAQ fallback is used. Where retrieved fee
extracts contain both 60% and 75% deposit figures, the answer explicitly flags
the conflict and directs students to confirm with the GZU Bursar/Accounts
Office. Confirm changing fees, deadlines, and personal account matters with
the relevant official GZU office/portal.

CAMPUS LOCATIONS
----------------
The existing campus directory data and matching functions are preserved. The
location lookup is kept separate and runs before live website/Gemini handling.
Directory entries marked unverified remain explicitly identified as such.
Do not treat a Mashava-specific detail as applying to another campus.

OFFLINE BEHAVIOUR
-----------------
The service worker caches the app shell/static files. The stored FAQ database
can answer matching questions while the app server is running, but live
website retrieval and Gemini require an internet connection. This is not a
fully offline hosted chatbot: the browser cannot contact the Render server
when the device has no connection.

SECURITY
--------
Never commit API keys. The app only queries a fixed list of official GZU URLs
and rejects redirects outside the official GZU domains. GROQ_API_KEY is sent only to Groq, GEMINI_API_KEY only to Google when backup
is needed, and TAVILY_API_KEY only to Tavily for web search. Search results are
untrusted external content and are provided as evidence, not instructions.
