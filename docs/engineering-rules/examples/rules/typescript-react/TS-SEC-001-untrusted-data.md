# TS-SEC-001: Render untrusted data only through safe sinks

- **Severity:** MUST
- **Category:** security
- **Applies to:** TypeScript / React — rendering, links, dynamic code, cross-window messaging

## Rule

1. Untrusted data (API responses, URL parameters, user input, `postMessage` payloads, third-party content) MUST be rendered as JSX text/attributes, which React escapes.
2. `dangerouslySetInnerHTML` MUST NOT be used with untrusted data unless the HTML is sanitized immediately before use with a maintained sanitizer (e.g. DOMPurify). Each use MUST carry an ESLint disable comment with justification.
3. URLs built from untrusted data and placed in `href`, `src`, `action` or passed to `window.location` / `window.open` MUST be parsed and checked against an allowlist of schemes (`https:`, and `http:`/`mailto:` only if needed). `javascript:` and `data:` URLs MUST be rejected.
4. Code MUST NOT pass untrusted strings to `eval`, `new Function`, `setTimeout(string)`, `setInterval(string)`, or direct DOM sinks (`element.innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`).
5. `message` event handlers MUST check `event.origin` against an allowlist before using `event.data`.

## Why

Cross-site scripting lets an attacker run code with the user's session: read data, perform actions, steal tokens. React escapes text by default, but the escape hatches above bypass that protection entirely (OWASP A05:2025 Injection; CWE-79).

## Good example

```tsx
import DOMPurify from "dompurify";

export function Comment({ author, body }: { readonly author: string; readonly body: string }) {
  // Text is escaped by React.
  return (
    <p>
      <strong>{author}</strong>: {body}
    </p>
  );
}

export function RichArticle({ html }: { readonly html: string }) {
  const clean = DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
  // eslint-disable-next-line react/no-danger -- HTML is sanitized with DOMPurify on the line above (TS-SEC-001)
  return <article dangerouslySetInnerHTML={{ __html: clean }} />;
}

const ALLOWED_PROTOCOLS = new Set(["https:", "mailto:"]);

export function safeUrl(raw: string): string | undefined {
  try {
    const url = new URL(raw, window.location.origin);
    return ALLOWED_PROTOCOLS.has(url.protocol) ? url.toString() : undefined;
  } catch {
    return undefined;
  }
}

export function ProfileLink({ website }: { readonly website: string }) {
  const href = safeUrl(website);
  if (href === undefined) return <span>{website}</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {website}
    </a>
  );
}

const TRUSTED_ORIGINS = new Set(["https://widgets.example.com"]);

window.addEventListener("message", (event: MessageEvent<unknown>) => {
  if (!TRUSTED_ORIGINS.has(event.origin)) return;
  handleWidgetMessage(event.data); // still validate the payload shape (TS-CODE-001)
});
```

## Bad example

```tsx
export function Comment({ body }: { body: string }) {
  return <div dangerouslySetInnerHTML={{ __html: body }} />; // stored XSS
}

export function ProfileLink({ website }: { website: string }) {
  return <a href={website}>site</a>; // "javascript:..." executes on click in many setups
}

function applyFormula(expr: string) {
  return eval(expr); // arbitrary code execution
}

window.addEventListener("message", (e) => {
  document.getElementById("out")!.innerHTML = e.data; // any origin, raw HTML sink
});
```

## Enforcement

| Method | How |
|---|---|
| Linter (eslint-plugin-react) | `react/no-danger` (error), `react/jsx-no-script-url` (error), `react/jsx-no-target-blank` (error). |
| Linter (ESLint core / typescript-eslint) | `no-eval`, `no-new-func`, `no-implied-eval` / `@typescript-eslint/no-implied-eval`. |
| SAST | CodeQL JavaScript/TypeScript queries (e.g. DOM-based XSS) or Semgrep rules on PRs. |
| Browser | Content-Security-Policy without `'unsafe-inline'`/`'unsafe-eval'` as defense-in-depth. |
| Code review | Every `eslint-disable` for `react/no-danger` has a justification and a sanitizer call. |

```js
// eslint.config.js (excerpt)
import { defineConfig } from "eslint/config";
import react from "eslint-plugin-react";

export default defineConfig({
  plugins: { react },
  settings: { react: { version: "detect" } },
  rules: {
    "react/no-danger": "error",
    "react/jsx-no-script-url": "error",
    "react/jsx-no-target-blank": "error",
    "no-eval": "error",
    "no-new-func": "error",
  },
});
```

> Compatibility: eslint-plugin-react 7.37.x declares peer support for ESLint up to 9.x. Pin ESLint 9 in projects that use it until the plugin declares ESLint 10 support.

## References

- React — `dangerouslySetInnerHTML`: https://react.dev/reference/react-dom/components/common#dangerously-setting-the-inner-html
- OWASP Cross Site Scripting Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html
- OWASP DOM based XSS Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/DOM_based_XSS_Prevention_Cheat_Sheet.html
- OWASP HTML5 Security Cheat Sheet (postMessage): https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html
- DOMPurify: https://github.com/cure53/DOMPurify
- eslint-plugin-react rules: https://github.com/jsx-eslint/eslint-plugin-react#list-of-supported-rules
- MDN — Content Security Policy: https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CSP
- CWE-79: https://cwe.mitre.org/data/definitions/79.html
