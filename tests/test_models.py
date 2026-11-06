import pytest

from coordinator.app.main import Settings
from grid_helpers import ADMIN, USER, auth, enroll, submit


def test_unapproved_model_is_rejected_for_jobs_and_enrollment(api):
    r = api.post("/api/jobs", headers=auth(USER), json={
        "model": "some-other-model", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 422 and "not approved" in r.text
    r = api.post("/api/admin/workers", headers=auth(ADMIN), json={"name": "x", "model": "some-other-model"})
    assert r.status_code == 422
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100


def test_operator_can_approve_additional_models(make_api):
    api = make_api(allowed_models=("demo-model", "qwen2.5-coder:1.5b"))
    enroll(api, "small", model="qwen2.5-coder:1.5b")
    assert submit(api, model="qwen2.5-coder:1.5b").status_code == 201


def test_allowed_models_parsed_from_env(monkeypatch):
    monkeypatch.setenv("CAG_ALLOWED_MODELS", " demo-model , qwen2.5-coder:1.5b ,")
    assert Settings.from_env().allowed_models == ("demo-model", "qwen2.5-coder:1.5b")


@pytest.mark.parametrize("models", [(), ("bad model!",)])
def test_invalid_allowlist_is_rejected(models):
    with pytest.raises(RuntimeError, match="CAG_ALLOWED_MODELS"):
        Settings(admin_key=ADMIN, user_key=USER, db_path="unused", allowed_models=models).validate()
