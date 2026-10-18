"""Point-in-time retrospective cohorts and reports over stored predictions and results.

Everything here except ``main`` is pure: callers pass already-loaded predictions and
result versions, so tests need no network or database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from proracle.contracts import OUTCOMES, PredictionRecord
from proracle.evaluate import Prediction, Result, _scores, evaluate

SMALL_SAMPLE_THRESHOLD = 30
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20261008
CONFIDENCE_Z = 1.959964


@dataclass(frozen=True)
class ResultVersion:
    """One append-only result observation, with the time it became known locally."""

    result_id: str
    fixture_id: str
    supersedes_id: str | None
    status: str
    actual_outcome: str | None
    kickoff_at: datetime
    available_at: datetime


@dataclass(frozen=True)
class StoredPrediction:
    record: PredictionRecord
    competition_id: str | None = None


@dataclass(frozen=True)
class Cohort:
    model_identity: dict[str, str]
    predicted_from: datetime | None
    predicted_until: datetime | None
    scored: tuple[Prediction, ...]
    training: tuple[Result, ...]
    competitions: Mapping[str, str | None]
    prediction_ids: Mapping[str, str]
    result_ids: Mapping[str, str]
    exclusions: Mapping[str, int]
    sha256: str


def resolve_version(
    versions: Iterable[ResultVersion], *, known_before: datetime | None = None
) -> ResultVersion | None:
    """Return the current version of one fixture's chain, optionally as known at a time.

    ``known_before`` keeps only versions available strictly earlier, so a later
    correction can never change what a past training set could have known.
    """
    considered = [
        version
        for version in versions
        if known_before is None or version.available_at < known_before
    ]
    for version in considered:
        if version.available_at < version.kickoff_at:
            raise ValueError("a result cannot be available before kickoff")
    ids = {version.result_id for version in considered}
    superseded = {version.supersedes_id for version in considered if version.supersedes_id}
    if not superseded <= ids and known_before is None:
        raise ValueError("result chain references a missing version")
    heads = [version for version in considered if version.result_id not in superseded]
    if len(heads) > 1:
        raise ValueError("ambiguous result chain: more than one current version")
    return heads[0] if heads else None


def current_training_results(
    versions: Iterable[ResultVersion], *, known_before: datetime | None = None
) -> tuple[list[Result], int]:
    """Final outcomes as known before a time, plus the count whose current head is not FINAL."""
    by_fixture: dict[str, list[ResultVersion]] = defaultdict(list)
    for version in versions:
        by_fixture[version.fixture_id].append(version)
    training: list[Result] = []
    non_final = 0
    for fixture_id in sorted(by_fixture):
        head = resolve_version(by_fixture[fixture_id], known_before=known_before)
        if head is None:
            continue
        if head.status != "FINAL" or head.actual_outcome not in OUTCOMES:
            non_final += 1
            continue
        training.append(
            Result(
                fixture_id=fixture_id,
                kickoff_at=head.kickoff_at,
                actual_outcome=head.actual_outcome,
                available_at=head.available_at,
            )
        )
    return training, non_final


def _identity(record: PredictionRecord) -> tuple[str, str, str, str, str]:
    return (
        record.model.provider,
        record.model.name,
        record.model.version,
        record.prompt_version,
        record.input_schema_version,
    )


def _identity_dict(identity: tuple[str, str, str, str, str]) -> dict[str, str]:
    keys = ("model_provider", "model_name", "model_version", "prompt_version", "input_schema_version")
    return dict(zip(keys, identity))


def _canonical_digest(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_cohort(
    predictions: Sequence[StoredPrediction],
    versions: Sequence[ResultVersion],
    *,
    model_name: str | None = None,
    model_version: str | None = None,
    predicted_from: datetime | None = None,
    predicted_until: datetime | None = None,
) -> Cohort:
    """Select one model's first prematch prediction per fixture and its point-in-time baseline data.

    Reruns are never cherry-picked: the earliest prediction per fixture is the scored one.
    Training results are those known strictly before the cohort's earliest prediction.
    """
    identities = {_identity(item.record) for item in predictions}
    wanted = {
        identity
        for identity in identities
        if (model_name is None or identity[1] == model_name)
        and (model_version is None or identity[2] == model_version)
    }
    if len(wanted) > 1:
        names = sorted(f"{item[1]}@{item[2]}" for item in wanted)
        raise ValueError(
            "predictions contain more than one model identity; choose one with "
            f"--model-name/--model-version: {', '.join(names)}"
        )
    if not wanted:
        raise ValueError("no predictions match the requested model")
    identity = next(iter(wanted))

    exclusions: Counter[str] = Counter()
    other_identity = [item for item in predictions if _identity(item.record) != identity]
    if other_identity:
        exclusions["OTHER_MODEL_IDENTITY"] = len(other_identity)

    selected = [item for item in predictions if _identity(item.record) == identity]
    in_window: list[StoredPrediction] = []
    for item in selected:
        predicted_at = item.record.predicted_at
        if (predicted_from and predicted_at < predicted_from) or (
            predicted_until and predicted_at >= predicted_until
        ):
            exclusions["OUTSIDE_WINDOW"] += 1
        else:
            in_window.append(item)

    first_per_fixture: dict[str, StoredPrediction] = {}
    for item in sorted(in_window, key=lambda p: (p.record.predicted_at, str(p.record.prediction_id))):
        key = str(item.record.fixture_id)
        if key in first_per_fixture:
            exclusions["ADDITIONAL_PREDICTION_NOT_SCORED"] += 1
        else:
            first_per_fixture[key] = item

    by_fixture: dict[str, list[ResultVersion]] = defaultdict(list)
    for version in versions:
        by_fixture[version.fixture_id].append(version)

    scored: list[Prediction] = []
    competitions: dict[str, str | None] = {}
    prediction_ids: dict[str, str] = {}
    result_ids: dict[str, str] = {}
    for fixture_id, item in sorted(first_per_fixture.items()):
        record = item.record
        head = resolve_version(by_fixture.get(fixture_id, ()))
        if head is None:
            exclusions["NO_RESULT_YET"] += 1
        elif head.status != "FINAL" or head.actual_outcome not in OUTCOMES:
            exclusions[f"RESULT_{head.status}"] += 1
        elif head.available_at <= record.predicted_at:
            exclusions["RESULT_KNOWN_BEFORE_PREDICTION"] += 1
        else:
            scored.append(
                Prediction(
                    fixture_id=fixture_id,
                    kickoff_at=record.kickoff_at,
                    actual_outcome=head.actual_outcome,
                    prediction_at=record.predicted_at,
                    probabilities=record.probabilities.as_dict(),
                    available_at=head.available_at,
                )
            )
            competitions[fixture_id] = item.competition_id
            prediction_ids[fixture_id] = str(record.prediction_id)
            result_ids[fixture_id] = head.result_id

    training: list[Result] = []
    if scored:
        cohort_start = min(row.prediction_at for row in scored)
        scored_ids = {row.fixture_id for row in scored}
        candidates, non_final = current_training_results(versions, known_before=cohort_start)
        training = [row for row in candidates if row.fixture_id not in scored_ids]
        if non_final:
            exclusions["TRAINING_RESULT_NOT_FINAL"] = non_final

    digest = _canonical_digest(
        {
            "model": identity,
            "scored": [
                [
                    row.fixture_id,
                    prediction_ids[row.fixture_id],
                    result_ids[row.fixture_id],
                    row.prediction_at.isoformat(),
                    row.actual_outcome,
                    [row.probabilities[outcome] for outcome in OUTCOMES],
                ]
                for row in scored
            ],
            "training": [
                [row.fixture_id, row.available_at.isoformat(), row.actual_outcome]
                for row in training
            ],
        }
    )
    return Cohort(
        model_identity=_identity_dict(identity),
        predicted_from=predicted_from,
        predicted_until=predicted_until,
        scored=tuple(scored),
        training=tuple(training),
        competitions=competitions,
        prediction_ids=prediction_ids,
        result_ids=result_ids,
        exclusions=dict(sorted(exclusions.items())),
        sha256=digest,
    )


def wilson_interval(successes: int, trials: int) -> dict[str, float]:
    """95% Wilson score interval for a proportion."""
    if trials <= 0:
        raise ValueError("trials must be positive")
    z2 = CONFIDENCE_Z**2
    phat = successes / trials
    denominator = 1 + z2 / trials
    centre = (phat + z2 / (2 * trials)) / denominator
    margin = CONFIDENCE_Z * math.sqrt(phat * (1 - phat) / trials + z2 / (4 * trials**2)) / denominator
    return {"estimate": phat, "lower": max(0.0, centre - margin), "upper": min(1.0, centre + margin)}


def paired_bootstrap_interval(differences: Sequence[float]) -> dict[str, float]:
    """Seeded 95% percentile bootstrap for a mean paired difference."""
    count = len(differences)
    if count == 0:
        raise ValueError("at least one paired difference is required")
    rng = random.Random(BOOTSTRAP_SEED)
    means = sorted(
        sum(differences[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(BOOTSTRAP_RESAMPLES)
    )
    return {
        "mean_difference": sum(differences) / count,
        "lower": means[int(0.025 * BOOTSTRAP_RESAMPLES)],
        "upper": means[int(0.975 * BOOTSTRAP_RESAMPLES) - 1],
    }


def _headline(records: Sequence[Prediction], baseline: dict[str, float] | None) -> dict[str, float]:
    scores = _scores(list(records), baseline)
    return {key: scores[key] for key in ("accuracy", "log_loss", "brier_score")}


def _slice(
    scored: Sequence[Prediction],
    baseline: dict[str, float],
    key: Callable[[Prediction], str],
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Prediction]] = defaultdict(list)
    for row in scored:
        groups[key(row)].append(row)
    return {
        label: {
            "fixtures": len(rows),
            "small_sample": len(rows) < SMALL_SAMPLE_THRESHOLD,
            "model": _headline(rows, None),
            "historical_frequency_baseline": _headline(rows, baseline),
        }
        for label, rows in sorted(groups.items())
    }


def build_report(cohort: Cohort) -> dict[str, Any]:
    """Score a cohort with uncertainty, slices, exclusions, and baseline differences."""
    if not cohort.scored:
        raise ValueError("no scorable predictions: every prediction lacks a FINAL result")
    scored = list(cohort.scored)
    report = evaluate(list(cohort.training), scored)
    baseline = report["historical_frequency_baseline_probabilities"]

    model_log, base_log, model_brier, base_brier = [], [], [], []
    for row in scored:
        model_log.append(_scores([row])["log_loss"])
        base_log.append(_scores([row], baseline)["log_loss"])
        model_brier.append(_scores([row])["brier_score"])
        base_brier.append(_scores([row], baseline)["brier_score"])
    count = len(scored)
    model_correct = round(report["model"]["accuracy"] * count)
    base_correct = round(report["historical_frequency_baseline"]["accuracy"] * count)

    report["cohort"] = {
        "model_identity": cohort.model_identity,
        "predicted_from": cohort.predicted_from.isoformat() if cohort.predicted_from else None,
        "predicted_until": cohort.predicted_until.isoformat() if cohort.predicted_until else None,
        "first_prediction_at": min(row.prediction_at for row in scored).isoformat(),
        "last_prediction_at": max(row.prediction_at for row in scored).isoformat(),
        "scored_fixtures": count,
        "training_fixtures": len(cohort.training),
        "scoring_rule": "earliest prediction per fixture; training results known strictly before the earliest scored prediction",
        "sha256": cohort.sha256,
    }
    report["exclusions"] = dict(cohort.exclusions)
    report["uncertainty"] = {
        "sample_size": count,
        "descriptive_only": count < SMALL_SAMPLE_THRESHOLD,
        "confidence_level": 0.95,
        "accuracy": {
            "model": wilson_interval(model_correct, count),
            "historical_frequency_baseline": wilson_interval(base_correct, count),
        },
        "paired_difference_model_minus_baseline": {
            "log_loss": paired_bootstrap_interval(
                [m - b for m, b in zip(model_log, base_log)]
            ),
            "brier_score": paired_bootstrap_interval(
                [m - b for m, b in zip(model_brier, base_brier)]
            ),
        },
        "notes": "Negative loss differences favor the model. An interval spanning zero is not evidence of an improvement; fewer than 30 fixtures is descriptive only.",
    }
    report["slices"] = {
        "actual_outcome": _slice(scored, baseline, lambda row: row.actual_outcome),
        "competition": _slice(
            scored, baseline, lambda row: cohort.competitions.get(row.fixture_id) or "UNKNOWN"
        ),
        "kickoff_month": _slice(scored, baseline, lambda row: row.kickoff_at.strftime("%Y-%m")),
    }
    report["claim_limits"] = (
        "Prediction metrics only. Accuracy, calibration, and betting return are different; this report makes no profitability claim."
    )
    return report


def _draw_calibration(rows: Sequence[Prediction]) -> dict[str, float]:
    mean_draw = sum(row.probabilities["DRAW"] for row in rows) / len(rows)
    observed = sum(row.actual_outcome == "DRAW" for row in rows) / len(rows)
    return {
        "mean_draw_probability": mean_draw,
        "observed_draw_rate": observed,
        "gap": mean_draw - observed,
        "absolute_gap": abs(mean_draw - observed),
    }


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def build_paired_comparison(
    predictions: Sequence[StoredPrediction],
    versions: Sequence[ResultVersion],
    *,
    candidate_name: str,
    reference_name: str,
    candidate_version: str | None = None,
    reference_version: str | None = None,
    predicted_from: datetime | None = None,
    predicted_until: datetime | None = None,
) -> dict[str, Any]:
    """Score two stored model identities on the same fixtures, candidate minus reference.

    Each identity's cohort follows decision 0003 on its own (earliest prediction per fixture
    per identity, current FINAL label, label not known before that prediction). Only fixtures
    scored in both cohorts are compared, so neither side can gain from fixtures the other
    missed; unpaired fixtures are counted, not dropped silently.
    """
    candidate = build_cohort(
        predictions,
        versions,
        model_name=candidate_name,
        model_version=candidate_version,
        predicted_from=predicted_from,
        predicted_until=predicted_until,
    )
    reference = build_cohort(
        predictions,
        versions,
        model_name=reference_name,
        model_version=reference_version,
        predicted_from=predicted_from,
        predicted_until=predicted_until,
    )
    if candidate.model_identity == reference.model_identity:
        raise ValueError("candidate and reference must be different model identities")
    cand_rows = {row.fixture_id: row for row in candidate.scored}
    ref_rows = {row.fixture_id: row for row in reference.scored}
    paired_ids = sorted(set(cand_rows) & set(ref_rows))
    if not paired_ids:
        raise ValueError("the two identities share no scorable fixture")
    cand = [cand_rows[key] for key in paired_ids]
    ref = [ref_rows[key] for key in paired_ids]

    def per_row(rows: Sequence[Prediction], metric: str) -> list[float]:
        return [_scores([row])[metric] for row in rows]

    log_diff = [c - r for c, r in zip(per_row(cand, "log_loss"), per_row(ref, "log_loss"))]
    brier_diff = [c - r for c, r in zip(per_row(cand, "brier_score"), per_row(ref, "brier_score"))]
    slices: dict[str, dict[str, Any]] = {}
    for outcome in OUTCOMES:
        idx = [i for i, row in enumerate(cand) if row.actual_outcome == outcome]
        if idx:
            slices[outcome] = {
                "fixtures": len(idx),
                "small_sample": len(idx) < SMALL_SAMPLE_THRESHOLD,
                "mean_log_loss_difference": sum(log_diff[i] for i in idx) / len(idx),
            }

    def lead_hours(rows: Sequence[Prediction]) -> float:
        return _median([(row.kickoff_at - row.prediction_at).total_seconds() / 3600 for row in rows])

    count = len(paired_ids)
    digest = _canonical_digest(
        {"candidate": candidate.sha256, "reference": reference.sha256, "paired": paired_ids}
    )
    return {
        "candidate": {
            "model_identity": candidate.model_identity,
            "cohort_sha256": candidate.sha256,
            "scored_fixtures": len(candidate.scored),
            "unpaired_fixtures": len(cand_rows) - count,
            "exclusions": dict(candidate.exclusions),
            "paired": _headline(cand, None),
            "draw_calibration": _draw_calibration(cand),
            "median_lead_time_hours": lead_hours(cand),
        },
        "reference": {
            "model_identity": reference.model_identity,
            "cohort_sha256": reference.sha256,
            "scored_fixtures": len(reference.scored),
            "unpaired_fixtures": len(ref_rows) - count,
            "exclusions": dict(reference.exclusions),
            "paired": _headline(ref, None),
            "draw_calibration": _draw_calibration(ref),
            "median_lead_time_hours": lead_hours(ref),
        },
        "paired_fixtures": count,
        "same_prediction_time_share": sum(
            c.prediction_at == r.prediction_at for c, r in zip(cand, ref)
        ) / count,
        "descriptive_only": count < SMALL_SAMPLE_THRESHOLD,
        "paired_difference_candidate_minus_reference": {
            "log_loss": paired_bootstrap_interval(log_diff),
            "brier_score": paired_bootstrap_interval(brier_diff),
        },
        "slices": {"actual_outcome": slices},
        "sha256": digest,
        "scoring_rule": "earliest prediction per fixture per identity; only fixtures scored for both identities are paired",
        "notes": "Negative differences favor the candidate. An interval spanning zero is not evidence of an improvement; fewer than 30 paired fixtures is descriptive only.",
        "claim_limits": "Prediction metrics only. This comparison makes no profitability claim.",
    }


def export_jsonl(cohort: Cohort, directory: Path) -> tuple[Path, Path]:
    """Write the existing evaluator's input shape; keep the output private by default."""
    directory.mkdir(parents=True, exist_ok=True)
    training_path = directory / "training_results.jsonl"
    predictions_path = directory / "predictions.jsonl"

    def stamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    training_path.write_text(
        "".join(
            json.dumps(
                {
                    "fixture_id": row.fixture_id,
                    "kickoff_at": stamp(row.kickoff_at),
                    "actual_outcome": row.actual_outcome,
                    "available_at": stamp(row.available_at),
                },
                sort_keys=True,
            )
            + "\n"
            for row in cohort.training
            if row.available_at is not None
        ),
        encoding="utf-8",
    )
    predictions_path.write_text(
        "".join(
            json.dumps(
                {
                    "fixture_id": row.fixture_id,
                    "kickoff_at": stamp(row.kickoff_at),
                    "prediction_at": stamp(row.prediction_at),
                    "actual_outcome": row.actual_outcome,
                    "probabilities": row.probabilities,
                },
                sort_keys=True,
            )
            + "\n"
            for row in cohort.scored
        ),
        encoding="utf-8",
    )
    return training_path, predictions_path


