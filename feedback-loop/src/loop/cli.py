"""loop ingest | triage | digest | publish | prune | sample | labels-check | eval | review-export | review-import"""

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from loop import labels, store
from loop.changelog import parse_changelog, parse_release_feed
from loop.github import GitHub


def _date(s: str) -> datetime:
    return datetime.combine(date.fromisoformat(s), datetime.min.time(), tzinfo=UTC)


def cmd_ingest(args) -> None:
    conn = store.connect(args.db)
    gh = GitHub(token=os.environ.get("GITHUB_TOKEN"))
    since = _date(args.since) if args.since else store.last_updated(conn, args.repo)
    since = since or datetime.now(UTC) - timedelta(days=7)
    now, n = datetime.now(UTC), 0
    for item in gh.iter_issues(args.repo, since=since):
        store.upsert_issue(conn, args.repo, item, now)
        n += 1
    print(f"issues: {n} upserted (updated since {since:%Y-%m-%d %H:%M})")
    if args.changelog:
        entries = parse_changelog(gh.get_file(args.repo, args.changelog))
        store.upsert_changelog(conn, args.repo, entries)
        print(f"changelog: {len(entries)} entries")
    if args.feed:
        releases = parse_release_feed(gh.get_file(args.repo, args.feed))
        store.upsert_releases(conn, args.repo, releases)
        print(f"releases: {len(releases)} dated")


def cmd_triage(args) -> None:
    from loop.llm import make_client
    from loop.triage import Taxonomy, run_triage

    conn = store.connect(args.db)
    s = run_triage(conn, make_client(), args.repo, _date(args.since), effort=args.effort, limit=args.limit,
                   max_cost=args.max_cost, workers=args.workers, run_id=uuid.uuid4().hex[:12],
                   taxonomy=Taxonomy.load(Path(args.taxonomy)) if args.taxonomy else None)
    print(f"candidates {s.candidates} | triaged {s.triaged} ({s.needs_review} need review) | failed {s.failed} "
          f"| ${s.cost_usd:.2f}" + (" | STOPPED: budget reached" if s.stopped_on_budget else ""))
    if os.environ.get("GITHUB_ACTIONS") and (s.stopped_on_budget or s.failed):
        print(f"::warning::triage incomplete: {s.candidates - s.triaged} of {s.candidates} issues not triaged "
              f"({'budget reached' if s.stopped_on_budget else f'{s.failed} API failures'}); the digest will say so")


def cmd_digest(args) -> None:
    from loop.digest import build_digest

    client = None
    if not args.no_cluster:
        from loop.llm import make_client
        client = make_client()
    conn = store.connect(args.db)
    end = date.fromisoformat(args.week_ending) if args.week_ending else datetime.now(UTC).date()
    md = build_digest(conn, args.repo, end, days=args.days, client=client, effort=args.effort,
                      run_id=uuid.uuid4().hex[:12], feedback_repo=args.feedback_repo or None)
    out = Path(args.out) / f"digest-{args.repo.replace('/', '-')}-{end}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(out)


def cmd_publish(args) -> None:
    from loop.publish import publish

    page = publish(Path(args.digest).read_text(), Path(args.site), date.fromisoformat(args.week_ending),
                   site_url=args.site_url)
    print(page)


def cmd_prune(args) -> None:
    conn = store.connect(args.db)
    before = datetime.now(UTC) - timedelta(days=args.keep_days)
    print(f"pruned body text of {store.prune_bodies(conn, args.repo, before)} triaged issues created before "
          f"{before:%Y-%m-%d}")


def _taxonomy(args):
    from loop.triage import Taxonomy

    return Taxonomy.load(Path(args.taxonomy)) if args.taxonomy else Taxonomy.load()


def cmd_sample(args) -> None:
    conn = store.connect(args.db)
    path = Path(args.labels)
    existing = labels.load(path, set(_taxonomy(args).areas))
    rows = labels.sample(conn, args.repo, args.n, exclude={r["number"] for r in existing})
    labels.append(path, rows)
    frozen = sum(r["split"] == "frozen" for r in rows)
    print(f"added {len(rows)} unlabeled rows to {path} ({frozen} frozen, {len(rows) - frozen} tune). "
          "Fill in kind, area, blocked and severity using evals/CODEBOOK.md.")


