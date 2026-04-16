#!/usr/bin/env python3
"""cairn — triage feed builder.

Reads the hive + work-queue + git-log data sources and renders a single
static index.html via the Jinja-ish template in templates/index.html.

Stdlib + PyYAML only. Rerunnable; idempotent. Designed to fail soft on
malformed data — skip the row, don't crash the build.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    print("error: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)


# --- paths -----------------------------------------------------------------

HIVE_DIR = Path("/mnt/c/ObsidianNotes/.hive")
QUEUE_YAML = HIVE_DIR / "work-queue.yaml"
ITERATIONS_DIR = HIVE_DIR / "iterations"
IDLE_BEATS_DIR = HIVE_DIR / "idle-beats"
CALIBRATION_JSONL = HIVE_DIR / "verifier-calibration-results.jsonl"

# Project registry — used for git-log trailer reads and deploy-state lookups.
# Keeping it local to build.py so v1 stays single-file.
PROJECT_REGISTRY: list[dict[str, str]] = [
    {"id": "electric-app", "path": "/mnt/c/projects/electric-app",
     "repo_url": "https://github.com/konradish/electric-app"},
    {"id": "family-movie-queue", "path": "/mnt/c/projects/family-movie-queue",
     "repo_url": "https://github.com/konradish/family-movie-queue"},
    {"id": "lawpass-ai", "path": "/mnt/c/projects/lawpass-ai",
     "repo_url": "https://github.com/konradish/lawpass-ai"},
    {"id": "obsidian-vault", "path": "/mnt/c/ObsidianNotes", "repo_url": ""},
]

SHIPPED_WINDOW_HOURS = 24
WATCHING_WINDOW_DAYS = 7
CALIBRATION_RECENT_N = 3

THIS_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = THIS_DIR / "templates" / "index.html"
OUTPUT_PATH = THIS_DIR / "index.html"


# --- data model ------------------------------------------------------------

@dataclass
class QueueItem:
    id: str
    project: str
    type: str
    status: str
    created: str | None = None
    started: str | None = None
    completed: str | None = None
    completion_ref: str | None = None
    merge_ref: str | None = None
    outcome: str | None = None
    iterations_used: int | None = None
    source: str | None = None
    spec: str = ""
    depends_on: list[str] = field(default_factory=list)
    notes: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class Gate:
    id: str
    item_id: str | None
    sent_at: str | None
    status: str  # pending | approved | timed_out | ...
    raw: dict


@dataclass
class CommitTrailer:
    sha: str
    short: str
    subject: str
    project: str
    repo_url: str
    date: str
    author: str
    trailers: dict[str, str]


@dataclass
class IdleBeat:
    beat: str
    completed_at: str
    worker_model: str
    artifact: str
    status: str  # completed | no_op | errored
    tick_id: str


@dataclass
class CalibrationResult:
    case_id: str
    score: str  # true_positive | true_negative | false_positive | false_negative | partial
    tested: str
    skill_ref: str


# --- helpers ---------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # tolerate trailing Z and naive strings
        cleaned = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def humanize_delta(dt: datetime | None, reference: datetime | None = None) -> str:
    if dt is None:
        return "—"
    ref = reference or now_utc()
    delta = ref - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        # future
        secs = -secs
        if secs < 3600:
            return f"in {secs // 60}m"
        if secs < 86400:
            return f"in {secs // 3600}h"
        return f"in {secs // 86400}d"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def safe_short(s: str | None, n: int = 7) -> str:
    if not s:
        return ""
    return s.strip().split()[0][:n] if s.strip() else ""


def load_queue_yaml(path: Path) -> dict[str, Any]:
    """Load the work-queue, tolerating hand-written YAML rough edges.

    Two specific pre-scrubs for bullets in block sequences (the `acceptance_criteria`
    section is where this happens in practice):

    1. Bullets starting with ``**`` (or ``*``) are YAML aliases — PyYAML raises.
    2. Bullets that start with a double-quoted phrase but then have unquoted
       trailing text on the same line (``- "Foo" bar``) parse as a scalar
       followed by garbage.

    In both cases we rewrite the line to wrap the entire value in single
    quotes, escaping embedded single quotes by doubling them. This is a
    narrow transform — we only touch hyphen-bulleted lines.
    """
    text = path.read_text(encoding="utf-8")

    def _wrap(indent: str, body: str) -> str:
        # strip trailing whitespace, then single-quote-escape
        body = body.rstrip()
        escaped = body.replace("'", "''")
        return f"{indent}- '{escaped}'"

    # A "structured" bullet opens a mapping, e.g. `- id: foo` or `- key: value`.
    # We leave those alone. Everything else in a block sequence is free-form
    # and safe to wrap.
    mapping_key_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:\s")

    def _scrub(line: str) -> str:
        m = re.match(r"^(\s*)-\s+(.*)$", line)
        if not m:
            return line
        indent, body = m.group(1), m.group(2)
        if not body:
            return line
        # already safely single-quoted?
        if body.startswith("'") and body.endswith("'") and (body.count("'") % 2 == 0):
            return line
        # already safely double-quoted end-to-end?
        if body.startswith('"') and body.endswith('"') and body.count('"') == 2:
            return line
        # Structured mapping-bullet → leave alone
        if mapping_key_re.match(body):
            return line
        # Anything else that looks free-form — wrap it defensively. This covers
        # `**bold**`, `"leading quote" trailing text`, backticks, etc.
        return _wrap(indent, body)

    cleaned_lines = [_scrub(ln) for ln in text.splitlines()]
    cleaned = "\n".join(cleaned_lines)
    # PyYAML in strict mode rejects `\'` inside double-quoted scalars; the
    # hand-written spec strings include it. Swap for a bare apostrophe — a
    # no-op outside a string context.
    cleaned = cleaned.replace("\\'", "'")
    try:
        data = yaml.safe_load(cleaned)
        if data:
            return data
    except yaml.YAMLError as exc:
        print(f"warn: queue YAML parse failed whole-file ({exc}); "
              f"falling back to per-item slicing", file=sys.stderr)

    # Fallback: slice items + metadata apart and parse each item block alone.
    # This survives a single broken item without losing the rest of the queue.
    return _load_queue_fallback(cleaned)


def _load_queue_fallback(text: str) -> dict[str, Any]:
    """Parse items individually; on failure, skip that item."""
    lines = text.splitlines()
    # find `items:` and `metadata:` boundaries (at column 0)
    items_start = None
    metadata_start = None
    for idx, ln in enumerate(lines):
        if ln.rstrip() == "items:":
            items_start = idx + 1
        elif ln.rstrip() == "metadata:":
            metadata_start = idx
            break

    items_out: list[Any] = []
    if items_start is not None:
        end = metadata_start if metadata_start is not None else len(lines)
        # an item block begins at a line matching `- id: ...` at 2-space indent-equivalent
        item_starts: list[int] = []
        for i in range(items_start, end):
            if re.match(r"^-\s+id:\s", lines[i]):
                item_starts.append(i)
        item_starts.append(end)
        for si, ei in zip(item_starts, item_starts[1:]):
            block = "\n".join(lines[si:ei])
            try:
                parsed = yaml.safe_load(block)
                if isinstance(parsed, list) and parsed:
                    items_out.extend(parsed)
            except yaml.YAMLError:
                # Extract just id + status so the item is at least countable
                m_id = re.search(r"^-\s+id:\s*(.+)$", block, re.MULTILINE)
                m_st = re.search(r"^\s+status:\s*(.+)$", block, re.MULTILINE)
                m_proj = re.search(r"^\s+project:\s*(.+)$", block, re.MULTILINE)
                if m_id:
                    items_out.append({
                        "id": m_id.group(1).strip().strip("'\""),
                        "status": (m_st.group(1).strip().strip("'\"") if m_st else "unknown"),
                        "project": (m_proj.group(1).strip().strip("'\"") if m_proj else ""),
                        "type": "",
                        "spec": "",
                        "_fallback": True,
                    })

    metadata_out: dict[str, Any] = {}
    if metadata_start is not None:
        md_block = "\n".join(lines[metadata_start:])
        try:
            md_parsed = yaml.safe_load(md_block)
            if isinstance(md_parsed, dict):
                metadata_out = md_parsed.get("metadata") or {}
        except yaml.YAMLError as exc:
            print(f"warn: metadata parse failed: {exc}", file=sys.stderr)

    return {"items": items_out, "metadata": metadata_out}


def parse_queue_items(raw: dict[str, Any]) -> list[QueueItem]:
    out: list[QueueItem] = []
    for it in raw.get("items") or []:
        if not isinstance(it, dict):
            continue
        try:
            out.append(QueueItem(
                id=str(it.get("id", "")),
                project=str(it.get("project", "")),
                type=str(it.get("type", "")),
                status=str(it.get("status", "")),
                created=it.get("created"),
                started=it.get("started"),
                completed=it.get("completed"),
                completion_ref=it.get("completion_ref"),
                merge_ref=it.get("merge_ref"),
                outcome=it.get("outcome"),
                iterations_used=it.get("iterations_used"),
                source=it.get("source"),
                spec=str(it.get("spec") or ""),
                depends_on=list(it.get("depends_on") or []),
                notes=it.get("notes"),
                tags=list(it.get("tags") or []),
            ))
        except Exception as exc:  # noqa: BLE001 — fail soft
            print(f"warn: skipping malformed queue item: {exc}", file=sys.stderr)
    return out


def parse_gates(raw: dict[str, Any]) -> list[Gate]:
    md = raw.get("metadata") or {}
    out: list[Gate] = []
    for g in md.get("gates") or []:
        if not isinstance(g, dict):
            continue
        out.append(Gate(
            id=str(g.get("id", "")),
            item_id=g.get("item_id"),
            sent_at=g.get("sent_at"),
            status=str(g.get("status", "")),
            raw=g,
        ))
    return out


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def load_idle_beats(dir_: Path, window_days: int) -> list[IdleBeat]:
    if not dir_.exists():
        return []
    cutoff = now_utc() - timedelta(days=window_days)
    out: list[IdleBeat] = []
    for fp in dir_.glob("*.jsonl"):
        for rec in load_jsonl(fp):
            if not isinstance(rec, dict):
                continue
            ts = parse_iso(rec.get("completed_at") or rec.get("started_at"))
            if ts is None:
                # fall back to file mtime
                try:
                    ts = datetime.fromtimestamp(fp.stat().st_mtime, timezone.utc)
                except OSError:
                    continue
            if ts < cutoff:
                continue
            out.append(IdleBeat(
                beat=str(rec.get("beat") or fp.stem.split("-")[0]),
                completed_at=ts.isoformat(),
                worker_model=str(rec.get("worker_model") or ""),
                artifact=str(rec.get("artifact") or rec.get("note") or "(no artifact)"),
                status=str(rec.get("status") or "completed"),
                tick_id=str(rec.get("tick_id") or ""),
            ))
    return out


def load_calibration(path: Path, recent_n: int) -> dict[str, list[CalibrationResult]]:
    """Group calibration results by inferred category.

    Category heuristic: case_id prefix before the first run of non-alpha
    characters, or the first two hyphen-separated tokens — whichever gives
    useful buckets. We don't have a category field on the JSONL, so derive.
    """
    records = load_jsonl(path)
    buckets: dict[str, list[CalibrationResult]] = defaultdict(list)
    for r in records:
        if not isinstance(r, dict):
            continue
        cid = str(r.get("case_id") or "")
        if not cid:
            continue
        # derive category: take tokens that aren't ticket-like ids
        parts = cid.split("-")
        if len(parts) >= 2:
            cat = "-".join(parts[:2])
        else:
            cat = cid
        buckets[cat].append(CalibrationResult(
            case_id=cid,
            score=str(r.get("score") or ""),
            tested=str(r.get("tested") or ""),
            skill_ref=str(r.get("skill_ref") or ""),
        ))
    # sort each bucket newest-first, keep top N
    trimmed: dict[str, list[CalibrationResult]] = {}
    for cat, lst in buckets.items():
        lst.sort(key=lambda x: x.tested, reverse=True)
        trimmed[cat] = lst[:recent_n]
    return trimmed


def summarize_calibration(buckets: dict[str, list[CalibrationResult]]) -> list[dict[str, Any]]:
    """Reduce to a compact per-category row for the health strip."""
    out = []
    for cat, lst in sorted(buckets.items()):
        dots = []
        for r in lst:
            if r.score in ("true_positive", "true_negative"):
                dots.append({"cls": "pass", "label": r.score})
            elif r.score in ("false_positive", "false_negative"):
                dots.append({"cls": "fail", "label": r.score})
            else:
                dots.append({"cls": "partial", "label": r.score or "unknown"})
        out.append({
            "category": cat,
            "dots": dots,
            "last_score": lst[0].score if lst else "",
            "last_case": lst[0].case_id if lst else "",
        })
    return out


# --- git trailer parsing ---------------------------------------------------

TRAILER_RE = re.compile(r"^([A-Z][A-Za-z-]+):\s+(.+)$")


def parse_trailers(message: str) -> dict[str, str]:
    """Parse the final trailer block after the last blank line."""
    if not message.strip():
        return {}
    # drop any trailing whitespace lines
    chunks = re.split(r"\n\s*\n", message.strip())
    if not chunks:
        return {}
    last = chunks[-1]
    trailers: dict[str, str] = {}
    for line in last.splitlines():
        m = TRAILER_RE.match(line.strip())
        if m:
            trailers[m.group(1)] = m.group(2).strip()
    return trailers


def git_log(project: dict[str, str], since_hours: int) -> list[CommitTrailer]:
    path = project["path"]
    if not Path(path, ".git").exists():
        return []
    since = f"{since_hours} hours ago"
    fmt = "%H%x1f%h%x1f%s%x1f%an%x1f%aI%x1f%b%x1e"
    try:
        result = subprocess.run(
            ["git", "-C", path, "log", f"--since={since}", f"--format={fmt}"],
            capture_output=True, text=True, check=True, timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"warn: git log failed for {project['id']}: {exc}", file=sys.stderr)
        return []
    out: list[CommitTrailer] = []
    for record in result.stdout.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 6:
            continue
        sha, short, subject, author, date, body = parts[:6]
        trailers = parse_trailers(body)
        out.append(CommitTrailer(
            sha=sha, short=short, subject=subject, project=project["id"],
            repo_url=project.get("repo_url", ""), date=date, author=author,
            trailers=trailers,
        ))
    return out


def latest_main_sha(project: dict[str, str]) -> str:
    path = project["path"]
    if not Path(path, ".git").exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        return result.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def latest_prod_tag(project: dict[str, str]) -> str:
    """Return the most recent tag or tagged ref if any. Empty string otherwise."""
    path = project["path"]
    if not Path(path, ".git").exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", path, "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        return result.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


# --- section assembly ------------------------------------------------------


def build_health(items: list[QueueItem], metadata: dict[str, Any],
                 calibration: list[dict[str, Any]]) -> dict[str, Any]:
    active = [i for i in items if i.status in ("pending", "in_progress", "blocked", "waiting")]
    counts = {
        "pending": sum(1 for i in active if i.status == "pending"),
        "in_progress": sum(1 for i in active if i.status == "in_progress"),
        "blocked": sum(1 for i in active if i.status == "blocked"),
        "waiting": sum(1 for i in active if i.status == "waiting"),
    }
    last_tick_raw = metadata.get("last_tick")
    last_tick_dt = parse_iso(last_tick_raw) if last_tick_raw else None
    tick_count = metadata.get("tick_count")
    deploys: list[dict[str, str]] = []
    for proj in PROJECT_REGISTRY:
        sha = latest_main_sha(proj)
        tag = latest_prod_tag(proj)
        if not sha and not tag:
            continue
        deploys.append({
            "id": proj["id"],
            "sha": sha,
            "tag": tag,
            "repo_url": proj.get("repo_url", ""),
        })
    return {
        "counts": counts,
        "total_active": sum(counts.values()),
        "last_tick_raw": last_tick_raw or "",
        "last_tick_human": humanize_delta(last_tick_dt) if last_tick_dt else "—",
        "tick_count": tick_count,
        "calibration": calibration,
        "deploys": deploys,
    }


def build_needs_you(items: list[QueueItem], gates: list[Gate]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    # Pending gates first
    for g in gates:
        if g.status != "pending":
            continue
        related_item = next((i for i in items if i.id == g.item_id), None)
        cards.append({
            "kind": "gate",
            "title": f"Gate: {g.id}",
            "subtitle": g.item_id or "",
            "body": (related_item.outcome if related_item else "") or (related_item.spec[:240] if related_item else ""),
            "sent_at": g.sent_at,
            "age": humanize_delta(parse_iso(g.sent_at)),
            "severity": "red",
        })
    # Blocked and waiting items
    for i in items:
        if i.status == "blocked":
            cards.append({
                "kind": "blocked",
                "title": i.id,
                "subtitle": i.project,
                "body": (i.notes or i.spec[:240]),
                "sent_at": i.created,
                "age": humanize_delta(parse_iso(i.created)),
                "severity": "amber",
            })
        elif i.status == "waiting":
            cards.append({
                "kind": "waiting",
                "title": i.id,
                "subtitle": i.project,
                "body": (i.notes or i.spec[:240]),
                "sent_at": i.created,
                "age": humanize_delta(parse_iso(i.created)),
                "severity": "amber",
            })
    return cards


def build_shipped(items: list[QueueItem],
                  trailers_by_short: dict[str, CommitTrailer],
                  window_hours: int) -> list[dict[str, Any]]:
    cutoff = now_utc() - timedelta(hours=window_hours)
    out: list[dict[str, Any]] = []
    for i in items:
        if i.status != "completed":
            continue
        done = parse_iso(i.completed)
        if done is None or done < cutoff:
            continue
        # Try to find the commit trailer
        commit_short = safe_short(i.completion_ref) or safe_short(i.merge_ref)
        tr = trailers_by_short.get(commit_short) if commit_short else None
        rollback = None
        verify_link = None
        if tr:
            rb = tr.trailers.get("Rollback")
            if rb:
                rollback = rb.replace("<hash>", tr.short)
            verify_link = tr.trailers.get("Verifier-Skill-Ref")
        repo_url = tr.repo_url if tr else next(
            (p["repo_url"] for p in PROJECT_REGISTRY if p["id"] == i.project), "")
        commit_url = f"{repo_url}/commit/{tr.sha}" if (tr and repo_url) else ""
        out.append({
            "id": i.id,
            "project": i.project,
            "outcome": i.outcome or "(no outcome recorded)",
            "completed": i.completed,
            "completed_human": humanize_delta(done),
            "commit_short": commit_short,
            "commit_subject": tr.subject if tr else "",
            "commit_url": commit_url,
            "rollback": rollback,
            "verify_link": verify_link,
            "iterations_used": i.iterations_used,
        })
    out.sort(key=lambda c: c["completed"] or "", reverse=True)
    return out


def build_running(items: list[QueueItem]) -> list[dict[str, Any]]:
    out = []
    for i in items:
        if i.status != "in_progress":
            continue
        out.append({
            "id": i.id,
            "project": i.project,
            "started": i.started,
            "started_human": humanize_delta(parse_iso(i.started)),
            "spec_hook": (i.spec.splitlines()[0] if i.spec else "")[:180],
            "iterations_used": i.iterations_used,
        })
    out.sort(key=lambda c: c["started"] or "", reverse=True)
    return out


def build_watching(beats: list[IdleBeat]) -> list[dict[str, Any]]:
    groups: dict[str, list[IdleBeat]] = defaultdict(list)
    for b in beats:
        groups[b.beat].append(b)
    out: list[dict[str, Any]] = []
    for beat, lst in sorted(groups.items()):
        lst.sort(key=lambda b: b.completed_at, reverse=True)
        out.append({
            "beat": beat,
            "count": len(lst),
            "entries": [{
                "completed_at": b.completed_at,
                "completed_human": humanize_delta(parse_iso(b.completed_at)),
                "artifact": b.artifact,
                "status": b.status,
                "tick_id": b.tick_id,
            } for b in lst],
        })
    # Most-recent group first
    out.sort(key=lambda g: g["entries"][0]["completed_at"] if g["entries"] else "",
             reverse=True)
    return out


# --- rendering -------------------------------------------------------------


def render(template: str, context: dict[str, Any]) -> str:
    """Tiny mustache-ish renderer.

    Supports:
      {{ key }}                   — HTML-escaped value
      {{! key }}                  — raw value (trust the source)
      {{# key }}...{{/ key }}     — iterate list; inside, {{ . }} is the item,
                                    and {{ key.subkey }} dots into the item.
      {{^ key }}...{{/ key }}     — render when key is falsy/empty
    """
    import html

    def lookup(ctx: Any, key: str) -> Any:
        if key == ".":
            return ctx
        parts = key.split(".")
        val = ctx
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p)
            else:
                val = getattr(val, p, None)
            if val is None:
                return None
        return val

    # Process section blocks first (non-greedy, nested-safe via recursive regex-replace loop).
    section_re = re.compile(
        r"\{\{\s*([#^])\s*([\w.]+)\s*\}\}(.*?)\{\{\s*/\s*\2\s*\}\}",
        re.DOTALL,
    )

    def render_sections(tpl: str, ctx: Any) -> str:
        while True:
            m = section_re.search(tpl)
            if not m:
                break
            kind, key, inner = m.group(1), m.group(2), m.group(3)
            val = lookup(ctx, key)
            if kind == "#":
                if isinstance(val, list):
                    rendered = "".join(render_sections(inner, item) for item in val)
                elif val:
                    rendered = render_sections(inner, val if isinstance(val, dict) else ctx)
                else:
                    rendered = ""
            else:  # ^
                rendered = render_sections(inner, ctx) if not val else ""
            tpl = tpl[:m.start()] + rendered + tpl[m.end():]
        return tpl

    body = render_sections(template, context)

    # Now substitute leaf vars. Order: raw ({{! key }}) before escaped ({{ key }}).
    def sub_raw(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        val = lookup(context, key)
        return "" if val is None else str(val)

    def sub_escaped(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        val = lookup(context, key)
        if val is None:
            return ""
        return html.escape(str(val))

    body = re.sub(r"\{\{!\s*([\w.]+)\s*\}\}", sub_raw, body)
    # Leaf substitution inside a section uses the section's own context, which
    # the function above already baked in — but the section may have already
    # been flattened. Re-run with root context for any remaining tokens.
    body = re.sub(r"\{\{\s*([\w.]+)\s*\}\}", sub_escaped, body)
    return body


# Two-pass rendering: we actually need per-item context during section
# expansion. The renderer above handles that by using `ctx` inside the
# section. For leaf substitution inside sections to resolve against the item
# ctx, we need to substitute leaves *before* flattening — so we rewrite the
# logic: render leaves first on inner template with item ctx, then stitch.

def render_template(template: str, context: dict[str, Any]) -> str:
    import html

    section_re = re.compile(
        r"\{\{\s*([#^])\s*([\w.]+)\s*\}\}(.*?)\{\{\s*/\s*\2\s*\}\}",
        re.DOTALL,
    )
    raw_leaf_re = re.compile(r"\{\{!\s*([\w.]+)\s*\}\}")
    esc_leaf_re = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")

    def lookup(ctx: Any, key: str) -> Any:
        if key == ".":
            return ctx
        parts = key.split(".")
        val = ctx
        for p in parts:
            if val is None:
                return None
            if isinstance(val, dict):
                val = val.get(p)
            else:
                val = getattr(val, p, None)
        return val

    def render_inner(tpl: str, ctx: Any) -> str:
        # sections first (inside-out naturally via re.sub with replace fn)
        def section_sub(m: re.Match[str]) -> str:
            kind, key, inner = m.group(1), m.group(2), m.group(3)
            val = lookup(ctx, key)
            if kind == "#":
                if isinstance(val, list):
                    return "".join(render_inner(inner, item) for item in val)
                if val:
                    return render_inner(inner, val if isinstance(val, dict) else ctx)
                return ""
            # inverted
            if not val:
                return render_inner(inner, ctx)
            return ""

        prev = None
        out = tpl
        # loop until stable (handles nested sections)
        while out != prev:
            prev = out
            out = section_re.sub(section_sub, out)

        def raw_sub(m: re.Match[str]) -> str:
            val = lookup(ctx, m.group(1).strip())
            return "" if val is None else str(val)

        def esc_sub(m: re.Match[str]) -> str:
            val = lookup(ctx, m.group(1).strip())
            return "" if val is None else html.escape(str(val))

        out = raw_leaf_re.sub(raw_sub, out)
        out = esc_leaf_re.sub(esc_sub, out)
        return out

    return render_inner(template, context)


# --- main ------------------------------------------------------------------


def main() -> int:
    t0 = time.perf_counter()

    raw = load_queue_yaml(QUEUE_YAML)
    items = parse_queue_items(raw)
    gates = parse_gates(raw)
    metadata = raw.get("metadata") or {}

    calibration_buckets = load_calibration(CALIBRATION_JSONL, CALIBRATION_RECENT_N)
    calibration_summary = summarize_calibration(calibration_buckets)

    beats = load_idle_beats(IDLE_BEATS_DIR, WATCHING_WINDOW_DAYS)

    # Gather trailers from each project's git log within the shipped window
    # (extended by 7 days so we can still resolve older completion_refs that
    # belong to stale completed items — but the shipped cards themselves are
    # gated by the 24h completion window).
    trailers_by_short: dict[str, CommitTrailer] = {}
    for proj in PROJECT_REGISTRY:
        for t in git_log(proj, since_hours=SHIPPED_WINDOW_HOURS + 24 * 7):
            trailers_by_short[t.short] = t

    context = {
        "generated_at": now_utc().strftime("%Y-%m-%d %H:%M UTC"),
        "generated_age_hint": "rerun `make build` to refresh",
        "health": build_health(items, metadata, calibration_summary),
        "needs_you": build_needs_you(items, gates),
        "shipped": build_shipped(items, trailers_by_short, SHIPPED_WINDOW_HOURS),
        "running": build_running(items),
        "watching": build_watching(beats),
        "window_hours": SHIPPED_WINDOW_HOURS,
        "window_days": WATCHING_WINDOW_DAYS,
    }

    # Post-compute conveniences for the template (it doesn't do logic)
    context["needs_you_count"] = len(context["needs_you"])
    context["shipped_count"] = len(context["shipped"])
    context["running_count"] = len(context["running"])
    context["watching_count"] = sum(g["count"] for g in context["watching"])
    context["health"]["calibration_has_any"] = bool(calibration_summary)
    context["health"]["deploys_has_any"] = bool(context["health"]["deploys"])

    if not TEMPLATE_PATH.exists():
        print(f"error: template missing at {TEMPLATE_PATH}", file=sys.stderr)
        return 1
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html_out = render_template(template, context)
    OUTPUT_PATH.write_text(html_out, encoding="utf-8")

    elapsed = (time.perf_counter() - t0) * 1000
    print(
        f"cairn: wrote {OUTPUT_PATH} "
        f"(needs:{context['needs_you_count']} "
        f"shipped:{context['shipped_count']} "
        f"running:{context['running_count']} "
        f"watching:{context['watching_count']}) "
        f"in {elapsed:.0f}ms",
        file=sys.stderr,
    )
    # Emit a machine-readable status line on stdout for scripts
    status = {
        "needs_you": context["needs_you_count"],
        "shipped": context["shipped_count"],
        "running": context["running_count"],
        "watching": context["watching_count"],
        "health": {
            "active": context["health"]["total_active"],
            "counts": context["health"]["counts"],
            "last_tick": context["health"]["last_tick_human"],
            "tick_count": context["health"]["tick_count"],
            "deploys": len(context["health"]["deploys"]),
            "calibration_categories": len(context["health"]["calibration"]),
        },
    }
    print(json.dumps(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
