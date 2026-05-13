from httpx import AsyncClient


async def test_users_me_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/users/me")

    assert response.status_code == 401


async def test_users_me_works_with_token(client: AsyncClient) -> None:
    user_payload = {
        "email": "admin@example.com",
        "password": "strong-password",
        "full_name": "Admin User",
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

    response = await client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["email"] == user_payload["email"]
    assert data["full_name"] == user_payload["full_name"]
