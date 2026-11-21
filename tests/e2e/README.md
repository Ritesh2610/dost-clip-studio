# Interface test

Drives the real page in Chromium against a mock of the HTTP API.

```
npm install --no-save playwright
npx playwright install chromium
node tests/e2e/ui.test.mjs
```

It starts and stops its own mock server on ports 5412 and 5413. This test is not part of CI.
