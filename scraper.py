#!/usr/bin/env python3
"""
indiankanoon_scraper.py
=======================

Scrape search results from https://indiankanoon.org and filter/rank them by a
topic of your choice across multiple result pages.

WHAT IT DOES
------------
1. You give it a search query (the `formInput`, e.g. "kapoor").
2. It walks through result pages (pagenum = 0, 1, 2, ...) and collects every
   result on each page: title, court/source, date, document id, link, snippet.
3. You optionally give it a "topic" (one or more keywords). Every result is then
   scored by how well its title + snippet match that topic, and results that do
   not match are dropped. Leave the topic blank to keep everything.
4. Results are printed (best match first) and saved to CSV + JSON.

TWO MODES
---------
* SCRAPE MODE (default, free, no account):
    Uses `cloudscraper` to get past the site's bot protection and BeautifulSoup
    to parse the HTML. May break if the site changes its protection/markup.

* API MODE (optional, reliable):
    Indian Kanoon has an official API (https://api.indiankanoon.org) that returns
    clean JSON. If you have a token, pass `--api-token YOUR_TOKEN` and the script
    uses that instead of scraping. Their API `pagenum` starts at 0.

USAGE
-----
Interactive (just run it and answer the prompts):
    python indiankanoon_scraper.py

Non-interactive (good for automation):
    python indiankanoon_scraper.py --query kapoor --topic "property dispute" --pages 5
    python indiankanoon_scraper.py --query "freedom of speech" --pages 3 --api-token XXXX

Be polite: keep the delay reasonable so you don't hammer their servers. Scraping
a public site is your responsibility — check their terms; the official API is the
sanctioned route.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from urllib.parse import quote_plus, urljoin
from collections import Counter


try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Run:  pip install beautifulsoup4 lxml")

# requests is needed for API mode and as a fallback; cloudscraper for scrape mode.
try:
    import requests
except ImportError:
    requests = None

try:
    import cloudscraper
except ImportError:
    cloudscraper = None


SITE = "https://indiankanoon.org"
API = "https://api.indiankanoon.org"

# A result link on the site looks like /doc/123456/ or /docfragment/123456/?...
DOC_HREF_RE = re.compile(r"/(?:doc|docfragment)/(\d+)/")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# Words that carry no legal/search signal and should be ignored when turning a
# free-text case description into a query.
STOPWORDS = set("""
a an the and or but if then else for to of in on at by with from into onto over
under again further once here there all any both each few more most other some
such no nor not only own same so than too very can will just don should now this
that these those is am are was were be been being have has had having do does did
doing we our us you your they them their he she it its i me my mine he's we're
one two three made make making get got getting received receive receiving
completed complete completing done already still while after before during about
who whom which what when where why how also been would could may might must shall
client clients customer customers company companies project projects work works
team teams initial initially partial remaining amount amounts thing things case
cases find similar like regarding etc per via using used use
""".split())

# Legal / contractual terms get extra weight in both query-building and ranking,
# so a case description is matched on its legal substance, not its filler.
LEGAL_TERMS = set("""
payment payments pay paying paid nonpayment unpaid refund refunds reimburse
reimbursement contract contracts contractual breach breached agreement agreements
deliverable deliverables delivery delivered dispute disputes recovery recover
dues consideration damages liability liable default defaulted defaults invoice
invoices terminate terminated termination rescind rescission rescinded money
sum quantum meruit performance nonperformance obligation obligations clause
arbitration suit decree settlement compensation deposit advance balance retention
fraud misrepresentation specific relief unjust enrichment
""".split())

LEGAL_BOOST = 2.0  # multiplier applied to LEGAL_TERMS


# --------------------------------------------------------------------------- #
# HTTP session helpers
# --------------------------------------------------------------------------- #
def build_session(force_requests=False):
    """Return a session-like object with a .get(url, **kw) method.

    Prefers cloudscraper (handles the bot challenge); falls back to requests.
    """
    if not force_requests and cloudscraper is not None:
        sess = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        return sess
    if requests is not None:
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1"
        })
        print(
            "[warn] cloudscraper not available; using plain requests. "
            "The site's bot protection may block this (HTTP 403).",
            file=sys.stderr,
        )
        return sess
    sys.exit("Neither cloudscraper nor requests is installed. "
             "Run:  pip install cloudscraper requests")


def fetch(session, url, headers=None, retries=3, timeout=30):
    """GET a URL with simple exponential backoff. Returns the Response or None."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp
            last_err = f"HTTP {resp.status_code}"
            # 403/429 -> back off harder
            if resp.status_code in (403, 429, 503):
                time.sleep(2 * attempt)
        except Exception as exc:  # network errors, etc.
            last_err = str(exc)
            time.sleep(1.5 * attempt)
    print(f"[warn] giving up on {url} ({last_err})", file=sys.stderr)
    return None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def parse_results_html(html):
    """Parse a search-results HTML page into a list of result dicts.

    Robust to small markup changes: it primarily looks for the result blocks the
    site uses, and falls back to scanning for /doc/ links if those aren't found.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()

    # Primary path: the site groups each hit in <div class="result"> blocks.
    blocks = soup.select("div.result")

    def add_from_block(block):
        link = block.select_one(".result_title a") or block.find("a", href=DOC_HREF_RE)
        if not link:
            return
        href = link.get("href", "")
        m = DOC_HREF_RE.search(href)
        if not m:
            return
        docid = m.group(1)
        if docid in seen:
            return
        title = _clean(link.get_text(" "))
        if not title:
            return
        src_el = block.find(class_="docsource")
        head_el = block.find(class_="headline")
        date_el = block.find(class_="cite_tag") or block.find(class_="docdate")
        seen.add(docid)
        results.append({
            "docid": docid,
            "title": title,
            "url": urljoin(SITE, f"/doc/{docid}/"),
            "source": _clean(src_el.get_text(" ")) if src_el else "",
            "date": _clean(date_el.get_text(" ")) if date_el else "",
            "snippet": _clean(head_el.get_text(" ")) if head_el else "",
        })

    if blocks:
        for block in blocks:
            add_from_block(block)

    # Fallback: no recognizable result blocks -> scan every /doc/ link.
    if not results:
        for link in soup.find_all("a", href=DOC_HREF_RE):
            href = link.get("href", "")
            m = DOC_HREF_RE.search(href)
            if not m:
                continue
            docid = m.group(1)
            if docid in seen:
                continue
            title = _clean(link.get_text(" "))
            if not title or len(title) < 3:
                continue
            container = link.find_parent("div") or link.parent
            src_el = container.find(class_="docsource") if container else None
            head_el = container.find(class_="headline") if container else None
            seen.add(docid)
            results.append({
                "docid": docid,
                "title": title,
                "url": urljoin(SITE, f"/doc/{docid}/"),
                "source": _clean(src_el.get_text(" ")) if src_el else "",
                "date": "",
                "snippet": _clean(head_el.get_text(" ")) if head_el else "",
            })

    return results


def parse_related_topics_html(html):
    """Best-effort: pull the site's own 'related queries / categories' links."""
    soup = BeautifulSoup(html, "html.parser")
    related = []
    seen = set()
    for a in soup.find_all("a", href=re.compile(r"/search/\?formInput=")):
        text = _clean(a.get_text(" "))
        if text and text.lower() not in seen:
            seen.add(text.lower())
            related.append(text)
    return related


