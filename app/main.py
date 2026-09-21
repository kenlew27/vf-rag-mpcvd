"""
app/main.py

FastAPI entrypoint for the materials decision-support agent.

Run:  uvicorn app.main:app --reload
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth import authenticate_proxy_request
from app.api.routes_agent import router as agent_router
from app.api.routes_bigquery import router as bigquery_router
from app.api.routes_documents import router as documents_router
from app.api.routes_ingestion import (
    router as ingestion_router,
    start_job_status_listener,
    stop_job_status_listener,
)
from app.api.routes_vector_search import router as vector_search_router

from tools.retrieval.reranker import warm_default_leaf_reranker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        warm_default_leaf_reranker()
    except Exception:
        logger.warning("Default leaf reranker warmup failed during startup.", exc_info=True)
    start_job_status_listener()
    try:
        yield
    finally:
        await stop_job_status_listener()


app = FastAPI(title="Materials Decision-Support Agent", lifespan=lifespan)


@app.middleware("http")
async def proxy_auth_boundary(request, call_next):
    try:
        authenticate_proxy_request(request)
    except HTTPException as exc:
        return JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )
    return await call_next(request)

# CORS: list exact frontend origins (do not use "*" with credentials).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # extend with production frontend origin before deployment
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=True,
)

app.include_router(documents_router)
app.include_router(ingestion_router)
app.include_router(vector_search_router)
app.include_router(bigquery_router)
app.include_router(agent_router)


@app.get("/")
def root():
    return {"service": "materials-decision-agent", "status": "ok"}
