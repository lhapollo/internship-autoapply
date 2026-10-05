"""Pull postings out of internship-list GitHub repos.

Most popular lists (SimplifyJobs, vanshb03, ...) keep a machine-readable
`.github/scripts/listings.json`; we use that when present and otherwise fall
back to scraping the README's markdown/HTML tables.
"""
import hashlib
import html
import re
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

RAW = "https://raw.githubusercontent.com/{repo}/HEAD/{path}"
TRACKING_PARAMS = re.compile(r"^(utm_.*|ref|source|src|gh_src|lever-source.*|embed)$", re.I)


def repo_slug(spec: str) -> str:
    m = re.search(r"github\.com/([^/]+/[^/#?]+)", spec)
    return (m.group(1) if m else spec).removesuffix(".git").strip("/")


def detect_ats(url: str) -> str:
    u = urlparse(url)
    host = u.netloc.lower()
    if "greenhouse.io" in host or _gh_jid(u):
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    for name in ("myworkdayjobs", "icims", "oraclecloud", "smartrecruiters", "workable", "jobvite",
                 "successfactors", "taleo", "bamboohr", "jazzhr", "dover"):
        if name in host:
            return name
    return "other"


def _gh_jid(u) -> str:
    """Company career pages that wrap Greenhouse carry the job id as ?gh_jid=."""
    return dict(parse_qsl(u.query)).get("gh_jid", "") if "greenhouse.io" not in u.netloc.lower() else ""


def normalize_url(url: str) -> str:
    """Strip tracking params and point at the application form itself."""
    u = urlparse(url.strip())
    if jid := _gh_jid(u):
        # Greenhouse's embed form resolves the company from the job id alone.
        return f"https://boards.greenhouse.io/embed/job_app?token={jid}"
    query = [(k, v) for k, v in parse_qsl(u.query) if not TRACKING_PARAMS.match(k)]
    path = u.path.rstrip("/")
    ats = detect_ats(url)
    if ats == "lever" and not path.endswith("/apply"):
        path += "/apply"
    if ats == "ashby":
        path = re.sub(r"/application$", "", path) + "/application"
    return urlunparse((u.scheme or "https", u.netloc, path, "", urlencode(query), ""))


TERM_RE = re.compile(r"\b(summer|fall|autumn|winter|spring)\b(\s*(/|or|and|&)\s*(summer|fall|autumn|winter|spring))*"
                     r"(\s+20\d\d)?|\b20\d\d\b", re.I)


def role_key(p: dict) -> str:
    """Company + title with term/year mentions stripped, so one role listed per location or board matches."""
    title = TERM_RE.sub(" ", p["title"].lower())
    title = re.sub(r"[()\[\]]", " ", title)
    title = re.sub(r"(\s*[-–—,:|]\s*)+$", "", re.sub(r"\s+", " ", title).strip())
    return f"{p['company'].strip().lower()}|{title}"


def posting_key(url: str) -> str:
    return hashlib.sha1(normalize_url(url).lower().encode()).hexdigest()[:16]


def _from_listings_json(repo: str, items: list) -> list[dict]:
    out = []
    for x in items:
        if not x.get("active", True) or not x.get("is_visible", True) or not x.get("url"):
            continue
        out.append({
            "repo": repo,
            "company": x.get("company_name", ""),
            "title": x.get("title", ""),
            "url": x["url"],
            "locations": x.get("locations") or [],
            "date_posted": x.get("date_posted"),
            "category": x.get("category", ""),
            "terms": x.get("terms") or [],
            "sponsorship": x.get("sponsorship", ""),
        })
    return out


def _cell_text(c: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>|\*\*|\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) or "", c)).strip()


def _apply_link(cells: list[str]) -> str | None:
    for c in reversed(cells):  # the apply link is usually the right-most link
        for href in re.findall(r'href="([^"]+)"|\]\((https?://[^)\s]+)\)', c):
            href = href[0] or href[1]
            if "simplify.jobs/c/" not in href and "github.com" not in href and "imgur" not in href:
                return html.unescape(href)
    return None


def _from_readme(repo: str, text: str) -> list[dict]:
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", text, re.S):  # HTML tables
        rows.append(re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S))
    for line in text.splitlines():  # markdown tables
        if line.startswith("|") and not re.match(r"^\|\s*:?-", line):
            rows.append([c for c in line.strip().strip("|").split("|")])
    out, last_company = [], ""
    for cells in rows:
        if len(cells) < 3:
            continue
        url = _apply_link(cells)
        if not url or "🔒" in "".join(cells):
            continue
        company = _cell_text(cells[0])
        if company.startswith("↳") or not company:
            company = last_company
        last_company = company
        out.append({"repo": repo, "company": company, "title": _cell_text(cells[1]), "url": url,
                    "locations": [_cell_text(cells[2])], "date_posted": None,
                    "category": "", "terms": [], "sponsorship": ""})
    return out


def fetch_repo(spec: str) -> list[dict]:
    repo = repo_slug(spec)
    r = requests.get(RAW.format(repo=repo, path=".github/scripts/listings.json"), timeout=60)
    if r.ok:
        postings = _from_listings_json(repo, r.json())
    else:
        r = requests.get(RAW.format(repo=repo, path="README.md"), timeout=60)
        r.raise_for_status()
        postings = _from_readme(repo, r.text)
    for p in postings:
        p["ats"] = detect_ats(p["url"])
        p["key"] = posting_key(p["url"])
        p["apply_url"] = normalize_url(p["url"])
    return postings


def passes_filters(p: dict, f: dict) -> tuple[bool, str]:
    """Return (ok, reason). `f` is the `filters:` block of config.yaml."""
    title = p["title"]
    if f.get("max_age_days") and p.get("date_posted"):
        if time.time() - p["date_posted"] > f["max_age_days"] * 86400:
            return False, "older than max_age_days"
    if f.get("title_include") and not any(re.search(rx, title, re.I) for rx in f["title_include"]):
        return False, "title not in title_include"
    if any(re.search(rx, title, re.I) for rx in f.get("title_exclude") or []):
        return False, "title matched title_exclude"
    if any(c.lower() == p.get("company", "").lower() for c in f.get("company_exclude") or []):
        return False, "company excluded"
    if f.get("categories") and p.get("category") and p["category"] not in f["categories"]:
        return False, f"category {p['category']!r} not wanted"
    if f.get("terms") and p.get("terms") and not set(p["terms"]) & set(f["terms"]):
        return False, f"terms {p['terms']} not wanted"
    if p.get("sponsorship") in (f.get("sponsorship_exclude") or []):
        return False, f"sponsorship: {p['sponsorship']}"
    locs = " | ".join(p.get("locations") or [])
    if f.get("locations_include") and locs and not any(s.lower() in locs.lower() for s in f["locations_include"]):
        return False, "location not wanted"
    return True, ""
