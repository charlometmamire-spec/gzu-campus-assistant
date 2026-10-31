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
The app reads GEMINI_API_KEY from the Render environment. You previously
configured this variable; keep it private and do not send it in chat. The
optional GEMINI_MODEL defaults to gemini-2.5-flash. No code change is needed
for the key itself. If the key is missing, invalid, or over quota, the app
falls back to its stored FAQ response rather than crashing.

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
For general university questions, the app selects relevant pages from the
official GZU website (www.gzu.ac.zw) and the official GZU e-learning domain
(elearning.gzu.ac.zw), extracts page text, and asks Gemini to answer only from
those extracts. The notices page is included to help surface current notices.
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
and rejects redirects outside the official GZU domains. The key is sent only
to Google's Gemini API when an answer is requested.