def _bound(value: str | None, label: str) -> datetime | None:
    if value is None:
        return None
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), datetime.min.time(), timezone.utc)
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise ValueError(f"{label} must be YYYY-MM-DD or an ISO-8601 timestamp with offset") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    import os

    from proracle.supabase_adapter import SupabaseRESTStore, SupabaseStoreError

    parser = argparse.ArgumentParser(
        description="Read-only retrospective report over your own Supabase predictions and results."
    )
    parser.add_argument("--from", dest="predicted_from", help="include predictions made at or after this UTC date/time")
    parser.add_argument("--until", dest="predicted_until", help="include predictions made before this UTC date/time")
    parser.add_argument("--model-name", help="required when more than one model identity is stored")
    parser.add_argument("--model-version")
    parser.add_argument(
        "--reference-model-name",
        help="compare --model-name (candidate) with this stored identity on the same fixtures",
    )
    parser.add_argument("--reference-model-version")
    parser.add_argument(
        "--export-dir",
        type=Path,
        help="also write evaluator-shaped JSONL here (reports/ is git-ignored; never publish real rows)",
    )
    args = parser.parse_args(argv)
    try:
        project_url = os.environ.get("SUPABASE_URL", "")
        service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not project_url or not service_key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required")
        store = SupabaseRESTStore(
            project_url, service_key, schema=os.environ.get("PRORACLE_DB_SCHEMA", "proracle")
        )
        if args.reference_model_name is not None:
            if args.model_name is None or args.export_dir is not None:
                raise ValueError("--reference-model-name needs --model-name and no --export-dir")
            report = build_paired_comparison(
                store.stored_predictions(),
                store.result_versions(),
                candidate_name=args.model_name,
                candidate_version=args.model_version,
                reference_name=args.reference_model_name,
                reference_version=args.reference_model_version,
                predicted_from=_bound(args.predicted_from, "--from"),
                predicted_until=_bound(args.predicted_until, "--until"),
            )
            print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
            return 0
        cohort = build_cohort(
            store.stored_predictions(),
            store.result_versions(),
            model_name=args.model_name,
            model_version=args.model_version,
            predicted_from=_bound(args.predicted_from, "--from"),
            predicted_until=_bound(args.predicted_until, "--until"),
        )
        report = build_report(cohort)
        if args.export_dir is not None:
            export_jsonl(cohort, args.export_dir)
    except SupabaseStoreError as error:
        print(f"PRORACLE stopped: {error.error_code}", file=sys.stderr)
        return 3
    except (OSError, ValueError) as error:
        print(f"retrospective error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
