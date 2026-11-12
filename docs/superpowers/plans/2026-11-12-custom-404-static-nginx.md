# Custom 404 and Static Nginx Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve an autowiki-styled 404 page with the correct HTTP status and harden Nginx for static documentation hosting without blocking valid MkDocs assets.

**Architecture:** Add `site/404.html` as the source template, copy it into the active site directory during runner startup when no operator-provided 404 page exists, and route Nginx 404s to it internally. Keep Nginx rooted at `/srv/docs`, permit only GET/HEAD, hide dot paths, disable directory listings, and retain the catalog's no-store policy alongside security headers.

**Tech Stack:** Nginx, Python 3.12 `unittest`, HTML/CSS, Docker.

---

## File map

- Create `site/404.html`: standalone responsive error page using the palette, fonts, and brand mark from `site/index.html`.
- Modify `Dockerfile`: copy the error page template into `/site/` beside the landing page and navigation script.
- Modify `lib/runner.py`: define the 404 template path and install it into `site_dir` only if no file already exists.
- Modify `tests/test_runner.py`: test default 404 installation and preservation of an operator-provided 404 page.
- Modify `conf/nginx.conf`: serve the page for 404s and enforce the approved static-only boundary.

The working tree already includes user edits in the Dockerfile, Nginx config, runner, tests, compose file, and `site/autowiki-nav.js`. Make focused edits around those changes. Do not replace those files wholesale or remove existing navigation behavior.

### Task 1: Add runner installation tests

**Files:**
- Modify: `tests/test_runner.py`

- [x] **Step 1: Add the default-install test**

Add this method to the existing `CatalogTests` class, after `test_installs_landing_page_into_site_directory`:

```python
    def test_installs_not_found_page_into_site_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            site_dir = Path(temp_dir)

            install_landing_page(site_dir)

            not_found_page = site_dir / "404.html"
            self.assertTrue(not_found_page.is_file())
            self.assertIn("This page isn’t here.", not_found_page.read_text())
```

- [x] **Step 2: Add the custom-page preservation test**

Add this method to the same class:

```python
    def test_preserves_existing_not_found_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            site_dir = Path(temp_dir)
            not_found_page = site_dir / "404.html"
            not_found_page.write_text("<h1>Custom not found page</h1>")

            install_landing_page(site_dir)

            self.assertEqual(
                not_found_page.read_text(),
                "<h1>Custom not found page</h1>",
            )
```

- [x] **Step 3: Ensure installation does not follow a dangling custom-page symlink**

Add this method to `CatalogTests`:

```python
    def test_does_not_write_through_dangling_not_found_symlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            site_dir = root / "site"
            site_dir.mkdir()
            target = root / "outside.html"
            not_found_page = site_dir / "404.html"
            not_found_page.symlink_to(target)

            install_landing_page(site_dir)

            self.assertTrue(not_found_page.is_symlink())
            self.assertFalse(target.exists())
```

- [x] **Step 4: Run the new default-install test and confirm it fails**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_runner.py' -v`

Expected: the default-install test fails because `install_landing_page` does not yet create `404.html`; the custom-page preservation test passes; the dangling-symlink test fails because the current implementation follows the link and writes its target.

### Task 2: Add the error page and runtime installation

**Files:**
- Create: `site/404.html`
- Modify: `Dockerfile`
- Modify: `lib/runner.py`
- Test: `tests/test_runner.py`

- [x] **Step 1: Create `site/404.html`**

