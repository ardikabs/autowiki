# autowiki

**Your engineering wiki, automatically built from Git.**

autowiki brings engineering documentation together in one browsable portal. It watches the repositories you register, builds their MkDocs sites, and presents the published guides in a searchable landing-page library. Git remains the source of truth; autowiki keeps the portal refreshed from it.

## What it does

- Builds documentation from multiple independent Git repositories.
- Serves each site at `/{name}/` and lists successfully published sites on the home page.
- Searches the portal's registered documentation names and descriptions from the landing page.
- Synchronizes configured refs with shallow Git fetches.
- Installs repository-specific `requirements.txt` files before building when present.
- Builds repositories concurrently, with configurable worker limits and timeouts.
- Prevents overlapping work on the same Git remote or checkout, including across runner instances sharing the lock directory.
- Gracefully stops build workers and their child processes when it receives `SIGTERM` or `SIGINT`.

autowiki is a Git-backed documentation portal: content is authored and maintained in your repositories rather than edited in the browser.

## How it works

1. The runner reads `/etc/wiki.yaml`, keeps the first valid entry for repeated repository names or equivalent Git remotes, and writes the current documentation catalog.
2. Up to `max_concurrent_builds` isolated worker processes sync their repositories and build changed revisions.
3. A worker's checkout and remote locks stay held until that worker exits; timed-out workers and their process groups are terminated before locks are released.
4. The runner updates `catalog.json` as published sites become available. The home page displays only repositories whose site `index.html` exists.
5. Nginx serves the landing page at `/` and documentation sites at `/{name}/`.

## Configuration

Mount a YAML file at `/etc/wiki.yaml`:

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `site_url` | string | Required | Public base URL used by MkDocs when building each site. |
| `site_dir` | path | Required | Output directory for the home page catalog and generated documentation sites. |
| `repo_dir` | path | Required | Parent directory for repository checkouts. |
| `poll_interval_seconds` | number | `60` | Time between checks for updated Git revisions. |
| `build_timeout_seconds` | number | `600` | Maximum lifetime of a repository sync/build worker. |
| `max_concurrent_builds` | integer | `4` | Maximum number of repository workers running at once. |
| `lock_dir` | path | `{repo_dir}/.autowiki-locks` | Directory for shared checkout/remote and pip-install locks. Use a shared filesystem path if multiple runner instances need mutual exclusion. |
| `repositories` | list | `[]` | Repository configurations to publish. |

Each repository entry supports:

| Key | Type | Description |
| --- | --- | --- |
| `name` | string | Unique site ID; used as the URL path and output folder. Must be a direct child name (no path separators). Also used as the card title if `label` is unset. |
| `git` | string | Git remote URL. Equivalent common HTTPS and SSH forms are treated as the same source. |
| `revision` | string | Branch, tag, or ref to fetch, such as `main` or `refs/heads/release`. |
| `docs` | string | Path inside the checkout to the MkDocs project directory or config file. |
| `label` | string | Optional human-friendly title shown for the documentation card. Falls back to `name`. |
| `description` | string | Optional short summary shown on the landing page. Falls back to `Documentation maintained from {name}.` |

Repeated names and equivalent remotes use the first valid config entry. This avoids publishing conflicting configurations for one checkout/source.

### Example

```yaml
site_url: "https://docs.example.com"
site_dir: "/srv/docs"
repo_dir: "/repos"
poll_interval_seconds: 60
build_timeout_seconds: 600
max_concurrent_builds: 4
# lock_dir: "/run/autowiki-locks"

repositories:
  - name: "platform"
    label: "Platform Handbook"
    git: "https://github.com/example/platform.git"
    revision: "main"
    docs: "docs"
    description: "Architecture, operations, and platform development guides."

  - name: "service-api"
    label: "Service API"
    git: "https://github.com/example/service-api.git"
    revision: "main"
    docs: "website"
    description: "API references and service integration guides."
```

## Run with Docker

Build the image:

```bash
docker build -t autowiki:latest .
```

Start the portal with persistent repository and site directories:

```bash
docker run -d \
  --name autowiki \
  -p 8080:8080 \
  -v "$(pwd)/config.yaml:/etc/wiki.yaml:ro" \
  -v autowiki-repos:/repos \
  -v autowiki-docs:/srv/docs \
  autowiki:latest
```

Open `http://localhost:8080/` for the landing page. Published sites are available at `http://localhost:8080/{name}/`.

## Local development

Requirements: Python 3.12+, Git, and Nginx.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml`, then run the service with your local config path:

```bash
CONFIG_PATH=./config.yaml python lib/runner.py
```

The runner copies `site/index.html` to the configured `site_dir` on startup when no root `index.html` already exists. This lets an existing custom home page take precedence. Run with `CONFIG_PATH=./config.yaml python lib/runner.py` to use the local configuration file.

## Project structure

```text
autowiki/
├── conf/nginx.conf       # Nginx routes for landing page and generated sites
├── lib/runner.py         # Repository syncing, builds, locking, and supervision
├── site/index.html       # Searchable documentation-portal landing page
├── config.example.yaml   # Example runner configuration
├── Dockerfile
└── requirements.txt
```
