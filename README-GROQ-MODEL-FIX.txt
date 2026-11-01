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
