# Great Zimbabwe University Campus Assistant

A Flask-based student information chatbot for Great Zimbabwe University (GZU), with an installable Progressive Web App (PWA) interface.

## Main capabilities
- Student-facing campus and university information chat
- General academic questions handled separately from GZU-specific questions
- GZU-focused answers use official university context where available
- Current/dynamic GZU questions can use bounded Tavily web retrieval when configured
- Official-source links are included when retrieved information is used
- Image and document/PDF search intents with a media gallery and clickable links
- November examination timetable discovery/extraction workflow, with honest reporting when official files are unavailable
- Campus directory and directions for listed facilities, including Google Maps links
- PWA app shell and offline page

## Technologies
- Python and Flask
- Gunicorn
- SQLite
- OpenPyXL for Excel timetable processing
- pypdf for PDF extraction
- Groq primary AI provider; Gemini fallback (configured through environment variables)
- Tavily for optional bounded web search

## Project files
- `app.py` — Flask routes, chatbot logic, retrieval, and integrations
- `templates/index.html` — main user interface
- `static/` — PWA manifest, service worker, offline page, and icons
- `campus_directory.json` — campus directory/location data
- `campus_chatbot.db` — SQLite database used by the application
- `requirements.txt` — Python dependencies
- `README-DEPLOY.txt` — setup, deployment, and operational notes
- `TEST-REPORT.txt` — checks performed and limitations

## Campus locations
The campus directory and Compass Locations functionality use `campus_directory.json` and the matching logic in the application. Some entries may be marked unverified; do not treat unverified directions or campus-specific details as confirmed.

## Academic routing and web retrieval
- General academic questions are not restricted to GZU website sources.
- GZU-specific questions use GZU context and official-source retrieval when available.
- Common student shorthand is interpreted only where context makes the subject clear.
- Tavily is optional. If `TAVILY_API_KEY` is not configured, the application continues without Tavily search.
- Timetable responses should rely on verified official documents and must not invent dates, courses, or links.

## Security
Never commit API keys, passwords, or private credentials. Configure secrets in the hosting provider's environment settings. See `README-DEPLOY.txt`.

## Official links
- GZU examination timetable: https://www.gzu.ac.zw/exam-timetable/
- Deployed application: https://gzu-campus-assistant.onrender.com
