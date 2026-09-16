"""
app/api/routes_bigquery.py

FastAPI route for natural-language BigQuery search over the diamond growth database.

POST /bigquery/search
  body: { "question": "show me samples with growth temp above 900" }
  returns: a structured database evidence packet

The route delegates to agent/nodes/retrieve_bigquery (the database agent node),
which converts the database question to one typed query specification, compiles
parameterized SQL, and returns the executed rows without post-query projection.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.nodes.retrieve_bigquery import retrieve_bigquery
from agent.state import AgentState, UserQuery

router = APIRouter(prefix="/bigquery", tags=["bigquery"])


class SearchRequest(BaseModel):
    question: str


@router.post("/search")
def search(req: SearchRequest):
    if not req.question.strip():
        raise HTTPException(400, "question must not be empty")

    model = os.environ.get("ANTHROPIC_MODEL")
    if not model:
        raise HTTPException(503, "ANTHROPIC_MODEL env var is not set")

    state = AgentState(query=UserQuery(raw_text=req.question))
    result = retrieve_bigquery(state, model=model)
    return result.database_evidence
