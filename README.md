# Cold Opener

Paste a company's website. Get a 3-line opener for a cold email to its founder, built **only from what the site says**. Every line comes with the exact phrase from the site it's based on, and the code checks that phrase really exists before showing you the result.

It drafts. It never sends anything.

**Live demo:** https://cold-opener-9dypjckjxszehebajcjttx.streamlit.app/ (free hosting: if it's been idle, it takes ~30 seconds to wake up. If Gemini is busy you'll see a 503 message; try again in a minute.)

```
$ python cli.py simpleanalytics.com
Hi Adriaan,
Simple Analytics measures website traffic when visitors reject consent without using cookies.
Running alongside GA4 gives teams an independent dataset to compare missing traffic.
Are you seeing significant discrepancies in your own setup?

--- based on ---
  "measure website traffic when visitors reject consent"
  "Run Simple Analytics alongside GA4 to compare observed website traffic"
  pages read: https://www.simpleanalytics.com/, https://www.simpleanalytics.com/about, https://www.simpleanalytics.com/pricing
```

More real outputs (full JSON, with evidence and token counts) are in [`examples/`](examples/).

## How it works

```
URL ─► normalize, block private IPs ─► fetch homepage + up to 2 of /about, /pricing… ─► strip HTML, cap at 12K chars (~3K tokens)
                                                                                                  │
                                                                                                  ▼
                                                   Gemini: 3 lines + an exact quote from the site per line (JSON schema)
                                                                                                  │
                                                                                                  ▼
            verify (code): 3 lines · ≤25 words · quotes ≥3 words and found verbatim · no filler phrases ·
                           every capitalised name appears on the site or in your pitch · founder name backed by a quote
                                     │ fail: retry once, telling the model exactly what failed       │ pass
                                     ▼                                                               ▼
                         best attempt + visible warnings                                    opener + evidence
```

| Runs in code (deterministic, free, testable) | Runs in the model (judgment) |
|---|---|
| URL cleanup; refusing private/internal addresses, re-checked on every redirect | Deciding what's interesting about the company |
| Fetching pages with timeouts and a 2 MB cap; choosing About/Pricing links | Writing three lines in a human tone |
| Stripping HTML, de-duplicating, trimming to a fixed token budget | Choosing the phrase each line relies on |
| Checking every output (table above) and one targeted retry | Spotting a founder name, if the site gives one |
| Caching, friendly errors for quota (429) and overload (503), per-visitor demo cap | |

The model only sees the text the code scraped, never the open web, and it never gets the final say: the code checks its claims.

## Examples (real runs, 21 Nov 2026, `gemini-3.5-flash-lite`)

| Site | Opener |
|---|---|
| plausible.io | You built a lightweight and open-source Google Analytics alternative. / Keeping scripts small helps avoid affecting page speed or Core Web Vitals. / How do you handle feature requests while keeping the dashboard simple? |
| pirsch.io | Noticed Pirsch provides a cookieless alternative to Google Analytics with server-side integration options. / Building a privacy-focused analytics SaaS while keeping the core open-source sounds like a great model. / Would you be open to sharing how you handle server-side tracking vs script snippets? |
| carrd.co | You built Carrd for creating simple, fully responsive one-page sites for pretty much anything. / Users can start with dozens of templates or a blank page. / Are you seeing most people stick to the free three-site limit? |
| plausible.io with pitch "we translate marketing sites into Spanish and German" | You built Plausible as a lightweight and open-source Google Analytics alternative. / Growing to over 22,000 paying subscribers brings steady demand for localized versions in Europe. / Do you translate your marketing pages into Spanish or German to capture more European traffic? |

Measured usage: about **3,100 input and 140–170 output tokens per run** (a small one-page site like carrd.co: ~880 input). No run in this set needed the retry.

## Known limitations (seen in the examples above)

- **A quote proves a phrase exists, not that the line is fair.** In the pitch example, "22,000 paying subscribers" is real, but "brings steady demand for localized versions" is the model's inference.
- **Line 3 may state a fact without a quote.** Carrd's "free three-site limit" isn't checked. Capitalised names are checked, but plain facts aren't.
- **Some sites return little or no text:** bot protection (403), or content that only loads with JavaScript. The tool warns when it finds under 400 characters.
- **Gemini sometimes returns 503 "high demand"**, even after the SDK's retries. In testing, 2 of 3 back-to-back attempts failed this way before succeeding.
- The cache lives in memory, so it resets when the app restarts.

## Run it locally

```bash
git clone <this repo> && cd cold-opener
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # paste your free key from https://aistudio.google.com/apikey
python cli.py plausible.io
python cli.py plausible.io --pitch "we translate marketing sites into Spanish and German"
python cli.py plausible.io --json   # full result with evidence and token usage
streamlit run app.py                # web page at http://localhost:8501
```

Tests run offline, with no key and no network: `pip install pytest && pytest -q` (15 tests, including a fake-model test of the retry path).

## Model and cost

Default model: `gemini-3.5-flash-lite` (stable, free tier available). Override it with `GEMINI_MODEL` in `.env`.

Paid price: $0.30 per 1M input tokens and $2.50 per 1M output tokens ([Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing)). At the measured ~3,100 in and ~160 out, that's **about $0.0013 per run, or ~$1.35 per 1,000 runs**. Google doesn't publish fixed free-tier daily limits; it says to check your project's limits in AI Studio ([rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)).

On the free tier, Google may use prompts and outputs to improve its products; on the paid tier it doesn't (same pricing page).

## Deploy the live demo (free)

1. Push this repo to GitHub.
2. Go to [share.streamlit.io](https://share.streamlit.io), click **Create app**, pick the repo, and set the main file to `app.py`.
3. Under **Advanced settings → Secrets**, add `GEMINI_API_KEY = "your-key"`.
4. Deploy. Each visitor gets 15 runs per session, and results are cached for 24 hours.

## Files

- `opener/core.py`: the whole pipeline (fetch → extract → draft → verify), one small function per step
- `cli.py`: command line
- `app.py`: Streamlit web page
- `tests/test_core.py`: offline tests
- `examples/`: real outputs as JSON
