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
import urllib.error
import urllib.request
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


def verify_official_url(url: str, timeout: int = 30) -> str:
    """Check that an official documentation page exists.

    Returns "ok" when the page is available, "missing" when the server says
    it does not exist, and "unknown" when the page could not be checked.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": "llvm-docs-preview"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return "ok" if 200 <= response.status < 400 else "missing"
    except urllib.error.HTTPError as error:
        return "missing" if error.code in (400, 401, 403, 404, 410) else "unknown"
    except (urllib.error.URLError, OSError, TimeoutError):
        return "unknown"


def eligible_rows(
    docset: Docset,
    build_dir: Path,
    check_urls: bool = True,
) -> list[dict]:
    """The documents that can be shown as a before/after pair.

    A document qualifies only when its original version has a published HTML
    page on the official documentation site and its converted version has
    actually been generated.
    """
    rows: list[dict] = []
    for page in docset.pages:
        if not page.official:
            # No published counterpart: READMEs, fixtures and templates are
            # not part of the comparison.
            continue
        if not (build_dir / page.after).is_file():
            continue
        if check_urls:
            status = verify_official_url(page.official)
            if status == "missing":
                print(
                    f"warning: {page.official} does not exist, "
                    f"skipping {page.source}",
                    file=sys.stderr,
                )
                continue
            if status == "unknown":
                print(
                    f"warning: could not check {page.official}, "
                    f"keeping {page.source}",
                    file=sys.stderr,
                )
        path = (
            f"{docset.path}/{page.after}"
            if docset.mode == "sphinx" and docset.path
            else page.after
        )
        rows.append(
            {
                "path": path,
                "official": page.official,
                "after": page.after,
                "output": docset.output,
            }
        )
    return rows


def validate_site(site_dir: Path, projects: list[Project], state: dict) -> list[str]:
    """Check the assembled site: landing pages, links and assets."""
    problems: list[str] = []
    index = site_dir / "index.html"
    if not index.is_file():
        return [f"missing landing page {index}"]

    for project in projects:
        rows = state.get("projects", {}).get(project.name, {}).get("rows", [])
        project_index = site_dir / project.name / "index.html"
        if not rows:
            # Without a before/after pair there is nothing to compare, so the
            # project has no section on the site.
            if project_index.is_file():
                problems.append(
                    f"{project.name}: landing page exists but the project has "
                    "no before/after pairs"
                )
            continue
        if not project_index.is_file():
            problems.append(f"missing project landing page {project_index}")
            continue
        links = _collect_links(project_index)
        for row in rows:
            relative = f"after/{row['output'] + '/' if row['output'] else ''}{row['after']}"
            if relative not in links:
                problems.append(
                    f"{project.name}: landing page does not link to {relative}"
                )
            if not (site_dir / project.name / relative).is_file():
                problems.append(
                    f"{project.name}: landing page links to missing {relative}"
                )
        for row in rows:
            if row["official"] not in links:
                problems.append(
                    f"{project.name}: landing page does not link to "
                    f"{row['official']}"
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
h2 { font-size: 1.2rem; margin: 2rem 0 .3rem; }
table { border-collapse: collapse; width: 100%; margin: 1.2rem 0; }
th, td { border-bottom: 1px solid #d8dee4; padding: .45rem .7rem; text-align: left;
         vertical-align: top; }
th { background: #f6f8fa; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       font-size: .9em; background: #f6f8fa; padding: .1rem .3rem; border-radius: 3px; }
a { color: #0969da; }
"""


def _comparison_table(rows: list[dict], after_prefix: str) -> str:
    """A minimal Path / Before / After table, like the rnk preview site."""
    body = []
    for row in rows:
        after = f"{after_prefix}{row['output'] + '/' if row['output'] else ''}{row['after']}"
        body.append(
            "<tr>"
            f"<td><code>{html.escape(row['path'])}</code></td>"
            f'<td><a href="{html.escape(row["official"], quote=True)}">before</a></td>'
            f'<td><a href="{html.escape(after, quote=True)}">after</a></td>'
            "</tr>"
        )
    return (
        "<table>\n<thead><tr><th>Path</th><th>Before</th><th>After</th></tr>"
        "</thead>\n<tbody>\n" + "\n".join(body) + "\n</tbody>\n</table>"
    )


def render_project_index(project: Project, project_dir: Path, state: dict) -> Path | None:
    """Write site/<project>/index.html with the before/after table.

    Returns None when the project has no before/after pair; then no landing
    page is written and a stale one is removed.
    """
    rows = state.get("projects", {}).get(project.name, {}).get("rows", [])
    target = project_dir / "index.html"
    if not rows:
        if target.is_file():
            target.unlink()
        return None

    document = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(project.title)}</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>{html.escape(project.title)}</h1>
{_comparison_table(rows, "after/")}
</body>
</html>
"""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    return target


def render_root_index(projects: list[Project], site_dir: Path, state: dict) -> Path:
    """Write site/index.html with one before/after table per project."""
    sections = []
    for project in projects:
        rows = state.get("projects", {}).get(project.name, {}).get("rows", [])
        if not rows:
            continue
        sections.append(
            f'<h2><a href="{html.escape(project.name, quote=True)}/index.html">'
            f"{html.escape(project.title)}</a></h2>\n"
            + _comparison_table(rows, f"{project.name}/after/")
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
{"\n".join(sections)}
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