def cmd_labels_check(args) -> None:
    rows = labels.load(Path(args.labels), set(_taxonomy(args).areas))  # raises LabelError on a bad row
    hash_path = Path(args.frozen_hash)
    r = labels.check(rows, hash_path)
    print(f"{r.total} rows: {r.labeled['frozen']} frozen and {r.labeled['tune']} tune labeled, {r.unlabeled} unlabeled")
    if args.update:
        hash_path.parent.mkdir(parents=True, exist_ok=True)
        hash_path.write_text(r.current_hash + "\n")
        print(f"wrote {hash_path}: the frozen set is now {r.labeled['frozen']} cases")
    elif r.hash_ok is False:
        sys.exit(f"frozen labels changed but {hash_path} was not updated. If the change is intended, run "
                 "`loop labels-check --update` and commit both files together.")
    elif r.hash_ok is None and r.labeled["frozen"]:
        print(f"no {hash_path} yet: run `loop labels-check --update` once the frozen labels are final")


def cmd_eval(args) -> None:
    from loop import evaluate
    from loop.llm import make_client

    taxonomy = _taxonomy(args)
    rows = labels.load(Path(args.labels), set(taxonomy.areas))
    accepted_path = Path(args.accepted)
    accepted = json.loads(accepted_path.read_text()) if accepted_path.exists() else None
    conn = store.connect(args.db) if Path(args.db).exists() else None
    report = evaluate.run_eval(
        rows, repo=args.repo, split=args.split, client=make_client(), taxonomy=taxonomy, effort=args.effort,
        repeats=args.repeats, max_cost=args.max_cost, workers=args.workers,
        gate_cfg=evaluate.load_gate(Path(args.gate)), accepted=None if args.no_compare else accepted,
        conn=conn, gh=GitHub(token=os.environ.get("GITHUB_TOKEN")))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval-report.json").write_text(json.dumps(report, indent=2) + "\n")
    md = evaluate.render_markdown(report)
    (out / "eval-report.md").write_text(md)
    print(md)
    if args.accept:
        if not report["complete"]:
            sys.exit("not accepting an incomplete eval")
        accepted_path.write_text(json.dumps(evaluate.accepted_record(report), indent=2) + "\n")
        print(f"wrote {accepted_path}: commit it with the change it measures")
    elif args.gate_enforce and not report["gate"]["passed"]:
        sys.exit("eval gate failed")


def cmd_review_export(args) -> None:
    from loop import review

    conn = store.connect(args.db)
    flags = []
    if args.feedback_repo:
        gh = GitHub(token=os.environ.get("GITHUB_TOKEN"))
        flags = [f for item in gh.iter_labeled_issues(args.feedback_repo, review.FLAG_LABEL)
                 if (f := review.parse_flag(item))]
    labeled = {r["number"] for r in labels.load(Path(args.labels), set(_taxonomy(args).areas))}
    path = Path(args.queue)
    queue = review.read_queue(path)
    new = review.build_queue(conn, args.repo, flags, labeled, {q["number"] for q in queue})
    review.write_queue(path, queue + new)
    print(f"{len(new)} new items ({len(flags)} reader flags read); {len(queue) + len(new)} in {path}. "
          'Fill in the label fields, set "confirmed": true, then run `loop review-import`.')


