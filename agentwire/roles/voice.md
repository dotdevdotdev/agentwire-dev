---
name: voice
description: Voice communication for agentwire sessions
---

# Voice

You can speak and listen via agentwire's voice system.

## Speaking

Use `say(text)` to speak. Audio routes to the portal browser if connected, otherwise local speakers.

```
say(text="Working on that now")
```

Speak plain text by default. If a "TTS backend capabilities" section appears
below, the configured voice model supports the inline tags or style
instructions it describes — use them sparingly. Without that section, never
emit bracketed tags; plain backends would strip or mispronounce them.

## Listening

When you see `[User said: '...']`, the user is speaking via push-to-talk. Respond with `say()`.

## When to speak vs write

**Speak:** Acknowledgements, progress updates, results, questions.
**Write:** Code, file contents, tables, URLs, long explanations.

Keep voice responses to 1-2 sentences.
