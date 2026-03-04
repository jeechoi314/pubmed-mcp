from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
import xmltodict
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query

load_dotenv()

app = FastAPI(title="PubMed MCP Server", version="1.0.0")

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

TOOL = os.getenv("PUBMED_TOOL", "pubmed-mcp")
EMAIL = os.getenv("PUBMED_EMAIL", "")
API_KEY = os.getenv("NCBI_API_KEY", "") or None
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20"))

# ---- Simple in-memory cache (per-process) ----
_CACHE: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], Tuple[float, Any]] = {}
CACHE_TTL_SEC = 300  # 5 minutes


def _cache_key(url: str, params: Dict[str, Any]) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
    # Make params stable
    items: List[Tuple[str, str]] = []
    for k, v in params.items():
        if v is None:
            continue
        items.append((str(k), str(v)))
    items.sort()
    return (url, tuple(items))


def _cache_get(key):
    item = _CACHE.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > CACHE_TTL_SEC:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_set(key, val):
    _CACHE[key] = (time.time(), val)


def _ncbi_params(extra: Dict[str, Any]) -> Dict[str, Any]:
    params = dict(extra)
    params["tool"] = TOOL
    if EMAIL:
        params["email"] = EMAIL
    if API_KEY:
        params["api_key"] = API_KEY
    return params


def _get_json(url: str, params: Dict[str, Any]) -> Any:
    params = _ncbi_params(params)
    key = _cache_key(url, params)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"NCBI request failed: {e}")

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"NCBI returned {r.status_code}: {r.text[:200]}")

    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail=f"NCBI response is not JSON: {r.text[:200]}")

    _cache_set(key, data)
    return data


def _get_text(url: str, params: Dict[str, Any]) -> str:
    params = _ncbi_params(params)
    key = _cache_key(url, params)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"NCBI request failed: {e}")

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"NCBI returned {r.status_code}: {r.text[:200]}")

    _cache_set(key, r.text)
    return r.text


def _parse_abstract_from_pubmed_xml(xml_text: str) -> Dict[str, Any]:
    """
    Parse PubMed XML (efetch) into a minimal dict:
    - pmid
    - title
    - abstract (string or list if structured)
    - journal
    - year
    - authors (list)
    """
    try:
        doc = xmltodict.parse(xml_text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to parse XML: {e}")

    articles = []
    root = doc.get("PubmedArticleSet", {})
    pa = root.get("PubmedArticle")

    if pa is None:
        return {"articles": []}

    if isinstance(pa, dict):
        pa = [pa]

    for item in pa:
        medline = item.get("MedlineCitation", {})
        article = medline.get("Article", {})
        pmid = medline.get("PMID", "")
        if isinstance(pmid, dict):
            pmid = pmid.get("#text", "")

        title = article.get("ArticleTitle", "")

        journal = (article.get("Journal", {}) or {}).get("Title", "")
        journal_issue = (article.get("Journal", {}) or {}).get("JournalIssue", {}) or {}
        pub_date = journal_issue.get("PubDate", {}) or {}
        year = pub_date.get("Year", "") or pub_date.get("MedlineDate", "")

        # Authors
        authors_out: List[str] = []
        author_list = article.get("AuthorList", {}) or {}
        authors = author_list.get("Author")
        if authors:
            if isinstance(authors, dict):
                authors = [authors]
            for a in authors:
                last = a.get("LastName", "")
                fore = a.get("ForeName", "")
                if last and fore:
                    authors_out.append(f"{fore} {last}")
                elif last:
                    authors_out.append(last)

        # Abstract
        abstract = ""
        abstract_obj = article.get("Abstract")
        if isinstance(abstract_obj, dict):
            abs_text = abstract_obj.get("AbstractText")
            # AbstractText can be str, dict(with Label), or list
            if isinstance(abs_text, str):
                abstract = abs_text
            elif isinstance(abs_text, dict):
                label = abs_text.get("@Label") or abs_text.get("@NlmCategory") or ""
                txt = abs_text.get("#text", "")
                abstract = f"{label}: {txt}".strip(": ").strip()
            elif isinstance(abs_text, list):
                parts = []
                for part in abs_text:
                    if isinstance(part, str):
                        parts.append(part)
                    elif isinstance(part, dict):
                        label = part.get("@Label") or part.get("@NlmCategory") or ""
                        txt = part.get("#text", "")
                        if label:
                            parts.append(f"{label}: {txt}")
                        else:
                            parts.append(txt)
                abstract = "\n".join([p for p in parts if p])
        elif isinstance(abstract_obj, str):
            abstract = abstract_obj

        articles.append(
            {
                "pmid": str(pmid),
                "title": title,
                "abstract": abstract,
                "journal": journal,
                "year": year,
                "authors": authors_out,
            }
        )

    return {"articles": articles}


@app.get("/health")
def health():
    return {"status": "ok", "server": "pubmed-mcp", "base": BASE}


@app.get("/search")
def search_pubmed(
    query: str = Query(..., description="PubMed query string"),
    retmax: int = Query(20, ge=1, le=200),
    sort: str = Query("relevance", description="relevance|pub_date"),
    mindate: Optional[str] = Query(None, description="YYYY or YYYY/MM/DD"),
    maxdate: Optional[str] = Query(None, description="YYYY or YYYY/MM/DD"),
):
    """
    PubMed search → returns PMIDs (and count).
    """
    url = f"{BASE}/esearch.fcgi"
    params: Dict[str, Any] = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": retmax,
        "sort": sort,
    }
    # Date range if provided
    if mindate or maxdate:
        params["datetype"] = "pdat"
        if mindate:
            params["mindate"] = mindate
        if maxdate:
            params["maxdate"] = maxdate

    data = _get_json(url, params)
    return data


