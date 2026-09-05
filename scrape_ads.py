#!/usr/bin/env python3
"""
Facebook Ads Library scraper.

Give it an Ads Library URL, it opens a real browser, scrolls until no more ads
load, captures the GraphQL responses Facebook uses to fill the page, and
extracts everything useful from every ad: page name, page URL, page ID, ad
text, headline, CTA, landing link, WhatsApp numbers, emails, phone numbers,
media URLs, dates, platforms.

Usage:
    python scrape_ads.py "<ads library url>" [-o out.json] [--csv out.csv]
                         [--max-ads 500] [--headed] [--deep] [--debug-dir dumps]

Output: JSON (one object per ad) and optionally CSV. A second file
"<out>.pages.json" groups the contact info per advertiser page.
"""

import argparse
import asyncio
import csv
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import phonenumbers
from playwright.async_api import async_playwright

# --------------------------------------------------------------------------- #
# Regexes for contact extraction
# --------------------------------------------------------------------------- #
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
WA_LINK_RE = re.compile(
    r"(?:https?://)?(?:api\.whatsapp\.com/send/?\?phone=|wa\.me/|whatsapp\.com/send\?phone=)"
    r"\+?(\d{6,15})",
    re.I,
)
# a WhatsApp keyword followed (within ~120 chars) by a phone-looking number
WA_CONTEXT_RE = re.compile(
    r"(?:whats?app|واتس ?اب|واتساب|\bwa\b)(.{0,120}?)(\+?[\d][\d\s\-().]{6,20}\d)", re.I | re.S
)
NON_EXTERNAL_HOSTS = ("facebook.com", "fb.com", "fb.me", "whatsapp.com", "wa.me", "messenger.com", "instagram.com")
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
# Arabic-Indic digits -> ASCII
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def norm_digits(s):
    return s.translate(AR_DIGITS) if s else s


def extract_phones(text, region):
    """Return E.164 phone numbers found in text."""
    found = set()
    if not text:
        return found
    text = norm_digits(text)
    for m in phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.POSSIBLE):
        try:
            if phonenumbers.is_valid_number(m.number):
                found.add(phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164))
        except Exception:
            pass
    return found


def extract_whatsapp(text, urls, region):
    found = set()
    for u in urls:
        for m in WA_LINK_RE.finditer(unquote(u)):
            found.add("+" + m.group(1))
    if text:
        t = norm_digits(text)
        for m in WA_LINK_RE.finditer(t):
            found.add("+" + m.group(1))
        for m in WA_CONTEXT_RE.finditer(t):
            found |= extract_phones(m.group(2), region)
    return found


def extract_emails(text):
    return set(EMAIL_RE.findall(norm_digits(text) if text else ""))


# --------------------------------------------------------------------------- #
# Walk arbitrary JSON to find ad objects
# --------------------------------------------------------------------------- #
def iter_ads(obj):
    """Yield every dict that looks like an ad (has ad_archive_id + snapshot)."""
    if isinstance(obj, dict):
        if "ad_archive_id" in obj and "snapshot" in obj:
            yield obj
            return
        for v in obj.values():
            yield from iter_ads(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_ads(v)


def parse_graphql_body(text):
    """Facebook sometimes returns several JSON objects concatenated by newlines."""
    out = []
    for chunk in text.split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        # strip "for (;;);" prefix if present
        if chunk.startswith("for (;;);"):
            chunk = chunk[9:]
        try:
            out.append(json.loads(chunk))
        except json.JSONDecodeError:
            pass
    return out


def g(d, *path, default=None):
    """Safe nested get."""
    cur = d
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list) and isinstance(p, int) and len(cur) > p:
            cur = cur[p]
        else:
            return default
        if cur is None:
            return default
    return cur


def text_of(v):
    """snapshot.body may be {"text": ...} or a string."""
    if isinstance(v, dict):
        return v.get("text") or v.get("markup", {}).get("__html") if isinstance(v.get("markup"), dict) else v.get("text")
    return v if isinstance(v, str) else None


def ts_to_date(ts):
    if not ts:
        return None
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ts)))
    except Exception:
        return None


