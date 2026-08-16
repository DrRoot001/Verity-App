"""Candidate Graph retrieval invariants (PRD §12.3, §12.6, AC-SEC-001).

These assert the guarantees Grounded Candidate Mode rests on. If any of them
regress, the copilot can assert something the candidate never confirmed — or
something belonging to a different person.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.candidate_graph.embeddings import EntityType, GraphEmbedding
from verity.modules.candidate_graph.indexer import GraphIndexer, build_projection
from verity.modules.candidate_graph.models import (
    Achievement,
    CandidateStory,
    Experience,
    NodeStatus,
    Project,
    ProvenanceSource,
    StoryStatus,
)
from verity.modules.candidate_graph.retrieval import GraphRetrievalService, RetrievalQuery
from verity.modules.identity.models import User

pytestmark = pytest.mark.integration


async def _approved_experience(
    db: AsyncSession, user: User, *, title: str, company: str, description: str = ""
) -> Experience:
    row = Experience(
        user_id=user.id,
        company_name=company,
        title=title,
        description=description,
        start_month=dt.date(2021, 1, 1),
        end_month=dt.date(2024, 6, 1),
        status=NodeStatus.APPROVED,
        source=ProvenanceSource.RESUME,
        confirmed_at=dt.datetime.now(dt.UTC),
    )
    db.add(row)
    await db.flush()
    return row


# ── Approved-only guarantee (FR-GRAPH-001) ───────────────────────────


async def test_unapproved_nodes_are_never_indexed(
    db: AsyncSession, user: User, indexer: GraphIndexer
) -> None:
    pending = Experience(
        user_id=user.id,
        company_name="Northwind Systems",
        title="Staff Engineer",
        status=NodeStatus.PENDING_REVIEW,
        source=ProvenanceSource.RESUME,
    )
    db.add(pending)
    await db.flush()

    written = await indexer.index_node(
        user_id=user.id, entity_type=EntityType.EXPERIENCE, row=pending
    )

    assert written is False
    count = await db.scalar(
        select(GraphEmbedding).where(GraphEmbedding.entity_id == pending.id).exists().select()
    )
    assert count is False


async def test_unapproving_a_node_removes_it_from_the_index(
    db: AsyncSession, user: User, indexer: GraphIndexer, retrieval: GraphRetrievalService
) -> None:
    """A retraction must take effect immediately, not at the next reindex."""
    experience = await _approved_experience(
        db, user, title="Payments Engineer", company="Northwind", description="Kafka pipelines"
    )
    await indexer.index_node(user_id=user.id, entity_type=EntityType.EXPERIENCE, row=experience)

    found = await retrieval.retrieve(RetrievalQuery(user_id=user.id, text="Kafka pipelines"))
    assert experience.id in found.evidence_ids

    experience.status = NodeStatus.PENDING_REVIEW
    await db.flush()
    await indexer.index_node(user_id=user.id, entity_type=EntityType.EXPERIENCE, row=experience)

    after = await retrieval.retrieve(RetrievalQuery(user_id=user.id, text="Kafka pipelines"))
    assert experience.id not in after.evidence_ids


async def test_resolve_evidence_rejects_unapproved_ids(
    db: AsyncSession, user: User, indexer: GraphIndexer, retrieval: GraphRetrievalService
) -> None:
    """The grounding validator's backstop: unapproved ids resolve to nothing."""
    pending = Experience(
        user_id=user.id,
        company_name="Ghost Corp",
        title="Principal Engineer",
        status=NodeStatus.PENDING_REVIEW,
        source=ProvenanceSource.INFERENCE,
    )
    db.add(pending)
    await db.flush()

    resolved = await retrieval.resolve_evidence(user.id, [pending.id])
    assert resolved == {}


# ── Tenant isolation (AC-SEC-001) ────────────────────────────────────


async def test_retrieval_never_crosses_users(
    db: AsyncSession,
    user: User,
    other_user: User,
    indexer: GraphIndexer,
    retrieval: GraphRetrievalService,
) -> None:
    theirs = await _approved_experience(
        db, other_user, title="Distinctive Zebrafish Researcher", company="Acme Labs"
    )
    await indexer.index_node(user_id=other_user.id, entity_type=EntityType.EXPERIENCE, row=theirs)

    result = await retrieval.retrieve(
        RetrievalQuery(user_id=user.id, text="Distinctive Zebrafish Researcher")
    )

    assert theirs.id not in result.evidence_ids
    assert result.nodes == []


