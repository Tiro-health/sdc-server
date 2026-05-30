"""The root path content-negotiates: a browser is redirected to the Swagger UI,
an API client gets a small JSON pointer."""


def test_root_redirects_browser_to_docs(client):
    """A browser (Accept: text/html) is redirected to the Swagger UI."""
    r = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/docs"


def test_root_returns_json_pointer_for_api_clients(client):
    """An API client (no explicit text/html, e.g. `*/*` or application/json) gets
    a JSON pointer rather than a redirect into an HTML page."""
    for accept in ("application/json", "*/*"):
        r = client.get("/", headers={"accept": accept}, follow_redirects=False)
        assert r.status_code == 200, accept
        assert r.json()["docs"] == "/docs"
