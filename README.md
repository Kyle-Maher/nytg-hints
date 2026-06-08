# nytg-hints

An AI agent that runs daily, fetches the current **NYT Connections** puzzle,
researches dictionary definitions for each of the 16 words, and publishes them
to a GitHub Pages site.

The words are listed in their **original pre-shuffle board order** (the
`position` field in the puzzle data). The site never reveals the groupings or
categories, so it doesn't solve the puzzle or hand out direct hints — it's a
pure vocabulary reference. Where a word has several meanings, every main
definition is included (1–5 senses), even one that might seem misleading for
the puzzle's eventual category.

## How it works

1. **Fetch** — pulls `https://www.nytimes.com/svc/connections/v2/<date>.json`,
   sorts all 16 cards by `position`, and keeps only the words (category titles
   are discarded so nothing can leak).
2. **Research** — Claude (`claude-opus-4-8`) uses the `web_search` tool to look
   up each word and returns concise dictionary definitions as JSON.
3. **Publish** — writes `docs/<date>.html` (a daily page) and refreshes
   `docs/index.html` (the archive), which GitHub Pages serves.

`agent.py` is the whole agent; `.github/workflows/daily.yml` runs it on a cron
schedule and commits the result.

## Setup

1. **Add the API key as a repo secret.** In GitHub → Settings → Secrets and
   variables → Actions → *New repository secret*:
   - Name: `ANTHROPIC_API_KEY`
   - Value: a valid key from <https://platform.claude.com/settings/keys>
2. **Enable GitHub Pages.** Settings → Pages → Source: *Deploy from a branch*,
   Branch: `main`, Folder: `/docs`.
3. **Done.** The workflow runs daily at 09:00 UTC (≈ 4–5 am US/Eastern, after
   the midnight-ET puzzle drop). Trigger a run manually any time from the
   Actions tab ("Run workflow"), optionally passing a specific `YYYY-MM-DD`.

The site lives at `https://<your-username>.github.io/nytg-hints/`.

## Run locally

```bash
pip install -r requirements.txt
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env   # .env is gitignored
python agent.py            # today's puzzle (US/Eastern)
python agent.py 2024-06-01 # a specific date
```

Generated pages go in `docs/`. The script soft-exits if the day's puzzle isn't
published yet, and skips a date that's already been generated.

---

*Not affiliated with or endorsed by The New York Times.*