def normalize_ad(ad, region):
    snap = ad.get("snapshot") or {}
    page_id = ad.get("page_id") or snap.get("page_id")
    page_name = ad.get("page_name") or snap.get("page_name")
    page_uri = snap.get("page_profile_uri") or (f"https://www.facebook.com/{page_id}" if page_id else None)

    body = text_of(snap.get("body"))
    title = snap.get("title")
    caption = snap.get("caption")
    link_desc = snap.get("link_description")
    link_url = snap.get("link_url")
    cta_text = snap.get("cta_text")
    cta_type = snap.get("cta_type")

    # Cards (carousel) also carry text + links
    cards = snap.get("cards") or []
    card_texts, card_links, card_titles = [], [], []
    for c in cards:
        if not isinstance(c, dict):
            continue
        card_texts.append(text_of(c.get("body")) or "")
        card_titles.append(c.get("title") or "")
        if c.get("link_url"):
            card_links.append(c["link_url"])

    images = [i.get("original_image_url") or i.get("resized_image_url")
              for i in (snap.get("images") or []) if isinstance(i, dict)]
    images += [c.get("original_image_url") or c.get("resized_image_url")
               for c in cards if isinstance(c, dict) and (c.get("original_image_url") or c.get("resized_image_url"))]
    videos = [v.get("video_hd_url") or v.get("video_sd_url")
              for v in (snap.get("videos") or []) if isinstance(v, dict)]
    videos += [c.get("video_hd_url") or c.get("video_sd_url")
               for c in cards if isinstance(c, dict) and (c.get("video_hd_url") or c.get("video_sd_url"))]

    extra_texts = [text_of(t) or "" for t in (snap.get("extra_texts") or []) if t]
    extra_links = [l if isinstance(l, str) else (l.get("link_url") or l.get("url") or "")
                   for l in (snap.get("extra_links") or []) if l]
    extra_links = [l for l in extra_links if l]

    # Gather every string for contact mining
    all_text = "\n".join(filter(None, [body, title, caption, link_desc, cta_text, snap.get("byline")]
                                 + card_texts + card_titles + extra_texts))
    all_urls = set(filter(None, [link_url] + card_links + extra_links + URL_RE.findall(all_text)))

    whatsapp = extract_whatsapp(all_text, all_urls, region)
    if cta_type and "WHATSAPP" in str(cta_type).upper():
        # CTA is a WhatsApp button; the link may hide the number
        for u in all_urls:
            whatsapp |= {"+" + m.group(1) for m in WA_LINK_RE.finditer(unquote(u))}
    phones = extract_phones(all_text, region) - whatsapp
    emails = extract_emails(all_text)

    ext_links = sorted(u for u in all_urls
                       if not any(h in urlparse(u).netloc.lower() for h in NON_EXTERNAL_HOSTS))

    return {
        "ad_archive_id": ad.get("ad_archive_id"),
        "ad_library_url": f"https://www.facebook.com/ads/library/?id={ad.get('ad_archive_id')}",
        "page_id": page_id,
        "page_name": page_name,
        "page_url": page_uri,
        "page_url_by_id": f"https://www.facebook.com/{page_id}" if page_id else None,
        "byline": snap.get("byline"),
        "page_profile_picture": snap.get("page_profile_picture_url"),
        "page_likes": snap.get("page_like_count"),
        "page_categories": snap.get("page_categories"),
        "instagram_handle": snap.get("instagram_actor_name") or g(snap, "instagram_actor", "username"),
        "is_active": ad.get("is_active"),
        "start_date": ts_to_date(ad.get("start_date")),
        "end_date": ts_to_date(ad.get("end_date")),
        "platforms": ad.get("publisher_platform"),
        "total_active_time_sec": ad.get("total_active_time"),
        "targeted_countries": ad.get("targeted_or_reached_countries"),
        "ad_id": ad.get("ad_id"),
        "categories": ad.get("categories"),
        "collation_count": ad.get("collation_count"),
        "display_format": snap.get("display_format"),
        "title": title,
        "body": body,
        "caption": caption,
        "link_description": link_desc,
        "cta_text": cta_text,
        "cta_type": cta_type,
        "link_url": link_url,
        "card_titles": [t for t in card_titles if t],
        "card_texts": [t for t in card_texts if t],
        "extra_texts": [t for t in extra_texts if t],
        "external_links": ext_links,
        "whatsapp_numbers": sorted(whatsapp),
        "phone_numbers": sorted(phones),
        "emails": sorted(emails),
        "images": [i for i in images if i],
        "videos": [v for v in videos if v],
        "impressions": g(ad, "impressions_with_index", "impressions_text"),
        "spend": ad.get("spend"),
        "currency": ad.get("currency"),
        "reach": ad.get("reach_estimate") or g(ad, "aaa_info", "eu_total_reach"),
    }


