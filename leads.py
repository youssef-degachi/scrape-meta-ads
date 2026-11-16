#!/usr/bin/env python3
"""
Build a leads.md file for a collection: one block per advertiser with
Website, Facebook, Instagram, Whatsapp, Phone and other links.

    python leads.py canada-roofing        # one collection (folder under ./data)
    python leads.py --all                 # every collection
    python leads.py --all --no-enrich     # only what the ads themselves contain
    python leads.py --all --no-facebook   # skip Facebook pages (e.g. when it asks for login)

Contacts come from three places, merged:
  1. the ads (landing link, WhatsApp button, Instagram button, numbers in the text)
  2. the advertiser's Facebook page "Intro" box (website, Instagram, phone, email)
  3. the advertiser's website home page (social links, wa.me, tel:, mailto:)

Steps 2 and 3 are cached in data/enrich_cache.json, so re-runs are fast and an
interrupted run continues where it stopped.
"""

import argparse
import asyncio
import json
import re
import ssl
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from scrape_ads import EMAIL_RE, URL_RE, extract_phones

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# hosts that are never "the advertiser's website"
SOCIAL_HOSTS = ("tiktok.com", "snapchat.com", "youtube.com", "youtu.be", "linkedin.com", "twitter.com", "x.com",
                "pinterest.", "threads.net", "threads.com", "t.me", "telegram.me", "linktr.ee", "lin.ee",
                "line.me", "taplink.", "beacons.ai", "bio.link", "linkin.bio")
NOT_WEBSITE_HOSTS = SOCIAL_HOSTS + (
    "facebook.com", "fb.com", "fb.me", "m.me", "messenger.com", "instagram.com", "whatsapp.com", "wa.me", "wa.link",
    "google.com", "goo.gl", "bit.ly", "tinyurl.com", "forms.cloud.microsoft", "forms.office.com", "forms.gle",
    "typeform.com", "calendly.com", "apple.com", "apple.co", "onelink.me", "app.link", "w3.org", "fbcdn.net",
    "schema.org", "gstatic.com", "googleapis.com")
IG_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})", re.I)
IG_SKIP = {"p", "reel", "reels", "explore", "accounts", "share", "tv", "stories", "sharer", "about", "legal",
           "developer", "direct", "static", "embed.js", "privacy"}
WA_NUM_RE = re.compile(r"(?:wa\.me/|whatsapp\.com/send/?\?(?:[^\"'\s]*?&)?phone=)\+?(\d{7,15})", re.I)
WA_SHORT_RE = re.compile(r"https?://(?:wa\.link/[A-Za-z0-9]+|wa\.me/message/[A-Za-z0-9]+|"
                         r"api\.whatsapp\.com/message/[A-Za-z0-9]+[^\s\"'<>]*)", re.I)
HREF_RE = re.compile(r"""href\s*=\s*["']([^"'<>\s]+)["']""", re.I)
DOMAIN_LINE_RE = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)(/\S*)?$", re.I)


