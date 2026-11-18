"""Truthful lexical fallback when semantic retrieval is unavailable."""
import logging
import re
logger = logging.getLogger(__name__)

async def keyword_search(repo, user_id, query, memory_type=None, limit=10):
    terms = set(re.findall(r"\w+", query.casefold()))
    rows = await repo.list_memories(user_id=user_id, memory_type=memory_type, limit=1000, offset=0)
    matches = []
    for row in rows:
        words = set(re.findall(r"\w+", str(row.get("content", "")).casefold()))
        overlap = terms & words
        if overlap:
            matches.append({**row, "similarity_score": len(overlap) / max(len(terms), 1), "retrieval_mode": "keyword"})
    return sorted(matches, key=lambda row: row["similarity_score"], reverse=True)[:limit]
