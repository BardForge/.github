#!/usr/bin/env python3
"""Audit/plan GitHub management metadata; mutate only with --apply --review PLAN."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parent
ALLOWED_REPOSITORIES = frozenset({
    "BardForge/.github", "BardForge/bardforge", "BardForge/bardforge-foundry",
    "ShinozakiAichi/BardForge-Studio",
})


class SetupError(RuntimeError):
    pass


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def local_path(value: str) -> Path:
    path = (ROOT / value).resolve()
    if not path.is_relative_to(ROOT):
        raise SetupError("All configuration/review/output files must stay in this management directory.")
    return path


def validate_config(config: dict) -> None:
    if config.get("schema_version") != 1:
        raise SetupError("Unsupported configuration schema.")
    if set(config.get("repositories", [])) != ALLOWED_REPOSITORIES:
        raise SetupError("Configuration repository list must equal the hardcoded allowlist.")
    labels = config.get("labels")
    if not isinstance(labels, list) or not labels:
        raise SetupError("A nonempty label catalog is required.")
    owners: dict[str, str] = {}
    canonical = set()
    for label in labels:
        name = label.get("name", "")
        if not isinstance(name, str) or not name or len(name) > 50 or name.casefold() in canonical:
            raise SetupError("Invalid or duplicate canonical label name.")
        canonical.add(name.casefold())
        if not re.fullmatch(r"[0-9a-fA-F]{6}", label.get("color", "")):
            raise SetupError("Invalid label color: " + name)
        description = label.get("description")
        if not isinstance(description, str) or not description or len(description) > 100:
            raise SetupError("Label description must contain 1–100 characters: " + name)
        aliases = label.get("aliases", [])
        if not isinstance(aliases, list):
            raise SetupError("Aliases must be arrays.")
        for candidate in [name, *aliases]:
            if not isinstance(candidate, str) or not candidate or len(candidate) > 50:
                raise SetupError("Invalid label alias.")
            owner = owners.setdefault(candidate.casefold(), name)
            if owner != name:
                raise SetupError("Alias belongs to multiple canonical labels: " + candidate)
    milestone_titles = set()
    milestone_aliases: dict[str, str] = {}
    for milestone in config.get("milestones", []):
        title = milestone.get("title", "")
        if not isinstance(title, str) or not title or title.casefold() in milestone_titles:
            raise SetupError("Invalid or duplicate milestone title.")
        milestone_titles.add(title.casefold())
        if not isinstance(milestone.get("description"), str) or not milestone["description"]:
            raise SetupError("Milestone descriptions are required.")
        if "due_on" in milestone or "state" in milestone:
            raise SetupError("This workflow must not invent due dates or reopen/close milestones.")
        for candidate in [title, *milestone.get("aliases", [])]:
            owner = milestone_aliases.setdefault(candidate.casefold(), title)
            if owner != title:
                raise SetupError("Milestone alias belongs to multiple titles.")
    if len(milestone_titles) != 4:
        raise SetupError("Exactly four planned milestones are required.")


class GitHub:
    def __init__(self, repo: str, allow_write: bool = False):
        if repo not in ALLOWED_REPOSITORIES:
            raise SetupError("Repository is not allowlisted.")
        self.repo = repo
        self.prefix = "repos/" + repo + "/"
        self.allow_write = allow_write

    def request(self, method: str, endpoint: str, body: dict | None = None):
        if not endpoint.startswith(self.prefix):
            raise SetupError("API endpoint escaped the selected repository.")
        if method not in {"GET", "POST", "PATCH"}:
            raise SetupError("Only GET, POST, and PATCH are supported; deletion is forbidden.")
        if method != "GET" and not self.allow_write:
            raise SetupError("Write blocked: default mode is read-only.")
        args = ["gh", "api", "--hostname", "github.com", "--method", method,
                "-H", "Accept: application/vnd.github+json",
                "-H", "X-GitHub-Api-Version: 2026-03-10", endpoint]
        payload = None
        if body is not None:
            args.extend(["--input", "-"])
            payload = json.dumps(body, ensure_ascii=False)
        # No shell, interpolation, temporary request file, or credential printing.
        result = subprocess.run(args, input=payload, text=True, encoding="utf-8",
                                capture_output=True, timeout=90, check=False)
        if result.returncode:
            raise SetupError(f"GitHub API failed for {method} {endpoint}: {result.stderr.strip()}")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except json.JSONDecodeError as error:
            raise SetupError("GitHub returned non-JSON output.") from error

    def pages(self, resource: str, **query) -> list:
        result = []
        for page in range(1, 10001):
            endpoint = self.prefix + resource + "?" + urlencode({**query, "per_page": 100, "page": page})
            values = self.request("GET", endpoint)
            if not isinstance(values, list):
                raise SetupError("Expected a paginated JSON array.")
            result.extend(values)
            if len(values) < 100:
                return result
        raise SetupError("Pagination safety bound exceeded; no writes were attempted by audit.")


def audit(api: GitHub, config: dict) -> dict:
    labels = sorted([
        {"name": row["name"], "color": row["color"].lower(), "description": row.get("description") or "",
         "archived_at": row.get("archived_at")}
        for row in api.pages("labels")
    ], key=lambda row: row["name"].casefold())
    milestones = sorted([
        {"number": row["number"], "title": row["title"], "description": row.get("description") or "",
         "state": row["state"], "due_on": row.get("due_on")}
        for row in api.pages("milestones", state="all")
    ], key=lambda row: row["number"])
    alias_names = {name.casefold() for label in config["labels"] for name in label.get("aliases", [])
                   if name.casefold() != label["name"].casefold()}
    assignments = {}
    for label in labels:
        if label["name"].casefold() in alias_names:
            # /issues includes PRs. state=all preserves closed issue/PR assignments too.
            assignments[label["name"]] = sorted([
                {"number": issue["number"], "labels": sorted(item["name"] for item in issue["labels"])}
                for issue in api.pages("issues", state="all", labels=label["name"])
            ], key=lambda row: row["number"])
    return {"repo": api.repo, "labels": labels, "milestones": milestones, "alias_assignments": assignments}


def plan(config: dict, snapshot: dict) -> dict:
    repo = snapshot["repo"]
    if repo not in ALLOWED_REPOSITORIES:
        raise SetupError("Snapshot repository is not allowlisted.")
    prefix = "repos/" + repo + "/"
    labels = {item["name"].casefold(): item for item in snapshot["labels"]}
    if len(labels) != len(snapshot["labels"]):
        raise SetupError("Audit returned colliding label names.")
    operations = []
    retained_aliases = []

    def add(method, endpoint, body, reason):
        operations.append({"method": method, "endpoint": prefix + endpoint, "body": body, "reason": reason})

    additions: dict[int, set[str]] = {}
    for desired in config["labels"]:
        name = desired["name"]
        canonical = labels.get(name.casefold())
        aliases = []
        for alias in desired.get("aliases", []):
            existing = labels.get(alias.casefold())
            if existing and existing["name"].casefold() != name.casefold() and existing not in aliases:
                aliases.append(existing)
        source = canonical
        if source is None and aliases:
            source = aliases.pop(0)
        if source and source.get("archived_at"):
            raise SetupError("Canonical/source label is archived; review manually without silently unarchiving: " + source["name"])
        body = {"name": name, "color": desired["color"].lower(), "description": desired["description"]}
        if source is None:
            add("POST", "labels", body, "Create missing canonical label; no known alias exists.")
        elif any(source.get(key) != value for key, value in body.items()):
            update_body = {"new_name": body["name"], "color": body["color"], "description": body["description"]}
            add("PATCH", "labels/" + quote(source["name"], safe=""), update_body,
                "Rename alias in place and preserve all assignments." if source is not canonical else
                "Normalize existing canonical label metadata/capitalization.")
        for alias in aliases:
            retained_aliases.append({"alias": alias["name"], "canonical": name,
                                     "action": "Preserve legacy label; add canonical to all existing assignments."})
            for issue in snapshot["alias_assignments"].get(alias["name"], []):
                if name.casefold() not in {value.casefold() for value in issue["labels"]}:
                    additions.setdefault(issue["number"], set()).add(name)
    for number, names in sorted(additions.items()):
        add("POST", f"issues/{number}/labels", {"labels": sorted(names)},
            "Add canonical labels without replacing or removing any issue/PR labels.")

    milestones = snapshot["milestones"]
    # Organization infrastructure shares labels, not the product release roadmap.
    # Existing infrastructure milestones are also left entirely unchanged.
    desired_milestones = [] if repo == "BardForge/.github" else config["milestones"]
    for desired in desired_milestones:
        title = desired["title"]
        possible = {title.casefold(), *(alias.casefold() for alias in desired.get("aliases", []))}
        matches = [item for item in milestones if item["title"].casefold() in possible]
        if len(matches) > 1:
            raise SetupError("Ambiguous milestone aliases; review existing milestones before proceeding: " + title)
        body = {"title": title, "description": desired["description"]}
        if matches:
            source = matches[0]
            if any(source.get(key) != value for key, value in body.items()):
                add("PATCH", f"milestones/{source['number']}", body,
                    "Normalize milestone in place; retain issue assignments, due date, and open/closed state.")
        else:
            version = re.match(r"v\d+\.\d+\.\d+", title)
            if version and any(item["title"].casefold().startswith(version.group().casefold()) for item in milestones):
                raise SetupError("Unrecognized existing title for this milestone version; review aliases: " + title)
            add("POST", "milestones", body, "Create milestone without a due date.")
    return {
        "schema_version": 1, "repo": repo, "configuration_sha256": digest(config),
        "audit_sha256": digest(snapshot), "audit": snapshot, "operations": operations,
        "retained_aliases": retained_aliases,
        "policy": "No deletion, no replacement of issue labels, no invented dates, no milestone state changes.",
    }


def verified_plan(config: dict, snapshot: dict, reviewed: dict) -> dict:
    fresh = plan(config, snapshot)
    # Compare the complete derived plan rather than executing user-edited operations.
    if fresh != reviewed:
        raise SetupError("Reviewed plan differs from the fresh API audit/configuration. Generate and review a new plan.")
    return fresh


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", choices=sorted(ALLOWED_REPOSITORIES))
    parser.add_argument("--config", default="desired-state.json")
    parser.add_argument("--validate", action="store_true", help="Validate local JSON only; no API requests.")
    parser.add_argument("--apply", action="store_true", help="Apply an unchanged reviewed plan after a fresh API audit.")
    parser.add_argument("--review", help="Exact plan JSON previously generated in dry-run mode.")
    parser.add_argument("--output", help="Output JSON within this management directory.")
    args = parser.parse_args(argv)
    config = json.loads(local_path(args.config).read_text(encoding="utf-8"))
    validate_config(config)
    if args.validate:
        if args.apply or args.review:
            raise SetupError("--validate cannot be combined with --apply or --review.")
        print(f"Valid: {len(config['labels'])} canonical labels, {len(config['milestones'])} milestones, four allowlisted repositories.")
        return 0
    if not args.repo:
        raise SetupError("Choose one explicit --repo. No repository is inferred from the working directory.")
    if args.apply != bool(args.review):
        raise SetupError("Apply requires both --apply and --review PLAN.json; otherwise omit both for dry-run.")
    api = GitHub(args.repo)
    before = audit(api, config)
    current = plan(config, before)
    stem = args.repo.replace("/", "--")
    if not args.apply:
        output = local_path(args.output or f"{stem}.plan.json")
        write_json(output, current)
        print(f"DRY RUN: {len(current['operations'])} proposed operations; {len(current['retained_aliases'])} retained legacy aliases.")
        print(f"Review file: {output.name}. No GitHub writes performed.")
        return 0
    reviewed = json.loads(local_path(args.review).read_text(encoding="utf-8"))
    current = verified_plan(config, before, reviewed)
    output = local_path(args.output or f"{stem}.result.json")
    report = {"repo": args.repo, "started_at": datetime.now(timezone.utc).isoformat(),
              "reviewed_audit_sha256": current["audit_sha256"], "confirmed_operations": [], "complete": False}
    write_json(output, report)
    api.allow_write = True
    try:
        for operation in current["operations"]:
            api.request(operation["method"], operation["endpoint"], operation["body"])
            report["confirmed_operations"].append(operation)
            write_json(output, report)
        after = audit(api, config)
        remaining = plan(config, after)
        report.update({"audit_after": after, "remaining_operations": remaining["operations"],
                       "complete": not remaining["operations"]})
        if remaining["operations"]:
            raise SetupError("Post-apply API audit found remaining work. Review the result and generate a fresh plan.")
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output, report)
    print(f"API-CONFIRMED: {len(report['confirmed_operations'])} operations; post-apply audit has no remaining work.")
    print(f"Result file: {output.name}. Legacy collision aliases remain intentionally preserved.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SetupError, OSError, ValueError, subprocess.SubprocessError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        raise SystemExit(1)