# --------------------------------------------------------------------------- #
# Optional: visit the page's "About" tab for phone / email / website
# --------------------------------------------------------------------------- #
async def deep_page_info(context, page_id, page_url, region):
    info = {"about_phones": [], "about_emails": [], "about_websites": [], "about_address": None}
    if not page_id and not page_url:
        return info
    url = f"https://www.facebook.com/{page_id}/about" if page_id else page_url.rstrip("/") + "/about"
    pg = await context.new_page()
    try:
        await pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        await pg.wait_for_timeout(3000)
        # close login popup if present
        for sel in ['div[aria-label="Close"]', 'div[role="button"][aria-label="Close"]']:
            try:
                await pg.click(sel, timeout=1500)
            except Exception:
                pass
        txt = await pg.evaluate("() => document.body.innerText")
        html = await pg.content()
        info["about_phones"] = sorted(extract_phones(txt, region))
        info["about_emails"] = sorted(extract_emails(txt))
        sites = set()
        for u in URL_RE.findall(html):
            u = unquote(u)
            if "l.facebook.com/l.php" in u:
                q = parse_qs(urlparse(u).query).get("u")
                if q:
                    u = q[0]
            host = urlparse(u).netloc.lower()
            if host and not any(k in host for k in ("facebook", "fbcdn", "fb.com", "instagram", "messenger", "whatsapp", "w3.org")):
                sites.add(u.split("?")[0])
        info["about_websites"] = sorted(sites)[:10]
        m = re.search(r"(?:Address|العنوان)\s*\n(.+)", txt)
        if m:
            info["about_address"] = m.group(1).strip()
    except Exception as e:
        info["about_error"] = str(e)[:200]
    finally:
        await pg.close()
    return info


