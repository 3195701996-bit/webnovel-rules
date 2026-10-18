from pathlib import Path
import xml.etree.ElementTree as ET

RES = Path(__file__).resolve().parents[1] / "android/app/src/main/res"


def styles(folder):
    return {style.get("name"): style for style in ET.parse(RES / folder / "themes.xml").getroot()}


def values(style):
    return {item.get("name"): item.text for item in style.findall("item")}


def test_warm_chrome_has_dark_status_icons_and_safe_legacy_navigation():
    theme = styles("values")
    base = theme["Theme.MobileServer.Base"]
    assert base.get("parent") == "android:Theme.Material.Light.NoActionBar"
    items = values(base)
    assert items["android:windowLightStatusBar"] == "true"
    assert items["android:statusBarColor"] == "#FBF8F2"
    assert items["android:windowBackground"] == "#F5F1E9"
    assert items["android:navigationBarColor"] == "#0F1115"
    assert theme["Theme.MobileServer"].get("parent") == "Theme.MobileServer.Base"


def test_api26_plus_switches_navigation_background_and_icons_together():
    theme = styles("values-v26")["Theme.MobileServer"]
    assert theme.get("parent") == "Theme.MobileServer.Base"
    assert values(theme) == {"android:navigationBarColor": "#FBF8F2",
                             "android:windowLightNavigationBar": "true"}