def host_of(url):
    try:
        return urlparse(url if "//" in url else "http://" + url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def is_host(url, hosts):
    h = host_of(url)
    return any(k in h for k in hosts)


def ig_handles(text):
    out = []
    for h in IG_RE.findall(text or ""):
        h = h.strip(".").lower()
        if h and h not in IG_SKIP and h not in out:
            out.append(h)
    return out


def uniq(seq):
    out = []
    for x in seq:
        if x and x not in out:
            out.append(x)
    return out


# --------------------------------------------------------------------------- #
# 1. What the ads themselves tell us
# --------------------------------------------------------------------------- #
def from_ads(page, ads):
    texts, links = [], []
    for a in ads:
        links += [a.get("link_url")] + (a.get("external_links") or [])
        texts += [a.get("body"), a.get("title"), a.get("caption"), a.get("link_description")]
        texts += (a.get("card_texts") or []) + (a.get("extra_texts") or [])
    blob = "\n".join(t for t in texts if t)
    links = [l for l in links if l] + URL_RE.findall(blob)

    sites = Counter(host_of(l) for l in links if host_of(l) and "." in host_of(l)
                    and not is_host(l, NOT_WEBSITE_HOSTS))
    website = None
    if sites:
        host = sites.most_common(1)[0][0]
        scheme = next((urlparse(l).scheme for l in links if host_of(l) == host and urlparse(l).scheme), "https")
        website = f"{scheme}://{host}/"
    return {
        "website": website,
        "instagram": uniq(h for l in links for h in ig_handles(l)) or ig_handles(blob),
        "whatsapp": list(page.get("whatsapp_numbers") or []),
        "whatsapp_links": uniq(WA_SHORT_RE.findall("\n".join(links) + "\n" + blob)),
        "phones": list(page.get("phone_numbers") or []) + list(page.get("about_phones") or []),
        "emails": list(page.get("emails") or []) + list(page.get("about_emails") or []),
        "other": uniq(l.split("?")[0] for l in links if is_host(l, SOCIAL_HOSTS)),
    }


# --------------------------------------------------------------------------- #
# 2. Facebook page "Intro" box (works logged out)
# --------------------------------------------------------------------------- #
FB_JS = """() => ({
  text: document.body.innerText,
  links: [...document.querySelectorAll('a[href]')].map(a => [a.href, (a.innerText || '').trim()])
})"""


def parse_fb(data, region):
    text = data.get("text") or ""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    intro = lines
    if "Intro" in lines:
        i = lines.index("Intro")
        end = next((j for j in range(i + 1, len(lines)) if lines[j] in ("Photos", "Privacy", "See all photos")),
                   min(len(lines), i + 25))
        intro = lines[i + 1:end]
    intro_set = {l.lower().lstrip("@") for l in intro}
    intro_text = "\n".join(intro)

    out = {"website": None, "instagram": [], "whatsapp": [], "whatsapp_links": [], "phones": [], "emails": [],
           "other": []}
    for href, label in data.get("links") or []:
        target = href
        if "l.facebook.com/l.php" in href:
            target = (parse_qs(urlparse(href).query).get("u") or [""])[0]
        target = unquote(target)
        if not target or "facebook.com" in host_of(target):
            continue
        in_intro = label.lower().lstrip("@") in intro_set
        for n in WA_NUM_RE.findall(target):
            out["whatsapp"].append("+" + n)
        out["whatsapp_links"] += WA_SHORT_RE.findall(target)
        if not in_intro:
            continue
        if "instagram.com" in host_of(target):
            out["instagram"] += ig_handles(target)
        elif is_host(target, SOCIAL_HOSTS):
            out["other"].append(target.split("?")[0])
        elif not is_host(target, NOT_WEBSITE_HOSTS) and not out["website"]:
            out["website"] = target.split("?")[0]
    if not out["website"]:
        for l in intro:
            m = DOMAIN_LINE_RE.match(l)
            if m and "@" not in l and not is_host(l, NOT_WEBSITE_HOSTS):
                out["website"] = l if l.startswith("http") else "https://" + l
                break
    out["phones"] = sorted(extract_phones(intro_text, region)) if "Intro" in lines else []
    out["emails"] = sorted(set(EMAIL_RE.findall(intro_text))) if "Intro" in lines else []
    for k in ("instagram", "whatsapp", "whatsapp_links", "other"):
        out[k] = uniq(out[k])
    return out


async def enrich_facebook(todo, region, cache, save, concurrency=8):
    """todo: list of (page_id, page_url). Fills cache['fb'][page_id]."""
    from playwright.async_api import async_playwright

    todo = [(pid, url) for pid, url in todo if pid and pid not in cache["fb"]]
    if not todo:
        return
    state = {"done": 0, "blocked": 0, "stop": False}
    sem = asyncio.Semaphore(concurrency)

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(locale="en-US", user_agent=UA, viewport={"width": 1300, "height": 900})
        await ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font")
                        else r.continue_())

        async def one(pid, url):
            if state["stop"]:
                return
            async with sem:
                if state["stop"]:
                    return
                pg = await ctx.new_page()
                try:
                    await pg.goto(url or f"https://www.facebook.com/{pid}", wait_until="domcontentloaded",
                                  timeout=40000)
                    try:
                        await pg.wait_for_function(
                            "() => /Intro|isn't available|followers|likes/.test(document.body.innerText)",
                            timeout=8000)
                    except Exception:
                        pass
                    await pg.wait_for_timeout(700)
                    if "/login" in pg.url or "checkpoint" in pg.url:
                        state["blocked"] += 1
                        if state["blocked"] >= 15:
                            state["stop"] = True
                        return
                    state["blocked"] = 0
                    data = await pg.evaluate(FB_JS)
                    res = parse_fb(data, region)
                    res["unavailable"] = "isn't available" in (data.get("text") or "")[:400]
                    cache["fb"][pid] = res
                except Exception as e:
                    cache["fb"][pid] = {"error": str(e)[:120]}
                finally:
                    await pg.close()
                    state["done"] += 1
                    if state["done"] % 25 == 0 or state["done"] == len(todo):
                        print(f"[*] facebook pages: {state['done']}/{len(todo)}", file=sys.stderr, flush=True)
                        save()

        await asyncio.gather(*(one(pid, url) for pid, url in todo))
        await browser.close()
    save()
    if state["stop"]:
        print("[!] Facebook started asking for login; stopped reading pages (what was read is kept).",
              file=sys.stderr)


