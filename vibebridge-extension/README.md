# VibeBridge browser extension

Source of the VibeBridge Chrome/Edge extension (loads unpacked). Forked from
ZeroScript Free — same UI, same providers, same agent loop. Only the
application changed: Roblox Studio → VSCode.

- `core/` — provider-agnostic loop, parser, system prompt, chips, menu.
- `providers/` — DeepSeek, ChatGPT, Gemini, Kimi, GLM, Qwen, Arena, Meta AI.
- `manifest.json` — version 0.1.0, same host permissions as upstream.

Internal plumbing ids/classes/markers (`zs-*`, `⟦ZS-SYS⟧`) are intentionally
unchanged from upstream so behavior stays identical.