@app.get("/summary")
def summary_pubmed(
    pmid: str = Query(..., description="Single PMID"),
):
    """
    PubMed summary for one PMID (NCBI esummary JSON).
    """
    url = f"{BASE}/esummary.fcgi"
    params = {"db": "pubmed", "id": pmid, "retmode": "json"}
    return _get_json(url, params)


@app.get("/summary_batch")
def summary_batch(
    pmids: str = Query(..., description="Comma-separated PMIDs"),
):
    """
    Batch summary: pass pmids like '123,456,789'
    """
    url = f"{BASE}/esummary.fcgi"
    params = {"db": "pubmed", "id": pmids, "retmode": "json"}
    return _get_json(url, params)


@app.get("/fetch_xml")
def fetch_xml(
    pmid: str = Query(..., description="Single PMID"),
):
    """
    PubMed efetch (XML). Useful for full parsing.
    """
    url = f"{BASE}/efetch.fcgi"
    params = {"db": "pubmed", "id": pmid, "retmode": "xml"}
    return {"pmid": pmid, "xml": _get_text(url, params)}


@app.get("/abstract")
def abstract(
    pmid: str = Query(..., description="Single PMID"),
):
    """
    Returns parsed title/abstract/authors/year/journal from efetch XML.
    """
    url = f"{BASE}/efetch.fcgi"
    params = {"db": "pubmed", "id": pmid, "retmode": "xml"}
    xml_text = _get_text(url, params)
    parsed = _parse_abstract_from_pubmed_xml(xml_text)
    # For convenience return the first record when a single pmid
    if parsed["articles"]:
        return parsed["articles"][0]
    return {"pmid": pmid, "title": "", "abstract": "", "journal": "", "year": "", "authors": []}


@app.get("/abstract_batch")
def abstract_batch(
    pmids: str = Query(..., description="Comma-separated PMIDs (recommend <= 50)"),
):
    """
    Batch abstract parsing via efetch XML.
    """
    url = f"{BASE}/efetch.fcgi"
    params = {"db": "pubmed", "id": pmids, "retmode": "xml"}
    xml_text = _get_text(url, params)
    return _parse_abstract_from_pubmed_xml(xml_text)


@app.get("/links")
def links(
    pmid: str = Query(..., description="Single PMID"),
    linkname: str = Query("pubmed_pubmed_citedin", description="e.g., pubmed_pubmed_citedin or pubmed_pubmed_refs"),
):
    """
    PubMed elink:
    - citedin: papers that cite this PMID (when available in NCBI links)
    - refs: references from this PMID (when available)
    """
    url = f"{BASE}/elink.fcgi"
    params = {"dbfrom": "pubmed", "id": pmid, "linkname": linkname, "retmode": "json"}
    return _get_json(url, params)


@app.get("/resolve")
def resolve_literature_mining_bundle(
    query: str = Query(..., description="PubMed query string"),
    retmax: int = Query(20, ge=1, le=100),
    mindate: Optional[str] = Query(None, description="YYYY or YYYY/MM/DD"),
    maxdate: Optional[str] = Query(None, description="YYYY or YYYY/MM/DD"),
):
    """
    One-shot helper:
    - search → pmids
    - batch summary
    - batch abstract (parsed)
    Returns a compact bundle for literature mining.
    """
    search_data = search_pubmed(query=query, retmax=retmax, sort="relevance", mindate=mindate, maxdate=maxdate)
    ids = search_data.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return {"query": query, "count": 0, "pmids": [], "summaries": {}, "abstracts": []}

    pmids_csv = ",".join(ids)

    summaries = summary_batch(pmids=pmids_csv)
    abstracts = abstract_batch(pmids=pmids_csv)

    return {
        "query": query,
        "count": len(ids),
        "pmids": ids,
        "summaries": summaries,
        "abstracts": abstracts.get("articles", []),
    }
