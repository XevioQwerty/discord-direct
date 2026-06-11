# downloads/

Static files hosted in the repo so guide embeds can link them directly
(instead of relying on Discord attachments, whose CDN URLs expire).

## How to link a file

Upload the file into this folder, then reference it in an embed with a **raw** URL:

```
https://raw.githubusercontent.com/XevioQwerty/discord-direct/main/downloads/<FILENAME>
```

Clicking that link downloads the file directly.

## Naming rules

- **No spaces** — use underscores (`My_File.zip`, not `My File.zip`). Spaces break URLs.
- Keep names short and descriptive; they show up in the link.

## Limits

- GitHub hard-limits files to **100 MB** (warns at 50 MB). Fine for mods, configs,
  `.ini`, `.dll`, small `.zip`s — **not** for full game repacks (link those externally).

## Current / planned files

| File | Used by guide |
|------|---------------|
| `LEGO_Batman_Engine_No_VRR.zip` | LEGO Batman |
| `LEGO_Batman_Engine_VRR.zip` | LEGO Batman |
| `lego_batman_unlocks.zip` | LEGO Batman |
| `TwelvePlayerExpansion.dll` | Gamble w/ Unlimited Friends |
