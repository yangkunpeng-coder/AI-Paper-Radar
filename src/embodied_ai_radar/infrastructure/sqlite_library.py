from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from embodied_ai_radar.domain.library import (
    LibraryPaper,
    LibraryViewCounts,
    PaperUserState,
    SyncState,
    base_arxiv_id,
)
from embodied_ai_radar.domain.llm import PaperAIAnalysis
from embodied_ai_radar.domain.models import Paper, RankedPaper
from embodied_ai_radar.domain.research_domains import (
    DOMAIN_EMBODIED,
    TOPIC_ALL,
    PaperResearchTaxonomy,
    ResearchDomain,
    RESEARCH_DOMAINS,
    get_research_domain,
    research_domain_keys,
)

SCHEMA_VERSION = 9


class SQLitePaperRepository:
    """SQLite-backed local paper library and durable sync coverage state.

    The database contains only public paper metadata plus local user state,
    synchronization metadata, and generated analyses. API keys never enter this
    database.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing_version = self._existing_schema_version()
        if existing_version is not None and existing_version < SCHEMA_VERSION:
            self._create_pre_migration_backup(existing_version)
        # journal_mode is persistent database state. Set WAL once during database
        # initialization instead of renegotiating it on every short read/query
        # connection, which adds avoidable work while sync and UI reads overlap.
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS papers (
                    arxiv_id TEXT PRIMARY KEY,
                    latest_version_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    authors_json TEXT NOT NULL,
                    author_affiliations_json TEXT NOT NULL DEFAULT '[]',
                    affiliation_source TEXT NOT NULL DEFAULT '',
                    categories_json TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    abstract_url TEXT NOT NULL,
                    pdf_url TEXT,
                    rule_score INTEGER NOT NULL,
                    tags_json TEXT NOT NULL,
                    matched_terms_json TEXT NOT NULL,
                    interest_score INTEGER NOT NULL DEFAULT 0,
                    interest_tags_json TEXT NOT NULL DEFAULT '[]',
                    interest_matched_terms_json TEXT NOT NULL DEFAULT '[]',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_state (
                    arxiv_id TEXT PRIMARY KEY REFERENCES papers(arxiv_id) ON DELETE CASCADE,
                    is_favorite INTEGER NOT NULL DEFAULT 0,
                    is_read INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_analyses (
                    arxiv_id TEXT PRIMARY KEY REFERENCES papers(arxiv_id) ON DELETE CASCADE,
                    analyzed_version_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    relevance_score INTEGER NOT NULL,
                    summary_cn TEXT NOT NULL,
                    contribution_cn TEXT NOT NULL,
                    recommendation_cn TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    analyzed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_state (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                    last_successful_sync TEXT,
                    earliest_covered_date TEXT,
                    latest_covered_date TEXT,
                    sync_status TEXT NOT NULL DEFAULT 'idle',
                    last_attempt_at TEXT,
                    last_cancelled_at TEXT,
                    last_error TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS domain_sync_state (
                    domain_key TEXT PRIMARY KEY,
                    last_successful_sync TEXT,
                    earliest_covered_date TEXT,
                    latest_covered_date TEXT,
                    sync_status TEXT NOT NULL DEFAULT 'idle',
                    last_attempt_at TEXT,
                    last_cancelled_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(domain_key) REFERENCES domains(domain_key) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS domains (
                    domain_key TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    sort_order INTEGER NOT NULL,
                    data_enabled INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS topics (
                    domain_key TEXT NOT NULL REFERENCES domains(domain_key) ON DELETE CASCADE,
                    topic_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    filter_tag TEXT NOT NULL,
                    sort_order INTEGER NOT NULL,
                    PRIMARY KEY(domain_key, topic_key)
                );

                CREATE TABLE IF NOT EXISTS paper_domains (
                    arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
                    domain_key TEXT NOT NULL REFERENCES domains(domain_key) ON DELETE CASCADE,
                    rule_score INTEGER NOT NULL DEFAULT 0,
                    rule_tags_json TEXT NOT NULL DEFAULT '[]',
                    matched_terms_json TEXT NOT NULL DEFAULT '[]',
                    interest_score INTEGER NOT NULL DEFAULT 0,
                    interest_tags_json TEXT NOT NULL DEFAULT '[]',
                    interest_matched_terms_json TEXT NOT NULL DEFAULT '[]',
                    classification_source TEXT NOT NULL DEFAULT 'rule',
                    assigned_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(arxiv_id, domain_key)
                );

                CREATE TABLE IF NOT EXISTS paper_topics (
                    arxiv_id TEXT NOT NULL,
                    domain_key TEXT NOT NULL,
                    topic_key TEXT NOT NULL,
                    classification_source TEXT NOT NULL DEFAULT 'rule',
                    assigned_at TEXT NOT NULL,
                    PRIMARY KEY(arxiv_id, domain_key, topic_key),
                    FOREIGN KEY(arxiv_id, domain_key)
                        REFERENCES paper_domains(arxiv_id, domain_key) ON DELETE CASCADE,
                    FOREIGN KEY(domain_key, topic_key)
                        REFERENCES topics(domain_key, topic_key) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS domain_ai_analyses (
                    arxiv_id TEXT NOT NULL,
                    domain_key TEXT NOT NULL,
                    analyzed_version_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    relevance_score INTEGER NOT NULL,
                    summary_cn TEXT NOT NULL,
                    problem_cn TEXT NOT NULL DEFAULT '',
                    method_cn TEXT NOT NULL DEFAULT '',
                    contribution_cn TEXT NOT NULL,
                    results_cn TEXT NOT NULL DEFAULT '',
                    recommendation_cn TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    topic_keys_json TEXT NOT NULL DEFAULT '[]',
                    analysis_version INTEGER NOT NULL DEFAULT 1,
                    analyzed_at TEXT NOT NULL,
                    PRIMARY KEY(arxiv_id, domain_key),
                    FOREIGN KEY(arxiv_id, domain_key)
                        REFERENCES paper_domains(arxiv_id, domain_key) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_papers_updated_at
                ON papers(updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_papers_updated_arxiv
                ON papers(updated_at DESC, arxiv_id DESC);

                CREATE INDEX IF NOT EXISTS idx_paper_domains_domain
                ON paper_domains(domain_key, arxiv_id);

                CREATE INDEX IF NOT EXISTS idx_paper_domains_interest_order
                ON paper_domains(domain_key, interest_score DESC, rule_score DESC, arxiv_id DESC);

                CREATE INDEX IF NOT EXISTS idx_paper_domains_relevance_order
                ON paper_domains(domain_key, rule_score DESC, interest_score DESC, arxiv_id DESC);

                CREATE INDEX IF NOT EXISTS idx_paper_state_favorite
                ON paper_state(is_favorite, arxiv_id);

                CREATE INDEX IF NOT EXISTS idx_paper_topics_lookup
                ON paper_topics(domain_key, topic_key, arxiv_id);

                CREATE INDEX IF NOT EXISTS idx_domain_ai_analyses_lookup
                ON domain_ai_analyses(domain_key, arxiv_id);
                """
            )
            conn.execute("INSERT OR IGNORE INTO sync_state(singleton_id) VALUES(1)")
            current = conn.execute(
                "SELECT value FROM app_meta WHERE key = 'schema_version'"
            ).fetchone()
            current_version = int(current["value"]) if current is not None else None
            if current_version is not None and current_version not in {1, 2, 3, 4, 5, 6, 7, 8, 9}:
                raise RuntimeError(
                    f"Unsupported database schema version: {current_version} "
                    f"(expected <= {SCHEMA_VERSION})"
                )

            if current_version in {1, 2, 3, 4}:
                self._migrate_legacy_columns(conn)
            if current_version is not None and current_version <= 8:
                self._migrate_quick_read_analysis_columns(conn)

            self._seed_research_catalog(conn)
            self._seed_domain_sync_states(conn)
            if current_version is None or current_version <= 5:
                self._backfill_legacy_embodied_taxonomy(conn)
            if current_version is not None and current_version <= 7:
                self._backfill_legacy_embodied_analyses(conn)

            if current is None:
                conn.execute(
                    "INSERT INTO app_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif current_version != SCHEMA_VERSION:
                conn.execute(
                    "UPDATE app_meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )


    def _backfill_legacy_embodied_analyses(self, conn: sqlite3.Connection) -> None:
        """Preserve valid legacy DeepSeek cache as embodied-domain analysis.

        Through schema v7 the cache had no domain key and the prompt itself was
        explicitly embodied-AI oriented. Agent/LLM papers analyzed under that old
        prompt are left in the legacy ai_analyses table for data preservation but
        are not exposed as domain-aware cache.
        """

        conn.execute(
            """
            INSERT OR IGNORE INTO domain_ai_analyses(
                arxiv_id, domain_key, analyzed_version_id, model, relevance_score,
                summary_cn, contribution_cn, recommendation_cn, tags_json,
                topic_keys_json, analyzed_at
            )
            SELECT a.arxiv_id, ?, a.analyzed_version_id, a.model, a.relevance_score,
                   a.summary_cn, a.contribution_cn, a.recommendation_cn, a.tags_json,
                   '[]', a.analyzed_at
            FROM ai_analyses AS a
            JOIN paper_domains AS pd
              ON pd.arxiv_id = a.arxiv_id AND pd.domain_key = ?
            """,
            (DOMAIN_EMBODIED, DOMAIN_EMBODIED),
        )


    def _migrate_legacy_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(papers)").fetchall()}
        migrations = {
            "author_affiliations_json": "TEXT NOT NULL DEFAULT '[]'",
            "affiliation_source": "TEXT NOT NULL DEFAULT ''",
            "interest_score": "INTEGER NOT NULL DEFAULT 0",
            "interest_tags_json": "TEXT NOT NULL DEFAULT '[]'",
            "interest_matched_terms_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, definition in migrations.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {definition}")

        sync_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(sync_state)").fetchall()
        }
        sync_migrations = {
            "sync_status": "TEXT NOT NULL DEFAULT 'idle'",
            "last_attempt_at": "TEXT",
            "last_cancelled_at": "TEXT",
            "last_error": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in sync_migrations.items():
            if name not in sync_columns:
                conn.execute(f"ALTER TABLE sync_state ADD COLUMN {name} {definition}")

    def _migrate_quick_read_analysis_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(domain_ai_analyses)").fetchall()
        }
        migrations = {
            "problem_cn": "TEXT NOT NULL DEFAULT ''",
            "method_cn": "TEXT NOT NULL DEFAULT ''",
            "results_cn": "TEXT NOT NULL DEFAULT ''",
            "analysis_version": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, definition in migrations.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE domain_ai_analyses ADD COLUMN {name} {definition}"
                )


    def _seed_research_catalog(self, conn: sqlite3.Connection) -> None:
        for domain_order, domain in enumerate(RESEARCH_DOMAINS):
            conn.execute(
                """
                INSERT INTO domains(domain_key, label, sort_order, data_enabled)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(domain_key) DO UPDATE SET
                    label = excluded.label,
                    sort_order = excluded.sort_order,
                    data_enabled = excluded.data_enabled
                """,
                (domain.key, domain.label, domain_order, int(domain.data_enabled)),
            )
            for topic_order, topic in enumerate(domain.topics):
                conn.execute(
                    """
                    INSERT INTO topics(domain_key, topic_key, label, filter_tag, sort_order)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(domain_key, topic_key) DO UPDATE SET
                        label = excluded.label,
                        filter_tag = excluded.filter_tag,
                        sort_order = excluded.sort_order
                    """,
                    (
                        domain.key,
                        topic.key,
                        topic.label,
                        topic.paper_filter_tag,
                        topic_order,
                    ),
                )


    def _seed_domain_sync_states(self, conn: sqlite3.Connection) -> None:
        legacy = conn.execute(
            """
            SELECT last_successful_sync, earliest_covered_date, latest_covered_date,
                   sync_status, last_attempt_at, last_cancelled_at, last_error
            FROM sync_state WHERE singleton_id = 1
            """
        ).fetchone()
        for domain in RESEARCH_DOMAINS:
            if domain.key == DOMAIN_EMBODIED and legacy is not None:
                values = (
                    domain.key,
                    legacy["last_successful_sync"],
                    legacy["earliest_covered_date"],
                    legacy["latest_covered_date"],
                    legacy["sync_status"] or "idle",
                    legacy["last_attempt_at"],
                    legacy["last_cancelled_at"],
                    legacy["last_error"] or "",
                )
            else:
                values = (domain.key, None, None, None, "idle", None, None, "")
            conn.execute(
                """
                INSERT INTO domain_sync_state(
                    domain_key, last_successful_sync, earliest_covered_date,
                    latest_covered_date, sync_status, last_attempt_at,
                    last_cancelled_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain_key) DO NOTHING
                """,
                values,
            )

    def _backfill_legacy_embodied_taxonomy(self, conn: sqlite3.Connection) -> None:
        domain = get_research_domain(DOMAIN_EMBODIED)
        rows = conn.execute(
            """
            SELECT arxiv_id, rule_score, tags_json, matched_terms_json,
                   interest_score, interest_tags_json, interest_matched_terms_json,
                   first_seen_at, last_seen_at
            FROM papers
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO paper_domains(
                    arxiv_id, domain_key, rule_score, rule_tags_json, matched_terms_json,
                    interest_score, interest_tags_json, interest_matched_terms_json,
                    classification_source, assigned_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'legacy-v5', ?, ?)
                ON CONFLICT(arxiv_id, domain_key) DO NOTHING
                """,
                (
                    row["arxiv_id"],
                    domain.key,
                    int(row["rule_score"]),
                    row["tags_json"] or "[]",
                    row["matched_terms_json"] or "[]",
                    int(row["interest_score"]),
                    row["interest_tags_json"] or "[]",
                    row["interest_matched_terms_json"] or "[]",
                    row["first_seen_at"],
                    row["last_seen_at"],
                ),
            )
            topic_keys = _infer_topic_keys(
                domain,
                (*_json_tuple(row["tags_json"]), *_json_tuple(row["interest_tags_json"])),
            )
            for topic_key in topic_keys:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO paper_topics(
                        arxiv_id, domain_key, topic_key, classification_source, assigned_at
                    ) VALUES (?, ?, ?, 'legacy-v5', ?)
                    """,
                    (row["arxiv_id"], domain.key, topic_key, row["first_seen_at"]),
                )

    def upsert_ranked_papers(
        self,
        papers: Sequence[RankedPaper],
        *,
        domain_key: str = DOMAIN_EMBODIED,
        topic_keys_by_arxiv_id: Mapping[str, Sequence[str]] | None = None,
        classification_source: str = "rule",
    ) -> None:
        if not papers:
            return
        domain = _require_domain(domain_key)
        source = classification_source.strip() or "rule"
        now = _now_iso()
        with self._connect() as conn:
            for item in papers:
                stable_id = base_arxiv_id(item.paper.arxiv_id)
                existing = conn.execute(
                    "SELECT latest_version_id, author_affiliations_json, affiliation_source "
                    "FROM papers WHERE arxiv_id = ?",
                    (stable_id,),
                ).fetchone()
                version_changed = (
                    existing is not None
                    and existing["latest_version_id"] != item.paper.arxiv_id
                )
                existing_affiliations = (
                    tuple(
                        tuple(values)
                        for values in json.loads(existing["author_affiliations_json"] or "[]")
                    )
                    if existing is not None
                    else ()
                )
                effective_affiliations = _merge_affiliations(
                    item.paper.author_affiliations,
                    existing_affiliations,
                    len(item.paper.authors),
                )
                effective_affiliation_source = _merged_affiliation_source(
                    incoming_source=item.paper.affiliation_source,
                    incoming=item.paper.author_affiliations,
                    existing_source=(
                        existing["affiliation_source"] if existing is not None else ""
                    ),
                    existing=existing_affiliations,
                )

                if domain.key == DOMAIN_EMBODIED or existing is None:
                    conn.execute(
                        """
                        INSERT INTO papers (
                            arxiv_id, latest_version_id, title, summary, authors_json,
                            author_affiliations_json, affiliation_source, categories_json,
                            published_at, updated_at, abstract_url, pdf_url, rule_score,
                            tags_json, matched_terms_json, interest_score, interest_tags_json,
                            interest_matched_terms_json, first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(arxiv_id) DO UPDATE SET
                            latest_version_id = excluded.latest_version_id,
                            title = excluded.title,
                            summary = excluded.summary,
                            authors_json = excluded.authors_json,
                            author_affiliations_json = excluded.author_affiliations_json,
                            affiliation_source = excluded.affiliation_source,
                            categories_json = excluded.categories_json,
                            published_at = excluded.published_at,
                            updated_at = excluded.updated_at,
                            abstract_url = excluded.abstract_url,
                            pdf_url = excluded.pdf_url,
                            rule_score = excluded.rule_score,
                            tags_json = excluded.tags_json,
                            matched_terms_json = excluded.matched_terms_json,
                            interest_score = excluded.interest_score,
                            interest_tags_json = excluded.interest_tags_json,
                            interest_matched_terms_json = excluded.interest_matched_terms_json,
                            last_seen_at = excluded.last_seen_at
                        """,
                        _paper_row_values(
                            stable_id,
                            item,
                            effective_affiliations,
                            effective_affiliation_source,
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE papers
                        SET latest_version_id = ?, title = ?, summary = ?, authors_json = ?,
                            author_affiliations_json = ?, affiliation_source = ?,
                            categories_json = ?, published_at = ?, updated_at = ?,
                            abstract_url = ?, pdf_url = ?, last_seen_at = ?
                        WHERE arxiv_id = ?
                        """,
                        (
                            item.paper.arxiv_id,
                            item.paper.title,
                            item.paper.summary,
                            _json_list(item.paper.authors),
                            _json_nested_list(effective_affiliations),
                            effective_affiliation_source,
                            _json_list(item.paper.categories),
                            item.paper.published_at.isoformat(),
                            item.paper.updated_at.isoformat(),
                            item.paper.abstract_url,
                            item.paper.pdf_url,
                            now,
                            stable_id,
                        ),
                    )

                conn.execute(
                    """
                    INSERT INTO paper_state(arxiv_id, is_favorite, is_read, updated_at)
                    VALUES (?, 0, 0, ?)
                    ON CONFLICT(arxiv_id) DO NOTHING
                    """,
                    (stable_id, now),
                )

                if version_changed:
                    conn.execute(
                        "UPDATE paper_state SET is_read = 0, updated_at = ? WHERE arxiv_id = ?",
                        (now, stable_id),
                    )
                    conn.execute("DELETE FROM ai_analyses WHERE arxiv_id = ?", (stable_id,))
                    conn.execute(
                        "DELETE FROM domain_ai_analyses WHERE arxiv_id = ?",
                        (stable_id,),
                    )

                conn.execute(
                    """
                    INSERT INTO paper_domains(
                        arxiv_id, domain_key, rule_score, rule_tags_json, matched_terms_json,
                        interest_score, interest_tags_json, interest_matched_terms_json,
                        classification_source, assigned_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(arxiv_id, domain_key) DO UPDATE SET
                        rule_score = excluded.rule_score,
                        rule_tags_json = excluded.rule_tags_json,
                        matched_terms_json = excluded.matched_terms_json,
                        interest_score = excluded.interest_score,
                        interest_tags_json = excluded.interest_tags_json,
                        interest_matched_terms_json = excluded.interest_matched_terms_json,
                        classification_source = excluded.classification_source,
                        updated_at = excluded.updated_at
                    """,
                    (
                        stable_id,
                        domain.key,
                        item.score,
                        _json_list(item.tags),
                        _json_list(item.matched_terms),
                        item.interest_score,
                        _json_list(item.interest_tags),
                        _json_list(item.interest_matched_terms),
                        source,
                        now,
                        now,
                    ),
                )

                explicit_topics = _topic_keys_for_item(
                    topic_keys_by_arxiv_id,
                    item.paper.arxiv_id,
                    stable_id,
                )
                topic_keys = (
                    _validate_topic_keys(domain, explicit_topics)
                    if explicit_topics is not None
                    else _infer_topic_keys(domain, (*item.tags, *item.interest_tags))
                )
                conn.execute(
                    """
                    DELETE FROM paper_topics
                    WHERE arxiv_id = ? AND domain_key = ?
                      AND classification_source NOT LIKE 'deepseek%'
                    """,
                    (stable_id, domain.key),
                )
                for topic_key in topic_keys:
                    conn.execute(
                        """
                        INSERT INTO paper_topics(
                            arxiv_id, domain_key, topic_key, classification_source, assigned_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(arxiv_id, domain_key, topic_key) DO UPDATE SET
                            classification_source = excluded.classification_source,
                            assigned_at = excluded.assigned_at
                        """,
                        (stable_id, domain.key, topic_key, source, now),
                    )

    def update_author_affiliations(
        self,
        arxiv_id: str,
        affiliations: Sequence[Sequence[str]],
        *,
        source: str,
    ) -> None:
        stable_id = base_arxiv_id(arxiv_id)
        normalized = tuple(tuple(value for value in group if value) for group in affiliations)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT authors_json, author_affiliations_json, affiliation_source FROM papers WHERE arxiv_id = ?",
                (stable_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown arXiv paper: {arxiv_id}")
            author_count = len(json.loads(row["authors_json"]))
            existing = tuple(
                tuple(values) for values in json.loads(row["author_affiliations_json"] or "[]")
            )
            merged = _merge_affiliations(normalized, existing, author_count)
            source_value = _combine_sources(row["affiliation_source"] or "", source)
            conn.execute(
                "UPDATE papers SET author_affiliations_json = ?, affiliation_source = ? WHERE arxiv_id = ?",
                (_json_nested_list(merged), source_value, stable_id),
            )

    def get_app_meta(self, key: str) -> str | None:
        normalized = key.strip()
        if not normalized:
            raise ValueError("app meta key is required")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?",
                (normalized,),
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def set_app_meta(self, key: str, value: str) -> None:
        normalized = key.strip()
        if not normalized:
            raise ValueError("app meta key is required")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO app_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (normalized, value),
            )

    def load_recent(
        self,
        *,
        limit: int = 500,
        offset: int = 0,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
        sort_mode: str = "interest",
    ) -> list[LibraryPaper]:
        return self._load_library(
            limit=limit,
            offset=offset,
            domain_key=domain_key,
            topic_key=topic_key,
            favorites_only=favorites_only,
            sort_mode=sort_mode,
        )

    def load_since(
        self,
        updated_after: datetime,
        *,
        limit: int = 5000,
        offset: int = 0,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
        sort_mode: str = "interest",
    ) -> list[LibraryPaper]:
        if limit < 1:
            return []
        return self._load_library(
            updated_after=updated_after,
            limit=limit,
            offset=offset,
            domain_key=domain_key,
            topic_key=topic_key,
            favorites_only=favorites_only,
            sort_mode=sort_mode,
        )

    def load_all_since(
        self,
        updated_after: datetime,
        *,
        batch_size: int = 500,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
        sort_mode: str = "interest",
    ) -> list[LibraryPaper]:
        """Fetch one query in batches from a snapshot, released before file generation."""
        if batch_size < 1:
            raise ValueError("batch_size must be > 0")
        entries: list[LibraryPaper] = []
        with self._connect() as conn:
            conn.execute("PRAGMA query_only = ON")
            # A deferred read transaction pins the snapshot on the first SELECT.
            # WAL writers may continue committing while subsequent batches are read.
            conn.execute("BEGIN")
            cursor = self._select_library(
                conn,
                updated_after=updated_after,
                limit=-1,
                domain_key=domain_key,
                topic_key=topic_key,
                favorites_only=favorites_only,
                sort_mode=sort_mode,
            )
            try:
                while rows := cursor.fetchmany(batch_size):
                    entries.extend(self._row_to_library_paper(row) for row in rows)
            finally:
                cursor.close()
        return entries

    def count_since(
        self,
        updated_after: datetime,
        *,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
    ) -> LibraryViewCounts:
        domain = _require_domain(domain_key) if domain_key is not None else None
        normalized_topic = TOPIC_ALL
        if topic_key is not None:
            if domain is None:
                raise ValueError("topic_key requires domain_key")
            if topic_key.strip().casefold() != TOPIC_ALL:
                normalized_topic = _validate_topic_keys(domain, (topic_key,))[0]

        params: dict[str, object] = {
            "updated_after": updated_after.isoformat(),
            "analysis_domain_key": domain.key if domain is not None else DOMAIN_EMBODIED,
        }
        domain_join = ""
        if domain is not None:
            domain_join = (
                "JOIN paper_domains AS pd ON pd.arxiv_id = p.arxiv_id "
                "AND pd.domain_key = :domain_key"
            )
            params["domain_key"] = domain.key

        filtered_conditions: list[str] = []
        if favorites_only:
            filtered_conditions.append("s.is_favorite = 1")
        if domain is not None and normalized_topic != TOPIC_ALL:
            filtered_conditions.append(
                "EXISTS ("
                "SELECT 1 FROM paper_topics AS pt "
                "WHERE pt.arxiv_id = p.arxiv_id "
                "AND pt.domain_key = :domain_key AND pt.topic_key = :topic_key"
                ")"
            )
            params["topic_key"] = normalized_topic
        filtered_predicate = " AND ".join(filtered_conditions) or "1"

        # Counts are requested frequently by Tablet live-sync/header refresh. Keep
        # total/favorite/analyzed/current-filter semantics in one indexed scan instead
        # of issuing a second COUNT query over the same date/domain range.
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN s.is_favorite = 1 THEN 1 ELSE 0 END) AS favorites,
                       SUM(CASE WHEN a.arxiv_id IS NOT NULL THEN 1 ELSE 0 END) AS analyzed,
                       SUM(CASE WHEN {filtered_predicate} THEN 1 ELSE 0 END) AS filtered
                FROM papers AS p
                JOIN paper_state AS s ON s.arxiv_id = p.arxiv_id
                {domain_join}
                LEFT JOIN domain_ai_analyses AS a
                  ON a.arxiv_id = p.arxiv_id
                 AND a.domain_key = :analysis_domain_key
                 AND a.analyzed_version_id = p.latest_version_id
                WHERE p.updated_at >= :updated_after
                """,
                params,
            ).fetchone()

        return LibraryViewCounts(
            total=int(row["total"] or 0),
            filtered=int(row["filtered"] or 0),
            favorites=int(row["favorites"] or 0),
            analyzed=int(row["analyzed"] or 0),
        )

    def get_taxonomies(
        self,
        arxiv_ids: Sequence[str],
    ) -> dict[str, PaperResearchTaxonomy]:
        stable_by_original = {item: base_arxiv_id(item) for item in arxiv_ids}
        stable_ids = set(stable_by_original.values())
        if not stable_ids:
            return {}
        placeholders = ",".join("?" for _ in stable_ids)
        with self._connect() as conn:
            domain_rows = conn.execute(
                f"""
                SELECT pd.arxiv_id, pd.domain_key
                FROM paper_domains AS pd
                JOIN domains AS d ON d.domain_key = pd.domain_key
                WHERE pd.arxiv_id IN ({placeholders})
                ORDER BY d.sort_order, pd.domain_key
                """,
                tuple(stable_ids),
            ).fetchall()
            topic_rows = conn.execute(
                f"""
                SELECT pt.arxiv_id, pt.domain_key, pt.topic_key
                FROM paper_topics AS pt
                JOIN domains AS d ON d.domain_key = pt.domain_key
                JOIN topics AS t
                  ON t.domain_key = pt.domain_key
                 AND t.topic_key = pt.topic_key
                WHERE pt.arxiv_id IN ({placeholders})
                ORDER BY d.sort_order, t.sort_order, pt.topic_key
                """,
                tuple(stable_ids),
            ).fetchall()

        domains_by_stable: dict[str, list[str]] = {stable_id: [] for stable_id in stable_ids}
        topics_by_stable: dict[str, list[tuple[str, str]]] = {
            stable_id: [] for stable_id in stable_ids
        }
        for row in domain_rows:
            domains_by_stable[row["arxiv_id"]].append(row["domain_key"])
        for row in topic_rows:
            topics_by_stable[row["arxiv_id"]].append(
                (row["domain_key"], row["topic_key"])
            )

        return {
            original: PaperResearchTaxonomy(
                arxiv_id=stable_id,
                domain_keys=tuple(domains_by_stable.get(stable_id, ())),
                topic_keys=tuple(topics_by_stable.get(stable_id, ())),
            )
            for original, stable_id in stable_by_original.items()
        }

    def get_domain_paper_versions(
        self, *, domain_key: str = DOMAIN_EMBODIED
    ) -> dict[str, str]:
        """Return stable ID -> latest version for papers already in a domain.

        A normal OAI sync intentionally treats an existing domain membership as
        complete for de-duplication purposes: overlap/category duplicates should
        not spend CPU re-ranking or write the same rows again.
        """

        domain = _require_domain(domain_key)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.arxiv_id, p.latest_version_id
                FROM paper_domains AS pd
                JOIN papers AS p ON p.arxiv_id = pd.arxiv_id
                WHERE pd.domain_key = ?
                """,
                (domain.key,),
            ).fetchall()
        return {str(row["arxiv_id"]): str(row["latest_version_id"]) for row in rows}

    def get_sync_state(self, *, domain_key: str = DOMAIN_EMBODIED) -> SyncState:
        domain = _require_domain(domain_key)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT last_successful_sync, earliest_covered_date, latest_covered_date,
                       sync_status, last_attempt_at, last_cancelled_at, last_error
                FROM domain_sync_state
                WHERE domain_key = ?
                """,
                (domain.key,),
            ).fetchone()
        if row is None:
            return SyncState()
        return _sync_state_from_row(row)

    def begin_sync(
        self,
        *,
        domain_key: str = DOMAIN_EMBODIED,
        attempted_at: datetime | None = None,
    ) -> SyncState:
        domain = _require_domain(domain_key)
        attempted = attempted_at or datetime.now(UTC)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE domain_sync_state
                SET sync_status = 'syncing', last_attempt_at = ?, last_error = ''
                WHERE domain_key = ?
                """,
                (attempted.isoformat(), domain.key),
            )
        return self.get_sync_state(domain_key=domain.key)

    def record_sync_checkpoint(
        self,
        *,
        from_date: date,
        until_date: date,
        domain_key: str = DOMAIN_EMBODIED,
        completed_at: datetime | None = None,
    ) -> SyncState:
        if from_date > until_date:
            raise ValueError("from_date must not be after until_date")
        domain = _require_domain(domain_key)
        completed = completed_at or datetime.now(UTC)
        current = self.get_sync_state(domain_key=domain.key)
        earliest = min(
            value
            for value in (current.earliest_covered_date, from_date)
            if value is not None
        )
        latest = max(
            value
            for value in (current.latest_covered_date, until_date)
            if value is not None
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE domain_sync_state
                SET last_successful_sync = ?, earliest_covered_date = ?, latest_covered_date = ?
                WHERE domain_key = ?
                """,
                (completed.isoformat(), earliest.isoformat(), latest.isoformat(), domain.key),
            )
        return self.get_sync_state(domain_key=domain.key)

    def finish_sync(self, *, domain_key: str = DOMAIN_EMBODIED) -> SyncState:
        domain = _require_domain(domain_key)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE domain_sync_state
                SET sync_status = 'success', last_error = ''
                WHERE domain_key = ?
                """,
                (domain.key,),
            )
        return self.get_sync_state(domain_key=domain.key)

    def cancel_sync(
        self,
        *,
        domain_key: str = DOMAIN_EMBODIED,
        cancelled_at: datetime | None = None,
    ) -> SyncState:
        domain = _require_domain(domain_key)
        cancelled = cancelled_at or datetime.now(UTC)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE domain_sync_state
                SET sync_status = 'cancelled', last_cancelled_at = ?, last_error = ''
                WHERE domain_key = ?
                """,
                (cancelled.isoformat(), domain.key),
            )
        return self.get_sync_state(domain_key=domain.key)

    def fail_sync(
        self,
        message: str,
        *,
        domain_key: str = DOMAIN_EMBODIED,
    ) -> SyncState:
        domain = _require_domain(domain_key)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE domain_sync_state
                SET sync_status = 'failed', last_error = ?
                WHERE domain_key = ?
                """,
                (message[:2000], domain.key),
            )
        return self.get_sync_state(domain_key=domain.key)

    def record_sync_success(
        self,
        *,
        from_date: date,
        until_date: date,
        domain_key: str = DOMAIN_EMBODIED,
        completed_at: datetime | None = None,
    ) -> SyncState:
        self.begin_sync(domain_key=domain_key, attempted_at=completed_at)
        self.record_sync_checkpoint(
            from_date=from_date,
            until_date=until_date,
            domain_key=domain_key,
            completed_at=completed_at,
        )
        return self.finish_sync(domain_key=domain_key)

    def get_states(self, arxiv_ids: Sequence[str]) -> dict[str, PaperUserState]:
        stable_ids = {base_arxiv_id(item) for item in arxiv_ids}
        if not stable_ids:
            return {}
        placeholders = ",".join("?" for _ in stable_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT arxiv_id, is_favorite, is_read FROM paper_state "
                f"WHERE arxiv_id IN ({placeholders})",
                tuple(stable_ids),
            ).fetchall()
        by_stable = {
            row["arxiv_id"]: PaperUserState(
                arxiv_id=row["arxiv_id"],
                is_favorite=bool(row["is_favorite"]),
                is_read=bool(row["is_read"]),
            )
            for row in rows
        }
        return {
            original: by_stable.get(
                base_arxiv_id(original),
                PaperUserState(arxiv_id=base_arxiv_id(original)),
            )
            for original in arxiv_ids
        }

    def set_favorite(self, arxiv_id: str, value: bool) -> PaperUserState:
        return self._set_state(arxiv_id, "is_favorite", value)

    def set_read(self, arxiv_id: str, value: bool) -> PaperUserState:
        return self._set_state(arxiv_id, "is_read", value)

    def save_analyses_with_topics(
        self,
        *,
        model: str,
        domain_key: str = DOMAIN_EMBODIED,
        analyses: Mapping[str, PaperAIAnalysis],
        classification_source: str = "deepseek-domain-v1",
    ) -> None:
        """Commit a completed batch and its topic assignments atomically."""
        if not analyses:
            return
        with self._connect() as conn:
            # Acquire the write transaction before checking paper versions so a
            # concurrent sync cannot replace them between validation and writes.
            conn.execute("BEGIN IMMEDIATE")
            self._save_analyses(conn, model=model, domain_key=domain_key, analyses=analyses)
            self._replace_analysis_topics(
                conn,
                domain_key=domain_key,
                topic_keys_by_arxiv_id={key: value.topic_keys for key, value in analyses.items()},
                classification_source=classification_source,
            )

    def save_analyses(
        self,
        *,
        model: str,
        domain_key: str = DOMAIN_EMBODIED,
        analyses: Mapping[str, PaperAIAnalysis],
    ) -> None:
        if not analyses:
            return
        with self._connect() as conn:
            self._save_analyses(conn, model=model, domain_key=domain_key, analyses=analyses)

    def _save_analyses(
        self,
        conn: sqlite3.Connection,
        *,
        model: str,
        domain_key: str,
        analyses: Mapping[str, PaperAIAnalysis],
    ) -> None:
        if not analyses:
            return
        domain = _require_domain(domain_key)
        now = _now_iso()
        for version_id, analysis in analyses.items():
            if analysis.domain_key != domain.key:
                raise ValueError(
                    f"Analysis domain {analysis.domain_key} does not match {domain.key}"
                )
            stable_id = base_arxiv_id(version_id)
            paper_row = conn.execute(
                """
                SELECT p.latest_version_id
                FROM papers AS p
                JOIN paper_domains AS pd
                  ON pd.arxiv_id = p.arxiv_id AND pd.domain_key = ?
                WHERE p.arxiv_id = ?
                """,
                (domain.key, stable_id),
            ).fetchone()
            if paper_row is None or paper_row["latest_version_id"] != version_id:
                continue
            conn.execute(
                """
                INSERT INTO domain_ai_analyses (
                    arxiv_id, domain_key, analyzed_version_id, model, relevance_score,
                    summary_cn, problem_cn, method_cn, contribution_cn, results_cn,
                    recommendation_cn, tags_json, topic_keys_json, analysis_version, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(arxiv_id, domain_key) DO UPDATE SET
                    analyzed_version_id = excluded.analyzed_version_id,
                    model = excluded.model,
                    relevance_score = excluded.relevance_score,
                    summary_cn = excluded.summary_cn,
                    problem_cn = excluded.problem_cn,
                    method_cn = excluded.method_cn,
                    contribution_cn = excluded.contribution_cn,
                    results_cn = excluded.results_cn,
                    recommendation_cn = excluded.recommendation_cn,
                    tags_json = excluded.tags_json,
                    topic_keys_json = excluded.topic_keys_json,
                    analysis_version = excluded.analysis_version,
                    analyzed_at = excluded.analyzed_at
                """,
                (
                    stable_id,
                    domain.key,
                    version_id,
                    model,
                    analysis.relevance_score,
                    analysis.summary_cn,
                    analysis.problem_cn,
                    analysis.method_cn,
                    analysis.contribution_cn,
                    analysis.results_cn,
                    analysis.recommendation_cn,
                    _json_list(analysis.tags),
                    _json_list(analysis.topic_keys),
                    analysis.analysis_version,
                    now,
                ),
            )
            if domain.key == DOMAIN_EMBODIED:
                # Keep the v7 cache mirrored for safe adjacent-version rollback.
                conn.execute(
                    """
                    INSERT INTO ai_analyses (
                        arxiv_id, analyzed_version_id, model, relevance_score,
                        summary_cn, contribution_cn, recommendation_cn, tags_json,
                        analyzed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(arxiv_id) DO UPDATE SET
                        analyzed_version_id = excluded.analyzed_version_id,
                        model = excluded.model,
                        relevance_score = excluded.relevance_score,
                        summary_cn = excluded.summary_cn,
                        contribution_cn = excluded.contribution_cn,
                        recommendation_cn = excluded.recommendation_cn,
                        tags_json = excluded.tags_json,
                        analyzed_at = excluded.analyzed_at
                    """,
                    (
                        stable_id,
                        version_id,
                        model,
                        analysis.relevance_score,
                        analysis.summary_cn,
                        analysis.contribution_cn,
                        analysis.recommendation_cn,
                        _json_list(analysis.tags),
                        now,
                    ),
                )

    def load_analyses(
        self,
        arxiv_ids: Sequence[str],
        *,
        domain_key: str = DOMAIN_EMBODIED,
    ) -> dict[str, PaperAIAnalysis]:
        if not arxiv_ids:
            return {}
        domain = _require_domain(domain_key)
        stable_by_version = {item: base_arxiv_id(item) for item in arxiv_ids}
        stable_ids = set(stable_by_version.values())
        placeholders = ",".join("?" for _ in stable_ids)
        params: list[object] = [domain.key, *stable_ids]
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.*, p.latest_version_id
                FROM domain_ai_analyses AS a
                JOIN papers AS p ON p.arxiv_id = a.arxiv_id
                WHERE a.domain_key = ?
                  AND a.arxiv_id IN ({placeholders})
                  AND a.analyzed_version_id = p.latest_version_id
                """,
                tuple(params),
            ).fetchall()
        by_stable = {
            row["arxiv_id"]: PaperAIAnalysis(
                arxiv_id=row["analyzed_version_id"],
                relevance_score=int(row["relevance_score"]),
                summary_cn=row["summary_cn"],
                problem_cn=row["problem_cn"],
                method_cn=row["method_cn"],
                contribution_cn=row["contribution_cn"],
                results_cn=row["results_cn"],
                recommendation_cn=row["recommendation_cn"],
                tags=tuple(json.loads(row["tags_json"])),
                domain_key=row["domain_key"],
                topic_keys=tuple(json.loads(row["topic_keys_json"])),
                analysis_version=int(row["analysis_version"]),
            )
            for row in rows
        }
        return {
            version_id: by_stable[stable_id]
            for version_id, stable_id in stable_by_version.items()
            if stable_id in by_stable
        }

    def replace_analysis_topics(
        self,
        *,
        domain_key: str,
        topic_keys_by_arxiv_id: Mapping[str, Sequence[str]],
        classification_source: str = "deepseek-domain-v1",
    ) -> None:
        if not topic_keys_by_arxiv_id:
            return
        with self._connect() as conn:
            self._replace_analysis_topics(
                conn,
                domain_key=domain_key,
                topic_keys_by_arxiv_id=topic_keys_by_arxiv_id,
                classification_source=classification_source,
            )

    def _replace_analysis_topics(
        self,
        conn: sqlite3.Connection,
        *,
        domain_key: str,
        topic_keys_by_arxiv_id: Mapping[str, Sequence[str]],
        classification_source: str,
    ) -> None:
        if not topic_keys_by_arxiv_id:
            return
        domain = _require_domain(domain_key)
        source = classification_source.strip() or "deepseek-domain-v1"
        if not source.startswith("deepseek"):
            raise ValueError("DeepSeek topic classification source must start with 'deepseek'")
        now = _now_iso()
        for arxiv_id, raw_topic_keys in topic_keys_by_arxiv_id.items():
            stable_id = base_arxiv_id(arxiv_id)
            latest = conn.execute(
                """
                SELECT p.latest_version_id
                FROM papers AS p
                JOIN paper_domains AS pd
                  ON pd.arxiv_id = p.arxiv_id AND pd.domain_key = ?
                WHERE p.arxiv_id = ?
                """,
                (domain.key, stable_id),
            ).fetchone()
            if latest is None or latest["latest_version_id"] != arxiv_id:
                continue
            topic_keys = _validate_topic_keys(domain, raw_topic_keys)
            conn.execute(
                """
                DELETE FROM paper_topics
                WHERE arxiv_id = ? AND domain_key = ?
                  AND classification_source LIKE 'deepseek%'
                """,
                (stable_id, domain.key),
            )
            for topic_key in topic_keys:
                conn.execute(
                    """
                    INSERT INTO paper_topics(
                        arxiv_id, domain_key, topic_key, classification_source, assigned_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(arxiv_id, domain_key, topic_key) DO NOTHING
                    """,
                    (stable_id, domain.key, topic_key, source, now),
                )

    def _load_library(
        self,
        *,
        updated_after: datetime | None = None,
        limit: int,
        offset: int = 0,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
        sort_mode: str = "interest",
    ) -> list[LibraryPaper]:
        if limit < 1:
            return []
        with self._connect() as conn:
            cursor = self._select_library(
                conn,
                updated_after=updated_after,
                limit=limit,
                offset=offset,
                domain_key=domain_key,
                topic_key=topic_key,
                favorites_only=favorites_only,
                sort_mode=sort_mode,
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [self._row_to_library_paper(row) for row in rows]

    def _select_library(
        self,
        conn: sqlite3.Connection,
        *,
        updated_after: datetime | None = None,
        limit: int,
        offset: int = 0,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
        sort_mode: str = "interest",
    ) -> sqlite3.Cursor:
        """Share validated filters/order; the caller owns and closes the cursor."""
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if sort_mode not in {"interest", "relevance", "latest"}:
            raise ValueError(f"Unsupported library sort mode: {sort_mode}")

        domain = _require_domain(domain_key) if domain_key is not None else None
        normalized_topic = TOPIC_ALL
        if topic_key is not None:
            if domain is None:
                raise ValueError("topic_key requires domain_key")
            if topic_key.strip().casefold() != TOPIC_ALL:
                normalized_topic = _validate_topic_keys(domain, (topic_key,))[0]

        where_parts: list[str] = []
        params: list[object] = []
        domain_join = ""
        analysis_domain_key = domain.key if domain is not None else DOMAIN_EMBODIED
        if domain is not None:
            domain_join = (
                "JOIN paper_domains AS pd ON pd.arxiv_id = p.arxiv_id "
                "AND pd.domain_key = ?"
            )
            params.append(domain.key)
            metric_projection = """
                pd.rule_score AS effective_rule_score,
                pd.rule_tags_json AS effective_tags_json,
                pd.matched_terms_json AS effective_matched_terms_json,
                pd.interest_score AS effective_interest_score,
                pd.interest_tags_json AS effective_interest_tags_json,
                pd.interest_matched_terms_json AS effective_interest_matched_terms_json,
                (
                    SELECT COALESCE(json_group_array(t.label), '[]')
                    FROM paper_topics AS pt
                    JOIN topics AS t
                      ON t.domain_key = pt.domain_key AND t.topic_key = pt.topic_key
                    WHERE pt.arxiv_id = p.arxiv_id AND pt.domain_key = pd.domain_key
                ) AS persisted_topic_labels_json,
            """
            order_clauses = {
                "interest": (
                    "pd.interest_score DESC, pd.rule_score DESC, "
                    "p.updated_at DESC, p.arxiv_id DESC"
                ),
                "relevance": (
                    "pd.rule_score DESC, pd.interest_score DESC, "
                    "p.updated_at DESC, p.arxiv_id DESC"
                ),
                "latest": (
                    "p.updated_at DESC, pd.interest_score DESC, "
                    "pd.rule_score DESC, p.arxiv_id DESC"
                ),
            }
            order_clause = order_clauses[sort_mode]
        else:
            metric_projection = """
                p.rule_score AS effective_rule_score,
                p.tags_json AS effective_tags_json,
                p.matched_terms_json AS effective_matched_terms_json,
                p.interest_score AS effective_interest_score,
                p.interest_tags_json AS effective_interest_tags_json,
                p.interest_matched_terms_json AS effective_interest_matched_terms_json,
                '[]' AS persisted_topic_labels_json,
            """
            order_clauses = {
                "interest": (
                    "p.interest_score DESC, p.rule_score DESC, "
                    "p.updated_at DESC, p.arxiv_id DESC"
                ),
                "relevance": (
                    "p.rule_score DESC, p.interest_score DESC, "
                    "p.updated_at DESC, p.arxiv_id DESC"
                ),
                "latest": (
                    "p.updated_at DESC, p.interest_score DESC, "
                    "p.rule_score DESC, p.arxiv_id DESC"
                ),
            }
            order_clause = order_clauses[sort_mode]
        params.append(analysis_domain_key)

        if updated_after is not None:
            where_parts.append("p.updated_at >= ?")
            params.append(updated_after.isoformat())
        if favorites_only:
            where_parts.append("s.is_favorite = 1")
        if domain is not None and normalized_topic != TOPIC_ALL:
            where_parts.append(
                "EXISTS ("
                "SELECT 1 FROM paper_topics AS pt "
                "WHERE pt.arxiv_id = p.arxiv_id "
                "AND pt.domain_key = ? AND pt.topic_key = ?"
                ")"
            )
            params.extend((domain.key, normalized_topic))

        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        params.extend((limit, offset))
        return conn.execute(
            f"""
            SELECT p.*, s.is_favorite, s.is_read,
                   {metric_projection}
                   a.domain_key AS ai_domain_key, a.analyzed_version_id,
                   a.relevance_score AS ai_relevance_score, a.summary_cn,
                   a.problem_cn, a.method_cn, a.contribution_cn, a.results_cn,
                   a.recommendation_cn, a.analysis_version,
                   a.tags_json AS ai_tags_json, a.topic_keys_json AS ai_topic_keys_json
            FROM papers AS p
            JOIN paper_state AS s ON s.arxiv_id = p.arxiv_id
            {domain_join}
            LEFT JOIN domain_ai_analyses AS a
              ON a.arxiv_id = p.arxiv_id
             AND a.domain_key = ?
             AND a.analyzed_version_id = p.latest_version_id
            {where_clause}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        )

    def _set_state(self, arxiv_id: str, column: str, value: bool) -> PaperUserState:
        if column not in {"is_favorite", "is_read"}:
            raise ValueError("Unsupported paper state column")
        stable_id = base_arxiv_id(arxiv_id)
        now = _now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM papers WHERE arxiv_id = ?", (stable_id,)
            ).fetchone()
            if existing is None:
                raise KeyError(f"Unknown arXiv paper: {arxiv_id}")
            conn.execute(
                f"UPDATE paper_state SET {column} = ?, updated_at = ? WHERE arxiv_id = ?",
                (int(value), now, stable_id),
            )
            row = conn.execute(
                "SELECT is_favorite, is_read FROM paper_state WHERE arxiv_id = ?",
                (stable_id,),
            ).fetchone()
        return PaperUserState(
            arxiv_id=stable_id,
            is_favorite=bool(row["is_favorite"]),
            is_read=bool(row["is_read"]),
        )

    def _row_to_library_paper(self, row: sqlite3.Row) -> LibraryPaper:
        paper = Paper(
            arxiv_id=row["latest_version_id"],
            title=row["title"],
            summary=row["summary"],
            authors=tuple(json.loads(row["authors_json"])),
            author_affiliations=tuple(
                tuple(values) for values in json.loads(row["author_affiliations_json"] or "[]")
            ),
            affiliation_source=row["affiliation_source"] or "",
            categories=tuple(json.loads(row["categories_json"])),
            published_at=datetime.fromisoformat(row["published_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            abstract_url=row["abstract_url"],
            pdf_url=row["pdf_url"],
        )
        rule_tags = tuple(json.loads(row["effective_tags_json"]))
        topic_labels = tuple(json.loads(row["persisted_topic_labels_json"] or "[]"))
        ranked = RankedPaper(
            paper=paper,
            score=int(row["effective_rule_score"]),
            tags=tuple(dict.fromkeys((*rule_tags, *topic_labels))),
            matched_terms=tuple(json.loads(row["effective_matched_terms_json"])),
            interest_score=int(row["effective_interest_score"]),
            interest_tags=tuple(json.loads(row["effective_interest_tags_json"])),
            interest_matched_terms=tuple(
                json.loads(row["effective_interest_matched_terms_json"])
            ),
        )
        state = PaperUserState(
            arxiv_id=row["arxiv_id"],
            is_favorite=bool(row["is_favorite"]),
            is_read=bool(row["is_read"]),
        )
        analysis = None
        if row["analyzed_version_id"] == row["latest_version_id"]:
            analysis = PaperAIAnalysis(
                arxiv_id=row["latest_version_id"],
                relevance_score=int(row["ai_relevance_score"]),
                summary_cn=row["summary_cn"],
                problem_cn=row["problem_cn"],
                method_cn=row["method_cn"],
                contribution_cn=row["contribution_cn"],
                results_cn=row["results_cn"],
                recommendation_cn=row["recommendation_cn"],
                tags=tuple(json.loads(row["ai_tags_json"])),
                domain_key=row["ai_domain_key"],
                topic_keys=tuple(json.loads(row["ai_topic_keys_json"] or "[]")),
                analysis_version=int(row["analysis_version"] or 1),
            )
        return LibraryPaper(
            ranked=ranked,
            state=state,
            first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
            analysis=analysis,
        )

    def _existing_schema_version(self) -> int | None:
        """Read schema metadata without mutating an existing database file."""

        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        with closing(sqlite3.connect(self.path)) as conn:
            has_meta = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_meta'"
            ).fetchone()
            if has_meta is None:
                return None
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = 'schema_version'"
            ).fetchone()
        return int(row[0]) if row is not None else None

    def _create_pre_migration_backup(self, from_version: int) -> Path:
        """Create a consistent SQLite snapshot before any schema migration.

        SQLite's backup API includes committed WAL content, unlike a raw file copy.
        Keep the latest three migration snapshots for this database and fail closed
        if the safety copy cannot be created.
        """

        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = backup_dir / (
            f"{self.path.stem}.schema{from_version}-to-{SCHEMA_VERSION}.{timestamp}.db"
        )
        with (
            closing(sqlite3.connect(self.path)) as source,
            closing(sqlite3.connect(backup_path)) as target,
        ):
            source.backup(target)

        pattern = f"{self.path.stem}.schema*-to-*.db"
        backups = sorted(backup_dir.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
        for stale in backups[3:]:
            stale.unlink(missing_ok=True)
        return backup_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # sqlite3's transaction context does not close the connection. Closing
        # outside it guarantees commit/rollback happens before releasing handles.
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 10000")
            with conn:
                yield conn



def _sync_state_from_row(row: sqlite3.Row) -> SyncState:
    return SyncState(
        last_successful_sync=(
            datetime.fromisoformat(row["last_successful_sync"])
            if row["last_successful_sync"]
            else None
        ),
        earliest_covered_date=(
            date.fromisoformat(row["earliest_covered_date"])
            if row["earliest_covered_date"]
            else None
        ),
        latest_covered_date=(
            date.fromisoformat(row["latest_covered_date"])
            if row["latest_covered_date"]
            else None
        ),
        status=row["sync_status"] or "idle",
        last_attempt_at=(
            datetime.fromisoformat(row["last_attempt_at"])
            if row["last_attempt_at"]
            else None
        ),
        last_cancelled_at=(
            datetime.fromisoformat(row["last_cancelled_at"])
            if row["last_cancelled_at"]
            else None
        ),
        last_error=row["last_error"] or "",
    )


def _paper_row_values(
    stable_id: str,
    item: RankedPaper,
    effective_affiliations: Sequence[Sequence[str]],
    effective_affiliation_source: str,
    now: str,
) -> tuple[object, ...]:
    return (
        stable_id,
        item.paper.arxiv_id,
        item.paper.title,
        item.paper.summary,
        _json_list(item.paper.authors),
        _json_nested_list(effective_affiliations),
        effective_affiliation_source,
        _json_list(item.paper.categories),
        item.paper.published_at.isoformat(),
        item.paper.updated_at.isoformat(),
        item.paper.abstract_url,
        item.paper.pdf_url,
        item.score,
        _json_list(item.tags),
        _json_list(item.matched_terms),
        item.interest_score,
        _json_list(item.interest_tags),
        _json_list(item.interest_matched_terms),
        now,
        now,
    )


def _json_tuple(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(str(item) for item in json.loads(value))


def _require_domain(domain_key: str | None) -> ResearchDomain:
    if domain_key not in research_domain_keys():
        raise ValueError(f"Unknown research domain: {domain_key}")
    return get_research_domain(domain_key)


def _infer_topic_keys(
    domain: ResearchDomain,
    values: Sequence[str],
) -> tuple[str, ...]:
    found: list[str] = []
    for value in values:
        topic = domain.topic(value)
        if topic is not None and topic.key not in found:
            found.append(topic.key)
    return tuple(found)


def _validate_topic_keys(
    domain: ResearchDomain,
    values: Sequence[str],
) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        if value.strip().casefold() == TOPIC_ALL:
            continue
        topic = domain.topic(value)
        if topic is None:
            raise ValueError(f"Unknown topic for {domain.key}: {value}")
        if topic.key not in normalized:
            normalized.append(topic.key)
    return tuple(normalized)


def _topic_keys_for_item(
    mapping: Mapping[str, Sequence[str]] | None,
    version_id: str,
    stable_id: str,
) -> Sequence[str] | None:
    if mapping is None:
        return None
    if version_id in mapping:
        return mapping[version_id]
    return mapping.get(stable_id)


def _json_list(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _json_nested_list(values: Sequence[Sequence[str]]) -> str:
    return json.dumps(
        [list(group) for group in values],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _merge_affiliations(
    incoming: Sequence[Sequence[str]],
    existing: Sequence[Sequence[str]],
    author_count: int,
) -> tuple[tuple[str, ...], ...]:
    merged: list[tuple[str, ...]] = []
    for index in range(author_count):
        new_values = tuple(incoming[index]) if index < len(incoming) else ()
        old_values = tuple(existing[index]) if index < len(existing) else ()
        merged.append(new_values or old_values)
    return tuple(merged)


def _has_affiliations(values: Sequence[Sequence[str]]) -> bool:
    return any(bool(group) for group in values)


def _combine_sources(left: str, right: str) -> str:
    parts: list[str] = []
    for value in (left, right):
        for part in (item.strip() for item in value.split("+")):
            if part and part not in parts:
                parts.append(part)
    return " + ".join(parts)


def _merged_affiliation_source(
    *,
    incoming_source: str,
    incoming: Sequence[Sequence[str]],
    existing_source: str,
    existing: Sequence[Sequence[str]],
) -> str:
    if _has_affiliations(incoming) and _has_affiliations(existing):
        return _combine_sources(existing_source, incoming_source or "arXiv metadata")
    if _has_affiliations(incoming):
        return incoming_source or "arXiv metadata"
    if _has_affiliations(existing):
        return existing_source
    return ""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