def parse_results_api(payload):
    """Parse the JSON returned by the official search API into result dicts."""
    docs = payload.get("docs", []) or []
    results = []
    for d in docs:
        docid = str(d.get("tid", "")).strip()
        if not docid:
            continue
        results.append({
            "docid": docid,
            "title": _clean(re.sub("<[^>]+>", "", d.get("title", ""))),
            "url": urljoin(SITE, f"/doc/{docid}/"),
            "source": _clean(d.get("docsource", "")),
            "date": _clean(d.get("publishdate", "")),
            "snippet": _clean(re.sub("<[^>]+>", "", d.get("headline", ""))),
        })
    return results


def parse_related_topics_api(payload):
    related = []
    for cat in payload.get("categories", []) or []:
        # cat is typically [category_name, [{value, formInput}, ...]]
        try:
            for item in cat[1]:
                val = _clean(item.get("value", ""))
                if val:
                    related.append(val)
        except (IndexError, TypeError, AttributeError):
            continue
    return related


# --------------------------------------------------------------------------- #
# Turning a free-text case description into a query + ranking by similarity
# --------------------------------------------------------------------------- #
import math
from collections import Counter


def _tokenize(text):
    """Lowercase word tokens, letters only, length >= 3."""
    return re.findall(r"[a-z]{3,}", (text or "").lower())