async def test_resolve_evidence_never_crosses_users(
    db: AsyncSession,
    user: User,
    other_user: User,
    retrieval: GraphRetrievalService,
) -> None:
    theirs = await _approved_experience(db, other_user, title="Engineer", company="Acme")
    resolved = await retrieval.resolve_evidence(user.id, [theirs.id])
    assert resolved == {}


# ── Retrieval behaviour ──────────────────────────────────────────────


async def test_hybrid_retrieval_finds_relevant_experience(
    db: AsyncSession, user: User, indexer: GraphIndexer, retrieval: GraphRetrievalService
) -> None:
    relevant = await _approved_experience(
        db,
        user,
        title="Site Reliability Engineer",
        company="Northwind",
        description="Led incident response and reduced mean time to recovery",
    )
    unrelated = await _approved_experience(
        db, user, title="Graphic Designer", company="Studio", description="Brand illustration"
    )
    for row in (relevant, unrelated):
        await indexer.index_node(user_id=user.id, entity_type=EntityType.EXPERIENCE, row=row)

    result = await retrieval.retrieve(
        RetrievalQuery(user_id=user.id, text="incident response and recovery time")
    )

    assert result.evidence_ids[0] == relevant.id


async def test_expansion_pulls_achievements_for_a_matched_experience(
    db: AsyncSession, user: User, indexer: GraphIndexer, retrieval: GraphRetrievalService
) -> None:
    """An experience without its metrics produces guidance with no specifics."""
    experience = await _approved_experience(
        db, user, title="Platform Engineer", company="Northwind", description="Deployment tooling"
    )
    achievement = Achievement(
        user_id=user.id,
        experience_id=experience.id,
        statement="Cut deploy time from 40 minutes to 9 minutes",
        metrics=[{"label": "deploy_time", "before": "40m", "after": "9m"}],
        has_quantified_metric=True,
        status=NodeStatus.APPROVED,
        source=ProvenanceSource.RESUME,
    )
    db.add(achievement)
    await db.flush()
    await indexer.index_node(user_id=user.id, entity_type=EntityType.EXPERIENCE, row=experience)

    result = await retrieval.retrieve(
        RetrievalQuery(user_id=user.id, text="deployment tooling", expand=True)
    )

    expanded = [n for n in result.nodes if n.source == "expansion"]
    assert achievement.id in {n.entity_id for n in expanded}
    metric_node = next(n for n in expanded if n.entity_id == achievement.id)
    assert metric_node.payload["metrics"][0]["after"] == "9m"


async def test_expansion_reaches_achievements_through_projects(
    db: AsyncSession, user: User, indexer: GraphIndexer, retrieval: GraphRetrievalService
) -> None:
    experience = await _approved_experience(
        db, user, title="Backend Engineer", company="Northwind", description="Search platform"
    )
    project = Project(
        user_id=user.id,
        experience_id=experience.id,
        name="Search relevance rewrite",
        status=NodeStatus.APPROVED,
        source=ProvenanceSource.RESUME,
    )
    db.add(project)
    await db.flush()
    achievement = Achievement(
        user_id=user.id,
        project_id=project.id,
        statement="Raised click-through rate by 12 percent",
        status=NodeStatus.APPROVED,
        source=ProvenanceSource.RESUME,
    )
    db.add(achievement)
    await db.flush()
    await indexer.index_node(user_id=user.id, entity_type=EntityType.EXPERIENCE, row=experience)

    result = await retrieval.retrieve(
        RetrievalQuery(user_id=user.id, text="search platform", expand=True)
    )

    assert achievement.id in result.evidence_ids


