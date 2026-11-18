"""Observium API answers that are not the API: an SSO portal in front of the server, an HTML page."""

import httpx
import pytest

from app.collectors.observium.client import ObserviumClient, ObserviumError


def _client(handler) -> ObserviumClient:
    return ObserviumClient(base_url="https://o.lab:44343", user="u", password="p", transport=httpx.MockTransport(handler))


def test_should_explain_the_sso_portal_when_the_api_call_is_redirected_to_a_login_page():
    """observium-svn behind F5 APM: every API call answers 302 to portal…/F5Networks-SSO-Req."""
    client = _client(lambda request: httpx.Response(302, headers={"location": "https://portal.lab/F5Networks-SSO-Req?x=1"}))
    with pytest.raises(ObserviumError, match="portal login/SSO"):
        client._get("/devices/", {"pagesize": 1})


def test_should_raise_an_observium_error_when_the_answer_is_html_instead_of_json():
    client = _client(lambda request: httpx.Response(200, html="<!DOCTYPE html><html></html>"))
    with pytest.raises(ObserviumError, match="text/html, bukan JSON"):
        client._get("/devices/", {"pagesize": 1})
