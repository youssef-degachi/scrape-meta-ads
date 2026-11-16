# scrape-meta-ads

See [HOW_IT_WORKS.md](HOW_IT_WORKS.md) for the full flow.

Scrape the Facebook Ads Library and track who you contacted.
Paste an Ads Library URL, get every ad and advertiser with page name, page URL, ad text, CTA, link,
WhatsApp numbers, phones, emails, images, dates. Browse it in a local website, tick the ones you
contacted (row turns gray), download Excel.

## Result

See [What the result looks like](HOW_IT_WORKS.md#what-the-result-looks-like) for the website, `leads.md`, `pages.json` and `ads.json` formats.

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

## Leads file

Every collection gets a `leads.md`: one block per advertiser with Website, Facebook, Instagram,
Whatsapp, Phone and other links. It merges what the ads contain with the advertiser's Facebook page
"Intro" box and their website home page. Download it with the **Leads** button, or rebuild it:

```
.venv/bin/python leads.py my_collection     # or --all
```

## Files

Each scrape is a folder in `data/<name>/`: `ads.json`, `pages.json`, `ads.csv`, `pages.csv`,
`<name>.xlsx`, `leads.md`, `leads.json`, `meta.json`, `scrape.log`. Contacted state is in `data/contacted.json`.

## Command line (optional)

```
.venv/bin/python scrape_ads.py "<ads library url>" --name my_collection
```

Quote the URL. Flags: `--max-ads N`, `--headed` (show browser), `--no-enrich` (faster, leads from the ads only), `--deep` (also read each page's
About tab for phone / email / website, slow).


## Example of result
# LG Home Comfort

```
Website: https://xxx.ca/
Facebook: https://www.facebook.com/xx/
Instagram: https://www.instagram.com/xx

Whatsapp:

Phone: +xxx

Other:
https://x.com/xx
https://www.linkedin.com/company/xx/
https://www.youtube.com/@xx/
https://www.tiktok.com/@xx/
Video:
```
