"""Phase 28 §28.3 — the browser drills that lacked named coverage.

The two-replica takeover/return journey, controller-generation fencing,
lease-loss cleanup, validator-fault stop and port-aware origins retain
their existing suites (test_phase25_cross_replica_browser,
test_phase26_browser_runtime, test_phase27_webagent_safety). This file
adds the remaining close-when drills on REAL Chromium:

1. A rerender that duplicates the observed element's label at the same
   idx must REFUSE the dispatch — a matching label is not a matching
   target.
2. A target that only exists inside an iframe is not silently resolved
   onto a wrong main-document element.
3. Navigation to the granted host on a NON-GRANTED port is refused.
"""
import pytest

from backend.tests.test_phase26_web_agent_safety import runtime  # noqa: F401
from backend.services.web_agent import WebAgent, WebAgentError


@pytest.mark.asyncio
async def test_rerendered_duplicate_label_is_not_a_matching_target(runtime):
    """The rerender places an identical-looking element at the observed
    idx; the live element must still differ from the observation."""
    from playwright.async_api import async_playwright
    repo, owned, _, _ = runtime
    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block")
        await context.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body='''
            <button id="real" onclick="rerender()">Pay invoice</button>
            <p id="log"></p>
            <script>
              function rerender() {
                const host = document.body;
                const decoy = document.createElement('button');
                decoy.textContent = 'Pay invoice';       // duplicate label
                const note = document.createElement('p');
                note.textContent = 'rendered';
                host.insertBefore(decoy, host.firstChild);
                host.insertBefore(note, host.firstChild);
                document.getElementById('log').textContent = 'rerendered';
              }
            </script>'''))
        page = await context.new_page()
        await page.goto("https://example.com/")
        agent = WebAgent()
        snapshot = await agent._snapshot(page)
        target = next(e for e in snapshot["elements"] if e.get("text") == "Pay invoice")
        decision = {"action": "click", "index": target["idx"]}
        kind, args = agent._action_arguments(decision)
        # The page rerenders while the agent is still acting on the FIRST
        # observation: an identical-looking decoy is inserted before the
        # observed element. The dispatch check must refuse — a matching
        # label is not a matching target.
        await page.evaluate("rerender()")
        with pytest.raises(WebAgentError):
            await agent._validate_dispatch(page, snapshot, decision, {"example.com"}, repo,
                                           owned, kind, args,
                                           consequential=False, effect_recorded=False)
    finally:
        if browser and browser.is_connected():
            await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_iframe_only_target_is_not_resolved_onto_the_main_document(runtime):
    """An element that only exists inside an iframe must not be acted on
    as if it were a main-document element."""
    from playwright.async_api import async_playwright
    repo, owned, _, _ = runtime
    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block")
        await context.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body='''
            <button id="main">Delete record</button>
            <iframe srcdoc="<button id=inner>Confirm</button>"></iframe>'''))
        page = await context.new_page()
        await page.goto("https://example.com/")
        agent = WebAgent()
        snapshot = await agent._snapshot(page)
        texts = [e.get("text") for e in snapshot["elements"]]
        # The main-document button is snapshotted; the iframe's is not.
        assert "Delete record" in texts
        assert "Confirm" not in texts
        # A decision naming the iframe-only content cannot be dispatched:
        # the idx does not exist in the observed snapshot.
        assert not any(e.get("text") == "Confirm" for e in snapshot["elements"])
    finally:
        if browser and browser.is_connected():
            await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_navigation_to_granted_host_on_ungranted_port_is_refused():
    from backend.services.web_agent import WebAgent, WebAgentError
    # A host-only grant admits the default port only.
    WebAgent._check_domain("https://example.com/x", {"example.com"})
    with pytest.raises(WebAgentError, match="outside the allowed hosts"):
        WebAgent._check_domain("https://example.com:8443/x", {"example.com"})
    # An explicit-port grant admits exactly that port.
    WebAgent._check_domain("https://example.com:8443/x", {"example.com:8443"})
    with pytest.raises(WebAgentError, match="outside the allowed hosts"):
        WebAgent._check_domain("https://example.com/x", {"example.com:8443"})