def cmd_review_import(args) -> None:
    from loop import review

    path = Path(args.queue)
    imported, remaining = review.import_confirmed(review.read_queue(path), Path(args.labels),
                                                  set(_taxonomy(args).areas))
    review.write_queue(path, remaining)
    print(f"imported {len(imported)} confirmed rows into {args.labels} as tune cases; {len(remaining)} left in queue")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="loop")
    p.add_argument("--db", default="data/loop.duckdb")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", help="fetch issues, changelog and release dates")
    i.add_argument("--repo", required=True)
    i.add_argument("--since", help="YYYY-MM-DD; default: last ingested update, else 7 days ago")
    i.add_argument("--changelog", default="CHANGELOG.md", help="path in the repo; '' to skip")
    i.add_argument("--feed", default="feed.xml", help="Atom release feed path in the repo; '' to skip")
    i.set_defaults(fn=cmd_ingest)

    t = sub.add_parser("triage", help="Claude triage of untriaged issues")
    t.add_argument("--repo", required=True)
    t.add_argument("--since", required=True, help="YYYY-MM-DD: triage issues created on or after")
    t.add_argument("--limit", type=int)
    t.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    t.add_argument("--max-cost", type=float, default=2.0, help="USD; stop submitting work at this spend")
    t.add_argument("--workers", type=int, default=4)
    t.add_argument("--taxonomy")
    t.set_defaults(fn=cmd_triage)

    d = sub.add_parser("digest", help="write the weekly digest")
    d.add_argument("--repo", required=True)
    d.add_argument("--week-ending", help="YYYY-MM-DD, exclusive; default today")
    d.add_argument("--days", type=int, default=7)
    d.add_argument("--no-cluster", action="store_true", help="skip Claude clustering (exact-ask grouping only)")
    d.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    d.add_argument("--out", default="out")
    d.add_argument("--feedback-repo", default=os.environ.get("LOOP_FEEDBACK_REPO", ""),
                   help="owner/repo whose 'wrong item' issue form the digest's flag links open")
    d.set_defaults(fn=cmd_digest)

    pb = sub.add_parser("publish", help="add a digest to the static site and rebuild its index and RSS feed")
    pb.add_argument("--digest", required=True, help="digest markdown written by `loop digest`")
    pb.add_argument("--week-ending", required=True)
    pb.add_argument("--site", default="docs")
    pb.add_argument("--site-url", required=True, help="public base URL of the site, for the RSS feed")
    pb.set_defaults(fn=cmd_publish)

    pr = sub.add_parser("prune", help="drop body text of older, already-triaged issues before the DB is saved")
    pr.add_argument("--repo", required=True)
    pr.add_argument("--keep-days", type=int, default=14,
                    help="keep bodies of issues newer than this; keep it above the triage window")
    pr.set_defaults(fn=cmd_prune)

    def label_args(x) -> None:
        x.add_argument("--labels", default=str(labels.DEFAULT_LABELS))
        x.add_argument("--taxonomy")

    sa = sub.add_parser("sample", help="add N stratified, unlabeled issues to the labels file")
    sa.add_argument("--repo", required=True)
    sa.add_argument("-n", type=int, default=150)
    label_args(sa)
    sa.set_defaults(fn=cmd_sample)

    lc = sub.add_parser("labels-check", help="validate labels and check the frozen set against its committed hash")
    label_args(lc)
    lc.add_argument("--frozen-hash", default=str(labels.DEFAULT_FROZEN_HASH))
    lc.add_argument("--update", action="store_true", help="record the current frozen set as intended")
    lc.set_defaults(fn=cmd_labels_check)

    e = sub.add_parser("eval", help="score triage against the hand labels and the rules baseline")
    e.add_argument("--repo", required=True)
    e.add_argument("--split", default="frozen", choices=["frozen", "tune", "all"])
    e.add_argument("--repeats", type=int, default=3)
    e.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    e.add_argument("--max-cost", type=float, default=10.0, help="USD; an eval cut short by this fails the gate")
    e.add_argument("--workers", type=int, default=4)
    e.add_argument("--gate", default="evals/gate.yaml")
    e.add_argument("--accepted", default="evals/accepted.json")
    e.add_argument("--no-compare", action="store_true", help="skip the regression check against accepted.json")
    e.add_argument("--accept", action="store_true", help="record this run as the new accepted baseline")
    e.add_argument("--gate-enforce", action="store_true", help="exit non-zero when the gate fails (CI)")
    e.add_argument("--out", default="out/eval")
    label_args(e)
    e.set_defaults(fn=cmd_eval)

    re_ = sub.add_parser("review-export", help="queue needs-review issues and reader flags for labeling")
    re_.add_argument("--repo", required=True)
    re_.add_argument("--feedback-repo", default=os.environ.get("LOOP_FEEDBACK_REPO", ""))
    re_.add_argument("--queue", default="evals/review_queue.jsonl")
    label_args(re_)
    re_.set_defaults(fn=cmd_review_export)

    ri = sub.add_parser("review-import", help="move confirmed queue rows into the labels file")
    ri.add_argument("--queue", default="evals/review_queue.jsonl")
    label_args(ri)
    ri.set_defaults(fn=cmd_review_import)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except labels.LabelError as e:
        sys.exit(f"label error: {e}")


if __name__ == "__main__":
    sys.exit(main())
