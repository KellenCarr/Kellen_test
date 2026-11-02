"""End to end: the real CLI, run as subprocesses, against local stand-ins for the GitHub REST API and the Claude
Messages API. The Anthropic SDK request path, DuckDB and every command run for real; issue text and Claude's
answers are synthetic, with known true labels and planted errors so the eval's scores can be checked exactly.

This is the test that caught incremental ingest crashing on the second scheduled run (pytz); unit tests didn't.
"""

import json
import os
import random
import re
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

REPO, FEEDBACK = "acme/widget", "me/loop"
GATE = Path(__file__).resolve().parents[1] / "evals" / "gate.yaml"

# (title, form, area, blocked, severity, body sentence) -- synthetic, no real issue content
SCENARIOS = [
    ("Startup exits with auth error on Bedrock", "bug", "third_party_platforms", True, "critical",
     "Every launch exits with an authentication error, so I cannot use the tool at all."),
    ("MCP server tools missing after restart", "bug", "mcp", False, "medium",
     "After restarting, tools from my MCP server are gone until I reconnect it manually."),
    ("Pasting an image freezes the terminal", "bug", "tui", False, "high",
     "Pasting a screenshot freezes the input box for about ten seconds, then it recovers."),
    ("Hook output is swallowed on failure", "bug", "hooks", False, "medium",
     "When a PreToolUse hook exits non-zero the message it prints never shows up."),
    ("Compaction drops my CLAUDE.md rules", "bug", "context_memory", False, "high",
     "After auto compaction the project rules in CLAUDE.md stop being followed."),
    ("Edits deleted files it was told to keep", "model", "model_behavior", True, "critical",
     "It deleted two files I explicitly said not to touch and I lost uncommitted work."),
    ("Support a per-project default model", "feature", "install_update", False, "low",
     "I switch models by hand in every project; a per-project default would save time."),
    ("Allow subagents to share a scratch directory", "feature", "subagents", False, "low",
     "Subagents can't see each other's intermediate files, so I copy them around."),
    ("Docs for permission rules are out of date", "docs", "docs", False, "low",
     "The permission rules page still shows the old syntax for allow lists."),
    ("Login loop on Windows after update", "bug", "auth_limits", True, "critical",
     "Since the last update, login opens the browser, succeeds, and asks again forever. No workaround."),
    ("VS Code extension shows blank panel", "bug", "ide_vscode", False, "medium",
     "The extension panel is blank until I reload the window; reloading is a workaround."),
    ("How do I change the model mid-session?", "free", "other", False, "low",
     "Is there a command to switch models without restarting? Couldn't find it."),
]
KIND = {"bug": "bug", "model": "model_behavior", "feature": "feature_request", "docs": "docs", "free": "question"}
N_ISSUES = 48
REFUSED, BAD_QUOTE = 11, 7                                  # planted: one refusal, one quote not in the issue
AREA_ERRORS = {i for i in range(N_ISSUES) if i % 6 == 2}    # planted: area answered as "other"


def render(fields: dict) -> str:
    return "\n\n".join(f"### {k}\n\n{v}" for k, v in fields.items())


