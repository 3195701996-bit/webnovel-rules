"""All manga clients must agree on the shared transport-to-identity contract."""
import json
from pathlib import Path

from server.state import manga_identity_source, manga_source_aliases


def test_python_identity_matches_android_and_web_golden_fixture():
    fixture_path = (Path(__file__).resolve().parents[1]
                    / "android/app/src/test/resources/manga_identity_parity.json")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert fixture.get("cases"), "shared manga identity fixture must not be empty"
    for case in fixture["cases"]:
        transport = case["transport"]
        assert manga_identity_source(transport) == case["identity"], case["name"]
        assert list(manga_source_aliases(transport)) == case["aliases"], case["name"]
