from httpx import AsyncClient


async def test_register_user(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "admin@example.com",
            "password": "strong-password",
            "full_name": "Admin User",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "admin@example.com"
    assert data["full_name"] == "Admin User"
    assert "hashed_password" not in data


async def test_duplicate_email_fails(client: AsyncClient) -> None:
    payload = {
        "email": "admin@example.com",
        "password": "strong-password",
        "full_name": "Admin User",
    }

    first_response = await client.post("/api/v1/auth/register", json=payload)
    second_response = await client.post("/api/v1/auth/register", json=payload)

    assert first_response.status_code == 201
    assert second_response.status_code == 400
    assert second_response.json()["detail"] == "A user with this email already exists"


async def test_login_works(client: AsyncClient) -> None:
    payload = {
        "email": "admin@example.com",
        "password": "strong-password",
        "full_name": "Admin User",
    }
    await client.post("/api/v1/auth/register", json=payload)

    response = await client.post(
        "/api/v1/auth/login",
        data={
            "username": payload["email"],
            "password": payload["password"],
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["access_token"]
    assert data["token_type"] == "bearer"


async def test_login_works_with_form_email_field(client: AsyncClient) -> None:
    """Browsers often POST ``email`` + ``password`` instead of OAuth2 ``username``."""
    payload = {
        "email": "formemail@example.com",
        "password": "strong-password",
        "full_name": "Form Email",
    }
    await client.post("/api/v1/auth/register", json=payload)

    response = await client.post(
        "/api/v1/auth/login",
        data={"email": payload["email"], "password": payload["password"]},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["access_token"]
    assert data["token_type"] == "bearer"


async def test_login_works_with_json_body(client: AsyncClient) -> None:
    payload = {
        "email": "jsonlogin@example.com",
        "password": "strong-password",
        "full_name": "JSON Login",
    }
    await client.post("/api/v1/auth/register", json=payload)

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["access_token"]
    assert data["token_type"] == "bearer"


async def test_login_json_accepts_username_alias(client: AsyncClient) -> None:
    payload = {
        "email": "jsonuseralias@example.com",
        "password": "strong-password",
        "full_name": "JSON Alias",
    }
    await client.post("/api/v1/auth/register", json=payload)

    response = await client.post(
        "/api/v1/auth/login",
        json={"username": payload["email"], "password": payload["password"]},
    )

    assert response.status_code == 200
    assert response.json()["access_token"]
