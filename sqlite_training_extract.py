"""Read-only, fail-closed boundary for prospective SQLite training candidates.

This module does not train, import the Excel preprocessing pipeline, write files,
or return unapproved feature/label rows. A candidate is an exact historical
observation under one fixed dataset contract, not permission to train a model.
"""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from reception_schema import SchemaValidationError, validate_history, validate_schema


class ExtractionValidationError(ValueError):
    """Extraction cannot prove that one complete SQLite snapshot is safe."""


def _extracted_at(clock):
    now = clock() if clock is not None else datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ExtractionValidationError("Extraction requires a timezone-aware clock.")
    return now.isoformat()


def extract_candidates(db_path, *, authority=None, clock=None, checkpoint=None):
    """Return only approved rows, using one validated, read-only DB snapshot.

    The application has no configured approval authority. Without one, every
    reception remains excluded; exclusion diagnostics contain IDs and reasons,
    never its intake features, saved predictions, or measured training label.
    ``checkpoint`` is a synchronous test hook called after the read snapshot is
    pinned. It does not grant a write connection or change the extraction policy.
    """
    # Kept separate from the store and predictor: future callers must enter the
    # quality boundary before constructing a DataFrame or invoking preprocessing.
    from quality_policy import (
        DATASET_CONTRACT, POLICY_VERSION, candidate_reasons, watermarks,
    )

    path = Path(db_path).absolute()
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                     isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        if (connection.execute("PRAGMA query_only").fetchone()[0] != 1
                or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1):
            raise ExtractionValidationError("Required read-only checks are unavailable.")
        connection.execute("BEGIN")
        # BEGIN alone is deferred. This first SELECT establishes the snapshot
        # before any schema/history/policy queries or possible concurrent writer.
        connection.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
        if checkpoint is not None:
            checkpoint("snapshot_acquired")
        validate_schema(connection, 2, allow_revision_required=True)
        validate_history(connection, 2)
        metadata = connection.execute(
            "SELECT database_id,write_epoch FROM store_metadata WHERE singleton=1"
        ).fetchone()
        extracted_at = _extracted_at(clock)
        snapshot = dict(metadata) | watermarks(connection) | dict(
            assessment_id=connection.execute(
                "SELECT COALESCE(MAX(assessment_id),0) FROM data_quality_assessments"
            ).fetchone()[0],
            extracted_at=extracted_at, policy_version=POLICY_VERSION,
            dataset_contract=DATASET_CONTRACT,
        )
        candidates, excluded = [], []
        for reception in connection.execute("SELECT * FROM receptions ORDER BY reception_order"):
            reception_id = reception["id"]
            reasons = candidate_reasons(connection, reception_id, authority=authority)
            if authority is None and not reasons:
                reasons = ["quality_authority_unavailable"]
            if reasons:
                excluded.append(dict(reception_id=reception_id, reasons=list(reasons)))
                continue
            assessment = connection.execute(
                "SELECT * FROM data_quality_assessments WHERE reception_id=? "
                "ORDER BY assessment_id DESC LIMIT 1", (reception_id,),
            ).fetchone()
            # Defense against accidentally weakening a policy helper: candidates
            # must still carry an explicit, current approval and valid event ID.
            if (assessment is None or assessment["decision"] != "eligible"
                    or reception["status"] != "seated"
                    or assessment["reception_revision"] != reception["revision"]
                    or assessment["valid_seated_event_id"] is None):
                raise ExtractionValidationError("Candidate lacks a current explicit assessment.")
            candidate = {name: reception[name] for name in (
                "id", "display_number", "reception_order", "received_at", "seated_at",
                "adults", "children", "total_people", "actual_wait_minutes",
            )}
            candidate.update(
                reception_revision=reception["revision"],
                valid_seated_event_id=assessment["valid_seated_event_id"],
                assessment_id=assessment["assessment_id"],
                policy_version=POLICY_VERSION, dataset_contract=DATASET_CONTRACT,
                extracted_at=extracted_at,
            )
            candidates.append(candidate)
        connection.commit()
        return dict(snapshot=snapshot, candidates=candidates, excluded=excluded)
    except (SchemaValidationError, sqlite3.Error) as exc:
        raise ExtractionValidationError("SQLite extraction failed validation; no candidates returned.") from exc
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