def extract_keywords(text, max_terms=14):
    """Pull the most salient terms (and key 2-word phrases) from a description.

    Stopwords are dropped; legal/contractual terms are weighted higher; short
    adjacent phrases (e.g. 'software development') are detected and boosted.
    Returns an ordered list of terms (single words and phrases), strongest first.
    """
    raw = _tokenize(text)
    content = [t for t in raw if t not in STOPWORDS]

    freq = Counter(content)
    scored = {}
    for term, count in freq.items():
        weight = count * (LEGAL_BOOST if term in LEGAL_TERMS else 1.0)
        scored[term] = weight

    # Adjacent content-word bigrams become phrase candidates.
    bigrams = Counter()
    prev = None
    for tok in raw:
        if tok in STOPWORDS or len(tok) < 3:
            prev = None
            continue
        if prev:
            bigrams[(prev, tok)] += 1
        prev = tok
    for (a, b), count in bigrams.items():
        phrase = f"{a} {b}"
        weight = (count + 0.5) * 1.5
        if a in LEGAL_TERMS or b in LEGAL_TERMS:
            weight *= LEGAL_BOOST
        scored[phrase] = max(scored.get(phrase, 0.0), weight)

    ranked = sorted(scored.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)
    return [term for term, _ in ranked[:max_terms]]


def build_query(keywords, max_terms=4):
    """Build an Indian Kanoon query (implicit AND) from extracted keywords.

    Uses single words only (phrases would require exact matches and hurt recall);
    near-duplicate stems (e.g. 'delivered'/'deliverables') are collapsed so the
    query stays diverse; capped at max_terms so the AND isn't too strict.
    """
    unigrams = [k for k in keywords if " " not in k]
    chosen, seen_stems = [], set()
    for word in unigrams:
        stem = word[:4]  # cheap stemming: collapse shared 4-char prefixes
        if stem in seen_stems:
            continue
        seen_stems.add(stem)
        chosen.append(word)
        if len(chosen) >= max_terms:
            break
    return " ".join(chosen)


def is_narrative(text):
    text = text.strip()
    # Check if there are multiple sentences
    sentences = re.split(r'\.\s+|\!\s+|\?\s+', text)
    sentences = [s for s in sentences if s.strip()]
    if len(sentences) > 1:
        return True
    # Or if the word count is very high (e.g., > 20 words)
    if len(text.split()) > 20:
        return True
    return False


def prepare_search(user_text, max_query_terms=4):
    """Decide whether the input is a plain query or a full case description.

    Returns (query, rank_text, keywords):
      * short/simple input       -> (input, input, [])
      * long/narrative input     -> (built_query, full_text, keywords)
    """
    user_text_clean = user_text.strip()
    if not is_narrative(user_text_clean):
        # Concise query -> use exactly as search query
        return user_text_clean, user_text_clean, []
            
    # Fallback to standard keyword extraction
    keywords = extract_keywords(user_text_clean)
    query = build_query(keywords, max_terms=max_query_terms)
    if not query:
        query = " ".join(user_text_clean.split()[:4])
    return query, user_text_clean, keywords


