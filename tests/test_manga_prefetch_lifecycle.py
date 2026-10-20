"""Guard Android's next-chapter prefetch against per-page cancellation storms."""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
MANGA = (ROOT / "android/app/src/main/java/com/webnovel/mobile/MangaScreens.kt") \
    .read_text(encoding="utf-8")


def test_next_chapter_prefetch_keys_on_tail_threshold_not_each_page():
    assert re.search(r"val approachingChapterEnd = p\.count - pageNo <= 3", MANGA)
    effect = re.search(
        r"LaunchedEffect\(ch\.id, p\.count, approachingChapterEnd, localCatalog\) \{"
        r"([\s\S]*?)(?=\n\s*}\n\s*val transformState)",
        MANGA,
    )
    assert effect, "prefetch must be keyed to chapter identity and the tail threshold"
    assert "if (!approachingChapterEnd) return@LaunchedEffect" in effect.group(1)
    assert "pageNo" not in effect.group(0).split("{")[0], \
        "page position must not restart a still-running tail prefetch"
    assert "cache.containsKey(nextCh.id)" in effect.group(1), \
        "a cached next chapter must not be fetched again"
