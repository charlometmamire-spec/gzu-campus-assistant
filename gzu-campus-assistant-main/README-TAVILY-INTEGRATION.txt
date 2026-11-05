GZU CAMPUS ASSISTANT — TAVILY WEB SEARCH INTEGRATION

This build adds Tavily Search API as a bounded web-retrieval layer.

Render environment variable to add (do not put the key in source code):
  TAVILY_API_KEY = your Tavily API key

Existing environment variables remain unchanged:
  AI_PROVIDER=groq
  GROQ_API_KEY=... (keep secret)
  GEMINI_API_KEY=... (keep secret)

Behavior:
- Groq remains primary, using openai/gpt-oss-120b.
- Groq generation uses temperature 1, max_completion_tokens 2048, top_p 1,
  reasoning_effort medium, and stream=true (server consumes SSE internally).
- Gemini remains the fallback provider.
- Tavily is used for GZU dynamic/current questions that fixed official-page
  retrieval cannot answer, and for explicit/latest web-search requests.
- Official GZU domain results are preferred for campus questions.
- Responses append source titles and URLs when search results were used.
- Tavily search timeout is bounded at 4.5 seconds; failures do not crash chat.
- No Tavily key means existing app behavior continues without web retrieval.

Deployment:
1. In Render > gzu-campus-assistant > Environment, add TAVILY_API_KEY.
2. Save changes and allow Render to redeploy.
3. Keep the existing Start Command exactly:
   gunicorn app:app --bind 0.0.0.0:$PORT
4. Test a current GZU question and a general question. Look for "Sources (Tavily)"
   on answers where web search was used.

Security: never paste API keys into chat, source files, or public Git commits.
