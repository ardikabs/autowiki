# Design: Static Directory Support for autowiki

Date: 2026-11-18
Status: Approved
Scope: `lib/runner.py`, `config.example.yaml`, `README.md`, `tests/test_runner.py`

## Problem

Per `config.example.yaml`, `repositories[].docs` must point to a directory containing
`mkdocs.yml`/`mkdocs.yaml` (or directly to a config file). Users with pre-built
static sites (Hugo or other generators, e.g. output stored at `docs/website`
with `index.html` + assets, GitHub/GitLab Pages style) cannot publish through
autowiki today — the runner raises `FileNotFoundError: Missing MkDocs config`.

## Goal

If no MkDocs config is found at the referenced `docs` path, serve the directory
as-is (static copy) with a browser-safe allowlist, instead of failing. Zero-config
for the common case, with an explicit override for strictness.

Non-goals: rewriting asset URLs inside static output, executing any build tool
for static dirs, per-file cache headers in nginx.

## Decisions (from brainstorming)

1. Build mode detection: **both auto + override** (user-approved).
2. Allowlist: **web-only list** (user-approved).
3. Nav + index: **inject nav script, require `index.html` for catalog** (user-approved,
   reuses existing `build_catalog` behavior unchanged).

## Architecture

Single dispatcher inside the existing `build_repository()` entry point
(Approach A). Worker sync/lock/timeout/poll/catalog flow is unchanged.

```
sync_repository → git checkout --force --detach rev + clean -fdx
       → build_repository(repo, checkout_dir, revision, base_url, site_dir, lock_dir)
            → resolve docs_path (existing traversal guard)
            → resolve_mode(repo)  # auto | mkdocs | static
            → find_mkdocs_config(docs_path) or None
            → mode == mkdocs (or auto + config found) → existing MkDocs path
            → mode == static (or auto + no config)   → publish_static_directory()
            → run_poll_cycle reap → write_catalog (unchanged, needs index.html)
```

No changes to `launch_worker`, `build_worker`, locks, nginx routing
(`try_files $uri $uri/ =404` already serves static trees).

## Components

### 1. Mode resolution

- New optional repo key `mode: auto | mkdocs | static`, default `auto`.
- `auto`: MkDocs if `docs_path` is a file or contains `mkdocs.yml`/`mkdocs.yaml`;
  otherwise static.
- `mkdocs`: force MkDocs; missing config keeps current `FileNotFoundError`.
- `static`: force static copy even if a MkDocs config exists. If `docs_path`
  is a file while mode is static → `ValueError` (static requires a directory).
- Unknown `mode` value → invalid repo config: `deduplicate_repositories`
  skips the entry with a warning; as a backstop `build_repository` raises
  `ValueError` for any unknown mode that reaches it.

### 2. Static publisher `publish_static_directory()`

Signature:

```python
STATIC_ALLOWED_SUFFIXES = frozenset({...})
def publish_static_directory(name, docs_path, output_dir, base_url) -> int  # returns file count
```

Behavior:

- Preconditions: `docs_path` is a directory inside checkout (existing
  `is_relative_to` guard); `output_dir = contained_directory(site_dir, name)`.
- Build into temp dir `site_dir/.tmp-{name}-XXXX`, then `os.replace()` onto
  `output_dir` for atomic swap. On exception, temp dir is removed and the
  previous site stays. Wrap failures as
  `RuntimeError(f"Static publish failed for {name} ...")`.
- Walk `docs_path` recursively, for each entry:
  - Skip dotfiles/dot-dirs (any path part starting with `.`), `__pycache__`,
    non-regular files (dirs handled for traversal, fifos/sockets/devices skipped).
  - Resolve symlinks; skip if target resolves outside `docs_path` (after
    `resolve()`) or is absolute-escape. Do not copy the link itself — copy
    target content under the link's relative path, or skip entirely if escape.
    Simplest safe rule: skip all symlinks whose resolved path is not
    relative to `docs_path.resolve()`.
  - Suffix check on lowercase `Path.suffix`: must be in allowlist; files with
    no suffix or unknown suffix are skipped and counted as skipped.
  - Copy with `shutil.copy2` preserving relative tree; `mkdir(parents=True)`
    as needed. After copy, `chmod 0o644` on files, `0o755` on created dirs
    (strip exec bits).