# --------------------------------------------------------------------------- #
# 3. The advertiser's website home page
# --------------------------------------------------------------------------- #
def fetch_site(url, region):
    out = {"instagram": [], "whatsapp": [], "whatsapp_links": [], "phones": [], "emails": [], "other": []}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*",
                                                   "Accept-Language": "en-US,en;q=0.8"})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=12, context=ctx) as r:
            if "html" not in (r.headers.get("Content-Type") or "html"):
                return out
            html = r.read(1_500_000).decode("utf-8", errors="ignore")
    except Exception as e:
        out["error"] = str(e)[:120]
        return out
    hrefs = [unquote(h.replace("&amp;", "&")) for h in HREF_RE.findall(html)]
    for h in hrefs:
        low = h.lower()
        if low.startswith("tel:"):
            out["phones"] += sorted(extract_phones(h[4:], region))
        elif low.startswith("mailto:"):
            out["emails"] += EMAIL_RE.findall(h[7:].split("?")[0])
        elif "instagram.com" in low:
            out["instagram"] += ig_handles(h)
        elif WA_NUM_RE.search(h):
            out["whatsapp"] += ["+" + n for n in WA_NUM_RE.findall(h)]
        elif WA_SHORT_RE.search(h):
            out["whatsapp_links"] += WA_SHORT_RE.findall(h)
        elif low.startswith("http") and is_host(h, SOCIAL_HOSTS) and "/share" not in low and "intent/" not in low:
            out["other"].append(h.split("?")[0])
    for k in out:
        if isinstance(out[k], list):
            out[k] = uniq(out[k])[:6]
    return out


def enrich_websites(urls, region, cache, save):
    todo = uniq(u for u in urls if u and host_of(u) not in cache["web"])
    todo = list({host_of(u): u for u in todo}.values())
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=16) as ex:
        for i, (u, res) in enumerate(zip(todo, ex.map(lambda u: fetch_site(u, region), todo)), 1):
            cache["web"][host_of(u)] = res
            if i % 50 == 0 or i == len(todo):
                print(f"[*] websites: {i}/{len(todo)}", file=sys.stderr, flush=True)
                save()
    save()


# --------------------------------------------------------------------------- #
# Merge + write
# --------------------------------------------------------------------------- #
def merge(page, ads_info, fb, web):
    fb, web = fb or {}, web or {}

    def all_of(k):
        return uniq((ads_info.get(k) or []) + (fb.get(k) or []) + (web.get(k) or []))

    whatsapp = all_of("whatsapp")
    return {
        "name": page.get("page_name"),
        "page_id": page.get("page_id"),
        "ads_count": page.get("ads_count"),
        "website": fb.get("website") or ads_info.get("website"),
        "facebook": page.get("page_url"),
        "instagram": [f"https://www.instagram.com/{h}" for h in all_of("instagram")][:3],
        "whatsapp": [f"https://wa.me/{n.lstrip('+')}" for n in whatsapp] + all_of("whatsapp_links"),
        "phones": all_of("phones")[:5],
        "emails": all_of("emails")[:4],
        "other": all_of("other")[:8],
    }


