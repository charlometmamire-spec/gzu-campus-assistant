GZU CAMPUS ASSISTANT — DEPLOYMENT GUIDE
=======================================

This project is deployed from the existing GitHub repository to Render.
This guide describes the setup; it does not itself change GitHub or Render.

## 1. Install dependencies locally (optional)

Use Python 3.10+ and run:

    pip install -r requirements.txt

## 2. Required/optional environment variables

Set private keys in Render > Environment. Never place them in source files or GitHub.

- AI_PROVIDER=groq
- GROQ_API_KEY=<private Groq key>
- GROQ_MODEL=openai/gpt-oss-120b
- GEMINI_API_KEY=<private Gemini key>  (fallback provider)
- TAVILY_API_KEY=<private Tavily key> (optional web retrieval)

Do not paste any of these keys into chat, documentation, or public commits.

## 3. Render configuration

Keep the existing Render service and the exact start command:

    gunicorn app:app --bind 0.0.0.0:$PORT

Do not change the start command as part of routine source cleanup.

## 4. GitHub update procedure

1. Keep a backup of the current repository before replacing files.
2. Upload the contents of the project folder into the root of the existing repository.
3. For files intentionally removed from this cleaned package, delete those old tracked files in the same GitHub commit (or commit the repository's cleaned folder as a complete replacement).
4. Commit the changes. Render should deploy automatically from the connected repository.
5. Check the deployment logs and test the live app after Render finishes deploying.

## 5. Post-deployment smoke checks

Check:
- https://gzu-campus-assistant.onrender.com/health
- https://gzu-campus-assistant.onrender.com/manifest.json
- https://gzu-campus-assistant.onrender.com/service-worker.js

Then test a greeting, a fee/payment question, admissions, examinations/results, a Mashava-specific question, a campus location/directions request, an unrelated academic question, an image search, and a timetable request.

## 6. Important behavior and limitations

- GZU official information retrieval depends on the university website being reachable and containing relevant information.
- Gemini/Groq availability and quotas are external dependencies.
- Tavily search is optional and quota-limited; avoid unnecessary tests.
- Timetable extraction requires the official downloadable Excel/PDF files to be accessible. If the official site returns broken links or unavailable files, the app should report that instead of inventing an exam schedule.
- The browser PWA caches its app shell. This is not a fully offline hosted chatbot: chat server requests and live retrieval require connectivity.
- If official fee sources conflict (for example, different deposit percentages), the response should flag the conflict and direct the student to confirm with the relevant GZU office.

## 7. Campus locations

The existing campus directory and Compass Locations implementation are preserved. Directory entries marked unverified remain unverified; do not generalize details from one campus to another.

## 8. Security

Never commit `GROQ_API_KEY`, `GEMINI_API_KEY`, `TAVILY_API_KEY`, or other credentials. Rotate any credential that was accidentally exposed in a public repository.
