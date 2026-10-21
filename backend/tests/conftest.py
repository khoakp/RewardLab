import pytest
from fastapi.testclient import TestClient

from app.core.policy import PolicyEvaluator
from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def policy_evaluator() -> PolicyEvaluator:
    return PolicyEvaluator(record_audit=True)
