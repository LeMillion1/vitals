"""Browser tests: what the product does, as somebody using it experiences it.

Everything below the browser is already covered — services, routes, policy — and
this branch keeps finding the same shape of defect underneath all of it: a
feature whose service is right, whose route answers 200, and which no person can
actually use. A support grant a patient approves and an administrator then
cannot open. A console with no link. A page that renders a reply above the
message it answers. Nothing without a browser can tell those apart from working.

These are skipped unless Playwright and a Chromium are present, so the ordinary
suite is unaffected. Run them with::

    pip install playwright            # PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 if
    python -m playwright install chromium   # a Chromium is already cached
    pytest tests/ui -m ui
"""
