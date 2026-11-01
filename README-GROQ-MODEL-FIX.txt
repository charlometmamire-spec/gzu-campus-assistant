GZU CAMPUS ASSISTANT - GROQ MODEL UPDATE

This package changes the default Groq model to:
openai/gpt-oss-120b

It also logs a short Groq error response body if an API call fails, to make diagnosis clearer.

Render environment settings:
AI_PROVIDER=groq
GROQ_API_KEY=<your private Groq key>
GROQ_MODEL=openai/gpt-oss-120b

Do not publish or share GROQ_API_KEY.
Deployment start command remains:
gunicorn app:app --bind 0.0.0.0:$PORT


NETWORK DIAGNOSTIC UPDATE (User-Agent)
The Groq request now sends a descriptive User-Agent header. This is a targeted test for HTTP 403 / Cloudflare error 1010. It is not guaranteed to resolve an edge/network block. If 1010 persists, the Render outbound network/IP may be blocked upstream; changing API keys or models is unlikely to fix that.