Use this complete page. Its palette and typography match the existing landing page. Its muted text is darkened slightly to maintain a measured 4.75:1 contrast ratio against the paper background. The sole action returns to the real portal root.

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#f5f3ed">
    <meta name="description" content="The requested autowiki page could not be found.">
    <title>404 | autowiki</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
      :root {
        color-scheme: light;
        --paper: #f5f3ed;
        --ink: #22251f;
        --muted: #6a6d65;
        --line: #ddded5;
        --accent: #315fce;
      }
      * { box-sizing: border-box; }
      body {
        display: flex;
        min-height: 100vh;
        flex-direction: column;
        margin: 0;
        color: var(--ink);
        background: var(--paper);
        font-family: "DM Sans", sans-serif;
        -webkit-font-smoothing: antialiased;
      }
      a { color: inherit; }
      .shell { width: min(1120px, calc(100% - 48px)); margin: 0 auto; }
      .topbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 28px 0;
        border-bottom: 1px solid var(--line);
      }
      .brand {
        display: flex;
        min-height: 44px;
        align-items: center;
        gap: 11px;
        text-decoration: none;
      }
      .brand-mark {
        display: grid;
        width: 34px;
        height: 34px;
        place-items: center;
        color: #fff;
        background: var(--accent);
        border-radius: 9px;
        font-family: "Manrope", sans-serif;
        font-size: 20px;
        font-weight: 800;
      }
      .brand:focus-visible {
        outline: 3px solid var(--accent);
        outline-offset: 4px;
        border-radius: 4px;
      }
      .brand-name {
        font-family: "Manrope", sans-serif;
        font-size: 19px;
        font-weight: 800;
        letter-spacing: -0.7px;
      }
      .brand-auto { color: var(--accent); }
      .brand-wiki { color: var(--ink); }
      .top-note {
        color: var(--muted);
        font-family: "DM Mono", monospace;
        font-size: 11px;
        letter-spacing: 0.04em;
      }
      main {
        display: grid;
        flex: 1;
        place-items: center;
        padding: 64px 0;
      }
      .message { width: min(100%, 650px); }
      .eyebrow {
        margin: 0 0 18px;
        color: var(--accent);
        font-family: "DM Mono", monospace;
        font-size: 12px;
        letter-spacing: 0.1em;
      }
      h1 {
        margin: 0 0 16px;
        font-family: "Manrope", sans-serif;
        font-size: clamp(42px, 7vw, 68px);
        font-weight: 600;
        letter-spacing: -0.065em;
        line-height: 1.04;
      }
      .copy {
        max-width: 520px;
        margin: 0 0 28px;
        color: var(--muted);
        font-size: 16px;
        line-height: 1.7;
      }
      .home-link {
        display: inline-flex;
        min-height: 44px;
        align-items: center;
        justify-content: center;
        padding: 0 18px;
        color: #fff;
        background: var(--accent);
        border-radius: 7px;
        font-size: 14px;
        font-weight: 700;
        text-decoration: none;
        transition: background-color 160ms ease;
      }
      .home-link:hover { background: #274eae; }
      .home-link:focus-visible {
        outline: 3px solid var(--accent);
        outline-offset: 4px;
      }
      .footer {
        padding: 21px 0 30px;
        color: var(--muted);
        border-top: 1px solid var(--line);
        font-size: 12px;
      }
      @media (max-width: 540px) {
        .shell { width: min(100% - 34px, 1120px); }
        .top-note { display: none; }
        .topbar { padding: 22px 0; }
        main { padding: 48px 0; }
      }
    </style>
  </head>
  <body>
    <header class="shell topbar">
      <a class="brand" href="/" aria-label="autowiki home">
        <span class="brand-mark" aria-hidden="true">a</span>
        <span class="brand-name"><span class="brand-auto">auto</span><span class="brand-wiki">wiki</span></span>
      </a>
      <span class="top-note">ENGINEERING KNOWLEDGE PORTAL</span>
    </header>

    <main class="shell">
      <section class="message" aria-labelledby="not-found-title">
        <p class="eyebrow">404 / PAGE NOT FOUND</p>
        <h1 id="not-found-title">This page isn’t here.</h1>
        <p class="copy">The address may be out of date, or the page may have moved.</p>
        <a class="home-link" href="/">Back to autowiki</a>
      </section>
    </main>

    <footer class="shell footer">Source of truth: your Git repositories.</footer>
  </body>
</html>
```

- [x] **Step 2: Add the template path and copy-if-missing logic in `lib/runner.py`**

Add this constant beside `LANDING_PAGE_TEMPLATE` and `NAV_SCRIPT_TEMPLATE`:

```python
NOT_FOUND_PAGE_TEMPLATE = Path(__file__).resolve().parent.parent / "site" / "404.html"
```

In `install_landing_page`, after the landing-page copy and before the navigation-script copy, add:

```python
    not_found_page = site_dir / "404.html"
    if (
        not not_found_page.exists()
        and not not_found_page.is_symlink()
        and NOT_FOUND_PAGE_TEMPLATE.is_file()
    ):
        shutil.copyfile(NOT_FOUND_PAGE_TEMPLATE, not_found_page)
```

Keep the existing landing-page preservation and navigation-script installation logic intact.

- [x] **Step 3: Copy the template into the image**

In `Dockerfile`, add this line alongside the existing `site/` copies. Preserve the existing navigation-script line:

```dockerfile
COPY site/404.html /site/404.html
```

- [x] **Step 4: Run the focused runner tests**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_runner.py' -v`

Expected: all tests pass, including both new 404 installation tests and the existing landing-page/navigation tests.

### Task 3: Harden the Nginx static server

**Files:**
- Modify: `conf/nginx.conf`

- [x] **Step 1: Verify the pre-change 404 response**

Build the image with the new error-page asset but the old Nginx configuration, mount `site/` at `/srv/docs`, and request a missing URL.

Expected: HTTP 404 with Nginx's default response body, proving the custom page is not yet wired in. Observed: `RED confirmed: current Nginx returns its default 404 page`.

- [x] **Step 2: Update the existing `server` block to this configuration**

Keep the surrounding `http` block and logging directives unchanged. Replace only the current `server` block, retaining the existing `server_tokens off`, root, index, and catalog route:

```nginx
    server {
        listen 8080;
        server_name _;
        server_tokens off;

        root /srv/docs;
        index index.html;
        autoindex off;
        error_page 404 /404.html;

        if ($request_method !~ ^(GET|HEAD)$) {
            return 405;
        }

        add_header X-Content-Type-Options "nosniff" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Allow "GET, HEAD" always;

        location = / {
            try_files /index.html =404;
        }

        location = /404.html {
            internal;
            add_header Cache-Control "no-store" always;
            add_header X-Content-Type-Options "nosniff" always;
            add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        }

        location = /catalog.json {
            add_header Cache-Control "no-store" always;
            add_header X-Content-Type-Options "nosniff" always;
            add_header Referrer-Policy "strict-origin-when-cross-origin" always;
            try_files $uri =404;
        }

        location ~ (^|/)\. {
            return 404;
        }

        location / {
            try_files $uri $uri/ =404;
        }
    }
```

The security headers are repeated in `/catalog.json` and `/404.html` because those locations define their own `add_header` directives and therefore do not inherit the server-level headers on Nginx versions without merged `add_header` inheritance. The internal 404 location also sets `Cache-Control: no-store`, so missing URLs and a missing catalog are not cached. The server-level `Allow` header advertises the only accepted methods on 405 responses. The `always` parameter makes these headers appear on error responses.

- [x] **Step 3: Review the Nginx rules against the approved boundary**

Confirm the final block does all of the following: keeps every regular MkDocs asset type available, denies non-GET/HEAD methods with 405 and `Allow: GET, HEAD`, has no directory listing, returns 404 for hidden path segments, keeps `/catalog.json` and 404 responses no-store, and does not introduce proxy or server-side execution handlers.

### Task 4: Verify the image and HTTP behavior

**Files:**
- Verify: `site/404.html`, `Dockerfile`, `lib/runner.py`, `tests/test_runner.py`, `conf/nginx.conf`

- [x] **Step 1: Run all Python tests**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: all tests pass.

- [x] **Step 2: Build the container image**

Run: `docker build -t autowiki:404-test .`

Expected: the image builds with the existing navigation script and the new 404 template both present under `/site/`.

- [x] **Step 3: Validate Nginx configuration in the built image**

Run: `docker run --rm --entrypoint nginx autowiki:404-test -t`

Expected: `syntax is ok` and `test is successful`.

- [x] **Step 4: Run a temporary static-server smoke test**

Run this from the repository root. It starts Nginx from the built image on port 18080, creates an isolated document root inside the temporary container, checks response status, body, cache policy, security headers, methods, hidden paths, and asset availability, then removes the container:

```bash
set -euo pipefail
smoke_container="autowiki-404-smoke"
cleanup() {
  docker rm -f "$smoke_container" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --rm -d --name "$smoke_container" \
  -p 127.0.0.1:18080:8080 \
  --entrypoint nginx \
  autowiki:404-test -g 'daemon off;'

docker exec -i "$smoke_container" python3.12 - <<'PY'
from pathlib import Path

root = Path("/srv/docs")
(root / "guide").mkdir(parents=True)
(root / "empty").mkdir()
(root / "index.html").write_text("<h1>Portal</h1>")
(root / "catalog.json").write_text("[]")
(root / "guide" / "index.html").write_text("<h1>Guide</h1>")
(root / "guide" / "asset.bin").write_bytes(b"static asset")
(root / "empty" / "private.txt").write_text("not listed")
(root / ".env").write_text("not public")
PY
docker cp site/404.html "$smoke_container:/srv/docs/404.html"

python - <<'PY'
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

base = "http://127.0.0.1:18080"

def request(path, method="GET"):
    req = Request(base + path, method=method)
    try:
        response = urlopen(req, timeout=2)
    except HTTPError as exc:
        response = exc
    return response.code, response.headers, response.read()

for attempt in range(30):
    try:
        request("/")
        break
    except (URLError, TimeoutError):
        if attempt == 29:
            raise
        time.sleep(0.2)

status, headers, body = request("/missing-page")
assert status == 404
assert b"This page isn\xe2\x80\x99t here." in body
assert headers["Cache-Control"] == "no-store"
assert headers["X-Content-Type-Options"] == "nosniff"
assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

status, _, body = request("/404.html")
assert status == 404
assert b"This page isn\xe2\x80\x99t here." in body

assert request("/")[0] == 200
status, headers, _ = request("/", method="HEAD")
assert status == 200
assert headers["X-Content-Type-Options"] == "nosniff"
assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
assert request("/guide/")[0] == 200
assert request("/guide/asset.bin")[0] == 200
assert request("/.env")[0] == 404
assert request("/empty/")[0] == 403
for path in ("/", "/catalog.json"):
    status, headers, _ = request(path, method="POST")
    assert status == 405
    assert headers["Allow"] == "GET, HEAD"
status, headers, _ = request("/catalog.json")
assert status == 200
assert headers["Cache-Control"] == "no-store"
assert headers["X-Content-Type-Options"] == "nosniff"
assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
print("Nginx static-server smoke test passed")
PY

docker exec "$smoke_container" python3.12 -c 'from pathlib import Path; (Path("/srv/docs") / "catalog.json").unlink()'
python - <<'PY'
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

url = "http://127.0.0.1:18080/catalog.json"
for attempt in range(30):
    try:
        try:
            response = urlopen(url, timeout=2)
        except HTTPError as exc:
            response = exc
        break
    except URLError:
        if attempt == 29:
            raise
        time.sleep(0.2)
assert response.code == 404
assert response.headers["Cache-Control"] == "no-store"
print("Missing catalog response is uncached")
PY
```

Expected: the script prints `Nginx static-server smoke test passed`. If port 18080 is already occupied, stop the other listener and rerun the smoke test.

## Plan self-review

- Spec coverage: the page, 44px links, live-volume installation, custom-page and symlink handling, 404 status, static-only method and path rules, directory listing behavior, 405 `Allow` header, security headers, cached and missing catalog behavior, and varied MkDocs assets each have implementation or verification steps.
- Placeholder scan: no TODOs, TBDs, or unspecified implementation steps remain.
- Type and path consistency: the runner template constant points to `site/404.html`, the Dockerfile copies that same path to `/site/404.html`, and the runner installs it as `site_dir/404.html`, which matches the Nginx internal URI under `/srv/docs`.