def make_world(now: datetime) -> dict:
    rng = random.Random(7)
    forms = {
        "bug": ("[BUG] ", ["bug"], lambda s: {
            "What's Wrong?": s, "What Should Happen?": "It should work.",
            "Platform": rng.choice(["Anthropic API", "AWS Bedrock", "Google Vertex AI"]),
            "Operating System": rng.choice(["macOS", "Windows", "Ubuntu/Debian Linux"]),
            "Terminal/Shell": rng.choice(["iTerm2", "Windows Terminal"]), "Claude Model": "Opus",
            "Claude Code Version": "2.9.1", "Is this a regression?": "I don't know"}),
        "model": ("[MODEL] ", ["model"], lambda s: {"What did Claude do?": s, "Platform": "Anthropic API",
                                                    "Impact": "Critical - Data loss or corrupted project"}),
        "feature": ("[FEATURE] ", ["enhancement"], lambda s: {"Problem Statement": s,
                                                              "Priority": "Medium - Would be very helpful"}),
        "docs": ("[DOCS] ", ["documentation"], lambda s: {"Documentation issue": s}),
        "free": ("", [], None),
    }
    issues, truth = {}, {}
    for i in range(N_ISSUES):
        title, form, area, blocked, sev, sentence = SCENARIOS[i % len(SCENARIOS)]
        prefix, labels, fields = forms[form]
        full = f"{prefix}{title} (case {i})"
        body = render(fields(sentence)) if fields else sentence
        if i == 5:
            body += "\n\n```\n" + "\n".join(f"log line {n}" for n in range(80)) + "\n```"
        ts = (now - timedelta(days=rng.uniform(0.2, 5.5))).isoformat().replace("+00:00", "Z")
        n = 9000 + i
        issues[n] = {"number": n, "title": full, "body": body, "state": "open", "state_reason": None,
                     "labels": [{"name": lb} for lb in labels], "author_association": "NONE",
                     "comments": rng.randint(0, 6), "reactions": {"total_count": rng.randint(0, 15)},
                     "html_url": f"https://github.com/{REPO}/issues/{n}", "created_at": ts, "updated_at": ts,
                     "closed_at": None}
        truth[full] = {"kind": KIND[form], "area": area, "blocked": blocked, "severity": sev, "i": i}
    issues[9100] = {**issues[9000], "number": 9100, "title": "Internal: release checklist",
                    "author_association": "MEMBER", "html_url": f"https://github.com/{REPO}/issues/9100"}
    issues[9101] = {**issues[9001], "number": 9101, "pull_request": {"url": "x"}}
    flags = [{"number": 1, "html_url": f"https://github.com/{FEEDBACK}/issues/1", "labels": [{"name": "wrong-item"}],
              "body": render({"Digest week": str(now.date()), "Digest row": "x", "Issue number": "9002",
                              "What is wrong": "Severity", "What it should be": "medium"})}]
    day = lambda d: (now - timedelta(days=d)).date()  # noqa: E731
    files = {
        "CHANGELOG.md": "# Changelog\n\n## 3.1.0\n\n- Added plugin search\n- Fixed a crash on paste\n\n"
                        "## 3.0.9\n\n- [VSCode] Fixed blank panel\n- Improved startup time\n",
        "feed.xml": ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
                     f'<entry><title>v3.1.0</title><updated>{day(2)}T12:00:00Z</updated></entry>'
                     f'<entry><title>v3.0.9</title><updated>{day(4)}T12:00:00Z</updated></entry></feed>'),
    }
    return {"issues": issues, "truth": truth, "flags": flags, "files": files, "mode": "normal", "requests": []}


def triage_answer(world: dict, user: str) -> tuple[dict | None, str]:
    title = re.search(r"<title>(.*?)</title>", user, re.S).group(1)
    t = world["truth"][title]
    i = t["i"]
    if i == REFUSED:
        return None, "refusal"
    ans = {"kind": t["kind"], "area": "other" if i in AREA_ERRORS else t["area"], "proposed_area": None,
           "ask": "Fix " + re.sub(r"^\[\w+\] |\(case \d+\)", "", title).strip().lower(),
           "blocked": t["blocked"], "blocked_reason": "cannot work" if t["blocked"] else None,
           "severity": t["severity"], "evidence_quote": "not in the issue" if i == BAD_QUOTE else title[:25],
           "confidence": "high"}
    if world["mode"] == "regressed" and i % 4 == 0:
        ans["kind"] = "other"
    return ans, "end_turn"


def make_handler(world: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, data: bytes, headers=None, ctype="application/json"):
            self.send_response(200)
            self.send_header("content-type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # Messages API
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            world["requests"].append({"beta": self.headers.get("anthropic-beta"), "body": body})
            system, user = body["system"][0]["text"], body["messages"][0]["content"]
            if "group issue summaries" in system:
                groups: dict[str, list[int]] = {}
                for m in re.finditer(r"^#(\d+): (.*)$", user, re.M):
                    groups.setdefault(m.group(2), []).append(int(m.group(1)))
                data, stop = {"clusters": [{"label": k, "numbers": v} for k, v in groups.items()]}, "end_turn"
            else:
                data, stop = triage_answer(world, user)
            msg = {"id": "msg_local", "type": "message", "role": "assistant", "model": body["model"],
                   "content": [{"type": "text", "text": json.dumps(data)}] if data else [],
                   "stop_reason": stop, "stop_sequence": None,
                   "usage": {"input_tokens": 300, "output_tokens": 150, "cache_read_input_tokens": 1500,
                             "cache_creation_input_tokens": 0}}
            if stop == "refusal":
                msg["stop_details"] = {"type": "refusal", "category": "bio", "explanation": "planted"}
            self._send(json.dumps(msg).encode(), {"request-id": "req_local"})

        def do_GET(self):  # GitHub REST API
            u, q = urlparse(self.path), parse_qs(urlparse(self.path).query)
            if u.path == f"/repos/{REPO}/issues":
                since = datetime.fromisoformat(q["since"][0]) if "since" in q else None
                items = [x for x in sorted(world["issues"].values(), key=lambda x: x["updated_at"])
                         if since is None or datetime.fromisoformat(x["updated_at"]) >= since]
                page = int(q.get("page", ["1"])[0])
                headers = {}
                if page * 20 < len(items):  # 20 per page, to exercise pagination
                    nq = {k: v[0] for k, v in q.items()} | {"page": str(page + 1)}
                    headers["link"] = f'<http://{self.headers["host"]}{u.path}?{urlencode(nq)}>; rel="next"'
                return self._send(json.dumps(items[(page - 1) * 20: page * 20]).encode(), headers)
            if m := re.fullmatch(rf"/repos/{REPO}/issues/(\d+)", u.path):
                return self._send(json.dumps(world["issues"][int(m.group(1))]).encode())
            if u.path == f"/repos/{FEEDBACK}/issues":
                return self._send(json.dumps(world["flags"]).encode())
            if m := re.fullmatch(rf"/repos/{REPO}/contents/(.+)", u.path):
                return self._send(world["files"][m.group(1)].encode(), ctype="text/plain")
            self.send_response(404)
            self.end_headers()

    return Handler


@pytest.fixture
def world():
    w = make_world(datetime.now(UTC).replace(microsecond=0))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(w))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    w["base"] = f"http://127.0.0.1:{server.server_port}"
    yield w
    server.shutdown()


