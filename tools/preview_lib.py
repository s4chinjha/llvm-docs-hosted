"""Helpers for building and validating LLVM documentation previews.

The site layout produced by this module is::

    <site_dir>/
        index.html                  landing page listing every project
        state.json                  build metadata (source refs, shas, date)
        <project>/index.html        landing page for one project
        <project>/after/...         the converted sphinx output

Every link in the generated pages is relative, so the site works unchanged at
any base path (a GitHub Pages project site, a user site, or a custom domain).
"""

from __future__ import annotations

import dataclasses
import datetime
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import yaml

__all__ = [
    "Config",
    "Project",
    "build_project",
    "load_config",
    "render_project_index",
    "render_root_index",
    "resolve_source",
    "validate_project",
    "validate_site",
]


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Page:
    source: str
    official: str
    after: str


@dataclasses.dataclass
class Project:
    name: str
    title: str
    enabled: bool
    repository: str
    ref: str
    local_path: str | None
    docs_path: str
    python_path: str
    extra_paths: list[str]
    extra_args: list[str]
    warnings_as_errors: bool
    output_subdir: str
    official_base_url: str
    pages: list[Page]


@dataclasses.dataclass
class Config:
    site_dir: str
    projects: list[Project]
    source: Path


def load_config(path: str | os.PathLike) -> Config:
    """Read projects.yml."""
    config_path = Path(path).resolve()
    with config_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    projects = []
    for entry in raw.get("projects", []):
        source = entry.get("source", {})
        docs = entry.get("docs", {})
        build = entry.get("build", {})
        projects.append(
            Project(
                name=entry["name"],
                title=entry.get("title", entry["name"]),
                enabled=bool(entry.get("enabled", False)),
                repository=source.get("repository", ""),
                ref=source.get("ref", ""),
                local_path=source.get("local_path"),
                docs_path=docs.get("path", ""),
                python_path=docs.get("python_path", ""),
                extra_paths=list(source.get("extra_paths", [])),
                extra_args=list(docs.get("extra_args", [])),
                warnings_as_errors=bool(build.get("warnings_as_errors", False)),
                output_subdir=build.get("output_subdir", "after"),
                official_base_url=entry.get("official_base_url", ""),
                pages=[
                    Page(
                        source=page["source"],
                        official=page["official"],
                        after=page["after"],
                    )
                    for page in entry.get("pages", [])
                ],
            )
        )
    return Config(
        site_dir=raw.get("site_dir", "site"),
        projects=projects,
        source=config_path.parent,
    )


# --------------------------------------------------------------------------
# source acquisition
# --------------------------------------------------------------------------


