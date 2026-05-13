from httpx import AsyncClient


async def _auth_headers(client: AsyncClient) -> dict[str, str]:
    user_payload = {
        "email": "websites@example.com",
        "password": "strong-password",
        "full_name": "Website User",
    }
    await client.post("/api/v1/auth/register", json=user_payload)
    login_response = await client.post(
        "/api/v1/auth/login",
        data={
            "username": user_payload["email"],
            "password": user_payload["password"],
        },
    )
    token = login_response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_create_website_never_frequency(client: AsyncClient) -> None:
    headers = await _auth_headers(client)

    response = await client.post(
        "/api/v1/websites",
        headers=headers,
        json={
            "url": "https://example.com",
            "name": "Example",
            "frequency": "never",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["url"] == "https://example.com/"
    assert data["frequency"] == "never"
    assert data["next_crawl_at"] is None


async def test_create_website_daily_frequency_sets_next_crawl(client: AsyncClient) -> None:
    headers = await _auth_headers(client)

    response = await client.post(
        "/api/v1/websites",
        headers=headers,
        json={
            "url": "https://example.com",
            "name": "Example",
            "frequency": "1d",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["frequency"] == "1d"
    assert data["next_crawl_at"] is not None


async def test_create_website_only_once_sets_next_crawl(client: AsyncClient) -> None:
    headers = await _auth_headers(client)

    response = await client.post(
        "/api/v1/websites",
        headers=headers,
        json={
            "url": "https://example.com",
            "name": "Example",
            "frequency": "only_once",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["frequency"] == "only_once"
    assert data["next_crawl_at"] is not None


async def test_update_website_frequency(client: AsyncClient) -> None:
    headers = await _auth_headers(client)
    create_response = await client.post(
        "/api/v1/websites",
        headers=headers,
        json={"url": "https://example.com", "frequency": "never"},
    )
    website_id = create_response.json()["id"]

    response = await client.patch(
        f"/api/v1/websites/{website_id}",
        headers=headers,
        json={"frequency": "12h"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["frequency"] == "12h"
    assert data["next_crawl_at"] is not None
