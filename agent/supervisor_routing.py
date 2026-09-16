"""Dependency-free routing decisions shared by the supervisor and batch harness."""

from agent.request_plan import (
    KnowledgeSource,
    RequestPlan,
    RequestStatus,
    document_scopes_for_sources,
)


def routed_agents(plan: RequestPlan) -> list[str]:
    """Return the retrieval lanes the supervisor would page for ``plan``."""
    if plan.status is not RequestStatus.READY:
        return []

    agents: list[str] = []
    if KnowledgeSource.STRUCTURED_DATA in plan.knowledge_sources:
        agents.append("table_agent")
    agents.extend(
        f"{scope}_document_agent"
        for scope in document_scopes_for_sources(plan.knowledge_sources)
    )
    return agents
