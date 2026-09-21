from fastapi.testclient import TestClient

from minion.api import create_app


def test_health_ready_and_api_key(settings) -> None:
    settings.api_key = "secret"
    app = create_app(settings)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200

        unauthorized = client.get("/tasks/not-found")
        assert unauthorized.status_code == 401

        authorized = client.get(
            "/tasks/not-found",
            headers={"x-minion-api-key": "secret"},
        )
        assert authorized.status_code == 404

        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "minion_tasks_submitted_total" in metrics.text