def render(leads):
    blocks = []
    for l in leads:
        other = l["other"] + l["emails"]
        blocks.append("\n".join([
            f"# {l['name']}",
            "",
            "```",
            f"Website: {l['website'] or ''}".rstrip(),
            "", "",
            f"Facebook: {l['facebook'] or ''}".rstrip(),
            "", "",
            ("Instagram: " + "\n".join(l["instagram"])).rstrip(),
            "", "",
            ("Whatsapp: " + "\n".join(l["whatsapp"])).rstrip(),
            "",
            ("Phone: " + "\n".join(l["phones"])).rstrip(),
            "",
            "Other:",
            *other,
            "Video:",
            "```",
        ]))
    return "\n\n".join(blocks) + "\n"


def build_leads(folder, enrich=True, data_dir=None, fetch_fb=True, fetch_web=True):
    folder = Path(folder)
    data_dir = Path(data_dir) if data_dir else folder.parent
    pages = json.loads((folder / "pages.json").read_text(encoding="utf-8"))
    ads = json.loads((folder / "ads.json").read_text(encoding="utf-8"))
    meta = {}
    if (folder / "meta.json").exists():
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    region = (parse_qs(urlparse(meta.get("url", "")).query).get("country", [""])[0] or "").upper()
    region = region if len(region) == 2 else None

    cache_path = data_dir / "enrich_cache.json"
    cache = {"fb": {}, "web": {}}
    if cache_path.exists():
        try:
            cache.update(json.loads(cache_path.read_text(encoding="utf-8")))
        except Exception:
            pass

    def save():
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache_path)

    by_page = {}
    for a in ads:
        by_page.setdefault(a.get("page_id") or a.get("page_url") or a.get("page_name"), []).append(a)
    infos = [from_ads(p, by_page.get(p.get("page_id") or p.get("page_url") or p.get("page_name"), []))
             for p in pages]

    if enrich and fetch_fb:
        asyncio.run(enrich_facebook([(p.get("page_id"), p.get("page_url")) for p in pages], region, cache, save))
    if enrich and fetch_web:
        sites = [(cache["fb"].get(p.get("page_id")) or {}).get("website") or i["website"]
                 for p, i in zip(pages, infos)]
        enrich_websites(sites, region, cache, save)

    leads = []
    for p, i in zip(pages, infos):
        fb = cache["fb"].get(p.get("page_id")) if enrich else None
        site = (fb or {}).get("website") or i["website"]
        web = cache["web"].get(host_of(site)) if (enrich and site) else None
        leads.append(merge(p, i, fb, web))

    (folder / "leads.md").write_text(render(leads), encoding="utf-8")
    (folder / "leads.json").write_text(json.dumps(leads, ensure_ascii=False, indent=1), encoding="utf-8")
    n = len(leads)
    stats = {k: sum(1 for l in leads if l[k]) for k in ("website", "instagram", "whatsapp", "phones", "emails")}
    print(f"{folder.name}: {n} leads -> {folder / 'leads.md'} | website {stats['website']} | "
          f"instagram {stats['instagram']} | whatsapp {stats['whatsapp']} | phone {stats['phones']} | "
          f"email {stats['emails']}", flush=True)
    return stats


def main():
    ap = argparse.ArgumentParser(description="Write leads.md for one or all collections.")
    ap.add_argument("names", nargs="*", help="collection folder names under the data dir")
    ap.add_argument("--all", action="store_true", help="every collection that has ads.json")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip reading Facebook pages and websites (fast, less complete)")
    ap.add_argument("--no-facebook", action="store_true",
                    help="do not open Facebook pages (use what is cached); still reads websites")
    ap.add_argument("--cached-only", action="store_true",
                    help="no network at all: ads + whatever is already in data/enrich_cache.json")
    a = ap.parse_args()
    root = Path(a.data_dir)
    names = a.names
    if a.all:
        names = sorted(d.name for d in root.iterdir() if d.is_dir() and (d / "ads.json").exists())
    if not names:
        ap.error("give a collection name or --all")
    for n in names:
        t = time.time()
        build_leads(root / n, enrich=not a.no_enrich, data_dir=root,
                    fetch_fb=not (a.no_facebook or a.cached_only), fetch_web=not a.cached_only)
        print(f"  ({time.time() - t:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