def _run(
    args: list[str],
    cwd: str | os.PathLike | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _github_archive_url(repository: str, ref: str) -> list[str]:
    """Candidate archive URLs for a ref, most specific first."""
    owner_repo = repository.rstrip("/").removeprefix("https://github.com/")
    return [
        f"https://github.com/{owner_repo}/archive/refs/heads/{ref}.tar.gz",
        f"https://github.com/{owner_repo}/archive/{ref}.tar.gz",
    ]


# Directories that every LLVM documentation build needs: the shared sphinx
# configuration (utils/docs) loads the MLIR lexer from mlir/utils/pygments.
COMMON_PATHS = ["mlir/utils/pygments"]


def _sparse_paths(project: Project) -> list[str]:
    """Directories that have to be present to build this project."""
    paths = list(COMMON_PATHS)
    paths.append(project.docs_path)
    if project.python_path:
        paths.append(project.python_path)
    paths.extend(project.extra_paths)
    return [path for path in paths if path]


def resolve_source(project: Project, ref: str | None = None) -> tuple[Path, str]:
    """Materialise the documentation sources with a sparse checkout.

    Only the directories named by the configuration are checked out, so the
    llvm-project monorepo does not have to be downloaded in full. Nothing is
    written into the source repository.
    """
    ref = ref or project.ref
    workdir = Path(tempfile.mkdtemp(prefix=f"llvm-docs-src-{project.name}-"))

    remote = project.repository
    if project.local_path:
        local = Path(project.local_path)
        if (local / ".git").exists():
            remote = f"file://{local.resolve()}"
        else:
            print(
                f"warning: local_path {local} is not a git checkout, "
                f"using {project.repository or 'no repository'} instead",
                file=sys.stderr,
            )

    if not remote:
        shutil.rmtree(workdir, ignore_errors=True)
        raise RuntimeError(f"project {project.name} has no source repository")

    checkout = workdir
    checkout.mkdir(parents=True, exist_ok=True)

    steps = [
        ["git", "init", "--quiet"],
        ["git", "remote", "add", "origin", remote],
        [
            "git",
            "fetch",
            "--quiet",
            "--depth",
            "1",
            "--filter=blob:none",
            "origin",
            ref,
        ],
    ]
    for command in steps:
        result = _run(command, cwd=str(checkout))
        if result.returncode != 0:
            shutil.rmtree(workdir, ignore_errors=True)
            raise RuntimeError(
                f"could not fetch '{ref}' from {remote}:\n{result.stdout.strip()}"
            )

    paths = _sparse_paths(project)
    if paths:
        # Cone mode keeps whole directories, which is what the configuration
        # describes. Fall back to pattern mode for file paths.
        result = _run(["git", "sparse-checkout", "set", *paths], cwd=str(checkout))
        if result.returncode != 0:
            patterns = [f"{path}/*" for path in paths]
            result = _run(
                ["git", "sparse-checkout", "set", "--no-cone", *patterns],
                cwd=str(checkout),
            )
        if result.returncode != 0:
            shutil.rmtree(workdir, ignore_errors=True)
            raise RuntimeError(
                f"could not apply sparse checkout {paths}:\n{result.stdout.strip()}"
            )

    result = _run(["git", "checkout", "--quiet", "FETCH_HEAD"], cwd=str(checkout))
    if result.returncode != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        raise RuntimeError(f"checkout failed:\n{result.stdout.strip()}")

    sha = _run(["git", "rev-parse", "FETCH_HEAD"], cwd=str(checkout)).stdout.strip()
    return workdir, sha


# --------------------------------------------------------------------------
# sphinx build
# --------------------------------------------------------------------------


def build_project(
    project: Project,
    source_root: Path,
    output_dir: Path,
    extra_args: list[str] | None = None,
) -> str:
    """Run sphinx-build for one project.

    Raises RuntimeError when the build fails, so that a broken build never
    replaces a working preview.
    """
    docs_dir = source_root / project.docs_path
    if not docs_dir.is_dir():
        raise RuntimeError(f"documentation directory {docs_dir} does not exist")

    env = dict(os.environ)
    python_path = [str(source_root / project.python_path)] if project.python_path else []
    if env.get("PYTHONPATH"):
        python_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path)

    doctrees = Path(tempfile.mkdtemp(prefix="llvm-docs-doctrees-"))
    args = ["-b", "html"]
    if project.warnings_as_errors:
        args.append("-W")
    args += ["-d", str(doctrees)]
    args += list(project.extra_args)
    args += list(extra_args or [])
    args += [str(docs_dir), str(output_dir)]

    result = _run([sys.executable, "-m", "sphinx", *args], cwd=str(source_root), env=env)
    shutil.rmtree(doctrees, ignore_errors=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"sphinx build failed for {project.name} (exit {result.returncode}):\n"
            f"{result.stdout.strip()}"
        )
    return result.stdout


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in ("href", "src") and value:
                self.links.append(value)


def _collect_links(path: Path) -> list[str]:
    parser = _LinkCollector()
    try:
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    return parser.links


def _is_external(link: str) -> bool:
    lowered = link.lower()
    return (
        not link
        or link.startswith("#")
        or lowered.startswith(("http://", "https://", "mailto:", "data:", "javascript:"))
    )


def validate_project(project: Project, output_dir: Path) -> list[str]:
    """Check that a built project is complete and internally consistent."""
    problems: list[str] = []

    if not output_dir.is_dir():
        return [f"{project.name}: output directory {output_dir} does not exist"]

    static = output_dir / "_static"
    if not static.is_dir():
        problems.append(f"{project.name}: missing _static directory")
    else:
        for asset in ("basic.css", "pygments.css"):
            if not (static / asset).is_file():
                problems.append(f"{project.name}: missing _static/{asset}")

    for page in project.pages:
        target = output_dir / page.after
        if not target.is_file():
            problems.append(f"{project.name}: expected page {page.after} was not built")

    # Every relative link and asset referenced by a generated page must exist.
    missing = 0
    for html_file in sorted(output_dir.rglob("*.html")):
        for link in _collect_links(html_file):
            if _is_external(link):
                continue
            clean = link.split("#", 1)[0].split("?", 1)[0]
            if not clean:
                continue
            target = (html_file.parent / clean).resolve()
            if not target.exists():
                problems.append(
                    f"{project.name}: {html_file.relative_to(output_dir)} links to "
                    f"missing {clean}"
                )
                missing += 1
                if missing >= 20:
                    problems.append(f"{project.name}: further link problems suppressed")
                    break
        if missing >= 20:
            break

    return problems


