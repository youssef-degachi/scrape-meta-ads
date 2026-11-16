# How it works

**Input:** a Facebook (Meta) Ads Library URL.
**Output:** a list of every advertiser in that search, with their contact info, in a local website, Excel, CSV, JSON and a `leads.md` file.

Written for both humans and AI agents. Quick facts for an agent are at the bottom.

## Flow

```
Ads Library URL
      │
      ▼
1. scrape_ads.py   open Chromium (Playwright), scroll the page until no more ads load,
                   capture the GraphQL responses Facebook sends (they contain every ad)
      │
      ▼
2. normalize       per ad: page name, page URL/ID, text, headline, CTA, link,
                   images, dates, platforms + phones, WhatsApp numbers, emails
                   (regex + `phonenumbers`, using the URL's `country=` as region)
      │
      ▼
3. group           ads grouped by advertiser page -> one row per business
      │
      ▼
4. leads.py        enrich each advertiser: read the Facebook page "Intro" box
                   and the website home page -> website, Instagram, WhatsApp, phone, email
      │
      ▼
5. write files     data/<name>/ ads.json pages.json ads.csv pages.csv <name>.xlsx
                   leads.md leads.json meta.json scrape.log
      │
      ▼
6. serve.py        local website (web/index.html) at http://localhost:8000
                   browse leads, tick "contacted" (row turns gray), download Excel / leads.md
```

## Use it

Website (easiest):

```
./start.sh                  # then open http://localhost:8000
```
Paste the Ads Library URL, click **Start scrape**, wait for `done`, click **Open**.

Command line:

```
.venv/bin/python scrape_ads.py "<ads library url>" --name my_collection
.venv/bin/python leads.py my_collection        # rebuild leads.md (or --all)
```
Always quote the URL. Flags: `--max-ads N`, `--headed` (show browser), `--no-enrich` (faster), `--deep` (read each page's About tab, slow).

## Files

| File | Job |
|---|---|
| `scrape_ads.py` | Browser scrape, ad parsing, contact extraction, CSV/XLSX/JSON output |
| `leads.py` | Per-advertiser enrichment (Facebook intro + website) and `leads.md` |
| `serve.py` | Local HTTP server and JSON API for the website |
| `web/index.html` | The lead-tracker UI |
| `start.sh` | Starts the server (stops an old copy on the same port) |
| `data/` | All results. Git-ignored. `data/contacted.json` stores the ticked leads |

## API (serve.py)

- `GET /api/collections` list scrapes
- `GET /api/data?...` ads and pages of one scrape
- `GET/POST /api/contacted` read / save the contacted ticks
- `POST /api/scrape` start a scrape from a URL
- `GET /files/<collection>/<file>` download a result file

## Notes for agents

- Python 3.11 venv at `.venv`, deps in `requirements.txt`, then `python -m playwright install chromium`.
- No login or API key needed. Facebook may change its GraphQL shape; if ads come back empty, run with `--headed --debug-dir dumps` and inspect.
- `data/`, `*.csv`, `*.xlsx`, logs are git-ignored on purpose: scraped contacts must not be committed.
- Only scrape public Ads Library data, respect Facebook's terms and local privacy/spam laws when contacting leads.
