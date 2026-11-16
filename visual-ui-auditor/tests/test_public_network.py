"""Exercise real browser interception, including requests initiated after navigation."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.async_api import async_playwright

from backend.security import PUBLIC_BROWSER_ARGS, restrict_public_context


async def test_browser_does_not_reach_network_for_subresources_frames_or_websockets():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"unexpected network request")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    target = f"http://127.0.0.1:{server.server_port}"
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=PUBLIC_BROWSER_ARGS)
            try:
                context = await browser.new_context(service_workers="block", accept_downloads=False)
                # Even a calibration URL pointing at a real listener is fulfilled, never fetched.
                await restrict_public_context(context, target + "/demo")
                page = await context.new_page()
                await page.goto(target + "/demo", wait_until="networkidle")
                result = await page.evaluate("""async target => {
                    const results = [];
                    results.push(await fetch(target + '/fetch').then(() => false).catch(() => true));
                    results.push(await new Promise(resolve => {
                        const img = new Image(); img.onload = () => resolve(false);
                        img.onerror = () => resolve(true); img.src = target + '/image.png';
                    }));
                    const frame = document.createElement('iframe');
                    frame.src = target + '/frame'; document.body.append(frame);
                    results.push(await new Promise(resolve => {
                        const ws = new WebSocket(target.replace('http:', 'ws:') + '/socket');
                        ws.onclose = () => resolve(true); ws.onerror = () => resolve(true);
                        setTimeout(() => resolve(false), 2000);
                    }));
                    navigator.sendBeacon(target + '/beacon', 'blocked');
                    return results;
                }""", target)
                assert result == [True, True, True]
                await page.wait_for_load_state("networkidle")
                assert received == []
                await context.close()
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
