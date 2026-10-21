# Preview hosting for LLVM documentation migrations

Research note, written while building the preview site in this repository.

Scope: how do `rnk` (Reid Kleckner) and `zeyi2` (Zeyi Xu) host the rendered
"after" HTML for their `.rst` -> MyST Markdown migration work, and what of that
is reproducible for an ordinary contributor.

All findings below come from public sources: GitHub API (repositories, pull
requests, PR comments, workflow file contents) and plain HTTP requests to the
public preview sites. No private infrastructure was accessed.

---

## 1. rnk (Reid Kleckner)

### Verified

| Question | Answer | Evidence |
|---|---|---|
| Where is the HTML hosted? | A self-hosted nginx server at `llvmdocs.staging.reidkleckner.dev` | `curl -I` returns `server: nginx/1.27.5`; DNS resolves `staging.reidkleckner.dev` to `108.84.44.62` |
| Is it GitHub Pages? | No | Pages serves `Server: GitHub.com` and no `cname`; rnk's response header shows a plain nginx server |
| URL structure | Domain root with two trees: `/before/...` and `/after/...` | `https://llvmdocs.staging.reidkleckner.dev/` links to `/before/libcxx/docs/Status/Cxx17.html` and `/after/libcxx/docs/Status/Cxx17.html` |
| What does the site contain? | The whole LLVM monorepo docs tree, rebuilt with the converted sources | the landing page lists pages from `libc/docs/`, `libcxx/docs/`, `llvm/docs/AMDGPU*` |
| Landing page | A hand-written static table: `Path | Before | After`, 32 rows | `curl https://llvmdocs.staging.reidkleckner.dev/` |
| Is there per-commit history? | No visible history tree (`/runs/` returns 404) | `curl -I https://llvmdocs.staging.reidkleckner.dev/runs/` -> 404 |
| Extra tooling | Pixel/screenshot diffing between the two builds | landing page says "Screenshot pixel diffing is temporarily disabled" |
| How are previews linked from PRs? | Directly by URL in the PR body | PR #222065 body links `https://llvmdocs.staging.reidkleckner.dev/after/libcxx/docs/...` with a before/after table |

### Unknown

* How the site is built and deployed (locally, by CI, or by a private repo).
  No public repository was found under `github.com/rnk` that hosts this content
  (`gh api users/rnk/repos` lists 40 public repos; none is a docs staging repo).
* Whether the domain `reidkleckner.dev` is delegated to an LLVM-operated
  staging service or is self-hosted. The `llvmdocs.staging.` prefix strongly
  suggests a per-contributor staging service, but that could not be confirmed
  from public evidence.
* Whether the before/after trees are produced by one build each or patched
  incrementally.

### Reproducibility for an ordinary contributor

Low. The scheme needs a server, a domain, and (apparently) access to an LLVM
staging service. This repository therefore does **not** try to reproduce it.

---

## 2. zeyi2 (Zeyi Xu)

### Verified

| Question | Answer | Evidence |
|---|---|---|
| Where is the HTML hosted? | GitHub Pages, custom domain `broken.life` | `gh api repos/zeyi2/llvm-staging/pages` -> `"html_url": "http://broken.life/llvm-staging/"`, `"cname": "broken.life"`, `"build_type": "workflow"`, `"source": {"branch": "main", "path": "/"}` |
| Which repository? | `github.com/zeyi2/llvm-staging` (public, not a fork) | `gh api repos/zeyi2/llvm-staging` -> `"has_pages": true`, `"homepage": "broken.life/llvm-staging"` |
| Repository layout | One directory per migration, each containing the built Sphinx output plus its own `index.html` | `gh api repos/zeyi2/llvm-staging/git/trees/main?recursive=1` shows `modernize-markdown-port/docs/...`, `abseil-markdown-port/docs/...`, `bugprone-markdown-port/...`, `readability-markdown-port/...`, `small-modules-markdown-port/...`, `riscv/`, `runs/`, `site/` |
| URL structure | `https://broken.life/llvm-staging/<migration-dir>/index.html` | PR #232207 body links exactly that URL |
| How is it published? | `.github/workflows/pages.yaml`: on push to `main` (paths `runs/**`, `site/**`, `*-markdown-port/**`, `riscv/**`, workflow file) and on `workflow_dispatch`; it runs `python3 site/build.py --results . --out _site --ctit <clone>`, then `actions/configure-pages`, `actions/upload-pages-artifact`, `actions/deploy-pages` | workflow file fetched from `raw.githubusercontent.com/zeyi2/llvm-staging/main/.github/workflows/pages.yaml` |
| Are the built pages committed? | Yes. The workflow only assembles and deploys what is already in the repo | tree listing shows `.html` and `_static/` blobs under each `*-markdown-port/` directory |
| Action pinning | All third-party actions pinned to commit SHAs | workflow file (`actions/checkout@3d3c42e5...`, `actions/setup-python@5fda3b95...`, `actions/configure-pages@45bfe019...`, `actions/upload-pages-artifact@fc324d35...`, `actions/deploy-pages@368f8252...`) |
| Permissions | `contents: read` for the build job; `pages: write` + `id-token: write` only for the deploy job | workflow file |

