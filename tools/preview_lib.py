"""Helpers for building and validating LLVM documentation previews.

The site layout produced by this module is::

    <site_dir>/
        index.html                  landing page listing every project
        state.json                  build metadata (source refs, shas, date)
        <project>/index.html        landing page for one project
        <project>/after/...         the converted sphinx output

Only the documents that a migration PR actually converted are published,
together with the assets and the supporting pages that they need. Every link
in the generated pages is relative, so the site works unchanged at any base
path (a GitHub Pages project site, a user site, or a later custom domain).
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
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import yaml

__all__ = [
    "Config",
    "Docset",
    "Page",
    "Project",
    "build_docset",
    "load_config",
    "publish_docset",
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
    """One converted document of a migration."""

    source: str  # path of the converted .md file in the repository
    after: str  # path of the rendered page inside the docset output
    official: str = ""  # before URL on the official documentation site
    before_source: str = ""  # before URL of the original source, if any


@dataclasses.dataclass
class Docset:
    """A group of documents that are built together."""

    name: str
    mode: str  # "sphinx" (a real sphinx project) or "isolated" (loose .md)
    path: str  # sphinx source directory, relative to the repository root
    files: list[str]  # loose .md files, relative to the repository root
    python_path: str
    tags: list[str]
    extra_args: list[str]
    output: str  # subdirectory below <project>/after/
    official_base_url: str
    warnings_as_errors: bool
    pages: list[Page]


@dataclasses.dataclass
class Project:
    name: str
    title: str
    group: str
    enabled: bool
    repository: str
    ref: str
    branch: str
    local_path: str | None
    prs: dict[str, int]
    note: str
    docsets: list[Docset]


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
        docsets = []
        for docset in entry.get("docsets", []):
            docsets.append(
                Docset(
                    name=docset.get("name", "docs"),
                    mode=docset.get("mode", "sphinx"),
                    path=docset.get("path", ""),
                    files=list(docset.get("files", [])),
                    python_path=docset.get("python_path", ""),
                    tags=list(docset.get("tags", [])),
                    extra_args=list(docset.get("extra_args", [])),
                    output=docset.get("output", ""),
                    official_base_url=docset.get("official_base_url", ""),
                    warnings_as_errors=bool(
                        docset.get("warnings_as_errors", False)
                    ),
                    pages=[
                        Page(
                            source=page["source"],
                            after=page["after"],
                            official=page.get("official", ""),
                            before_source=page.get("before_source", ""),
                        )
                        for page in docset.get("pages", [])
                    ],
                )
            )
        projects.append(
            Project(
                name=entry["name"],
                title=entry.get("title", entry["name"]),
                group=entry.get("group", entry.get("title", entry["name"])),
                enabled=bool(entry.get("enabled", False)),
                repository=source.get("repository", ""),
                ref=source.get("ref", ""),
                branch=source.get("branch", ""),
                local_path=source.get("local_path"),
                prs=dict(entry.get("prs", {})),
                note=entry.get("note", ""),
                docsets=docsets,
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


# Directories that every LLVM documentation build needs: the shared sphinx
# configuration (utils/docs) loads the MLIR lexer from mlir/utils/pygments.
COMMON_PATHS = ["mlir/utils/pygments"]


def _sparse_paths(project: Project) -> list[str]:
    """Directories that have to be present to build this project."""
    paths = list(COMMON_PATHS)
    for docset in project.docsets:
        if docset.path:
            paths.append(docset.path)
        for name in docset.files:
            paths.append(str(Path(name).parent))
    for docset in project.docsets:
        if docset.python_path:
            paths.append(docset.python_path)
    seen: list[str] = []
    for path in paths:
        if path and path not in seen:
            seen.append(path)
    return seen


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


def _isolated_project(docset: Docset, source_root: Path, target: Path) -> str:
    """Create a small sphinx project for documents that have no sphinx home.

    The converted files are copied into the project below their repository
    path without the top level directory, so that the rendered pages keep a
    short and predictable layout.
    """
    target.mkdir(parents=True, exist_ok=True)
    docnames = []
    for name in docset.files:
        source = source_root / name
        if not source.is_file():
            raise RuntimeError(f"{source} does not exist in the source tree")
        relative = Path(name)
        parts = relative.parts[1:] if len(relative.parts) > 1 else relative.parts
        destination = target.joinpath(*parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        docnames.append(
            str(destination.relative_to(target).with_suffix("")).replace(os.sep, "/")
        )

    master_doc = docnames[0] if docnames else "index"
    (target / "conf.py").write_text(
        "project = 'LLVM documentation preview'\n"
        "extensions = ['myst_parser']\n"
        "master_doc = %r\n" % master_doc
        + "source_suffix = {'.rst': 'restructuredtext', '.md': 'markdown'}\n"
        + "myst_enable_extensions = ['substitution', 'colon_fence']\n"
        + "exclude_patterns = ['_build']\n",
        encoding="utf-8",
    )
    return target


def build_docset(
    docset: Docset,
    source_root: Path,
    output_dir: Path,
    extra_args: list[str] | None = None,
) -> str:
    """Run sphinx-build for one docset.

    Raises RuntimeError when the build fails, so that a broken build never
    replaces a working preview.
    """
    staging = Path(tempfile.mkdtemp(prefix="llvm-docs-sphinx-"))
    try:
        if docset.mode == "isolated":
            docs_dir = _isolated_project(docset, source_root, staging / "project")
        else:
            docs_dir = source_root / docset.path
            if not docs_dir.is_dir():
                raise RuntimeError(f"documentation directory {docs_dir} does not exist")

        env = dict(os.environ)
        python_path = (
            [str(source_root / docset.python_path)] if docset.python_path else []
        )
        if env.get("PYTHONPATH"):
            python_path.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(python_path)

        doctrees = staging / "doctrees"
        args = ["-b", "html"]
        if docset.warnings_as_errors:
            args.append("-W")
        for tag in docset.tags:
            args += ["-t", tag]
        args += ["-d", str(doctrees)]
        args += list(docset.extra_args)
        args += list(extra_args or [])
        args += [str(docs_dir), str(output_dir)]

        result = _run(
            [sys.executable, "-m", "sphinx", *args], cwd=str(docs_dir), env=env
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"sphinx build failed for {docset.name} "
                f"(exit {result.returncode}):\n{result.stdout.strip()}"
            )
        return result.stdout
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------------------
# publishing: keep the converted pages and what they need
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
        or lowered.startswith(
            ("http://", "https://", "mailto:", "data:", "javascript:")
        )
    )


def _split_link(link: str) -> tuple[str, str]:
    """Split a link into its path and its fragment/query part."""
    path, _, rest = link.partition("#")
    path, _, query = path.partition("?")
    return path, ("#" + rest if rest else "") + ("?" + query if query else "")


def _resolve(build_dir: Path, page: Path, link: str) -> Path | None:
    """Resolve a local link of a built page to a path inside the build."""
    clean, _ = _split_link(link)
    if not clean:
        return None
    target = (page.parent / clean).resolve()
    try:
        target.relative_to(build_dir)
    except ValueError:
        return None
    return target


def publish_docset(
    docset: Docset,
    build_dir: Path,
    target_dir: Path,
) -> dict:
    """Copy the converted pages and their dependencies to the site.

    Only the pages of the migration, the assets they need and the supporting
    pages without a published equivalent are copied. Links to other pages of
    the same project are rewritten to the official documentation site, so the
    preview stays small and no link breaks.
    """
    published: list[str] = []
    rewritten: list[str] = []

    def relative(path: Path) -> str:
        return path.relative_to(build_dir).as_posix()

    advertised = {page.after for page in docset.pages}
    for name in sorted(advertised):
        if not (build_dir / name).is_file():
            raise RuntimeError(f"{docset.name}: expected page {name} was not built")

    keep = set(advertised)
    # Auxiliary pages that the theme links to from every page.
    for name in ("genindex.html", "search.html"):
        if (build_dir / name).is_file():
            keep.add(name)

    # Assets: the theme files and everything the published pages reference.
    for path in build_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = relative(path)
        if rel.startswith("_static/") or rel.startswith("_images/"):
            keep.add(rel)

    queue = sorted(keep)
    while queue:
        rel = queue.pop()
        if rel not in keep:
            continue
        source = build_dir / rel
        if not source.is_file():
            continue
        if rel.endswith(".html"):
            for link in _collect_links(source):
                if _is_external(link):
                    continue
                resolved = _resolve(build_dir, source, link)
                if resolved is None:
                    continue
                if not resolved.is_file():
                    continue
                target_rel = relative(resolved)
                if target_rel in keep:
                    continue
                if resolved.suffix == ".html":
                    if docset.official_base_url:
                        # The document lives on the official site, point there.
                        continue
                    # No published equivalent: the page is needed for
                    # navigation, so publish it as a supporting page.
                    keep.add(target_rel)
                    queue.append(target_rel)
                else:
                    keep.add(target_rel)

    for rel in sorted(keep):
        source = build_dir / rel
        if not source.is_file():
            continue
        destination = target_dir / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        published.append(rel)

    # Rewrite links to pages that are not part of the preview.
    for rel in sorted(keep):
        if not rel.endswith(".html"):
            continue
        path = target_dir / rel
        text = path.read_text(encoding="utf-8", errors="replace")
        original = text

        def replace(match: re.Match) -> str:
            attribute = match.group(1)
            double_quoted = match.group(3)
            quote = '"' if double_quoted is not None else "'"
            link = double_quoted if double_quoted is not None else match.group(4)
            if _is_external(link):
                return match.group(0)
            resolved = _resolve(build_dir, build_dir / rel, link)
            if resolved is None or not resolved.is_file():
                return match.group(0)
            target_rel = relative(resolved)
            if target_rel in keep:
                return match.group(0)
            if resolved.suffix != ".html" or not docset.official_base_url:
                return match.group(0)
            _, suffix = _split_link(link)
            new_link = (
                docset.official_base_url.rstrip("/") + "/" + target_rel + suffix
            )
            rewritten.append(f"{rel}: {link} -> {new_link}")
            return f"{attribute}={quote}{new_link}{quote}"

        text = re.sub(
            r"""\b(href|src)=("([^"]*)"|'([^']*)')""",
            replace,
            text,
        )
        if text != original:
            path.write_text(text, encoding="utf-8")

    return {
        "published": published,
        "advertised": sorted(advertised),
        "supporting": sorted(keep - advertised),
        "rewritten_links": rewritten,
    }


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def validate_project(project: Project, project_dir: Path) -> list[str]:
    """Check that a published project is complete and internally consistent."""
    problems: list[str] = []

    for docset in project.docsets:
        output = project_dir / docset.output if docset.output else project_dir
        after = project_dir / "after"
        base = after / docset.output if docset.output else after
        if not base.is_dir():
            problems.append(f"{project.name}/{docset.name}: nothing was published")
            continue

        for page in docset.pages:
            if not (base / page.after).is_file():
                problems.append(
                    f"{project.name}/{docset.name}: advertised page "
                    f"{page.after} is missing"
                )

        static = base / "_static"
        if not static.is_dir():
            problems.append(f"{project.name}/{docset.name}: missing _static directory")
        else:
            for asset in ("basic.css", "pygments.css"):
                if not (static / asset).is_file():
                    problems.append(
                        f"{project.name}/{docset.name}: missing _static/{asset}"
                    )

        # Every local link of a published page must resolve.
        for html_file in sorted(base.rglob("*.html")):
            for link in _collect_links(html_file):
                if _is_external(link):
                    continue
                clean, _ = _split_link(link)
                if not clean:
                    continue
                if not (html_file.parent / clean).resolve().exists():
                    problems.append(
                        f"{project.name}/{docset.name}: "
                        f"{html_file.relative_to(base)} links to missing {clean}"
                    )
        del output

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
        links = _collect_links(project_index)
        for docset in project.docsets:
            for page in docset.pages:
                relative = f"after/{docset.output + '/' if docset.output else ''}{page.after}"
                if relative not in links:
                    problems.append(
                        f"{project.name}: landing page does not link to {relative}"
                    )
                if not (site_dir / project.name / relative).is_file():
                    problems.append(
                        f"{project.name}: landing page links to missing {relative}"
                    )

    for html_file in sorted(site_dir.rglob("*.html")):
        for link in _collect_links(html_file):
            if _is_external(link):
                continue
            clean, _ = _split_link(link)
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
       margin: 2.5rem auto; max-width: 68rem; padding: 0 1rem; color: #1b1f23; }
