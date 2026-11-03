GROQ 403 / 1010 RESILIENCE PATCH

- Groq remains the primary provider when AI_PROVIDER=groq.
- Preserves model openai/gpt-oss-120b and generation parameters: temperature=1,
  max_completion_tokens=2048, top_p=1, reasoning_effort=medium, stream=true.
- Adds an explicit application User-Agent to the Groq HTTP request because the
  Groq edge may reject urllib's default Python User-Agent (HTTP 403/code 1010).
- If Groq still rejects the request, existing Gemini backup flow remains in place.
- Compass Locations code/data/routing were not modified.

Validation: app.py compiles; archive integrity checked. No live Render test performed.
