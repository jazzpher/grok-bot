---
name: ui-preview-pass
description: "When the user asked for a page, landing, component, or to fix a design: write, preview with preview_page, critique against any attached reference, patch one or two issues, preview again, stop at 3 rounds."
---
# UI preview pass

Use when: the user asked for a web page, landing page, component, or "ayusin ang design" — any HTML/CSS/UI work
where looks matter. Needs `preview_page` (GROK_PREVIEW=on) and a vision-capable model.

## Steps
1. Write the HTML/CSS (one file or a small set) with write_file / edit_file.
2. Call `preview_page` on the main file (desktop first).
3. Compare the screenshot against any reference the user attached. List concrete differences:
   spacing, type scale, color, hierarchy, alignment, overflow. Use the layout numbers
   (horizontal_overflow, boxes, contrast) to explain WHY, the pixels for WHETHER it looks right.
4. Patch ONE or TWO issues with edit_file. Do not rewrite everything each pass.
5. `preview_page` again. Repeat until it looks right or you hit the 3-preview budget for the turn.
6. `preview_page` with viewport=mobile when the user asks for mobile.
7. Done means: show the last screenshot and say honestly what you could not fix.
   Never claim a design is finished from reading the CSS text — text is not pixels.

## Pitfalls
- Do not say done without previewing the LATEST version after the last edit.
- Do not rewrite the whole file every pass; fix the top one or two issues.
- Do not trust read_file as a design check.
- External images/fonts/URLs will not load in the preview (it is offline on purpose);
  use local assets or data: URLs, and say so when a reference needs them.

## Verification
- A preview of the final version exists (screenshot + layout).
- horizontal_overflow is false, or you disclosed it.
- The user (or you, from the screenshot) — not the CSS text — confirms the look.
