# Custom 404 and Static Nginx Hardening Design

## Goal

Replace Nginx's default 404 response with a clear autowiki-branded page while keeping the response status at 404. Tighten the Nginx server block for its role as a static documentation portal without breaking generated MkDocs assets.

## Context

- `site/index.html` defines the current autowiki visual identity: warm paper background, dark ink, a blue accent, and Manrope, DM Sans, and DM Mono typography.
- Nginx serves `/srv/docs`, with the landing page at `/`, the catalog at `/catalog.json`, and generated documentation sites below the root.
- The runner installs the bundled landing page into the configured site directory. The directory is commonly a mounted Docker volume, so the 404 template must be installed at runtime as well as included in the image.
- The working tree already contains unrelated, uncommitted edits to the Dockerfile, Nginx configuration, runner, tests, compose file, and navigation script. Those edits must be preserved while implementing this design.

## Chosen design

### Error page

Add `site/404.html` as a standalone, responsive page. Reuse the landing page's brand mark, palette, typography, and restrained spacing. Show the 404 code, a `Page not found` heading, one short explanation, and one working `Back to autowiki` link to `/`. Use semantic page structure, visible keyboard focus indicators, and a touch target of at least 44 pixels for both links. Do not add JavaScript, illustrations, or unrelated navigation.

Visual direction: Reading this as a calm documentation error page for engineering teams, in autowiki's existing warm editorial style, with ENERGY 1 / RHYTHM 1 / MOTION 1.

Reasons for the major visual decisions:

- Palette and typography: reuse the landing page's visual tokens so the error response remains recognizably part of autowiki.
- Secondary text: use `#6a6d65` on `#f5f3ed` for a measured 4.75:1 contrast ratio while retaining the landing page's muted-text role.
- Layout: retain the brand header and give the single recovery action clear prominence.
- Copy: state the problem directly and offer the one relevant next action.
- Motion: use only ordinary hover and focus feedback so movement does not distract from recovery.

### Runtime installation and HTTP behavior

- Add the 404 template to the container image beside the existing landing-page template.
- Extend the runner's landing-page installation routine to copy `404.html` into `site_dir` only when no filesystem entry, including a symlink, already occupies that path. This makes the error page available from the mounted live document root, preserves operator customizations, and prevents copying through a dangling symlink.
- Configure Nginx to internally serve `/404.html` for 404 responses. Direct requests to that file remain internal. Missing paths display the custom page, keep the original HTTP 404 status, and remain uncached.
- Keep `/` and `/catalog.json` behavior intact. The catalog remains `Cache-Control: no-store`.

### Static-only Nginx boundary

- Keep `/srv/docs` as the document root and retain support for all generated documentation asset types. Do not add an extension allowlist, since documentation repositories may publish less common assets and downloads.
- Permit only `GET` and `HEAD`; return 405 for other methods with `Allow: GET, HEAD`.
- Explicitly disable directory listings and return 404 for paths containing hidden file or directory segments.
- Keep request resolution file-based under the static document root. Do not add proxy, CGI, or server-side execution handlers.
- Keep `server_tokens off` and add `X-Content-Type-Options: nosniff` and `Referrer-Policy: strict-origin-when-cross-origin` to normal and error responses.
- Keep 404 responses, including a missing `/catalog.json`, uncached. Ensure `/catalog.json` retains `Cache-Control: no-store` and the security headers.
- Do not add a Content Security Policy in this change. The existing portal uses inline scripts and external fonts, while generated documentation can have repository-specific assets; a CSP needs a separate compatibility policy.

### Alternatives considered

1. Install `site/404.html` into the live site directory through the runner and use an internal Nginx error route. Chosen because it follows the existing landing-page installation pattern, works with a mounted site volume, and allows an operator's existing custom page to take precedence.
2. Serve a page directly from the image's `/site` directory. Not chosen because it couples error handling to the image template rather than the active document root and does not follow the runner's existing site-directory behavior.
3. Restrict Nginx to an extension allowlist. Not chosen because it can break valid MkDocs downloads and less common generated assets. Static-only behavior is enforced through methods, hidden-path handling, disabled listings, and the absence of dynamic handlers instead.

## Validation

- Unit-test runner installation of the bundled 404 page, preservation of an existing custom 404 page, and safe handling of a dangling `404.html` symlink.
- Validate the Nginx configuration with `nginx -t` in the target image environment.
- Verify that a missing URL returns HTTP 404 and the custom page body, while `/` and a generated documentation page continue to load.
- Verify that direct access to `/404.html` is internal-only, hidden paths are not served, non-GET/HEAD methods return 405 with `Allow: GET, HEAD`, and directory listings are unavailable.
- Verify that `/catalog.json` remains uncached when present or missing, and that the security headers are present on normal and 404 responses.
- Verify that representative generated assets, including less common file types, remain available.

## Scope

This change is limited to the branded 404 page, its runtime installation, and static-serving hardening in `conf/nginx.conf`. It does not change documentation build behavior, introduce host-specific routing, or alter the existing landing-page content or navigation script.
