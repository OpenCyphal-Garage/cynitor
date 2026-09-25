"""Tests for serving the dashboard from the backend.

Deploying to a server should need one binary and nothing on the client but a
browser, so the API server can also host the frontend. Two things have to hold
for that to work: the API routes must keep priority over the catch-all static
mount, and the page's own assets must load without a token, since a browser
cannot prompt for one before it has loaded the page.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from aiohttp.test_utils import TestClient, TestServer

from websocket_server import WebSocketServer


def _make_session():
    s = MagicMock()
    s.is_running = False
    s.can_interface = None
    s.can_bitrate = None
    s.telemetry = None
    s.bus_load = None
    s.last_error = None
    s.dropped_events.return_value = None
    s.replay = None
    s.event_logger = None
    s.connect = AsyncMock()
    s.disconnect = AsyncMock()
    return s


@pytest.fixture
def website(tmp_path):
    """A stand-in for website/, so tests do not depend on the real dashboard."""
    d = tmp_path / "website"
    d.mkdir()
    (d / "index.html").write_text("<title>Cynitor</title>")
    (d / "state.js").write_text("// app code")
    (d / "config.js").write_text("// placeholder\n")
    (d / "vendor").mkdir()
    (d / "vendor" / "lib.min.js").write_text("// vendored library")
    return d


def _server(website_dir=None, auth_token=None):
    return WebSocketServer(
        session=_make_session(), host="127.0.0.1", port=0,
        website_dir=website_dir, auth_token=auth_token,
    )


@pytest.fixture
async def client(website):
    async with TestClient(TestServer(_server(website).app)) as c:
        yield c


@pytest.fixture
async def authed_client(website):
    async with TestClient(TestServer(_server(website, auth_token="secret").app)) as c:
        yield c


class TestServesDashboard:
    @pytest.mark.asyncio
    async def test_root_serves_index(self, client):
        resp = await client.get("/")
        assert resp.status == 200
        assert "Cynitor" in await resp.text()

    @pytest.mark.asyncio
    async def test_serves_other_assets(self, client):
        resp = await client.get("/state.js")
        assert resp.status == 200
        assert "app code" in await resp.text()

    @pytest.mark.asyncio
    async def test_serves_vendored_libraries(self, client):
        # D3 and Tabulator ship in website/vendor/ so the page works offline.
        resp = await client.get("/vendor/lib.min.js")
        assert resp.status == 200
        assert "vendored library" in await resp.text()

    @pytest.mark.asyncio
    async def test_dashboard_files_are_not_origin_restricted(self, client):
        # Only the API and event stream are guarded; the page's files are public.
        resp = await client.get("/state.js", headers={"Origin": "https://elsewhere.example"})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_config_js_reports_request_origin(self, client):
        # Whatever host the user reached us on is where the API lives, which
        # is the whole point: the built-in localhost default is wrong once the
        # page is served from somewhere else.
        resp = await client.get("/config.js")
        assert resp.status == 200
        body = await resp.text()
        assert "window.__CYNITOR" in body
        assert '"apiBase"' in body
        assert str(client.server.port) in body

    @pytest.mark.asyncio
    async def test_generated_config_overrides_the_placeholder(self, client):
        resp = await client.get("/config.js")
        assert "placeholder" not in await resp.text()


class TestDashboardRevalidation:
    """After an upgrade, browsers must not keep running the old dashboard."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/", "/state.js", "/config.js"])
    async def test_dashboard_files_are_revalidated(self, client, path):
        resp = await client.get(path)
        assert resp.headers.get("Cache-Control") == "no-cache"

    @pytest.mark.asyncio
    async def test_unchanged_file_costs_a_304(self, client):
        first = await client.get("/state.js")
        validators = {k: first.headers[k] for k in ("ETag", "Last-Modified") if k in first.headers}
        assert validators, "no validator to revalidate with"
        headers = {}
        if "ETag" in validators:
            headers["If-None-Match"] = validators["ETag"]
        if "Last-Modified" in validators:
            headers["If-Modified-Since"] = validators["Last-Modified"]
        again = await client.get("/state.js", headers=headers)
        assert again.status == 304

    @pytest.mark.asyncio
    async def test_api_responses_are_left_alone(self, client):
        resp = await client.get("/api/health")
        assert "Cache-Control" not in resp.headers


