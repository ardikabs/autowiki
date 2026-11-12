(function () {
  "use strict";

  if (window.__AUTOWIKI_NAV_INITIALIZED__) return;
  window.__AUTOWIKI_NAV_INITIALIZED__ = true;

  function findNavScript() {
    if (document.currentScript && document.currentScript.src) {
      return document.currentScript;
    }
    try {
      return document.querySelector('script[src*="autowiki-nav.js"]');
    } catch (_) {
      return null;
    }
  }

  function resolvePortalUrl() {
    try {
      const script = findNavScript();
      if (script && script.src) {
        const url = new URL(script.src, window.location.href);
        const basePath = url.pathname.replace(/\/autowiki-nav\.js(\?.*)?$/, "");
        return basePath ? basePath + "/" : "/";
      }
    } catch (_) {}
    return "/";
  }

  const portalUrl = resolvePortalUrl();

  function isPortalPage() {
    try {
      const current = window.location.pathname;
      return current === portalUrl || current === portalUrl + "index.html";
    } catch (_) {
      return false;
    }
  }

  function mount() {
    if (document.getElementById("autowiki-nav-root")) return;
    if (isPortalPage()) return;
    if (!document.body) return;

    const host = document.createElement("div");
    host.id = "autowiki-nav-root";
    host.style.position = "fixed";
    host.style.bottom = "20px";
    host.style.left = "20px";
    host.style.zIndex = "2147483647"; // Max 32-bit int to stay on top
    host.style.pointerEvents = "auto";
    host.style.fontFamily = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "DM Sans", sans-serif';

    const shadow = host.attachShadow({ mode: "open" });

    const style = document.createElement("style");
    style.textContent = `
      :host {
        all: initial;
        display: block;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "DM Sans", sans-serif;
        font-size: 13px;
        line-height: 1.4;
      }

      * {
        box-sizing: border-box;
      }

      .widget {
        display: inline-flex;
        min-height: 44px;
        align-items: center;
        gap: 10px;
        padding: 6px 14px 6px 8px;
        background: rgba(255, 254, 250, 0.94);
        color: #22251f;
        border: 1px solid rgba(221, 222, 213, 0.9);
        border-radius: 9999px;
        box-shadow: 0 4px 18px rgba(34, 37, 31, 0.08), 0 1px 3px rgba(34, 37, 31, 0.04);
        backdrop-filter: blur(12px);
        -webkit-backdrop-filter: blur(12px);
        text-decoration: none;
        transition: transform 160ms cubic-bezier(0.16, 1, 0.3, 1),
                    box-shadow 160ms cubic-bezier(0.16, 1, 0.3, 1),
                    border-color 160ms ease,
                    background-color 160ms ease;
        user-select: none;
        -webkit-user-select: none;
      }

      @media (prefers-reduced-motion: reduce) {
        .widget,
        .back-action,
        .back-arrow {
          transition: none;
        }
        .widget:hover {
          transform: none;
        }
        .widget:hover .back-arrow {
          transform: none;
        }
      }

      .widget:hover {
        transform: translateY(-2px);
        border-color: #315fce;
        box-shadow: 0 8px 24px rgba(49, 95, 206, 0.16), 0 2px 6px rgba(49, 95, 206, 0.08);
        background: #ffffff;
      }

      .widget:active {
        transform: translateY(0);
      }

      .widget:focus-visible {
        outline: 2px solid #315fce;
        outline-offset: 2px;
      }

      .brand-mark {
        display: grid;
        place-items: center;
        width: 24px;
        height: 24px;
        background: #315fce;
        color: #ffffff;
        border-radius: 7px;
        font-family: "Manrope", -apple-system, sans-serif;
        font-size: 14px;
        font-weight: 800;
        line-height: 1;
        flex-shrink: 0;
      }

      .brand-name {
        font-family: "Manrope", -apple-system, sans-serif;
        font-size: 13px;
        font-weight: 800;
        letter-spacing: -0.4px;
        color: #22251f;
      }

      .brand-auto {
        color: #315fce;
      }

      .divider {
        width: 1px;
        height: 14px;
        background: #ddded5;
      }

      .back-action {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        color: #60635c;
        font-size: 12px;
        font-weight: 600;
        letter-spacing: -0.1px;
        transition: color 140ms ease;
      }

      .widget:hover .back-action {
        color: #315fce;
      }

      .back-arrow {
        display: inline-block;
        transition: transform 160ms ease;
      }

      .widget:hover .back-arrow {
        transform: translateX(-3px);
      }

      /* Dark mode support */
      @media (prefers-color-scheme: dark) {
        .widget {
          background: rgba(30, 32, 36, 0.92);
          color: #f0ede6;
          border-color: rgba(255, 255, 255, 0.12);
          box-shadow: 0 4px 20px rgba(0, 0, 0, 0.35);
        }
        .widget:hover {
          background: rgba(38, 41, 46, 0.98);
          border-color: #5582ea;
          box-shadow: 0 8px 28px rgba(0, 0, 0, 0.5);
        }
        .brand-name {
          color: #f0ede6;
        }
        .brand-auto {
          color: #6993f5;
        }
        .divider {
          background: rgba(255, 255, 255, 0.15);
        }
        .back-action {
          color: #a6a9a0;
        }
        .widget:hover .back-action {
          color: #6993f5;
        }
      }

      /* Responsive compact view */
      @media (max-width: 480px) {
        .brand-name {
          display: none;
        }
        .divider {
          display: none;
        }
        .widget {
          padding: 6px 12px 6px 6px;
        }
      }
    `;

    const link = document.createElement("a");
    link.className = "widget";
    link.href = portalUrl;
    link.setAttribute("aria-label", "Back to autowiki portal");
    link.title = "Back to autowiki portal";

    link.innerHTML = `
      <span class="brand-mark" aria-hidden="true">a</span>
      <span class="brand-name"><span class="brand-auto">auto</span>wiki</span>
      <span class="divider" aria-hidden="true"></span>
      <span class="back-action">
        <span class="back-arrow" aria-hidden="true">←</span>
        <span>Portal</span>
      </span>
    `;

    shadow.appendChild(style);
    shadow.appendChild(link);

    document.body.appendChild(host);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }

  // Handle SPA transitions (such as Material for MkDocs instant loading)
  window.addEventListener("popstate", () => {
    setTimeout(mount, 100);
  });
  window.addEventListener("hashchange", () => {
    setTimeout(mount, 100);
  });

  const observer = new MutationObserver(() => {
    if (!document.getElementById("autowiki-nav-root") && document.body) {
      mount();
    }
  });

  if (document.body) {
    observer.observe(document.body, { childList: true, subtree: false });
  } else {
    document.addEventListener("DOMContentLoaded", () => {
      observer.observe(document.body, { childList: true, subtree: false });
    });
  }
})();
