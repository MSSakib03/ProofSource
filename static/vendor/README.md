# Vendored front-end libraries

Served from the tool itself (not from a third-party CDN), so that no visitor's IP address or
browser details are sent to another site — as the Wikimedia Cloud Services terms ask of tools —
and so the Content-Security-Policy can stay `script-src 'self'`.

| File | Package | Licence | Source |
|---|---|---|---|
| `vue-3.4.21.global.prod.js` | vue@3.4.21 (`dist/vue.global.prod.js`) | MIT | https://registry.npmjs.org/vue/-/vue-3.4.21.tgz |
| `dompurify-3.2.6.min.js` | dompurify@3.2.6 (`dist/purify.min.js`) | MPL-2.0 OR Apache-2.0 | https://registry.npmjs.org/dompurify/-/dompurify-3.2.6.tgz |

To upgrade, download the new package tarball from the npm registry, copy the same `dist` file
here under a new versioned name, and update the `<script>` tags in `templates/index.html`.
