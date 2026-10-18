package com.webnovel.mobile

import androidx.test.platform.app.InstrumentationRegistry
import com.chaquo.python.Python
import java.nio.file.Files
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test

/** Opt-in, emulator-only live source probe. Never run against a user's library. */
class MangaLiveDownloadProbeTest {
    @Test fun selectedChapterDownloadsThroughRealMobileWorker() = runBlocking {
        val inst = InstrumentationRegistry.getInstrumentation()
        assumeTrue("External source requests require explicit opt-in",
            InstrumentationRegistry.getArguments().getString("allowExternal") == "true")
        val qemu = inst.uiAutomation.executeShellCommand("getprop ro.kernel.qemu")
            .use { android.os.ParcelFileDescriptor.AutoCloseInputStream(it).bufferedReader().readText().trim() }
        assertEquals("This probe is forbidden on physical devices", "1", qemu)
        val gateway = EngineGateway(inst.targetContext)
        assertTrue(runBlocking { gateway.connect() } is EngineState.Ready)
        val root = Files.createTempDirectory(inst.targetContext.cacheDir.toPath(), "live-manga-probe-").toFile()
        val ns = Python.getInstance().getModule("builtins").callAttr("dict")
        ns.callAttr("__setitem__", "probe_root", root.absolutePath)
        // The fixture owns every durable file. Only production source/image/
        // worker code is used; no network-success or image fixtures are injected.
        Python.getInstance().getModule("builtins").callAttr("exec", """
            import os, json
            from engine import netproxy
            from engine.manga import download_manager as dm
            from server import diag, state
            old_proxy = netproxy.current_proxy_raw()
            old_images = dm.images_resolver
            old_library_hook = dm.library_change_hook
            old_cache_root = state.MANGA_CACHE_DIR
            try:
                netproxy.set_proxy('http://10.0.2.2:7897', persist=False)
                state.MANGA_CACHE_DIR = os.path.join(probe_root, 'cache')
                dm.images_resolver = state._get_chapter_images
                dm.library_change_hook = None
                manager = dm.DownloadManager(state_file=os.path.join(probe_root, 'tasks.json'))
                manager.configure(downloads_root=os.path.join(probe_root, 'downloads'),
                                  state_dir=os.path.join(probe_root, 'state'),
                                  library_file=os.path.join(probe_root, 'library.json'))
                key = 'copymanga:shezhangyujiuyuxing'
                chapter = 'be6236cb-c985-11f1-80bc-fa163e02432f'
                manager._tasks[key] = dict(source='copymanga', comic_id='shezhangyujiuyuxing',
                    title='live probe', status='running', chapters=[chapter], paused=False,
                    cover='https://probe.invalid/cover.jpg')
                manager._active = 1
                manager._worker_alive.add(key)
                manager._worker(key)
                result = manager.status(key)
                summary = json.dumps(dict(status=result.get('status'), done=result.get('done'),
                    total=result.get('total'), images_done=result.get('images_done'),
                    error=result.get('error'), events=diag.events()['errors']), ensure_ascii=False)
                if result.get('status') == 'done':
                    from PIL import Image
                    page_dir = os.path.join(probe_root, 'downloads', 'copymanga', 'shezhangyujiuyuxing', chapter)
                    pages = [os.path.join(page_dir, name) for name in os.listdir(page_dir)
                             if name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.avif'))]
                    assert len(pages) == 30, len(pages)
                    for page in pages:
                        with Image.open(page) as image:
                            image.verify()
            finally:
                dm.images_resolver = old_images
                dm.library_change_hook = old_library_hook
                state.MANGA_CACHE_DIR = old_cache_root
                netproxy.set_proxy(old_proxy, persist=False)
        """.trimIndent(), ns)
        val summary = ns.callAttr("__getitem__", "summary").toString()
        println("MANGA_LIVE_PROBE $summary fixture=${root.absolutePath}")
        val result = org.json.JSONObject(summary)
        assertEquals(summary, "done", result.getString("status"))
        assertEquals(summary, 1, result.getInt("done"))
        assertEquals(summary, 30, result.getInt("images_done"))
        // Preserve failed evidence. Only a fully successful isolated fixture is removed.
        assertTrue("Probe fixture cleanup failed", root.deleteRecursively())
    }
}
