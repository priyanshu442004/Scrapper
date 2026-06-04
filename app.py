import re
import io
import sys
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from scraper import (
    build_session,
    search_page_scrape,
    enrich_with_full_text,
    rank_by_similarity,
    print_results,
    heuristic_law_analysis,
    fetch_document
)

app = FastAPI(
    title="Indian Kanoon Legal Scraper API",
    description="FastAPI wrapper for Indian Kanoon search and document extraction.",
    version="1.0.0"
)

class SimilarCasesRequest(BaseModel):
    best_keyword: List[str] = Field(alias="best-keyword")

    model_config = {
        "populate_by_name": True
    }

class DetailedCaseRequest(BaseModel):
    link: str

def extract_docid(url: str) -> Optional[str]:
    match = re.search(r'/doc/(\d+)', url)
    if match:
        return match.group(1)
    if url.strip().isdigit():
        return url.strip()
    return None

@app.post("/similar-cases")
async def get_similar_cases(payload: SimilarCasesRequest):
    keywords_list = payload.best_keyword
    if not keywords_list:
        raise HTTPException(status_code=400, detail="best_keyword list cannot be empty")
        
    captured_output = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured_output
    
    ranked = []
    all_related = []
    
    try:
        session = build_session(force_requests=False)
        engine = "cloudscraper" if "CloudScraper" in type(session).__name__ else "requests"
        print(f"[info] Scrape mode ({engine})")
        
        all_results = []
        seen_docids = set()
        
        # 1. Search page 0 for each keyword
        for kw in keywords_list:
            query = kw.strip()
            if "doctype" not in query.lower():
                query = f"{query}  doctypes: judgments"
                
            print(f"[info] query={query!r}  topic=none  pages=0..0")
            
            try:
                page_results, page_related = search_page_scrape(session, query, 0)
                if page_results is None:
                    print("[warn] page 0: request failed")
                    continue
                    
                new = 0
                if page_results:
                    for r in page_results:
                        if r["docid"] not in seen_docids:
                            seen_docids.add(r["docid"])
                            all_results.append(r)
                            new += 1
                    for t in page_related:
                        if t not in all_related:
                            all_related.append(t)
                            
                print(f"[info] page 0: {len(page_results) if page_results else 0} results ({new} new)")
            except Exception as e:
                print(f"[warn] Search failed for keyword {kw!r}: {e}")
                
        if not all_results:
            print("\n[info] nothing to save.")
            output_str = captured_output.getvalue()
            return {
                "output": output_str,
                "results": []
            }
            
        print(f"\n[info] collected {len(all_results)} unique result(s) total.")
        
        # 2. Fetch full text (up to 20 results by default)
        to_fetch = all_results[:20]
        try:
            enrich_with_full_text(session, to_fetch, False, None, 0, 1.0)
        except Exception as e:
            print(f"[warn] Failed to enrich results with full text: {e}")
            
        # 3. Rank results by similarity to the concatenated keywords list
        rank_text = " ".join(keywords_list)
        try:
            ranked = rank_by_similarity(rank_text, to_fetch)
            print(f"[info] ranked {len(ranked)} result(s) by similarity to your description.")
        except Exception as e:
            print(f"[warn] Similarity ranking failed: {e}. Returning unsorted results.")
            ranked = to_fetch
            
        # 4. Print results & Heuristic citations just like CLI
        print_results(ranked, all_related, print_full=False)
        heuristic_law_analysis(ranked)
        
        output_str = captured_output.getvalue()
    except Exception as e:
        output_str = f"Error executing similar cases search: {str(e)}"
    finally:
        sys.stdout = old_stdout
        
    # 5. Clean results before returning
    sanitized_results = []
    for r in ranked:
        sanitized_results.append({
            "docid": r.get("docid"),
            "title": r.get("title"),
            "source": r.get("source"),
            "date": r.get("date"),
            "url": r.get("url"),
            "similarity": r.get("similarity", 0.0),
            "snippet": r.get("snippet", "")
        })
        
    return {
        "output": output_str,
        "results": sanitized_results
    }

@app.post("/detailed-cases")
async def get_detailed_case(payload: DetailedCaseRequest):
    url = payload.link
    docid = extract_docid(url)
    if not docid:
        raise HTTPException(status_code=400, detail="Could not extract docid from the provided link")
        
    session = build_session(force_requests=False)
    try:
        doc = fetch_document(session, docid, None)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch document from Indian Kanoon: {str(e)}")
        
    if not doc or not doc.get("full_text"):
        raise HTTPException(status_code=404, detail=f"Document with ID {docid} not found or has empty content")
        
    return doc

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5000)