def heuristic_law_analysis(results):
    print("\n" + "=" * 70)
    print("  HEURISTIC LEGAL ANALYSIS (Extracted Acts & Sections)")
    print("=" * 70)
    
    all_sec_matches = []
    all_acts = []
    
    # Require Act to be a separate capitalized word
    pattern1 = re.compile(
        r'\bSection\s+(\d+[A-Za-z]*)\s+of\s+(?:the\s+)?([A-Z][a-zA-Z\s]*\bAct\b(?:,\s*\d{4})?)', 
        re.IGNORECASE
    )
    
    pattern_act = re.compile(
        r'\b([A-Z][a-zA-Z\s]*\bAct\b(?:,\s*\d{4})?)'
    )
    
    for r in results:
        text = r.get("full_text", "")
        if not text:
            text = r.get("snippet", "")
        
        # Normalise spaces
        text = re.sub(r'\s+', ' ', text)
        
        # Extract specific section-of-act patterns
        for m in pattern1.finditer(text):
            sec = m.group(1)
            act = m.group(2).strip()
            act = re.sub(r'\s+', ' ', act)
            if act[0].isupper():
                all_sec_matches.append((act, f"Section {sec}"))
                
        # Extract general Act names
        for m in pattern_act.finditer(text):
            act_name = m.group(1).strip()
            act_name = re.sub(r'\s+', ' ', act_name)
            words = act_name.split()
            if len(words) >= 2 and all(w[0].isupper() or w.lower() in ['of', 'and', 'the'] for w in words):
                if words[-1].rstrip(',.').lower() == 'act' or (len(words) > 1 and words[-2].lower() == 'act'):
                    act_clean = act_name.rstrip(',. ')
                    all_acts.append(act_clean)
                    
    sec_counts = Counter(all_sec_matches)
    act_counts = Counter(all_acts)
    
    if not act_counts and not sec_counts:
        print("[info] No specific Acts or Sections could be automatically extracted from the results.")
        return
        
    print("\nFrequently Cited Specific Sections & Acts:")
    if sec_counts:
        for (act, sec), count in sec_counts.most_common(10):
            print(f"  - {sec} of {act} (found in {count} instances)")
    else:
        print("  None detected.")
        
    print("\nFrequently Mentioned Acts:")
    if act_counts:
        for act, count in act_counts.most_common(10):
            print(f"  - {act} (found in {count} instances)")
    else:
        print("  None detected.")
    print("=" * 70)


def rank_by_similarity(description, results):
    """Add a 0..1 `similarity` to each result and sort best-match first.

    Cosine similarity between the description and each result's (title + snippet),
    over the description's vocabulary, TF-IDF weighted across the fetched results,
    with legal terms boosted. No external ML libraries needed.
    """
    desc_tokens = [t for t in _tokenize(description) if t not in STOPWORDS]
    if not desc_tokens or not results:
        for r in results:
            r["similarity"] = 0.0
        return results

    vocab = set(desc_tokens)
    desc_tf = Counter(desc_tokens)

    def result_text(r):
        return f"{r.get('title', '')} {r.get('full_text') or r.get('snippet', '')}"

    # Document frequency of each vocab term across the fetched results.
    docs_tokens, df = [], Counter()
    for r in results:
        toks = [t for t in _tokenize(result_text(r)) if t in vocab]
        docs_tokens.append(toks)
        for t in set(toks):
            df[t] += 1

    n = len(results)
    def idf(t):
        return math.log((n + 1) / (df.get(t, 0) + 1)) + 1.0

    def vectorize(tf):
        return {t: tf[t] * idf(t) * (LEGAL_BOOST if t in LEGAL_TERMS else 1.0)
                for t in tf}

    dvec = vectorize(desc_tf)
    dnorm = math.sqrt(sum(v * v for v in dvec.values())) or 1.0

    for r, toks in zip(results, docs_tokens):
        rvec = vectorize(Counter(toks))
        dot = sum(dvec.get(t, 0.0) * rvec.get(t, 0.0) for t in rvec)
        rnorm = math.sqrt(sum(v * v for v in rvec.values())) or 1.0
        r["similarity"] = round(dot / (dnorm * rnorm), 4)

    results.sort(key=lambda r: r["similarity"], reverse=True)
    return results


def read_multiline(prompt):
    """Read possibly-multi-line input; finishes on a blank line after content."""
    print(prompt)
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "":
            if lines:
                break
            continue
        lines.append(line)
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# Fetching the FULL document behind each result link (/doc/<id>/)
# --------------------------------------------------------------------------- #
def _collapse_blanklines(text):
    return re.sub(r"\n{3,}", "\n\n", (text or "")).strip()