- Allowlist (lowercased suffix):
  `.html .htm .css .js .mjs .json .xml .txt .md .svg .png .jpg .jpeg .gif
   .webp .avif .ico .woff .woff2 .ttf .eot .otf .pdf .map .webmanifest`
- If zero files copied or no root `index.html` in output → `logger.warning`
  (still publish; `build_catalog` hides entries without `index.html`).
- `requirements.txt` install is skipped on the static path (no Python build).
- Return copied file count for logging:
  `logger.info("Published static site for %s from %s (%d files)", ...)`.

### 3. Nav injection `inject_nav_script(html_path, nav_script_path)`

- Reuse `nav_script_path` computation from MkDocs path:
  `base_path = urlsplit(base_url).path.rstrip("/")`,
  `nav = f"{base_path}/autowiki-nav.js" or "/autowiki-nav.js"`.
- For each copied `*.html`/`*.htm`: read as UTF-8 (on `UnicodeDecodeError`,
  leave file untouched), insert
  `<script src="{nav}" defer></script>` before the last case-insensitive
  `</body>`; if no `</body>`, append at end. Write back only if changed.
- Failure to inject one file does not fail the publish; log warning.

### 4. Config / docs updates

- `config.example.yaml`: update `docs:` comment (fallback behavior) + add
  commented `# mode: "auto"` example with allowed values.
- `README.md`: repositories table gains `mode` row; `docs` row reworded to
  "MkDocs project dir/config file, or static site root (fallback)".
  Add short Static sites subsection documenting Hugo example:
  `hugo --baseURL https://docs.example.com/{name}/ -d docs/website`
  and the limitation that absolute asset paths must include `/{name}/`
  (autowiki does not rewrite them).

## Data flow

1. Manager `run_poll_cycle` launches worker (unchanged).
2. Worker `sync_repository` fetches shallow ref, returns revision.
3. `build_repository` checks out + cleans, resolves mode, builds or publishes.
4. Result JSON `{revision, built}` → manager updates `last_built`,
   `write_catalog` lists entry only if `{site_dir}/{name}/index.html` exists.

## Error handling

| Case | Behavior |
|---|---|
| `docs` path missing | `FileNotFoundError` (unchanged, both modes) |
| `mode: mkdocs`, no config | `FileNotFoundError` (unchanged) |
| `mode: auto`, no config | static publish |
| `mode: static`, `docs` is file | `ValueError` |
| static copy error mid-walk | `RuntimeError`, old site preserved |
| empty static dir / no `index.html` | warning, publish partial, hidden from catalog |
| symlink escape | skipped + debug log, never followed outside checkout |

## Security

- Existing `contained_directory` + `assert_no_nested_repository_names` +
  `assert_no_git_ancestor` unchanged and apply to both paths.
- Static path additionally: no dotfiles, no suffix-less/executable content
  (`.py .sh .exe .so` etc. never in allowlist), no symlink escape, no exec bits
  in output, atomic replace so partial trees are never served.
- nginx already blocks `..`-dot segments (`location ~ (^|/)\.` → 404) and only
  allows GET/HEAD.

## Testing

Extend `tests/test_runner.py`:

- `auto` with `mkdocs.yml` present → calls MkDocs `build` (existing test stays).
- `auto` without config → copies allowlisted files, skips `.py`, dotfiles,
  extensionless; `index.html` gets nav script tag.
- `mode: mkdocs` without config → raises `FileNotFoundError`.
- `mode: static` with config present → static path (no `build` call).
- `mode: static` with file `docs` → `ValueError`.
- symlink escape test: link to `/etc/passwd` or outside docs → not copied.
- atomicity: failed publish leaves previous `output_dir` intact.
- `mode` invalid → repo skipped with warning.

Run: `python -m pytest tests/ -q` (or `.venv` equivalent).

## Open limitations (accepted)

- No asset-URL rewriting: static generators must be invoked with the public
  `/{name}/` base. Documented in README.
- No per-extension cache headers; nginx defaults apply.
