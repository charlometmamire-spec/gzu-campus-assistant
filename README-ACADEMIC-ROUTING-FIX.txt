GZU CAMPUS ASSISTANT — ACADEMIC ROUTING / TAVILY ANSWER UPDATE

This update addresses observed live-chat behavior:
- General academic questions are routed to the general AI rather than being
  restricted to Great Zimbabwe University website context.
- Common student shorthand (for example, "chem") is explicitly interpreted
  when context makes the intended subject clear.
- GZU-specific questions continue to use official GZU context and Tavily.
- Timetable queries search Tavily before fixed-page retrieval, and the answer
  prompt now asks for the actual official PDF/document link when present.
- Common escaped Markdown artifacts are cleaned before returning the response.
- Groq model and generation parameters are unchanged; Gemini remains backup.

Render environment variables are unchanged. Add/retain TAVILY_API_KEY in Render.
Do not paste API keys into source code or chat.

Validation: Python syntax compile and ZIP integrity test passed. A local Flask
runtime/API smoke test could not be run in this build environment because Flask
is not installed here. Live Tavily and provider tests require deployment with
private Render environment variables.
