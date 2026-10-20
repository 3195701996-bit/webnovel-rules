package com.webnovel.mobile

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/** Verifies the Android/Chaquopy requests fallback reuses keep-alive across short-lived threads. */
@RunWith(AndroidJUnit4::class)
class MangaRequestsPoolAndroidTest {
    private lateinit var gateway: EngineGateway

    @Before
    fun startEngine() = runBlocking {
        gateway = EngineGateway(InstrumentationRegistry.getInstrumentation().targetContext)
        val state = gateway.connect()
        assertTrue("Embedded Python engine must start: $state", state is EngineState.Ready)
    }

    @After
    fun stopEngine() {
        runCatching { runBlocking { gateway.stopEngine() } }
    }

    @Test
    fun requestsFallbackReusesConnectionAcrossShortLivedThreads() {
        val python = com.chaquo.python.Python.getInstance()
        val code = """
            import concurrent.futures
            import http.server
            import threading
            from engine.manga import downloader as d

            seen = set()
            seen_lock = threading.Lock()
            class Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = 'HTTP/1.1'
                def do_GET(self):
                    with seen_lock:
                        seen.add(id(self.connection))
                    payload = b'ok'
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                def log_message(self, *args):
                    pass

            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            old_engine = d._curl_engine_available
            old_public = d.url_is_public_resolved
            old_pin = d.pin_requests_session
            server_thread.start()
            try:
                d._curl_engine_available = lambda: False
                d.url_is_public_resolved = lambda _url: (True, '')
                d.pin_requests_session = lambda *args, **kwargs: True
                url = 'http://127.0.0.1:%d/page' % server.server_address[1]
                def fetch(_):
                    response = d.fetch_image_checked(url, {}, timeout=4)
                    assert response.status_code == 200 and response.content == b'ok'
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(fetch, 1).result(timeout=6)
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(fetch, 2).result(timeout=6)
                assert len(seen) == 1, 'requests did not reuse TCP keep-alive across threads'
            finally:
                d._curl_engine_available = old_engine
                d.url_is_public_resolved = old_public
                d.pin_requests_session = old_pin
                server.shutdown()
                server.server_close()
        """.trimIndent()
        python.getModule("builtins").callAttr(
            "exec", code, python.getModule("builtins").callAttr("dict"))
    }
}
