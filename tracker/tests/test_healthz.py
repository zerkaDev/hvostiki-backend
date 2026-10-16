"""Служебные эндпоинты: проверка живости процесса (healthcheck nginx/compose, мониторинг)."""
import pytest


@pytest.mark.django_db
class TestHealthz:
    def test_returns_ok_without_auth(self, api_client):
        response = api_client.get('/healthz')

        assert response.status_code == 200
        assert response.json() == {'status': 'ok'}

    def test_does_not_require_trailing_slash(self, api_client):
        # healthcheck и nginx ходят ровно на /healthz: адрес без слэша — основной.
        assert api_client.get('/healthz').status_code == 200
        assert api_client.get('/healthz/').status_code == 404
