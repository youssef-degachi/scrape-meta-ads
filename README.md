# scrape-meta-ads

Scrape the Facebook Ads Library and track who you contacted.
Paste an Ads Library URL, get every ad and advertiser with page name, page URL, ad text, CTA, link,
WhatsApp numbers, phones, emails, images, dates. Browse it in a local website, tick the ones you
contacted (row turns gray), download Excel.

## Install

```
git clone git@github.com:youssef-degachi/scrape-meta-ads.git
cd scrape-meta-ads
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

## Run

```
./start.sh
```

Open http://localhost:8000

1. Paste a Facebook Ads Library URL in "New scrape" and click **Start scrape**.
2. When it says **done**, click **Open** to see the leads, or **Excel** to download the workbook.
3. Tick the checkbox on a clinic after you contact them. It turns gray and stays saved.

## Files

Each scrape is a folder in `data/<name>/`: `ads.json`, `pages.json`, `ads.csv`, `pages.csv`,
`<name>.xlsx`, `meta.json`, `scrape.log`. Contacted state is in `data/contacted.json`.

## Command line (optional)

```
.venv/bin/python scrape_ads.py "<ads library url>" --name my_collection
```

Quote the URL. Flags: `--max-ads N`, `--headed` (show browser), `--deep` (also read each page's
About tab for phone / email / website, slow).
