# PubMed MCP Server (FastAPI)

A minimal local HTTP service that bridges Claude (via MCP-style tool config) to NCBI PubMed E-utilities.

## Features
- `/search`: PubMed search (returns PMIDs)
- `/summary` and `/summary_batch`: metadata summaries
- `/abstract` and `/abstract_batch`: parsed title/abstract/authors/year/journal from PubMed XML
- `/links`: cited-in or references linkouts (NCBI elink)
- `/resolve`: one-shot bundle for literature mining (search + summaries + abstracts)

## Install
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.env .env
# edit .env to set PUBMED_EMAIL (recommended)
