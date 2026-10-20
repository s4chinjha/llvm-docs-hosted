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
    build_project,
    load_config,
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
        print(f"==> {project.name}: building {project.docs_path} from {project.ref}")

        source_root = None
        staging = None
        configured_ref = project.ref
        try:
            source_root, sha = resolve_source(project)

            if args.skip_build:
                target = site_dir / project.name / project.output_subdir
                if not target.is_dir():
                    raise RuntimeError(f"no existing build at {target}")
                build_log = "(skipped, reusing existing output)"
            else:
                staging = Path(tempfile.mkdtemp(prefix=f"llvm-docs-build-{project.name}-"))
                output = staging / project.output_subdir
                log = build_project(project, source_root, output)
                build_log = log.strip().splitlines()[-1] if log.strip() else "ok"

                problems = validate_project(project, output)
                if problems:
                    raise RuntimeError("\n".join(problems))

                target = site_dir / project.name / project.output_subdir
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.rmtree(target, ignore_errors=True)
                shutil.move(str(output), str(target))
                shutil.rmtree(staging, ignore_errors=True)
                staging = None

            render_project_index(project, site_dir / project.name)
            state["projects"][project.name] = {
                "ref": configured_ref,
                "sha": sha,
                "repository": project.repository,
                "built_at": utcnow(),
                "status": "ok",
            }
            print(f"    {build_log}")
        except Exception as error:  # noqa: BLE001 - report and continue
            failures.append(f"{project.name}: {error}")
            print(f"    FAILED: {error}", file=sys.stderr)
            state["projects"][project.name] = {
                "ref": configured_ref,
                "sha": "",
                "repository": project.repository,
                "built_at": utcnow(),
                "status": "failed",
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