### Unknown

* How the Sphinx build itself is run. `site/build.py` is a site assembler for
  CTIT (clang-tidy test results) and copies the already-built `*-markdown-port`
  directories into the output; the actual `sphinx-build` invocation for those
  directories is not in this repository.
* Whether the previews are refreshed by a push to `llvm-staging` after every
  PR update, or manually.

### Reproducibility for an ordinary contributor

High. This is plain GitHub Pages with the standard
`upload-pages-artifact` / `deploy-pages` actions, no custom domain required,
no paid service, and no secret beyond the repository's own `GITHUB_TOKEN`.

---

## 3. Comparison

| | rnk | zeyi2 |
|---|---|---|
| Host | self-hosted nginx | GitHub Pages (custom domain) |
| Public build script | no | yes (`site/build.py`, workflow file) |
| Committed build output | unknown | yes |
| URL shape | `https://<host>/before/...`, `https://<host>/after/...` | `https://<domain>/<repo>/<migration>/...` |
| Extra infra needed | server + domain + staging service | none beyond a GitHub account |

---

## 4. Presentation compared with this site

| | rnk | zeyi2 | this site |
|---|---|---|---|
| Landing page | one table with `Path`, `Before`, `After` for every changed page | a directory per migration, no shared before/after table | one table per project with the Markdown source, the before link, the after link, the pull requests and the built commit |
| Before pages | copied into `/before/…` on the same host | not published | linked to the official documentation site, or to the original source in the repository when no page is published |
| After pages | the whole rebuilt documentation tree under `/after/…` | the committed Sphinx output of the migration | only the converted documents, their assets and the pages they need |
| URL of a page | `https://llvmdocs.staging.reidkleckner.dev/after/<path>` | `https://broken.life/llvm-staging/<migration>/<path>` | `https://s4chinjha.github.io/llvm-docs-hosted/<project>/after/<path>` |
| Revision shown | no | no | the commit that was built, per project |

Two differences are deliberate:

* The before pages are **not** copied. The official documentation site keeps
  them, and a copy would only age badly.
* The after pages are **not** the whole documentation tree. rnk publishes
  everything because his landing page compares whole subprojects; for a
  reviewer of a single migration the interesting pages are the converted
  ones. Links from a preview to the other pages of the same project are
  rewritten to the official site, which keeps the preview small and every
  link working.

## 5. What this repository implements, and why

This repository follows **zeyi2's model** (GitHub Pages, committed or
CI-generated output, standard Pages actions), because it needs no server, no
domain and no credential:

* One public repository holds the configuration (`projects.yml`), the build
  tooling (`tools/`) and the Pages workflow (`.github/workflows/preview.yml`).
* The workflow builds each enabled project from a source repository and ref,
  validates the output, and publishes it with `actions/deploy-pages`.
* Only the **after** (converted) HTML is published. Before links point at the
  official LLVM documentation sites, so no pre-conversion HTML is duplicated.
* Landing pages use relative links only, so the site works at any Pages base
  path (project site, user site, or a later custom domain).

Deliberate differences from both upstream implementations:

* The built HTML is **not** committed; the workflow uploads it as the Pages
  artifact. A failed build then cannot damage the live site.
* Cross-repository rebuilds do not require a secret: the hosting workflow can
  fetch a public source repository by URL, so `workflow_dispatch` and the
  scheduled poll work with the default `GITHUB_TOKEN` alone.
