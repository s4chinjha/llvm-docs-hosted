# LLVM documentation preview site

Hosting for the rendered **after** pages of my LLVM documentation migrations
(reStructuredText -> MyST Markdown).

The original pages are not copied here. Every "before" link points at the
official documentation site, and every "after" link points at the converted
page that this repository builds and publishes.

* Preview of the libc++ release notes migration:
  `https://<pages-host>/libcxx/index.html` (the URL becomes known when the
  repository is published, see *Publishing* below)
* Source of the conversion: `s4chinjha/llvm-project`, branch
  `libcxx-docs-md-convert`
* Tracking issue for the whole migration:
  <https://github.com/llvm/llvm-project/issues/201242>

## How the site is put together

```text
llvm_docs_hosted/
├── README.md                 this file
├── projects.yml              configuration: one entry per LLVM docs project
├── research/
│   └── preview-hosting-research.md   how rnk and zeyi2 host their previews
├── tools/
│   ├── build_site.py         build + validate + write the landing pages
│   ├── compare_state.py      skip a deployment when nothing changed
│   ├── preview_lib.py        the shared implementation
│   └── requirements.txt      pinned build dependencies
├── site/                     generated output (not committed)
└── .github/workflows/preview.yml
```

The generated site looks like this:

```text
site/
├── index.html                landing page listing every project
├── state.json                build metadata: ref, commit, build time
├── libcxx/
│   ├── index.html            before/after table for libc++
│   └── after/                the converted sphinx output
│       ├── ReleaseNotes.html
│       ├── ReleaseNotes/{20,21,22,23,24}.html
│       └── _static/…
```

All links in the generated pages are relative, so the site works unchanged at
any base path: a GitHub Pages project site, a user site, or a custom domain.

## Adding a project

1. Add an entry to `projects.yml` (copy the `libcxx` entry):

   ```yaml
   - name: myproject
     title: My Project
     enabled: false
     source:
       repository: https://github.com/llvm/llvm-project
       ref: main
     docs:
       path: myproject/docs
       python_path: utils/docs
       extra_args: []
     build:
       warnings_as_errors: false
       output_subdir: after
     official_base_url: https://example.org/docs
     pages:
       - source: myproject/docs/Page.md
         official: https://example.org/docs/Page.html
         after: Page.html
   ```

2. Check that the project builds with the LLVM documentation configuration:

   ```bash
   python3 tools/build_site.py --project myproject \
       --override-repository myproject=https://github.com/llvm/llvm-project \
       --override myproject=main
   ```

   Every LLVM documentation project has its own `conf.py`, its own sphinx
   extensions and sometimes extra dependencies (`lldb/docs` needs the `furo`
   theme and `sphinx_automodapi`, `openmp/docs` has its own theme), so a
   project that is added without being verified will usually fail to build.
   That is the intended behaviour: the build fails, nothing is published.

3. Set `enabled: true` once the build succeeds and the before/after URLs in
   `pages` are verified to exist.

`source.local_path` is only used for local testing; the workflow ignores it
and falls back to `source.repository` when the path does not exist.

## Building locally

```bash
cd ~/oss_contribute/llvm_docs_hosted
python3 -m venv .venv && . .venv/bin/activate
pip install -r tools/requirements.txt

# build every enabled project into ./site
python3 tools/build_site.py

# build one project from another ref
python3 tools/build_site.py --project libcxx --override libcxx=main

# build from a local checkout instead of the configured repository
python3 tools/build_site.py --project libcxx \
    --source-local ~/oss_contribute/llvm-project

# validate what is in ./site without rebuilding
python3 tools/build_site.py --validate-only
```

Preview the result over HTTP (relative links need a server, `file://` is not
enough):

```bash
cd site && python3 -m http.server 8000
# http://localhost:8000/index.html
```

The build never writes into the LLVM source repository and never publishes
anything; publishing is the job of the workflow.

## Publishing

The workflow `.github/workflows/preview.yml` builds the site and publishes it
with GitHub Pages:

* `actions/checkout` – the preview repository
* `actions/setup-python` – Python 3.13
* `python3 tools/build_site.py` – sparse checkout of the source repository,
  sphinx build, validation, landing pages
* `actions/configure-pages`, `actions/upload-pages-artifact`,
  `actions/deploy-pages` – publication

