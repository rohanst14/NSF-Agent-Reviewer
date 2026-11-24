# NSF Reviewer App

Local multi-agent NSF proposal reviewer with:

- `1` compliance agent
- `3` independent full reviewer agents
- panel discussion rounds
- final consensus synthesis

## Run

```bash
python3 nsf_reviewer_server.py
```

Then open:

```text
http://127.0.0.1:8000
```

## Notes

- API requests are sent from the local Python server, not directly from the browser.
- That removes the direct browser CORS problem in the old HTML prototype.
- You can provide either an Anthropic key, an OpenAI key, or both.
- You can provide a Google Gemini API key and use Gemini's free tier.
- You can also use `Ollama` locally with no key, or provide an Ollama API key for Ollama cloud.
- Each agent can use `auto`, `anthropic`, `openai`, `gemini`, or `ollama`.
- If a model field is blank, the backend uses a default model for the selected provider.
- PDF extraction uses PDF.js in the browser. If that CDN is unavailable, paste text manually.

## Free Cloud Option

If you want a free cloud-backed option:

1. Create a Gemini API key in Google AI Studio.
2. Put the key in the `Google Gemini API key` field.
3. Set the agents to `Gemini` or leave them on `Auto`.

## Free Local Option

If you want to run without paid API keys:

1. Install Ollama.
2. Pull a local model such as `gemma3`.
3. Start Ollama.
4. In the app, set the agents to `Ollama`.
5. Leave the paid API key fields blank.

## Ollama Cloud Option

If you have an Ollama API key:

1. Put it in the `Ollama API key` field.
2. Set one or more agents to `Ollama`.
3. If the key is present, the backend uses `https://ollama.com/api/generate`.
4. If the key is blank, the backend uses local Ollama at `http://localhost:11434`.

## Files

- [nsf_reviewer_server.py](/Users/rohan/Library/CloudStorage/OneDrive-TexasA&MUniversity/Agent/nsf_reviewer_server.py)
- [static/index.html](/Users/rohan/Library/CloudStorage/OneDrive-TexasA&MUniversity/Agent/static/index.html)
- [static/styles.css](/Users/rohan/Library/CloudStorage/OneDrive-TexasA&MUniversity/Agent/static/styles.css)
- [static/app.js](/Users/rohan/Library/CloudStorage/OneDrive-TexasA&MUniversity/Agent/static/app.js)