# Block-level tags whose boundaries should become line breaks; inline tags
# (like <b>, <i>, <a>, <span>) must NOT, or words get split across lines.
_BLOCK_TAGS = ["p", "div", "br", "li", "tr", "pre", "blockquote",
               "h1", "h2", "h3", "h4", "h5", "h6"]


def _block_text(node):
    """Get readable text: paragraph breaks at block tags, spaces within them."""
    if node is None:
        return ""
    for tag in node.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    raw = node.get_text(" ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.split("\n")]
    return _collapse_blanklines("\n".join(lines))


def parse_document_html(html, docid):
    """Extract title, court, date and the full judgment text from a doc page."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    title_el = soup.find("h2", class_="doc_title")
    title = _clean(title_el.get_text(" ")) if title_el else _clean(
        soup.title.get_text() if soup.title else "")

    src_el = (soup.find("h2", class_="docsource_main")
              or soup.find(class_="docsource")
              or soup.find("div", class_="doc_bench"))
    source = _clean(src_el.get_text(" ")) if src_el else ""

    # The judgment body lives in <div class="judgments">; fall back to the
    # largest text-bearing <div> if the class isn't present.
    body = soup.find("div", class_="judgments")
    if body is None:
        divs = soup.find_all("div")
        body = max(divs, key=lambda d: len(d.get_text()), default=None)
    if body is not None:
        full_text = _block_text(body)
    else:
        full_text = _clean(soup.get_text(" "))

    date = ""
    m = re.search(r"\bon\s+(\d{1,2}\s+\w+,?\s+\d{4})", title)
    if m:
        date = m.group(1)

    return {
        "docid": str(docid), "title": title, "source": source, "date": date,
        "url": urljoin(SITE, f"/doc/{docid}/"), "full_text": full_text,
    }


def parse_document_api(payload, docid):
    raw_html = payload.get("doc", "") or ""
    text = _block_text(BeautifulSoup(raw_html, "html.parser"))
    return {
        "docid": str(docid),
        "title": _clean(re.sub("<[^>]+>", "", payload.get("title", ""))),
        "source": _clean(payload.get("docsource", "")),
        "date": _clean(payload.get("publishdate", "")),
        "url": urljoin(SITE, f"/doc/{docid}/"),
        "full_text": text,
    }


def fetch_document(session, docid, api_token=None):
    """Fetch and parse one full document. Returns a dict or None on failure."""
    if api_token:
        url = f"{API}/doc/{docid}/"
        headers = {"Authorization": f"Token {api_token}", "Accept": "application/json"}
        for attempt in range(1, 4):
            try:
                resp = session.post(url, headers=headers, timeout=40)
                if resp.status_code == 200:
                    return parse_document_api(resp.json(), docid)
                time.sleep(1.5 * attempt)
            except Exception:
                time.sleep(1.5 * attempt)
        return None
    url = f"{SITE}/doc/{docid}/"
    resp = fetch(session, url)
    if resp is None:
        return None
    return parse_document_html(resp.text, docid)


def enrich_with_full_text(session, results, using_api, api_token, max_docs, delay):
    """Visit each result's document page and attach its full text + clean metadata."""
    n = min(len(results), max_docs) if max_docs else len(results)
    print(f"[info] fetching full text for {n} document(s) "
          f"(of {len(results)} found)...")
    for i, r in enumerate(results):
        if max_docs and i >= max_docs:
            break
        doc = fetch_document(session, r["docid"], api_token if using_api else None)
        if doc and doc.get("full_text"):
            # Trust the document page over the (sometimes buggy) listing snippet.
            r["full_text"] = doc["full_text"]
            r["chars"] = len(doc["full_text"])
            if doc.get("title"):
                r["title"] = doc["title"]
            if doc.get("source"):
                r["source"] = doc["source"]
            if doc.get("date"):
                r["date"] = doc["date"]
            if doc.get("url"):
                r["url"] = doc["url"]
            print(f"[info]   [{i + 1}/{n}] {r['title'][:70]} "
                  f"({r['chars']} chars)")
        else:
            print(f"[warn]   [{i + 1}/{n}] could not fetch doc {r['docid']}")
        if i < n - 1:
            time.sleep(delay)
    return results


# --------------------------------------------------------------------------- #
# Fetching pages (scrape vs API)
# --------------------------------------------------------------------------- #
def search_page_scrape(session, query, pagenum):
    url = f"{SITE}/search/?formInput={quote_plus(query)}&pagenum={pagenum}"
    resp = fetch(session, url)
    if resp is None:
        return None, []
    html = resp.text
    return parse_results_html(html), parse_related_topics_html(html)


def search_page_api(session, query, pagenum, token):
    # The API uses POST and an Authorization: Token header.
    url = f"{API}/search/?formInput={quote_plus(query)}&pagenum={pagenum}"
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    last_err = None
    for attempt in range(1, 4):
        try:
            resp = session.post(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                payload = resp.json()
                return parse_results_api(payload), parse_related_topics_api(payload)
            last_err = f"HTTP {resp.status_code}"
            time.sleep(1.5 * attempt)
        except Exception as exc:
            last_err = str(exc)
            time.sleep(1.5 * attempt)
    print(f"[warn] API request failed for page {pagenum} ({last_err})", file=sys.stderr)
    return None, []


# --------------------------------------------------------------------------- #
# Filtering / ranking by topic
# --------------------------------------------------------------------------- #
def score_result(result, keywords):
    """Score a result by how well its title + snippet match the topic keywords.

    Title matches are weighted more heavily than snippet matches.
    Returns 0 if it matches nothing.
    """
    if not keywords:
        return 1  # no topic given -> keep everything, neutral score
    title = result["title"].lower()
    snippet = result["snippet"].lower()
    score = 0
    for kw in keywords:
        score += 3 * title.count(kw)
        score += 1 * snippet.count(kw)
    return score


def filter_and_rank(results, topic):
    keywords = [w for w in re.split(r"\s+", topic.lower().strip()) if w]
    scored = []
    for r in results:
        s = score_result(r, keywords)
        if s > 0:
            r = dict(r, _score=s)
            scored.append(r)
    scored.sort(key=lambda r: r["_score"], reverse=True)
    return scored


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def _slug(text, maxlen=60):
    s = re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")
    return (s[:maxlen] or "doc").rstrip("_")


def save_articles(results, basename):
    """Write each result's full text to <basename>_articles/<docid>_<title>.txt."""
    folder = f"{basename}_articles"
    written = []
    have_text = [r for r in results if r.get("full_text")]
    if not have_text:
        return folder, written
    os.makedirs(folder, exist_ok=True)
    for r in have_text:
        fname = f"{r['docid']}_{_slug(r.get('title', ''))}.txt"
        path = os.path.join(folder, fname)
        header = (
            f"{r.get('title', '')}\n"
            f"{r.get('source', '')}  {r.get('date', '')}\n"
            f"{r.get('url', '')}\n"
            + "=" * 70 + "\n\n"
        )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(header + r["full_text"])
        r["article_path"] = path
        written.append(path)
    return folder, written


def save_outputs(results, related, basename, save_full=True):
    csv_path = f"{basename}.csv"
    json_path = f"{basename}.json"

    articles_folder = None
    if save_full:
        articles_folder, _ = save_articles(results, basename)

    fields = ["docid", "title", "source", "date", "url", "similarity",
              "chars", "article_path", "snippet", "_score"]
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            writer.writerow(r)
    with open(json_path, "w", encoding="utf-8") as fh:
        # JSON keeps the full text inline so everything is in one place.
        json.dump({"results": results, "related_topics": related},
                  fh, ensure_ascii=False, indent=2)
    return csv_path, json_path, articles_folder


def print_results(results, related, limit=25, print_full=False):
    print("\n" + "=" * 70)
    print(f"  {len(results)} matching result(s)")
    print("=" * 70)
    for i, r in enumerate(results[:limit], 1):
        print(f"\n[{i}] {r.get('title', '(untitled)')}")
        meta = " | ".join(x for x in [r.get("source"), r.get("date")] if x)
        if meta:
            print(f"    {meta}")
        if r.get("url"):
            print(f"    {r['url']}")
        if "similarity" in r:
            print(f"    (similarity: {r['similarity'] * 100:.1f}%)")
        elif r.get("_score", "") != "":
            print(f"    (relevance score: {r['_score']})")
        body = r.get("full_text")
        if body:
            if print_full:
                print("    " + "-" * 60)
                print(body)
                print("    " + "-" * 60)
            else:
                excerpt = body[:500].replace("\n", " ")
                print(f"    {excerpt}{'...' if len(body) > 500 else ''}")
                if r.get("article_path"):
                    print(f"    [full article saved -> {r['article_path']}]")
        elif r.get("snippet"):
            snip = r["snippet"]
            print(f"    {snip[:240]}{'...' if len(snip) > 240 else ''}")
    if len(results) > limit:
        print(f"\n... and {len(results) - limit} more (see the saved files).")
    if related:
        print("\n" + "-" * 70)
        print("  Related topics suggested by the site:")
        print("-" * 70)
        for t in related[:30]:
            print(f"    - {t}")


# --------------------------------------------------------------------------- #
# Main driver
# --------------------------------------------------------------------------- #
def run(query, topic, pages, start_page, delay, api_token, out, force_requests,
        rank_text=None, fetch_full=True, max_docs=20, print_full=False):
    using_api = bool(api_token)
    if using_api:
        if requests is None:
            sys.exit("API mode needs the 'requests' library: pip install requests")
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        print(f"[info] API mode (api.indiankanoon.org)")
    else:
        session = build_session(force_requests=force_requests)
        engine = "cloudscraper" if (cloudscraper and not force_requests) else "requests"
        print(f"[info] Scrape mode ({engine})")

    topic_label = repr(topic) if topic.strip() else "none"
    print(f"[info] query={query!r}  topic={topic_label}  "
          f"pages={start_page}..{start_page + pages - 1}")

    all_results = []
    all_related = []
    seen_docids = set()

    for offset in range(pages):
        pagenum = start_page + offset
        if using_api:
            page_results, page_related = search_page_api(session, query, pagenum, api_token)
        else:
            page_results, page_related = search_page_scrape(session, query, pagenum)

        if page_results is None:
            print(f"[warn] page {pagenum}: request failed, stopping.", file=sys.stderr)
            break
        if not page_results:
            print(f"[info] page {pagenum}: no results -> reached the end.")
            break

        new = 0
        for r in page_results:
            if r["docid"] in seen_docids:
                continue
            seen_docids.add(r["docid"])
            all_results.append(r)
            new += 1
        for t in page_related:
            if t not in all_related:
                all_related.append(t)

        print(f"[info] page {pagenum}: {len(page_results)} results ({new} new)")

        if offset < pages - 1:
            time.sleep(delay)

    print(f"\n[info] collected {len(all_results)} unique result(s) total.")

    # Fetch the full article behind each result link before ranking, so
    # similarity is computed on the real judgment text (not the snippet).
    if fetch_full and all_results:
        if max_docs and max_docs > 0 and len(all_results) > max_docs:
            print(f"[info] limiting full-text fetch to the first {max_docs} "
                  f"of {len(all_results)} results (use --max-docs to change).")
            all_results = all_results[:max_docs]
        enrich_with_full_text(session, all_results, using_api, api_token, 0, delay)

    if rank_text and rank_text.strip():
        ranked = rank_by_similarity(rank_text, all_results)
        print(f"[info] ranked {len(ranked)} result(s) by similarity to your description.")
    elif topic.strip():
        ranked = filter_and_rank(all_results, topic)
        print(f"[info] {len(ranked)} of them match the topic {topic!r}.")
    else:
        ranked = all_results

    print_results(ranked, all_related, print_full=print_full)

    if ranked:
        if out:
            csv_path, json_path, articles_folder = save_outputs(
                ranked, all_related, out, save_full=fetch_full)
            print(f"\n[info] saved -> {csv_path}")
            print(f"[info] saved -> {json_path}")
            if articles_folder and any(r.get("article_path") for r in ranked):
                print(f"[info] full articles saved in -> {articles_folder}/")
        else:
            print("\n[info] Running in-memory mode. No CSV, JSON, or articles folder saved to disk.")
            print("[info] (To save these results, run again with: --out <filename_base>)")
            
        # Advocate / Legal Analysis execution
        heuristic_law_analysis(ranked)
    else:
        print("\n[info] nothing to save.")

    return ranked


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Scrape and topic-filter Indian Kanoon search results.")
    p.add_argument("--query", help="Search query (formInput), e.g. kapoor")
    p.add_argument("--describe",
                   help="A full case description; keywords are auto-extracted and "
                        "results are ranked by similarity to it.")
    p.add_argument("--describe-file",
                   help="Path to a text file containing the case description.")
    p.add_argument("--max-query-terms", type=int, default=4,
                   help="Max keywords ANDed into the auto-built query (default 4).")
    p.add_argument("--topic", default="",
                   help="Keyword(s) to filter/rank results by. Blank = keep all.")
    p.add_argument("--pages", type=int, default=2, help="How many pages to scan (default 2).")
    p.add_argument("--start-page", type=int, default=0,
                   help="First page number (site/API start at 0).")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Seconds to wait between page requests (be polite).")
    p.add_argument("--api-token", default=None,
                   help="Use the official API with this token instead of scraping.")
    p.add_argument("--out", default=None,
                   help="Output filename base (no extension) to save CSV/JSON. Default is None (do not save).")
    p.add_argument("--force-requests", action="store_true",
                   help="Force plain requests even if cloudscraper is installed.")
    p.add_argument("--no-input", action="store_true",
                   help="Don't prompt; use args/defaults only.")
    p.add_argument("--no-full", dest="fetch_full", action="store_false",
                   help="Do NOT open each result link; keep snippets only (faster).")
    p.add_argument("--max-docs", type=int, default=20,
                   help="Max number of full articles to fetch (default 20). 0 = all.")
    p.add_argument("--print-full", action="store_true",
                   help="Print full article text to the console (otherwise excerpt).")
    p.set_defaults(fetch_full=True)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    query = None
    rank_text = None
    topic = args.topic
    keywords = []

    # 1) Figure out what to search and what to rank against.
    description = ""
    if args.describe_file:
        with open(args.describe_file, encoding="utf-8") as fh:
            description = fh.read().strip()
    elif args.describe:
        description = args.describe

    if description:
        query, rank_text, keywords = prepare_search(description, args.max_query_terms)
    elif args.query:
        query, rank_text, keywords = prepare_search(args.query, args.max_query_terms)
    elif not args.no_input:
        user_text = read_multiline(
            "Enter a search term OR paste a full case description.\n"
            "(Finish by pressing Enter on an empty line; blank = 'kapoor')"
        )
        if not user_text:
            user_text = "kapoor"
        query, rank_text, keywords = prepare_search(user_text, args.max_query_terms)
    else:
        query = "kapoor"
        rank_text = "kapoor"

    # 2) If we auto-built a query from a description, show it and allow edits.
    if keywords:
        print("\n[info] extracted keywords/query terms: " + ", ".join(keywords))
        print(f"[info] auto-built query : {query}")
        if not args.no_input:
            edited = input(
                "Press Enter to use this query, or type your own "
                "(e.g. add \"breach of contract\"): "
            ).strip()
            if edited:
                query = edited

    # 3) Remaining prompts (only in interactive mode).
    if not args.no_input:
        if not rank_text and not topic:
            topic = input("Topic to filter by (blank = keep all): ").strip()
        if args.pages == 2:  # still default (changed to 2) -> let the user change it
            raw = input("How many pages to scan? [2]: ").strip()
            if raw.isdigit():
                args.pages = int(raw)

    run(
        query=query,
        topic=topic,
        pages=max(1, args.pages),
        start_page=max(0, args.start_page),
        delay=max(0.0, args.delay),
        api_token=args.api_token,
        out=args.out,
        force_requests=args.force_requests,
        rank_text=rank_text,
        fetch_full=args.fetch_full,
        max_docs=max(0, args.max_docs),
        print_full=args.print_full,
    )


if __name__ == "__main__":
    main()