def test_weekly_pipeline_end_to_end(world, tmp_path):
    now = datetime.now(UTC)
    since, today, tomorrow = ((now + timedelta(days=d)).date().isoformat() for d in (-7, 0, 1))
    runner = tmp_path / "run_loop.py"  # the only patch: point the GitHub client at the stand-in
    runner.write_text("import os, sys\nimport loop.github\nloop.github.API = os.environ['LOOP_TEST_GITHUB_API']\n"
                      "from loop.cli import main\nmain(sys.argv[1:])\n")
    env = {**os.environ, "LOOP_TEST_GITHUB_API": world["base"], "ANTHROPIC_BASE_URL": world["base"],
           "ANTHROPIC_API_KEY": "local-test-not-a-real-key", "GITHUB_TOKEN": "local-test",
           "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.pop("GITHUB_ACTIONS", None)

    def loop(*args, ok=True) -> str:
        p = subprocess.run([sys.executable, str(runner), *args], cwd=tmp_path, env=env, capture_output=True,
                           text=True, timeout=120)
        out = (p.stdout + p.stderr).strip()
        assert (p.returncode == 0) == ok, f"loop {' '.join(args)} exited {p.returncode}:\n{out}"
        return out

    # First weekly run: paginated ingest skips the PR and loads changelog + release dates.
    out = loop("ingest", "--repo", REPO, "--since", since)
    assert "issues: 49 upserted" in out and "releases: 2 dated" in out
    # Staff-filed issue excluded; the planted refusal and hallucinated quote go to review.
    assert "candidates 48 | triaged 48 (2 need review) | failed 0" in loop(
        "triage", "--repo", REPO, "--since", since, "--max-cost", "5")
    req = world["requests"][0]
    assert req["beta"] == "server-side-fallback-2026-07-01" and req["body"]["fallbacks"] == "default"
    assert req["body"]["output_config"]["effort"] == "low"
    assert req["body"]["output_config"]["format"]["type"] == "json_schema"
    assert req["body"]["system"][0]["cache_control"] == {"type": "ephemeral"} and "temperature" not in req["body"]
    assert any("lines of output omitted" in r["body"]["messages"][0]["content"] for r in world["requests"])

    path = loop("digest", "--repo", REPO, "--week-ending", tomorrow, "--feedback-repo", FEEDBACK).splitlines()[-1]
    digest = (tmp_path / path).read_text()
    for section in ("## What users are asking for", "## What is hurting users", "## Who is blocked",
                    "## What shipped", "## Pipeline health", "issues/new?template=wrong-item.yml"):
        assert section in digest
    assert "opened in window: 48; triaged: 48 (46 auto, 2 awaiting review)" in digest
    assert "Every launch exits" not in digest and "log line" not in digest  # no raw issue text
    loop("publish", "--digest", path, "--week-ending", today, "--site", "docs", "--site-url", "https://x.github.io/y")
    assert all((tmp_path / "docs" / p).exists() for p in (f"digests/{today}.md", "index.md", "feed.xml"))

    # Second run: incremental ingest (no --since) must work, and only an edited issue is re-triaged.
    n_calls = len(world["requests"])
    loop("ingest", "--repo", REPO, "--changelog", "", "--feed", "")
    assert "candidates 0" in loop("triage", "--repo", REPO, "--since", since, "--max-cost", "5")
    assert len(world["requests"]) == n_calls
    edited = world["issues"][9003]
    edited["body"] += "\n\nEdit: also on Linux."
    edited["updated_at"] = (now + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    loop("ingest", "--repo", REPO, "--changelog", "", "--feed", "")
    assert "candidates 1 | triaged 1" in loop("triage", "--repo", REPO, "--since", since, "--max-cost", "5")

    # Labels: sample everything, fill in the known truth, freeze; an unrecorded frozen relabel fails the check.
    loop("sample", "--repo", REPO, "-n", str(N_ISSUES))
    labels_path = tmp_path / "evals" / "labels.jsonl"
    titles = {n: x["title"] for n, x in world["issues"].items()}
    rows = [json.loads(x) for x in labels_path.read_text().splitlines()]
    for r in rows:
        r.update({k: world["truth"][titles[r["number"]]][k] for k in ("kind", "area", "blocked", "severity")})
    write = lambda rs: labels_path.write_text("".join(json.dumps(r) + "\n" for r in rs))  # noqa: E731
    write(rows)
    loop("labels-check", "--update")
    loop("labels-check")
    frozen_i = next(i for i, r in enumerate(rows) if r["split"] == "frozen")
    original = rows[frozen_i]["severity"]
    rows[frozen_i]["severity"] = "low" if original != "low" else "high"
    write(rows)
    assert "frozen labels changed" in loop("labels-check", ok=False)
    rows[frozen_i]["severity"] = original
    write(rows)

    # Eval: scores equal the planted rates; the gate passes, then blocks a regressed "prompt".
    gate = tmp_path / "gate.yaml"
    gate.write_text(GATE.read_text().replace("min_cases: 30", "min_cases: 10")
                    .replace("refusal_rate: 0.02", "refusal_rate: 0.10")
                    .replace("failure_rate: 0.02", "failure_rate: 0.10")
                    .replace("evidence_valid_rate: 0.95", "evidence_valid_rate: 0.80"))
    loop("eval", "--repo", REPO, "--split", "all", "--repeats", "2", "--gate", str(gate), "--accept")
    report = json.loads((tmp_path / "out/eval/eval-report.json").read_text())
    m = report["metrics"]
    assert m["cases"] == N_ISSUES and m["runs"] == 2 * N_ISSUES
    assert m["area_acc"] == round(1 - (len(AREA_ERRORS) + 1) / N_ISSUES, 4)  # +1: the refusal is wrong everywhere
    assert m["refusal_rate"] == round(1 / N_ISSUES, 4)
    assert m["evidence_valid_rate"] == round((N_ISSUES - 2) / N_ISSUES, 4)
    assert m["blocked_recall"] == 1.0 and m["area_acc"] > report["baseline"]["area_acc"]
    assert report["gate"]["passed"], report["gate"]["failures"]
    assert (tmp_path / "evals/accepted.json").exists()
    world["mode"] = "regressed"
    loop("eval", "--repo", REPO, "--split", "all", "--repeats", "2", "--gate", str(gate), "--gate-enforce", ok=False)
    failures = json.loads((tmp_path / "out/eval/eval-report.json").read_text())["gate"]["failures"]
    assert any("kind_acc regressed" in f for f in failures)
    world["mode"] = "normal"

    # Review loop: needs-review issues plus a reader flag; a confirmed row becomes a tune label.
    loop("review-export", "--repo", REPO, "--feedback-repo", FEEDBACK, "--labels", "evals/none.jsonl")
    queue_path = tmp_path / "evals" / "review_queue.jsonl"
    queue = [json.loads(x) for x in queue_path.read_text().splitlines()]
    assert len(queue) == 3 and sum(q["source"] == "reader_flag" for q in queue) == 1
    queue[0].update(kind="bug", area="docs", blocked=False, severity="low", confirmed=True)
    queue_path.write_text("".join(json.dumps(q) + "\n" for q in queue))
    assert "imported 1" in loop("review-import", "--labels", "evals/reviewed.jsonl")

    # Prune drops old bodies; a budget stop on a fresh database is visible in the digest.
    assert re.search(r"pruned body text of [1-9]\d* ", loop("prune", "--repo", REPO, "--keep-days", "3"))
    loop("--db", "data/b.duckdb", "ingest", "--repo", REPO, "--since", since, "--changelog", "", "--feed", "")
    loop("--db", "data/b.duckdb", "triage", "--repo", REPO, "--since", since, "--max-cost", "0.01", "--workers", "1")
    path = loop("--db", "data/b.duckdb", "digest", "--repo", REPO, "--no-cluster", "--week-ending", tomorrow,
                "--out", "out/b").splitlines()[-1]
    assert "**not triaged:" in (tmp_path / path).read_text()