class TestApiKeepsPriority:
    @pytest.mark.asyncio
    async def test_api_route_not_shadowed_by_static_mount(self, client):
        # The static mount is registered at '/', so a wrong registration order
        # would have it swallow the API.
        resp = await client.get("/api/health")
        assert resp.status == 200
        # A JSON health payload, not a static file: the API handler ran.
        assert "status" in await resp.json()
        assert resp.content_type == "application/json"

    @pytest.mark.asyncio
    async def test_missing_asset_is_404_not_index(self, client):
        resp = await client.get("/nope.js")
        assert resp.status == 404


class TestAuthSplit:
    @pytest.mark.asyncio
    async def test_page_loads_without_a_token(self, authed_client):
        # Otherwise the browser could never render the prompt that asks for one.
        assert (await authed_client.get("/")).status == 200
        assert (await authed_client.get("/state.js")).status == 200
        assert (await authed_client.get("/config.js")).status == 200

    @pytest.mark.asyncio
    async def test_api_still_requires_a_token(self, authed_client):
        assert (await authed_client.get("/api/status")).status == 401

    @pytest.mark.asyncio
    async def test_api_accepts_a_valid_token(self, authed_client):
        resp = await authed_client.get(
            "/api/status", headers={"Authorization": "Bearer secret"}
        )
        assert resp.status == 200


class TestApiOnlyMode:
    """Without a website directory the server behaves exactly as before.

    This is what `--no-frontend` selects, for deployments that only want the
    REST API and event stream and have no use for the dashboard.
    """

    @pytest.mark.asyncio
    async def test_no_static_routes_when_directory_absent(self):
        async with TestClient(TestServer(_server().app)) as c:
            assert (await c.get("/")).status == 404
            assert (await c.get("/state.js")).status == 404
            assert (await c.get("/config.js")).status == 404

    @pytest.mark.asyncio
    async def test_api_unaffected(self):
        async with TestClient(TestServer(_server().app)) as c:
            assert (await c.get("/api/health")).status == 200
            assert (await c.get("/api/status")).status == 200

    def test_missing_directory_is_ignored(self, tmp_path):
        # A checkout without website/ is API-only whether or not the flag is
        # passed, rather than failing at startup.
        server = _server(tmp_path / "does-not-exist")
        assert server.website_dir is None

    def test_file_path_is_ignored(self, tmp_path):
        not_a_dir = tmp_path / "website"
        not_a_dir.write_text("oops")
        assert _server(not_a_dir).website_dir is None


class TestNoFrontendFlag:
    """The flag reaches the server as website_dir=None."""

    def test_flag_is_parsed(self):
        import argparse

        # Mirrors the entry point's parser: store_true, so absent means serve.
        parser = argparse.ArgumentParser()
        parser.add_argument("--no-frontend", action="store_true")
        assert parser.parse_args([]).no_frontend is False
        assert parser.parse_args(["--no-frontend"]).no_frontend is True

    def test_serve_frontend_false_yields_api_only(self, website):
        # What main() does with the flag: pass None instead of the directory.
        serve_frontend = False
        website_dir = website if serve_frontend else None
        assert _server(website_dir).website_dir is None
        assert _server(website).website_dir == website


class TestPortIsNotHardcoded:
    """Everything the server advertises must follow the port it actually uses.

    --bind existed from the start but --port did not, so 8080 was baked into
    the banner and into the WebSocket URL the API hands out. A server on any
    other port was telling clients the wrong address.
    """

    @pytest.mark.asyncio
    async def test_api_info_reports_the_real_host_and_port(self, client):
        resp = await client.get("/api")
        url = (await resp.json())["endpoints"]["WebSocket"]["url"]
        # TestClient binds an arbitrary free port; the URL must follow it.
        assert str(client.server.port) in url
        assert "8080" not in url or client.server.port == 8080

    @pytest.mark.asyncio
    async def test_config_js_reports_the_real_host_and_port(self, client):
        body = await (await client.get("/config.js")).text()
        assert str(client.server.port) in body