h1 { font-size: 1.6rem; margin-bottom: .3rem; }
p.lead { color: #57606a; margin-top: 0; }
table { border-collapse: collapse; width: 100%; margin: 1.2rem 0; }
th, td { border-bottom: 1px solid #d8dee4; padding: .45rem .7rem; text-align: left;
         vertical-align: top; }
th { background: #f6f8fa; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       font-size: .9em; background: #f6f8fa; padding: .1rem .3rem; border-radius: 3px; }
a { color: #0969da; }
.note { background: #fff8c5; border-left: 4px solid #d4a72c; padding: .6rem .9rem;
        margin: 1rem 0; }
footer { margin-top: 2.5rem; color: #57606a; font-size: .9rem; }
"""


def _pr_links(project: Project) -> str:
    parts = []
    for kind, number in project.prs.items():
        label = f"{kind} PR #{number}"
        url = f"https://github.com/llvm/llvm-project/pull/{number}"
        parts.append(f'<a href="{url}">{label}</a>')
    return ", ".join(parts)


def render_project_index(project: Project, project_dir: Path, state: dict) -> Path:
    """Write site/<project>/index.html with the before/after table."""
    entry = state.get("projects", {}).get(project.name, {})
    rows = []
    for docset in project.docsets:
        prefix = f"after/{docset.output}/" if docset.output else "after/"
        for page in docset.pages:
            after = f"{prefix}{page.after}"
            if page.official:
                before = f'<a href="{html.escape(page.official, quote=True)}">official</a>'
            elif page.before_source:
                before = (
                    f'<a href="{html.escape(page.before_source, quote=True)}">'
                    "source</a><br><small>no published page</small>"
                )
            else:
                before = "<small>no published page</small>"
            rows.append(
                "<tr>"
                f"<td><code>{html.escape(page.source)}</code></td>"
                f"<td>{before}</td>"
                f'<td><a href="{html.escape(after, quote=True)}">preview</a></td>'
                "</tr>"
            )

    if rows:
        table = (
            "<table>\n<thead><tr><th>Markdown source</th><th>Before</th>"
            "<th>After</th></tr></thead>\n<tbody>\n"
            + "\n".join(rows)
            + "\n</tbody>\n</table>"
        )
    else:
        table = "<p>No converted documents are configured for this project yet.</p>"

    prs = _pr_links(project)
    pr_line = f"<p><strong>Pull requests:</strong> {prs}</p>" if prs else ""
    revision = entry.get("sha", "")
    revision_line = (
        f"<p><strong>Source revision:</strong> "
        f"<code>{html.escape(revision)}</code>"
        + (
            f" (branch <code>{html.escape(entry.get('branch', ''))}</code>)"
            if entry.get("branch")
            else ""
        )
        + "</p>"
        if revision
        else ""
    )

    notes = []
    if project.note:
        notes.append(f'<div class="note">{html.escape(project.note)}</div>')
    for docset in project.docsets:
        if docset.official_base_url:
            continue
        has_source = any(page.before_source for page in docset.pages)
        if has_source:
            notes.append(
                f"<div class=\"note\"><strong>{html.escape(docset.name)}</strong>: "
                "these documents have no published counterpart on the official "
                "documentation site; the before column points at the original "
                "source in the repository.</div>"
            )
        else:
            notes.append(
                f"<div class=\"note\"><strong>{html.escape(docset.name)}</strong>: "
                "these documents are test fixtures and have no published "
                "counterpart; they are rendered with the configuration of the "
                "test itself.</div>"
            )

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
<p class="lead">The converted Markdown of the migration, rendered with Sphinx and
MyST. The <em>Before</em> column links to the original page on the official
documentation site, the <em>After</em> column links to the rendered preview.</p>
{pr_line}
{revision_line}
{"".join(notes)}
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
            f"<td>{html.escape(project.group)}</td>"
            f"<td><code>{html.escape(entry.get('ref', ''))}</code></td>"
            f"<td><code>{html.escape(entry.get('sha', '')[:12])}</code></td>"
            f"<td>{html.escape(entry.get('built_at', ''))}</td>"
            "</tr>"
        )

    table = (
        "<table>\n<thead><tr><th>Project</th><th>Area</th><th>Source ref</th>"
        "<th>Commit</th><th>Built</th></tr></thead>\n<tbody>\n"
        + "\n".join(rows)
        + "\n</tbody>\n</table>"
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
