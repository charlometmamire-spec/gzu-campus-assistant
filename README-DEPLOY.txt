# GZU Campus Assistant — Consolidated Update Package

## Included files
- `app.py` — consolidated routing for greetings, general GZU questions, student services, conduct information, campus locations, and separate unrelated-question handling.
- `templates/index.html` — preserves the existing green interface and adds useful starter questions for About GZU, Explore GZU, and Conduct Information.
- `static/manifest.json` — PWA metadata.
- `static/service-worker.js` — caches the app shell/static assets and deliberately does not cache POST `/chat` responses.
- `static/icons/icon-192.png` and `static/icons/icon-512.png` — app icons.
- `static/offline.html` — optional standalone offline information page.

## IMPORTANT deployment notes
1. Back up the current `app.py` and `templates/index.html` in GitHub before replacing them.
2. Replace `app.py` with the package version.
3. Replace `templates/index.html` with the package version.
4. In the existing `static` folder, add/replace `manifest.json` and `service-worker.js`, add the `icons` folder with both PNG files, and optionally add `offline.html`.
5. **Keep the current `campus_chatbot.db`, `campus_directory.json`, and `requirements.txt` unchanged.** The package intentionally does not include replacement copies of those files.
6. Wait for Render to deploy, then check `/health`, `/manifest.json`, and `/service-worker.js`, and try the starter prompts.

## Scope and honest limitations
- The “Explore GZU” prompt links users to the official GZU website and explains that a verified photo gallery is not currently integrated. No unverified image URLs were invented.
- Student services and conduct responses direct users to official information because a complete, verified service list and current conduct regulations were not confirmed in this package.
- The existing campus directory may contain entries marked unverified; the UI/backend should continue to disclose that limitation.
- A PWA can cache the interface, but chatbot answers still need a network connection. The service worker does not cache dynamic chat answers.
- A full runtime smoke test could not be completed in this environment because Flask is not installed here. Run the final check on Render after deployment.


## Optional Gemini answer formatting (free-tier API, subject to Google's quotas)
The app works without Gemini and returns the original FAQ answer. To enable AI
formatting in Render, add this environment variable in Render → Service →
Environment:

- `GEMINI_API_KEY` = your key from Google AI Studio
- Optional `GEMINI_MODEL` = `gemini-2.5-flash` (default)

Gemini is only used to rephrase an answer already found in the FAQ database. It
is not allowed to add facts. Campus-location results and curated responses do
not pass through Gemini. If the key is absent, quota is exceeded, or the API is
unavailable, the original answer is returned. Never commit the API key to GitHub.

## Icon replacement
The generated GZU robot icon is supplied as `static/icons/icon-192.png` and
`static/icons/icon-512.png`, retaining the exact filenames referenced by the
existing PWA manifest. The service-worker cache was bumped to v2 so deployed
browsers can refresh the static assets.
