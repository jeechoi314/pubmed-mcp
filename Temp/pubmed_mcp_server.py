from fastapi import FastAPI
import reques
app = FastAPI()

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

@app.get("/search")
def search_pubmed(query: str, retmax: int = 10):

    url = f"{BASE}/esearch.fcgi"
    
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": retmax
    }

    r = requests.get(url, params=params)
    data = r.json()

    return data


@app.get("/summary")
def get_summary(pmid: str):

    url = f"{BASE}/esummary.fcgi"

    params = {
        "db": "pubmed",
        "id": pmid,
        "retmode": "json"
    }

    r = requests.get(url, params=params)

    return r.json()