All third-party actions are pinned to commit SHAs.

The build job only has `contents: read` and never sees a deployment
credential. The deploy job has `pages: write` and `id-token: write` and runs
no code from the LLVM repository, so building a preview from an untrusted
branch cannot leak the deployment token.

### Triggers

| Event | What happens |
|---|---|
| `workflow_dispatch` | rebuild with an explicit project, ref and/or repository |
| `push` to `main` | rebuild after a change to `projects.yml`, `tools/` or the workflow |
| `schedule` (every 6 hours) | rebuild the configured source branches |
| `repository_dispatch` (`preview-update`) | optional instant update from a source repository |

`workflow_dispatch` inputs:

| Input | Meaning |
|---|---|
| `project` | the project whose ref is overridden; other projects keep their configured ref |
| `ref` | branch, tag or commit SHA to build |
| `repository` | source repository to build from |

A rebuild from a specific commit, without touching the configuration:

```bash
gh workflow run preview.yml \
  --repo s4chinjha/llvm-docs-hosted \
  -f project=libcxx -f ref=<commit-sha>
```

### What is automatic and what is not

* Automatic: the periodic rebuild, the rebuild after a change to this
  repository, the validation, and the publication.
* Not automatic: a change to a branch in the LLVM repository. The hosting
  repository is a different repository, so GitHub does not deliver an event
  for it. Two ways to close that gap:

  1. **Poll** (already active): the scheduled build picks up new commits of
     the configured branch every six hours. This needs no secret.
  2. **Push notification** (optional): a small workflow in the LLVM fork
     sends a `repository_dispatch` event to this repository. That needs a
     credential with write access to this repository, for example a fine
     grained personal access token stored as a secret in the fork:

     ```yaml
     # .github/workflows/preview-dispatch.yml in the LLVM fork
     name: Notify the preview site
     on:
       push:
         branches: [libcxx-docs-md-convert]
     jobs:
       notify:
         runs-on: ubuntu-latest
         steps:
           - run: |
               gh api repos/s4chinjha/llvm-docs-hosted/dispatches \
                 -f event_type=preview-update \
                 -f "client_payload[project]=libcxx" \
                 -f "client_payload[ref]=${{ github.sha }}" \
                 -H "Authorization: token ${{ secrets.PREVIEW_DISPATCH_TOKEN }}"
         env:
           GH_TOKEN: ${{ secrets.PREVIEW_DISPATCH_TOKEN }}
     ```

     The secret is never printed and never leaves the fork.

  The polling route needs nothing, so the push notification is optional.

### Permissions and secrets

| Requirement | Needed for |
|---|---|
| `pages: write`, `id-token: write` (automatic `GITHUB_TOKEN`) | publishing the site |
| `contents: read` | checking out this repository |
| `PREVIEW_DISPATCH_TOKEN` secret (optional) | instant updates from the LLVM fork |

No paid service and no custom domain are required. A GitHub Pages project
site is enough; a custom domain can be added later in the repository settings.

## Validation performed before publishing

`tools/build_site.py` refuses to publish incomplete output:

* the sphinx build must succeed; for projects with
  `warnings_as_errors: true` any warning fails the build (libc++ uses this)
* every page listed in `pages` must exist in the output
* `_static/` must exist and contain the theme and highlight assets
* every relative link and asset referenced by a generated page must resolve
* the landing page must link to every generated page
* the build runs in a fresh directory, so stale output from an earlier build
  can never be published

A failed build leaves the previous deployment untouched, because the deploy
job only runs when the build job succeeded.

## Recovering from a failed build

1. Look at the failing step in the workflow run.
2. Reproduce it locally:

   ```bash
   python3 tools/build_site.py --project libcxx --override libcxx=<ref>
   ```

3. Fix the cause. If the cause is a problem in the LLVM sources, the preview
   cannot be fixed here; the previous preview stays online until the source
   branch is fixed.

## Recovering the site after a configuration mistake

The site is rebuilt from scratch on every run, so a bad configuration only
affects the next deployment. Roll back by reverting the change on `main`;
the next push rebuilds and republishes the previous configuration.

## Research notes

`research/preview-hosting-research.md` documents how `rnk` and `zeyi2` host
their previews, which parts are verified from public sources, and which parts
this repository deliberately does not reproduce.