async def test_entity_type_filter_restricts_results(
    db: AsyncSession, user: User, indexer: GraphIndexer, retrieval: GraphRetrievalService
) -> None:
    experience = await _approved_experience(
        db, user, title="Engineer", company="Northwind", description="mentoring juniors"
    )
    story = CandidateStory(
        user_id=user.id,
        title="Mentoring a struggling teammate",
        situation="A junior engineer was blocked",
        result="They shipped independently within a quarter",
        categories=["mentoring"],
        status=StoryStatus.APPROVED,
    )
    db.add(story)
    await db.flush()
    await indexer.index_node(user_id=user.id, entity_type=EntityType.EXPERIENCE, row=experience)
    await indexer.index_node(user_id=user.id, entity_type=EntityType.STORY, row=story)

    result = await retrieval.retrieve(
        RetrievalQuery(
            user_id=user.id,
            text="mentoring",
            entity_types=frozenset({EntityType.STORY}),
            expand=False,
        )
    )

    assert result.evidence_ids == [story.id]


async def test_story_becomes_retrievable_on_approval(
    db: AsyncSession, user: User, indexer: GraphIndexer, retrieval: GraphRetrievalService
) -> None:
    """PRD §11.4: approving a story makes Copilot able to retrieve it at once."""
    story = CandidateStory(
        user_id=user.id,
        title="Recovering a failed migration",
        situation="A schema migration locked the primary table",
        result="Restored service in eleven minutes",
        categories=["crisis_recovery"],
        status=StoryStatus.SUGGESTED,
    )
    db.add(story)
    await db.flush()

    await indexer.index_node(user_id=user.id, entity_type=EntityType.STORY, row=story)
    before = await retrieval.retrieve(RetrievalQuery(user_id=user.id, text="failed migration"))
    assert story.id not in before.evidence_ids

    story.status = StoryStatus.APPROVED
    await db.flush()
    await indexer.index_node(user_id=user.id, entity_type=EntityType.STORY, row=story)

    after = await retrieval.retrieve(RetrievalQuery(user_id=user.id, text="failed migration"))
    assert story.id in after.evidence_ids


# ── Indexer behaviour ────────────────────────────────────────────────


async def test_reindexing_unchanged_content_skips_the_embedding_call(
    db: AsyncSession, user: User, indexer: GraphIndexer
) -> None:
    """Re-embedding unchanged text is the dominant avoidable cost (PRD §33)."""
    experience = await _approved_experience(
        db, user, title="Engineer", company="Northwind", description="stable text"
    )

    assert await indexer.index_node(
        user_id=user.id, entity_type=EntityType.EXPERIENCE, row=experience
    )
    assert not await indexer.index_node(
        user_id=user.id, entity_type=EntityType.EXPERIENCE, row=experience
    )


async def test_changed_content_is_reindexed(
    db: AsyncSession, user: User, indexer: GraphIndexer
) -> None:
    experience = await _approved_experience(
        db, user, title="Engineer", company="Northwind", description="original"
    )
    await indexer.index_node(user_id=user.id, entity_type=EntityType.EXPERIENCE, row=experience)

    experience.description = "substantially rewritten description about Kubernetes"
    await db.flush()

    assert await indexer.index_node(
        user_id=user.id, entity_type=EntityType.EXPERIENCE, row=experience
    )


async def test_purge_user_removes_every_vector(
    db: AsyncSession, user: User, indexer: GraphIndexer
) -> None:
    """PRD §29.4: account deletion must leave no vectors behind."""
    for i in range(3):
        row = await _approved_experience(
            db, user, title=f"Role {i}", company=f"Company {i}", description=f"work {i}"
        )
        await indexer.index_node(user_id=user.id, entity_type=EntityType.EXPERIENCE, row=row)

    removed = await indexer.purge_user(user.id)
    assert removed == 3

    remaining = await db.scalar(
        select(GraphEmbedding).where(GraphEmbedding.user_id == user.id).exists().select()
    )
    assert remaining is False


def test_projection_includes_metrics_for_achievements() -> None:
    achievement = Achievement(
        statement="Reduced latency",
        action="Introduced caching",
        outcome="p95 dropped",
        metrics=[{"label": "p95", "before": "800ms", "after": "120ms"}],
    )
    projection = build_projection(EntityType.ACHIEVEMENT, achievement)

    assert "Reduced latency" in projection
    assert "800ms" in projection and "120ms" in projection
