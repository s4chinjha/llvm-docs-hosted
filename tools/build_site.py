#!/usr/bin/env python3
"""Build the LLVM documentation preview site.

Examples
--------
Build every enabled project into ./site::

    python3 tools/build_site.py

Build one project from a specific ref::

    python3 tools/build_site.py --project libcxx --ref libcxx-docs-md-convert

Build from a local checkout instead of the configured repository::

    python3 tools/build_site.py --project libcxx --source-local ~/oss_contribute/llvm-project

The script never writes into the source repository and never publishes
anything; publishing is the job of .github/workflows/preview.yml.
"""

from __future__ import annotations

import argparse
import dataclasses
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from preview_lib import (  # noqa: E402
    Project,
    build_docset,
    load_config,
    publish_docset,
    render_project_index,
    render_root_index,
    resolve_source,
    utcnow,
    validate_project,
    validate_site,
    write_state,
)


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        default=str(repo_root / "projects.yml"),
        help="path to projects.yml (default: %(default)s)",
    )
    parser.add_argument(
        "--site-dir",
        default=None,
        help="output directory (default: the site_dir of projects.yml)",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="only build this project (repeatable)",
    )
    parser.add_argument("--ref", default=None, help="override the source ref")
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="PROJECT=REF",
        help="override the ref of one project (repeatable)",
    )
    parser.add_argument(
        "--override-repository",
        action="append",
        default=[],
        metavar="PROJECT=URL",
        help="override the source repository of one project (repeatable)",
    )
    parser.add_argument(
        "--source-local",
        default=None,
        help="override source.local_path with this local git checkout",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="reuse the HTML that is already in the site directory",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="only validate the site directory",
    )
    return parser.parse_args()


def parse_overrides(values: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"expected PROJECT=VALUE, got '{value}'")
        name, _, setting = value.partition("=")
        overrides[name.strip()] = setting.strip()
    return overrides


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    site_dir = Path(args.site_dir) if args.site_dir else config.source / config.site_dir

    ref_overrides = parse_overrides(args.override)
    repository_overrides = parse_overrides(args.override_repository)

    selected = config.projects
    if args.project:
        wanted = set(args.project)
        unknown = wanted - {project.name for project in config.projects}
        if unknown:
            print(f"unknown project(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        selected = [project for project in config.projects if project.name in wanted]
    elif not args.validate_only:
        selected = [project for project in config.projects if project.enabled]

    if args.validate_only:
        present = [
            project
            for project in config.projects
            if (site_dir / project.name).is_dir()
        ]
        problems = validate_site(site_dir, present)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"{site_dir}: site is valid ({len(present)} project(s))")
        return 0

    site_dir.mkdir(parents=True, exist_ok=True)
    state = {"generated_at": utcnow(), "projects": {}}
    failures: list[str] = []

    for project in selected:
        if args.source_local:
            project = dataclasses.replace(project, local_path=args.source_local)
        if project.name in repository_overrides:
            project = dataclasses.replace(
                project, repository=repository_overrides[project.name], local_path=None
            )
        if args.ref:
            project = dataclasses.replace(project, ref=args.ref)
        if project.name in ref_overrides:
            project = dataclasses.replace(project, ref=ref_overrides[project.name])
        print(f"==> {project.name}: building from {project.ref}")

        source_root = None
        staging = None
        configured_ref = project.ref
        try:
            source_root, sha = resolve_source(project)

            after_dir = site_dir / project.name / "after"
            if args.skip_build:
                if not after_dir.is_dir():
                    raise RuntimeError(f"no existing build at {after_dir}")
                build_log = "(skipped, reusing existing output)"
                published = {"published": [], "advertised": [], "supporting": []}
            else:
                staging = Path(tempfile.mkdtemp(prefix=f"llvm-docs-build-{project.name}-"))
                built = staging / "build"
                for docset in project.docsets:
                    print(f"    {docset.name}: sphinx build")
                    # Every docset gets its own build directory, otherwise the
                    # documents of one docset would overwrite the other.
                    build_docset(docset, source_root, built / docset.name)

                # Publish only the converted documents and what they need.
                target = staging / "site" / project.name
                published = {"published": [], "advertised": [], "supporting": []}
                for docset in project.docsets:
                    print(f"    {docset.name}: publishing")
                    result = publish_docset(
                        docset,
                        built / docset.name,
                        target / "after" / docset.output
                        if docset.output
                        else target / "after",
                    )
                    prefix = f"{docset.output}/" if docset.output else ""
                    published["published"].extend(
                        f"{prefix}{name}" for name in result["published"]
                    )
                    published["advertised"].extend(
                        f"{prefix}{name}" for name in result["advertised"]
                    )
                    published["supporting"].extend(
                        f"{prefix}{name}"
                        for name in result["supporting"]
                        if name.endswith(".html")
                    )
                    for line in result["rewritten_links"]:
                        published.setdefault("rewritten_links", []).append(line)

                problems = validate_project(project, target)
                if problems:
                    raise RuntimeError("\n".join(problems))

                after_dir = site_dir / project.name / "after"
                after_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.rmtree(after_dir, ignore_errors=True)
                shutil.move(str(target / "after"), str(after_dir))
                shutil.rmtree(staging, ignore_errors=True)
                staging = None
                build_log = f"{len(published['published'])} file(s) published"

            state["projects"][project.name] = {
                "ref": configured_ref,
                "branch": project.branch,
                "sha": sha,
                "repository": project.repository,
                "built_at": utcnow(),
                "status": "ok",
                "prs": project.prs,
                "published": sorted(published["published"]),
                "advertised": sorted(published["advertised"]),
                "supporting": sorted(published["supporting"]),
            }
            render_project_index(project, site_dir / project.name, state)
            print(f"    {build_log}")
        except Exception as error:  # noqa: BLE001 - report and continue
            failures.append(f"{project.name}: {error}")
            print(f"    FAILED: {error}", file=sys.stderr)
            state["projects"][project.name] = {
                "ref": configured_ref,
                "branch": project.branch,
                "sha": "",
                "repository": project.repository,
                "built_at": utcnow(),
                "status": "failed",
                "prs": project.prs,
            }
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            if source_root is not None:
                shutil.rmtree(source_root, ignore_errors=True)

    render_root_index(selected, site_dir, state)
    write_state(site_dir, state)

    problems = validate_site(site_dir, selected)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)

    if failures or problems:
        print(f"\n{len(failures)} project(s) failed to build", file=sys.stderr)
        return 1

    print(f"\nsite written to {site_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
