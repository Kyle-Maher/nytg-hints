#!/usr/bin/env python3
"""Daily NYT Connections definition agent.

Fetches the day's NYT Connections puzzle, researches dictionary definitions for
each of the 16 words using Claude + web search, and writes a GitHub Pages site.

The site never reveals the puzzle's groupings/categories. Words are listed in
their original pre-shuffle board order (the `position` field in the NYT JSON),
so it gives no head start on solving — it is purely a vocabulary reference.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

# --- Configuration -----------------------------------------------------------

# Haiku is plenty for dictionary definitions and ~5x cheaper than Opus per token.
# Bump to "claude-sonnet-4-6" (and set EFFORT) if you want richer definitions.
MODEL = "claude-haiku-4-5"
# Thinking/effort meaningfully raise cost and aren't needed to define words.
# `effort` ONLY works on Opus 4.5+/Sonnet 4.6 — it 400s on Haiku, so leave it
# None unless you also switch MODEL to an Opus/Sonnet model.
THINKING = {"type": "disabled"}
EFFORT = None  # e.g. "medium" when using Sonnet/Opus
NYT_URL = "https://www.nytimes.com/svc/connections/v2/{date}.json"
DOCS = Path(__file__).resolve().parent / "docs"
MANIFEST = DOCS / "days.json"
ET = ZoneInfo("America/New_York")
MAX_TOOL_ROUNDS = 12  # safety cap on the server-side web_search loop


# --- Local .env loading (no-op in CI, where the key is a real env var) -------

def load_env() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# --- Step 1: fetch the puzzle ------------------------------------------------

def et_today() -> str:
    """Today's date in US/Eastern, where the NYT publishes the puzzle."""
    return dt.datetime.now(ET).strftime("%Y-%m-%d")


