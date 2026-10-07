"""Source and pipeline health, derived from the run history in papers.db.

Most failures that matter here are silent: a bot-walled feed parses as an
empty feed, a publisher swaps abstracts for teasers, a model alias moves to a
new release. So instead of relying on errors, each source is compared against
its own history: how often it normally produces new papers, and how many of
them normally arrive with a full abstract.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median

from . import db
from .fetch import SHORT_ABSTRACT_LEN, load_pending
from .settings import Settings

# A source is stale once it has gone this many times its typical gap between
# days with new papers, clamped so weekends never trip it and weekly
# journals still get flagged within a month.
STALE_FACTOR = 3
STALE_MIN_DAYS = 3
STALE_MAX_DAYS = 30
EMPTY_RUNS = 3  # consecutive runs with zero entries before warning
ERROR_RUNS = 2  # consecutive failed fetches before it's an error, not a blip
RUN_OVERDUE_HOURS = 36
MODEL_CHANGE_NOTICE_DAYS = 7

_SEVERITY_RANK = {"error": 0, "warn": 1, "info": 2}


@dataclass
class Issue:
    severity: str  # "error" | "warn" | "info"
    key: str  # stable id, used to notify only when a problem is new
    message: str
    source: str | None = None
    notify: bool = True  # False for transient blips not worth a desktop alert


@dataclass
class SourceHealth:
    name: str
    status: str = "unknown"  # "ok" | "warn" | "error" | "unknown"
    last_entries: int | None = None
    last_new: date | None = None
    typical_gap: float | None = None
    new_7d: int = 0
    abstract_pct: float | None = None  # share of last-7-day papers with full abstract


@dataclass
class Health:
    issues: list[Issue] = field(default_factory=list)
    sources: list[SourceHealth] = field(default_factory=list)
    last_run: dict | None = None

    @property
    def level(self) -> str:
        if not self.issues:
            return "ok"
        return min((i.severity for i in self.issues), key=_SEVERITY_RANK.get)

    @property
    def problems(self) -> list[Issue]:
        return [i for i in self.issues if i.severity != "info"]

    def summary(self) -> str:
        n = len(self.sources)
        bad = {i.source for i in self.problems if i.source}
        other = [i for i in self.problems if not i.source]
        parts = []
        if bad:
            parts.append(f"{len(bad)} of {n} sources need attention")
        else:
            parts.append(f"All {n} sources healthy")
        if other:
            parts.append(f"{len(other)} pipeline issue{'s' * (len(other) > 1)}")
        notes = len(self.issues) - len(self.problems)
        if notes:
            parts.append(f"{notes} note{'s' * (notes > 1)}")
        return " · ".join(parts)


def configured_sources(settings: Settings) -> list[str]:
    return (
        ["bioRxiv"]
        + [f"arXiv {c}" for c in settings.arxiv_categories]
        + [f.name for f in settings.feeds]
    )


def _day(ts: str) -> date:
    return date.fromisoformat(ts[:10])


def _model_family(model_id: str) -> str:
    for family in ("haiku", "sonnet", "opus"):
        if family in model_id:
            return family
    return model_id


def _model_changes(runs: list[dict], today: date) -> list[Issue]:
    """Flag recent changes in the model behind each alias (e.g. "haiku")."""
    issues = []
    history: dict[
        str, list[tuple[str, str]]
    ] = {}  # family -> [(run_at, id)] newest first
    for run in runs:
        for m in run["models"]:
            history.setdefault(_model_family(m), []).append((run["run_at"], m))
    for family, seen in history.items():
        current = seen[0][1]
        changed_at = None
        for run_at, model in seen:
            if model != current:
                if (
                    changed_at
                    and (today - _day(changed_at)).days <= MODEL_CHANGE_NOTICE_DAYS
                ):
                    issues.append(
                        Issue(
                            "info",
                            f"model:{current}",
                            f"{family.title()} changed {model} → {current} on "
                            f"{changed_at[:10]}; scores may shift",
                        )
                    )
                break
            changed_at = run_at
    return issues


def _source_health(
    name: str,
    runs: list[dict],
    activity: dict[date, tuple[int, int]],
    today: date,
) -> tuple[SourceHealth, list[Issue]]:
    """Assess one source from its recorded runs plus backfilled paper history.

    `activity` maps day -> (new papers, with full abstract) from the papers
    table, used for days before health tracking existed.
    """
    sh = SourceHealth(name)
    issues: list[Issue] = []

    # Merge per-day stats. Recorded runs count every new paper; backfill only
    # sees papers that survived pruning, but also covers unrecorded runs
    # (e.g. `sieve cite`, or runs before tracking), so take the larger.
    recorded: dict[date, list[int]] = {}
    for r in runs:
        rec = recorded.setdefault(_day(r["run_at"]), [0, 0])
        rec[0] += r["new"] or 0
        rec[1] += r["full_abstract"] or 0
    days: dict[date, list[int]] = {}
    for d in set(recorded) | set(activity):
        rec, back = recorded.get(d, [0, 0]), activity.get(d, (0, 0))
        days[d] = [max(rec[0], back[0]), max(rec[1], back[1])]

    if runs:
        sh.last_entries = runs[-1]["entries"]

    # Fetch errors and empty feeds.
    if runs and runs[-1]["error"]:
        failing = [r for r in reversed(runs) if r["error"]]
        err = failing[0]["error"].strip().splitlines()[0][:200]
        streak = next(
            (i for i, r in enumerate(reversed(runs)) if not r["error"]), len(runs)
        )
        since = runs[-streak]["run_at"][:10]
        if streak >= ERROR_RUNS:
            issues.append(
                Issue(
                    "error",
                    f"error:{name}",
                    f"{name}: fetch failing for {streak} runs since {since} ({err})",
                    name,
                )
            )
        else:
            issues.append(
                Issue(
                    "warn",
                    f"blip:{name}",
                    f"{name}: last fetch failed ({err})",
                    name,
                    notify=False,
                )
            )
    elif len(runs) >= EMPTY_RUNS and all(not r["entries"] for r in runs[-EMPTY_RUNS:]):
        issues.append(
            Issue(
                "warn",
                f"empty:{name}",
                f"{name}: returned no entries in the last {EMPTY_RUNS} runs",
                name,
            )
        )

    # Staleness relative to this source's own rhythm.
    active = sorted(d for d, (n, _) in days.items() if n > 0)
    if active:
        sh.last_new = active[-1]
        gaps = [(b - a).days for a, b in zip(active, active[1:])]
        if len(gaps) >= 2:
            sh.typical_gap = median(gaps)
            limit = min(
                max(STALE_MIN_DAYS, STALE_FACTOR * sh.typical_gap), STALE_MAX_DAYS
            )
            since = (today - sh.last_new).days
            if since > limit and not issues:
                issues.append(
                    Issue(
                        "warn",
                        f"stale:{name}",
                        f"{name}: no new papers in {since} days "
                        f"(usually every ~{sh.typical_gap:.0f})",
                        name,
                    )
                )
    elif len(runs) >= EMPTY_RUNS and not issues:
        issues.append(
            Issue(
                "warn",
                f"never:{name}",
                f"{name}: no new papers since health tracking began",
                name,
            )
        )

    # Abstract coverage: recent week vs the weeks before it.
    def coverage(lo: int, hi: int) -> tuple[int, int]:
        sel = [v for d, v in days.items() if lo <= (today - d).days <= hi]
        return sum(n for n, _ in sel), sum(f for _, f in sel)

    n_recent, f_recent = coverage(0, 7)
    n_prior, f_prior = coverage(8, 60)
    sh.new_7d = n_recent
    if n_recent:
        sh.abstract_pct = f_recent / n_recent
    if (
        n_recent >= 5
        and n_prior >= 10
        and f_prior / n_prior >= 0.6
        and f_recent / n_recent < 0.3
    ):
        issues.append(
            Issue(
                "warn",
                f"abstracts:{name}",
                f"{name}: only {f_recent / n_recent:.0%} of new papers have full "
                f"abstracts (was {f_prior / n_prior:.0%})",
                name,
            )
        )

    if issues:
        sh.status = min((i.severity for i in issues), key=_SEVERITY_RANK.get)
    elif runs or active:
        sh.status = "ok"
    return sh, issues


def compute_health(settings: Settings, now: datetime | None = None) -> Health:
    now = now or datetime.now()
    today = now.date()
    health = Health()

    # Ignore anything recorded after `now`, so the checks can be replayed
    # as of a past date.
    cutoff = now.isoformat(timespec="seconds")
    runs = [r for r in db.get_runs(limit=60) if r["run_at"] <= cutoff]
    source_runs = [r for r in db.get_source_runs(days=90) if r["run_at"] <= cutoff]
    activity: dict[str, dict[date, tuple[int, int]]] = {}
    for row in db.get_journal_activity(days=90, full_len=SHORT_ABSTRACT_LEN):
        if row["day"] > today.isoformat():
            continue
        activity.setdefault(row["journal"], {})[date.fromisoformat(row["day"])] = (
            row["n"],
            row["full_abstract"],
        )

    by_source: dict[str, list[dict]] = {}
    for r in source_runs:
        by_source.setdefault(r["source"], []).append(r)

    for name in configured_sources(settings):
        # arXiv papers are stored under one journal name for every category,
        # so per-category history only comes from recorded runs.
        backfill = {} if name.startswith("arXiv ") else activity.get(name, {})
        sh, issues = _source_health(name, by_source.get(name, []), backfill, today)
        health.sources.append(sh)
        health.issues.extend(issues)

    if runs:
        last = health.last_run = runs[0]
        hours = (now - datetime.fromisoformat(last["run_at"])).total_seconds() / 3600
        if hours > RUN_OVERDUE_HOURS:
            health.issues.append(
                Issue(
                    "warn",
                    "overdue",
                    f"Last run was {hours / 24:.1f} days ago; is the scheduled job running?",
                )
            )
        if last["error"]:
            health.issues.append(
                Issue("error", "run-error", f"Last run failed: {last['error']}")
            )
        elif last["failed_batches"]:
            health.issues.append(
                Issue(
                    "warn",
                    "failed-batches",
                    f"{last['failed_batches']} scoring batch(es) failed in the last run",
                )
            )
        health.issues.extend(_model_changes(runs, today))
    else:
        health.issues.append(
            Issue(
                "info",
                "no-history",
                "No runs recorded yet; run history starts with the next `sieve run`",
            )
        )

    pending = load_pending()
    if pending:
        health.issues.append(
            Issue(
                "info",
                "pending",
                f"{len(pending)} papers waiting to be re-scored after a failed run",
            )
        )

    health.issues.sort(key=lambda i: _SEVERITY_RANK[i.severity])
    return health