def validate_site(site_dir: Path, projects: list[Project]) -> list[str]:
    """Check the assembled site: landing pages, links and assets."""
    problems: list[str] = []
    index = site_dir / "index.html"
    if not index.is_file():
        return [f"missing landing page {index}"]

    for project in projects:
        project_index = site_dir / project.name / "index.html"
        if not project_index.is_file():
            problems.append(f"missing project landing page {project_index}")
            continue
        for page in project.pages:
            relative = f"{project.output_subdir}/{page.after}"
            if relative not in _collect_links(project_index):
                problems.append(
                    f"{project.name}: landing page does not link to {relative}"
                )
            if not (site_dir / project.name / project.output_subdir / page.after).is_file():
                problems.append(
                    f"{project.name}: landing page links to missing page {page.after}"
                )

    for html_file in sorted(site_dir.rglob("*.html")):
        for link in _collect_links(html_file):
            if _is_external(link):
                continue
            clean = link.split("#", 1)[0].split("?", 1)[0]
            if not clean:
                continue
            if not (html_file.parent / clean).resolve().exists():
                problems.append(
                    f"{html_file.relative_to(site_dir)} links to missing {clean}"
                )
    return problems


# --------------------------------------------------------------------------
# landing pages
# --------------------------------------------------------------------------

_STYLE = """
body { font: 16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
       margin: 2.5rem auto; max-width: 62rem; padding: 0 1rem; color: #1b1f23; }
h1 { font-size: 1.6rem; margin-bottom: .3rem; }
p.lead { color: #57606a; margin-top: 0; }
table { border-collapse: collapse; width: 100%; margin: 1.2rem 0; }
th, td { border-bottom: 1px solid #d8dee4; padding: .45rem .7rem; text-align: left;
         vertical-align: top; }
th { background: #f6f8fa; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       font-size: .9em; background: #f6f8fa; padding: .1rem .3rem; border-radius: 3px; }
a { color: #0969da; }
footer { margin-top: 2.5rem; color: #57606a; font-size: .9rem; }
"""


def render_project_index(project: Project, project_dir: Path) -> Path:
    """Write site/<project>/index.html with the before/after table."""
    rows = []
    for page in project.pages:
        after = f"{project.output_subdir}/{page.after}"
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(page.source)}</code></td>"
            f'<td><a href="{html.escape(page.official, quote=True)}">official</a></td>'
            f'<td><a href="{html.escape(after, quote=True)}">preview</a></td>'
            "</tr>"
        )

    if rows:
        table = (
            "<table>\n<thead><tr><th>Source file</th><th>Before HTML</th>"
            "<th>After HTML</th></tr></thead>\n<tbody>\n"
            + "\n".join(rows)
            + "\n</tbody>\n</table>"
        )
    else:
        table = "<p>No preview pages are configured for this project yet.</p>"

    document = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(project.title)} documentation preview</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>{html.escape(project.title)} documentation preview</h1>
<p class="lead">Converted documentation built from the LLVM documentation sources.
The <em>Before</em> column links to the same page on the official documentation
site, the <em>After</em> column links to the converted page hosted here.</p>
{table}
<footer>Generated by the preview workflow of the LLVM documentation preview site.</footer>
</body>
</html>
"""
    target = project_dir / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    return target


def render_root_index(projects: list[Project], site_dir: Path, state: dict) -> Path:
    """Write site/index.html listing every project with a preview."""
    rows = []
    for project in projects:
        entry = state.get("projects", {}).get(project.name, {})
        rows.append(
            "<tr>"
            f'<td><a href="{html.escape(project.name, quote=True)}/index.html">'
            f"{html.escape(project.title)}</a></td>"
            f"<td><code>{html.escape(entry.get('ref', ''))}</code></td>"
            f"<td><code>{html.escape(entry.get('sha', '')[:12])}</code></td>"
            f"<td>{html.escape(entry.get('built_at', ''))}</td>"
            "</tr>"
        )

    table = (
        "<table>\n<thead><tr><th>Project</th><th>Source ref</th><th>Commit</th>"
        "<th>Built</th></tr></thead>\n<tbody>\n" + "\n".join(rows) + "\n</tbody>\n</table>"
    )

    document = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLVM documentation preview</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>LLVM documentation preview</h1>
<p class="lead">Previews of documentation converted from reStructuredText to
MyST Markdown. The original pages stay on the official documentation sites.</p>
{table}
<footer>Generated by the preview workflow of the LLVM documentation preview site.</footer>
</body>
</html>
"""
    target = site_dir / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    return target


def write_state(site_dir: Path, state: dict) -> Path:
    target = site_dir / "state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