def fetch_words(date: str) -> list[str]:
    """Return the 16 words in their original pre-shuffle board order.

    Each card carries a ``position`` (0-15) = its slot on the initial board.
    We sort every card across every category by that position. We deliberately
    ignore the category titles so the published site can never leak groupings.
    """
    url = NYT_URL.format(date=date)
    req = urllib.request.Request(url, headers={"User-Agent": "nytg-hints/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)

    cards = []
    for category in data.get("categories", []):
        for card in category.get("cards", []):
            cards.append((card["position"], card["content"]))

    if len(cards) != 16:
        raise RuntimeError(f"Expected 16 cards for {date}, got {len(cards)}")

    cards.sort(key=lambda c: c[0])
    return [content for _, content in cards]


# --- Step 2: research definitions --------------------------------------------

SYSTEM_PROMPT = """\
You are a lexicographer assembling a plain dictionary reference. You will be \
given a list of words. Research each word with the web_search tool and report \
its main dictionary definitions.

Rules:
- Provide between 1 and 5 of the word's primary senses, depending on how many \
distinct common meanings the word genuinely has. A simple word may have one; a \
word like "press" or "current" may have several.
- Include every sense that is a MAIN definition of the word, even one that \
might seem unrelated or misleading in some context. Do not omit a primary \
meaning because it seems off-topic.
- Each definition is one concise, dictionary-style sentence. Note the part of \
speech where helpful (e.g. "(verb)").
- Treat every word completely independently. Do NOT group the words, do NOT \
mention or hint at any category, theme, puzzle, game, or connection between \
them. You are only defining words.
- You already know the standard dictionary meanings of common English words. \
Only use the web_search tool for a word you are genuinely unsure about — \
slang, proper nouns, brand names, or rare/technical terms. Do not search for \
ordinary words.
- Write each definition as plain text only. Do NOT include citation tags, \
footnote markers, source references, HTML, or any other markup.
- Return ONLY a JSON object, no prose before or after."""

USER_TEMPLATE = """\
Define these words. Return a JSON object of exactly this shape:

{{"words": [{{"word": "<the word>", "definitions": ["<sense 1>", "<sense 2>"]}}, ...]}}

Keep the words in the same order I give them. Words:
{word_list}"""

# Cap searches so a bad loop can't run up the bill; most days use 0–3.
# `allowed_callers=["direct"]` disables the code-execution dynamic-filtering path
# (which Haiku can't do) so the model calls web search directly.
WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 6,
    "allowed_callers": ["direct"],
}


def _extract_text(content) -> str:
    return "".join(b.text for b in content if b.type == "text")


_TAG_RE = re.compile(r"<[^>]+>")  # strips <cite ...>, </cite>, and any stray HTML


def _clean(text: str) -> str:
    """Remove citation/markup tags the model may emit from web-search results."""
    return re.sub(r"\s+", " ", _TAG_RE.sub("", text)).strip()


def _parse_json(text: str) -> dict:
    text = text.strip()
    # Strip an optional ```json ... ``` fence.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in model output:\n{text[:500]}")
    return json.loads(text[start:end + 1])


def research_definitions(words: list[str]) -> list[dict]:
    """Ask Claude to research each word; return [{word, definitions}] in order."""
    client = anthropic.Anthropic()
    word_list = "\n".join(f"{i + 1}. {w}" for i, w in enumerate(words))
    messages = [{"role": "user", "content": USER_TEMPLATE.format(word_list=word_list)}]

    base_kwargs = dict(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        tools=[WEB_SEARCH_TOOL],
    )
    if THINKING is not None:
        base_kwargs["thinking"] = THINKING
    if EFFORT is not None:
        base_kwargs["output_config"] = {"effort": EFFORT}

    response = None
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(messages=messages, **base_kwargs)
        if response.stop_reason == "pause_turn":
            # Server-side web_search loop hit its per-response cap; resume.
            messages.append({"role": "assistant", "content": response.content})
            continue
        break

    if response is None:
        raise RuntimeError("No response from the model")

    parsed = _parse_json(_extract_text(response.content))

    # Re-map onto the canonical board order, matching by word (case-insensitive)
    # so a model reorder can't desync definitions from positions.
    by_word = {}
    for item in parsed.get("words", []):
        defs = [_clean(d) for d in item.get("definitions", []) if d and d.strip()]
        defs = [d for d in defs if d]
        by_word[item.get("word", "").strip().upper()] = defs

    entries = []
    for word in words:
        defs = by_word.get(word.strip().upper())
        if not defs:
            defs = ["(definition unavailable)"]
        entries.append({"word": word, "definitions": defs[:5]})
    return entries


# --- Step 3: render the site -------------------------------------------------

PAGE_CSS = "style.css"


def _human_date(date: str) -> str:
    return dt.datetime.strptime(date, "%Y-%m-%d").strftime("%A, %B %-d, %Y")


def render_day_page(date: str, entries: list[dict]) -> None:
    rows = []
    for i, entry in enumerate(entries, start=1):
        defs = "".join(
            f"<li>{html.escape(d)}</li>" for d in entry["definitions"]
        )
        rows.append(
            f'<li class="word">'
            f'<span class="num">{i}</span>'
            f'<div><h2>{html.escape(entry["word"])}</h2>'
            f'<ul class="defs">{defs}</ul></div></li>'
        )
    body = "\n".join(rows)
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connections word definitions — {date}</title>
<link rel="stylesheet" href="{PAGE_CSS}">
</head>
<body>
<main>
<p class="back"><a href="index.html">← All days</a></p>
<h1>Word definitions</h1>
<p class="sub">{_human_date(date)}</p>
<p class="note">Definitions for each word in today's NYT Connections, in the
order the words appear on the board before shuffling. Groupings are not
revealed — this is a vocabulary reference, not a solution.</p>
<ol class="words">
{body}
</ol>
<footer>Definitions researched by an AI agent. Not affiliated with The New York Times.</footer>
</main>
</body>
</html>
"""
    (DOCS / f"{date}.html").write_text(page, encoding="utf-8")


def update_index() -> None:
    dates = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else []
    dates = sorted(set(dates), reverse=True)
    items = "\n".join(
        f'<li><a href="{d}.html">{_human_date(d)}</a></li>' for d in dates
    )
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NYT Connections word definitions</title>
<link rel="stylesheet" href="{PAGE_CSS}">
</head>
<body>
<main>
<h1>Connections word definitions</h1>
<p class="note">A daily AI-researched dictionary of every word in the NYT
Connections puzzle, listed in pre-shuffle board order. No groupings, no
spoilers — just definitions.</p>
<ul class="archive">
{items}
</ul>
<footer>Definitions researched by an AI agent. Not affiliated with The New York Times.</footer>
</main>
</body>
</html>
"""
    (DOCS / "index.html").write_text(page, encoding="utf-8")


def record_day(date: str) -> None:
    dates = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else []
    if date not in dates:
        dates.append(date)
    MANIFEST.write_text(json.dumps(sorted(set(dates), reverse=True), indent=2))


def write_static_assets() -> None:
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("")
    css = DOCS / PAGE_CSS
    if not css.exists():
        css.write_text(STYLE_CSS, encoding="utf-8")


STYLE_CSS = """\
:root { --bg:#fbfbf9; --fg:#1a1a1a; --muted:#666; --accent:#5a67d8; --card:#fff; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
main { max-width:46rem; margin:0 auto; padding:2.5rem 1.25rem 4rem; }
h1 { font-size:1.9rem; margin:0 0 .25rem; letter-spacing:-.02em; }
.sub { color:var(--muted); margin:.1rem 0 1.25rem; font-size:1.05rem; }
.note { color:var(--muted); background:#f0f0ec; border-radius:.6rem;
  padding:.8rem 1rem; font-size:.92rem; margin:0 0 1.75rem; }
.back a, .archive a { color:var(--accent); text-decoration:none; }
.back a:hover, .archive a:hover { text-decoration:underline; }
.back { margin:0 0 1rem; font-size:.9rem; }
ol.words { list-style:none; margin:0; padding:0; }
li.word { display:flex; gap:1rem; background:var(--card); border:1px solid #ececec;
  border-radius:.7rem; padding:1rem 1.1rem; margin:0 0 .75rem; }
li.word .num { flex:0 0 1.75rem; height:1.75rem; border-radius:50%;
  background:var(--accent); color:#fff; font-weight:600; font-size:.85rem;
  display:flex; align-items:center; justify-content:center; }
li.word h2 { font-size:1.15rem; margin:.1rem 0 .4rem; letter-spacing:.01em; }
ul.defs { margin:0; padding-left:1.1rem; color:#333; }
ul.defs li { margin:.15rem 0; }
ul.archive { list-style:none; padding:0; margin:0; }
ul.archive li { padding:.55rem 0; border-bottom:1px solid #ececec; }
footer { margin-top:2.5rem; color:var(--muted); font-size:.8rem;
  border-top:1px solid #ececec; padding-top:1rem; }
"""


# --- Orchestration -----------------------------------------------------------

def main() -> int:
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    date = sys.argv[1] if len(sys.argv) > 1 else et_today()
    write_static_assets()

    if (DOCS / f"{date}.html").exists():
        print(f"{date} already generated; nothing to do.")
        return 0

    print(f"Fetching Connections for {date} ...")
    try:
        words = fetch_words(date)
    except Exception as exc:  # puzzle may not be published yet
        print(f"Could not fetch puzzle for {date}: {exc}", file=sys.stderr)
        return 0  # soft-exit so the daily job doesn't fail spuriously

    print(f"Researching {len(words)} words ...")
    entries = research_definitions(words)

    render_day_page(date, entries)
    record_day(date)
    update_index()
    print(f"Wrote docs/{date}.html and refreshed the index.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
