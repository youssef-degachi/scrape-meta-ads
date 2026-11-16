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

## What the result looks like

All names, numbers and links below are made up.

**1. The website** (`http://localhost:8000`): one row per advertiser. Tick the box after you contact them and the row turns gray (saved in `data/contacted.json`).

| Done | Business | Ads | Website | WhatsApp | Phone | Facebook |
|---|---|---|---|---|---|---|
| [ ] | Sunrise Dental | 4 | sunrisedental.example | +965 5000 0000 | +965 2200 0000 | facebook.com/SunriseDental |
| [x] | Bright Smile Clinic | 2 | brightsmile.example | | +965 2255 0000 | facebook.com/BrightSmile |

**2. `leads.md`**: one block per advertiser, ready to copy into a CRM or notes app.

````
# Sunrise Dental

```
Website: https://sunrisedental.example/


Facebook: https://www.facebook.com/SunriseDental/


Instagram: https://www.instagram.com/sunrisedental


Whatsapp: +96550000000

Phone: +96522000000

Other:
https://www.tiktok.com/@sunrisedental
info@sunrisedental.example
Video:
```
````

**3. `pages.json`**: one object per advertiser (also exported as `pages.csv` and the Excel sheet).

```json
{
  "page_id": "262170691438369",
  "page_name": "Sunrise Dental",
  "page_url": "https://www.facebook.com/SunriseDental/",
  "page_likes": 6170,
  "page_categories": ["Dentist & Dental Office"],
  "instagram_handle": "sunrisedental",
  "ads_count": 4,
  "whatsapp_numbers": ["+96550000000"],
  "phone_numbers": ["+96522000000"],
  "emails": ["info@sunrisedental.example"],
  "external_links": ["https://sunrisedental.example/"]
}
```

**4. `ads.json`**: one object per ad, with the full detail.

```json
{
  "ad_archive_id": "1516871603363521",
  "ad_library_url": "https://www.facebook.com/ads/library/?id=1516871603363521",
  "page_name": "Sunrise Dental",
  "is_active": true,
  "start_date": "2025-10-03",
  "platforms": ["FACEBOOK", "INSTAGRAM"],
  "display_format": "VIDEO",
  "title": "Free first check-up this month",
  "body": "Book now on WhatsApp ...",
  "cta_text": "Send message",
  "link_url": "https://wa.me/96550000000",
  "whatsapp_numbers": ["+96550000000"],
  "phone_numbers": [],
  "emails": [],
  "images": ["https://..."],
  "videos": ["https://..."]
}
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