# --------------------------------------------------------------------------- #
# Main scrape
# --------------------------------------------------------------------------- #
async def scrape(url, max_ads, headed, deep, debug_dir, cookies_file, scroll_pause, idle_rounds):
    q = parse_qs(urlparse(url).query)
    region = (q.get("country", ["US"])[0] or "US").upper()
    if region == "ALL":
        region = "US"

    ads = {}
    raw_hits = 0
    debug_path = Path(debug_dir) if debug_dir else None
    if debug_path:
        debug_path.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed,
                                          args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="en-US",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        )
        if cookies_file:
            await context.add_cookies(json.loads(Path(cookies_file).read_text()))
        page = await context.new_page()

        async def on_response(resp):
            nonlocal raw_hits
            try:
                if "/api/graphql" not in resp.url or resp.request.method != "POST":
                    return
                body = await resp.text()
                if "ad_archive_id" not in body:
                    return
                raw_hits += 1
                if debug_path:
                    (debug_path / f"graphql_{raw_hits:03d}.json").write_text(body)
                for obj in parse_graphql_body(body):
                    for ad in iter_ads(obj):
                        aid = ad.get("ad_archive_id")
                        if aid and aid not in ads:
                            ads[aid] = ad
            except Exception as e:
                print(f"[warn] response parse: {e}", file=sys.stderr)

        page.on("response", on_response)

        print(f"[*] Opening {url}", file=sys.stderr)
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(4000)

        # Also scan the initial HTML for embedded ads (SSR payloads)
        html = await page.content()
        if debug_path:
            (debug_path / "initial.html").write_text(html)
        for m in re.finditer(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S):
            try:
                obj = json.loads(m.group(1))
                for ad in iter_ads(obj):
                    aid = ad.get("ad_archive_id")
                    if aid and aid not in ads:
                        ads[aid] = ad
            except Exception:
                pass

        # dismiss cookie / login dialogs
        for sel in ['button:has-text("Allow all cookies")', 'button:has-text("Decline optional cookies")',
                    '[aria-label="Close"]', 'div[role="button"]:has-text("Close")']:
            try:
                await page.click(sel, timeout=1500)
            except Exception:
                pass

        idle = 0
        last = -1
        while len(ads) < max_ads:
            await page.mouse.wheel(0, 20000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(scroll_pause)
            # click "See more" style loaders if present
            for sel in ['div[role="button"]:has-text("See more")', 'div[role="button"]:has-text("Load more")']:
                try:
                    await page.click(sel, timeout=800)
                except Exception:
                    pass
            n = len(ads)
            print(f"\r[*] ads collected: {n}   ", end="", file=sys.stderr)
            if n == last:
                idle += 1
                if idle >= idle_rounds:
                    break
            else:
                idle = 0
            last = n
        print(file=sys.stderr)

        if raw_hits == 0 and not ads:
            print("[!] No ad data captured. Facebook may be showing a login wall or the page layout "
                  "changed. Try --headed (and log in), or --cookies cookies.json, or --debug-dir to dump "
                  "responses.", file=sys.stderr)

        results = [normalize_ad(a, region) for a in list(ads.values())[:max_ads]]

        if deep:
            seen = {}
            for r in results:
                key = r["page_id"] or r["page_url"]
                if key and key not in seen:
                    print(f"[*] deep: {r['page_name']} ({key})", file=sys.stderr)
                    seen[key] = await deep_page_info(context, r["page_id"], r["page_url"], region)
                    await asyncio.sleep(1.5)
                r.update(seen.get(key, {}))

        await browser.close()
    return results


def group_pages(results):
    pages = {}
    for r in results:
        key = r["page_id"] or r["page_url"] or r["page_name"]
        p = pages.setdefault(key, {
            "page_id": r["page_id"], "page_name": r["page_name"], "page_url": r["page_url"],
            "page_likes": r["page_likes"], "page_categories": r["page_categories"],
            "instagram_handle": r["instagram_handle"], "ads_count": 0, "ad_ids": [],
            "whatsapp_numbers": set(), "phone_numbers": set(), "emails": set(),
            "external_links": set(), "about_phones": set(), "about_emails": set(),
            "about_websites": set(), "about_address": None,
        })
        p["ads_count"] += 1
        p["ad_ids"].append(r["ad_archive_id"])
        for k in ("whatsapp_numbers", "phone_numbers", "emails", "external_links",
                  "about_phones", "about_emails", "about_websites"):
            p[k] |= set(r.get(k) or [])
        p["about_address"] = p["about_address"] or r.get("about_address")
    out = []
    for p in pages.values():
        p["phone_numbers"] -= p["whatsapp_numbers"]
        p["about_phones"] -= p["whatsapp_numbers"] | p["phone_numbers"]
        for k in ("whatsapp_numbers", "phone_numbers", "emails", "external_links",
                  "about_phones", "about_emails", "about_websites"):
            p[k] = sorted(p[k])
        out.append(p)
    out.sort(key=lambda x: -x["ads_count"])
    return out


def write_csv(results, path):
    if not results:
        return
    cols = list(results[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in results:
            w.writerow({k: (" | ".join(map(str, v)) if isinstance(v, list) else v) for k, v in r.items()})


def write_xlsx(results, pages, path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    def sheet(ws, rows, cols):
        ws.append(cols)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1F4E78")
        for r in rows:
            ws.append([(" | ".join(map(str, r.get(k) or [])) if isinstance(r.get(k), list) else r.get(k)) for k in cols])
        for i, k in enumerate(cols, 1):
            width = max([len(str(k))] + [min(len(str(r.get(k) or "")), 60) for r in rows]) + 2
            ws.column_dimensions[get_column_letter(i)].width = min(width, 62)
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(vertical="top", wrap_text=True)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    wb = Workbook()
    ws = wb.active
    ws.title = "Pages (contacts)"
    sheet(ws, pages, ["page_name", "page_url", "page_id", "ads_count", "whatsapp_numbers", "phone_numbers",
                      "emails", "external_links", "about_phones", "about_emails", "about_websites",
                      "about_address", "page_likes", "page_categories", "instagram_handle"])
    ws2 = wb.create_sheet("Ads")
    sheet(ws2, results, ["page_name", "page_url", "ad_archive_id", "ad_library_url", "is_active", "start_date",
                         "end_date", "platforms", "cta_type", "cta_text", "link_url", "whatsapp_numbers",
                         "phone_numbers", "emails", "external_links", "title", "body", "caption",
                         "link_description", "card_titles", "card_texts", "display_format", "images", "videos",
                         "page_likes", "page_categories", "instagram_handle", "page_id"])
    wb.save(path)


def slugify(s):
    s = re.sub(r"[^\w\s-]", "", s, flags=re.U).strip().lower()
    return re.sub(r"[\s_-]+", "_", s) or "collection"


def default_name(url):
    q = parse_qs(urlparse(url).query)
    kw = q.get("q", [""])[0] or q.get("view_all_page_id", ["page"])[0]
    return slugify(f"{kw}_{q.get('country', ['all'])[0]}")


def main():
    ap = argparse.ArgumentParser(description="Scrape Facebook Ads Library search results.")
    ap.add_argument("url", help="Ads Library URL (quote it)")
    ap.add_argument("-n", "--name", help="Collection name -> everything is written to data/<name>/ "
                                        "(default: derived from the search keyword + country)")
    ap.add_argument("--data-dir", default="data", help="Root folder for collections (default ./data)")
    ap.add_argument("-o", "--out", help="(advanced) write a single JSON file here instead of a collection folder")
    ap.add_argument("--csv", help="(advanced) also write CSV here")
    ap.add_argument("--xlsx", help="(advanced) also write an Excel workbook here (sheets: Pages, Ads)")
    ap.add_argument("--max-ads", type=int, default=1000)
    ap.add_argument("--headed", action="store_true", help="Show the browser (lets you log in)")
    ap.add_argument("--cookies", help="Playwright cookies JSON to reuse a logged-in session")
    ap.add_argument("--deep", action="store_true",
                    help="Also visit each advertiser's Facebook About page for phone/email/website")
    ap.add_argument("--scroll-pause", type=int, default=2500, help="ms between scrolls")
    ap.add_argument("--idle-rounds", type=int, default=5,
                    help="stop after this many scrolls with no new ads")
    ap.add_argument("--debug-dir", help="dump raw GraphQL responses here")
    a = ap.parse_args()

    folder = None
    if not a.out:
        name = slugify(a.name) if a.name else default_name(a.url)
        folder = Path(a.data_dir) / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "meta.json").write_text(json.dumps({
            "name": name, "url": a.url, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "running"}, ensure_ascii=False, indent=1), encoding="utf-8")

    try:
        results = asyncio.run(scrape(a.url, a.max_ads, a.headed, a.deep, a.debug_dir,
                                     a.cookies, a.scroll_pause, a.idle_rounds))
    except Exception as e:
        if folder:
            m = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            m.update(status="error", error=str(e)[:500])
            (folder / "meta.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        raise
    pages = group_pages(results)
    wa = sum(1 for r in results if r["whatsapp_numbers"])
    em = sum(1 for r in results if r["emails"])
    ph = sum(1 for r in results if r["phone_numbers"])

    if folder:
        (folder / "ads.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "pages.json").write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
        write_csv(results, folder / "ads.csv")
        write_csv(pages, folder / "pages.csv")
        write_xlsx(results, pages, folder / f"{folder.name}.xlsx")
        m = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        m.update(status="done", finished=time.strftime("%Y-%m-%d %H:%M:%S"), ads=len(results),
                 pages=len(pages), ads_with_whatsapp=wa, ads_with_phone=ph, ads_with_email=em)
        (folder / "meta.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nDone. {len(results)} ads from {len(pages)} pages -> {folder}/  "
              f"(ads.json, pages.json, ads.csv, pages.csv, {folder.name}.xlsx)")
    else:
        Path(a.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        pages_path = Path(a.out).with_suffix(".pages.json")
        pages_path.write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
        if a.csv:
            write_csv(results, a.csv)
            write_csv(pages, str(Path(a.csv).with_suffix(".pages.csv")))
        if a.xlsx:
            write_xlsx(results, pages, a.xlsx)
        print(f"\nDone. {len(results)} ads from {len(pages)} pages -> {a.out}, {pages_path}"
              + (f", {a.csv}" if a.csv else "") + (f", {a.xlsx}" if a.xlsx else ""))
    print(f"  ads with WhatsApp: {wa} | with phone: {ph} | with email: {em}")


if __name__ == "__main__":
    main()
