import pytest


@pytest.mark.asyncio
async def test_auth_login_preflight_allows_loopback_origin_not_in_allowlist(client):
    """Browsers send Origin as full URL; 127.0.0.1 vs localhost must both work in dev/test."""
    response = await client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": "http://127.0.0.1:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
