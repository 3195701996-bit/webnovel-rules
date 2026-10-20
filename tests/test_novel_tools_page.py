"""Keep low-frequency novel tools reachable without crowding daily reading."""

from html.parser import HTMLParser


class ToolsMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.details = []
        self.buttons = {}
        self.closed_tools = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "details":
            self.details.append(attrs.get("id"))
            if attrs.get("id") == "novel-tools":
                self.closed_tools = "open" not in attrs
        if tag == "button" and attrs.get("id"):
            identity = attrs["id"]
            assert identity not in self.buttons, f"Duplicate tool identity: {identity}"
            self.buttons[identity] = tuple(self.details)

    def handle_endtag(self, tag):
        if tag == "details":
            self.details.pop()


def test_novel_tools_default_closed_and_preserve_existing_button_identities():
    import app

    response = app.app.test_client().get("/novel_detail?key=fixture")
    assert response.status_code == 200
    markup = ToolsMarkup()
    markup.feed(response.get_data(as_text=True))
    assert markup.closed_tools
    for identity in ("recovery-btn", "reader-export-btn", "metadata-btn"):
        assert markup.buttons[identity] == ("novel-tools",)
    for identity in ("read-btn", "check-btn", "clean-btn", "del-btn"):
        assert markup.buttons[identity] == ()
