"""Unit tests for the parts that need no cluster: the vault, the answers API
(secrets never come back), validation, redaction, overlays and the plans."""

import importlib
import time

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTALLER_DATA", str(tmp_path))
    from installer import vault
    importlib.reload(vault)
    from installer import app as appmod
    importlib.reload(appmod)
    appmod._session.update(answers=None, passphrase=None)
    return TestClient(appmod.app), appmod


def test_vault_round_trip_and_wrong_passphrase(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTALLER_DATA", str(tmp_path))
    from installer import vault
    importlib.reload(vault)
    vault.save({"repos": {"token": "ghp_secret"}}, "correct horse")
    assert vault.load("correct horse") == {"repos": {"token": "ghp_secret"}}
    assert b"ghp_secret" not in (tmp_path / "answers.enc").read_bytes()
    with pytest.raises(vault.VaultLocked):
        vault.load("wrong passphrase")


def test_answers_never_return_secrets_and_empty_keeps_them(api):
    client, appmod = api
    assert client.post("/api/unlock", json={"passphrase": "short"}).status_code == 400
    assert client.post("/api/unlock", json={"passphrase": "longenough1", "new": True}).status_code == 200
    body = {"repos": {"owner": "me", "token": "ghp_abcdefghij"}, "ai": {"mode": "cloud", "api_key": "sk-123456789"}}
    out = client.put("/api/answers", json={"answers": body}).json()["answers"]
    assert out["repos"]["token"] == "" and out["repos"]["token_saved"] is True
    assert "sk-123456789" not in str(out)
    # the wizard sends the masked copy back: the secrets must survive
    client.put("/api/answers", json={"answers": out})
    assert appmod._session["answers"]["repos"]["token"] == "ghp_abcdefghij"
    assert appmod._session["answers"]["ai"]["api_key"] == "sk-123456789"
    # a fresh process unlocks the same answers with the passphrase
    appmod._session.update(answers=None, passphrase=None)
    assert client.post("/api/unlock", json={"passphrase": "wrongwrong"}).status_code == 403
    assert client.post("/api/unlock", json={"passphrase": "longenough1"}).status_code == 200
    assert appmod._session["answers"]["repos"]["token"] == "ghp_abcdefghij"


def test_validate_lists_what_is_missing():
    from installer.app import validate
    miss = validate({"target": {"kind": "eks", "eks": {}}, "ai": {"mode": "cloud"}})
    assert "AWS region" in miss and "AWS credentials" in miss and "GitHub token" in miss
    assert "the AI provider and its key" in miss and "at least one admin" in miss
    ok = {"target": {"kind": "local", "context": "minikube"}, "repos": {"owner": "o", "token": "t"},
          "ai": {"mode": "same_machine", "endpoint": "http://x:1234/v1"}, "admins": ["a"]}
    assert validate(ok) == []


def test_run_log_redacts_secrets():
    from installer import runner
    seen = []

    def step(ctx):
        ctx.log("token is ghp_supersecret and key sk-abc12345")
        seen.append(1)

    run = runner.start("install", [runner.Step("s", "S", step)], {},
                       ["ghp_supersecret", "sk-abc12345"], ".")
    for _ in range(50):
        if run.status == "done":
            break
        time.sleep(0.05)
    assert run.status == "done" and seen
    text = " ".join(line["text"] for line in run.lines)
    assert "ghp_supersecret" not in text and "sk-abc12345" not in text and "••••" in text


def test_secret_values_cover_every_secret():
    from installer.plans import ensure_generated, secret_values
    a = {"repos": {"token": "T1"}, "ai": {"api_key": "K1", "ts_client_secret": "S1"},
         "oauth": {"client_secret": "O1"},
         "target": {"ssh": {"sudo_password": "P1", "private_key": "-----BEGIN-----\n" + "A" * 64 + "\n-----END-----"},
                    "eks": {"aws_secret_access_key": "A1"}}}
    ensure_generated(a)
    vals = secret_values(a)
    for v in ("T1", "K1", "S1", "O1", "P1", "A1", "A" * 64, a["generated"]["postgres_password"],
              a["generated"]["settings_key"], a["generated"]["jenkins_password"]):
        assert v in vals
    pw = a["generated"]["postgres_password"]
    assert all(c not in pw for c in "@:/")      # it goes into a database URL


def test_generated_values_are_kept_on_upgrade():
    from installer.plans import ensure_generated
    a = {}
    ensure_generated(a)
    first = dict(a["generated"])
    ensure_generated(a)
    assert a["generated"] == first


def test_plans_have_the_expected_steps():
    from installer.plans import destroy_plan, install_plan
    local = [s.id for s in install_plan({"target": {"kind": "local"}})]
    assert local[0] == "connect" and local[-1] == "result" and "terraform" not in local
    eks = [s.id for s in install_plan({"target": {"kind": "eks"}})]
    assert eks[:2] == ["terraform", "storage"] and "connect" not in eks
    assert [s.id for s in destroy_plan({"target": {"kind": "eks"}})][-1] == "terraform"


def test_jenkins_values_carry_env_and_jobs():
    from installer import platform
    v = yaml.safe_load(platform.jenkins_values(dict(
        registry_kind="ecr", registry="1.dkr.ecr.us-east-1.amazonaws.com", owner="fork",
        env="eks", jenkins_url="http://x/", aws_region="us-east-1", service_type="LoadBalancer",
        installer_repo="DevOpsCourse22/ai-scanner-installer", branch="env/eks")))
    casc = v["controller"]["JCasC"]["configScripts"]
    env = yaml.safe_load(casc["ci-registry"])["jenkins"]["globalNodeProperties"][0]["envVars"]["env"]
    for kv in (("CODE_OWNER", "fork"), ("DEPLOY_ENV", "eks"), ("DEPLOY_BRANCH", "env/eks"),
               ("INSTALLER_REPO", "DevOpsCourse22/ai-scanner-installer"), ("REGISTRY_KIND", "ecr")):
        assert {"key": kv[0], "value": kv[1]} in env
    jobs = casc["ci-jobs"]
    assert jobs.count("pipelineJob(") == 3 and "githubPush()" in jobs
    assert "scriptPath('environments/eks/ci/ai-scanner-backend.Jenkinsfile')" in jobs
    assert "ghp" not in str(v)     # the token is referenced, never inlined


def test_gateway_overlay_sets_the_address():
    from installer import overlays
    files = overlays.gateway(dict(registry="r", tag="t", public_url="https://scan.example.com/", admins=["a", "b"]))
    patch = files["app-patch.yaml"]
    assert 'value: "true"' in patch.split("GATEWAY_COOKIE_SECURE")[1].split(chr(10))[1]
    assert "https://scan.example.com/auth/github/callback" in patch
    assert "GATEWAY_DEFAULT" not in patch
    assert files["admins-patch.yaml"].rstrip().endswith("a" + chr(10) + "    b")


def test_derived_jenkinsfiles_keep_the_pipeline_and_deploy_here():
    from pathlib import Path
    from installer import pipelines
    for repo, app in pipelines.APPS.items():
        src = Path(__file__).resolve().parents[2] / repo / "Jenkinsfile"
        if not src.exists():
            continue
        out = pipelines.derive(repo, src.read_text(encoding="utf-8"), "local")
        for kept in ("stage('Test')", "stage('Build')", "stage('Scan')", "stage('Push')", "stage('Deploy')"):
            assert kept in out
        assert "skipDefaultCheckout()" in out and "stage('Checkout')" in out
        assert f"set_env_image" in out and f"environments/local/ci/{repo}.pod.yaml" in out
        assert "branch 'main'" not in out and "DevOpsCourse22/" + repo + "-deployment" not in out


def test_last_deploy_is_remembered_without_its_password(api):
    from installer import runner
    client, appmod = api
    client.post("/api/unlock", json={"passphrase": "longenough1", "new": True})
    run = runner.Run(kind="install", steps=[])
    run.status, run.result = "done", {"app_url": "http://x", "passwords": {"all": "pw-secret-123"}}
    appmod._remember(run)
    appmod._save()
    out = client.get("/api/answers").json()["answers"]
    assert out["last_result"]["app_url"] == "http://x" and "passwords" not in out["last_result"]
    assert "pw-secret-123" not in str(out)
    # the wizard sends answers back: it cannot change or drop the remembered deploy
    client.put("/api/answers", json={"answers": {**out, "last_result": {"app_url": "http://evil"}}})
    assert appmod._session["answers"]["last_result"]["app_url"] == "http://x"
    # a finished Remove forgets it
    gone = runner.Run(kind="destroy", steps=[])
    gone.status = "done"
    appmod._remember(gone)
    assert "last_result" not in appmod._session["answers"]


def _self_signed(names, days=30):
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .public_key(key.public_key()).serial_number(1).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())


def test_uploaded_certificate_is_checked():
    from installer import tls
    cert, key = _self_signed(["*.example.com", "example.com"])
    assert tls.check(cert, key, "scan.example.com")["ok"]
    assert not tls.check(cert, key, "scan.other.org")["ok"]
    assert not tls.check(cert, key, "10.0.0.5")["ok"]
    _, other_key = _self_signed(["x.example.com"])
    assert "does not belong" in " ".join(tls.check(cert, other_key, "scan.example.com")["problems"])
    old, old_key = _self_signed(["scan.example.com"], days=-1)
    assert "expired" in " ".join(tls.check(old, old_key, "scan.example.com")["problems"])
    assert not tls.check("not a cert", key, "scan.example.com")["ok"]


def test_certificate_puts_tls_on_the_ingress():
    from installer import overlays
    files = overlays.backend(dict(registry="r", tag="t", worker_tag="t", ai_mode="cloud", ingress_class="nginx",
                                  public_url="https://scan.example.com", tls_secret="ai-scan-tls",
                                  storage=dict(provisioner="p")))
    patch = yaml.safe_load(files["ingress-patch.yaml"])
    assert {"op": "add", "path": "/spec/tls",
            "value": [{"hosts": ["scan.example.com"], "secretName": "ai-scan-tls"}]} in patch


def test_health_says_gone_unreachable_or_up(monkeypatch, tmp_path):
    import subprocess as sp
    from installer import health, services
    a = {"target": {"kind": "local", "context": "minikube"}, "last_result": {"app_url": "http://localhost"}}
    monkeypatch.setattr(services, "connect", lambda a, w: (_ for _ in ()).throw(RuntimeError("connection refused")))
    assert health.check(a, str(tmp_path))["status"] == "unreachable"
    monkeypatch.setattr(services, "connect", lambda a, w: ("kc", {}))
    monkeypatch.setattr(health, "_kubectl", lambda kc, env, *args, timeout=30: sp.CompletedProcess(
        args, 1, "", 'Error from server (NotFound): namespaces "ai-scan-repos" not found'))
    r = health.check(a, str(tmp_path))
    assert r["status"] == "gone" and r["remembered"]
    items = {"items": [{"metadata": {"name": n}, "spec": {"replicas": 1}, "status": {"readyReplicas": 1}}
                       for n in health.APP]}
    monkeypatch.setattr(health, "_kubectl", lambda kc, env, *args, timeout=30: sp.CompletedProcess(
        args, 0, "namespace/ai-scan-repos" if "namespace" in args else __import__("json").dumps(items), ""))
    monkeypatch.setattr(health, "_probe", lambda url: (True, "HTTP 200"))
    assert health.check(a, str(tmp_path))["status"] == "up"
    monkeypatch.setattr(health, "_probe", lambda url: (False, "ConnectError"))
    assert health.check(a, str(tmp_path))["status"] == "degraded"


def test_forget_clears_the_record_and_sets_terraform_state_aside(api, tmp_path):
    from installer import plans
    client, appmod = api
    client.post("/api/unlock", json={"passphrase": "longenough1", "new": True})
    appmod._session["answers"]["last_result"] = {"app_url": "http://x"}
    state = plans.DATA / "terraform" / "terraform.tfstate"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{}")
    r = client.post("/api/forget").json()
    assert "last_result" not in r["answers"] and not state.exists()
    assert (state.parent / r["state_moved_to"]).exists()        # set aside, not deleted


def test_an_emptied_terraform_state_is_not_an_install(monkeypatch, tmp_path):
    import json
    from installer import plans
    monkeypatch.setattr(plans, "DATA", tmp_path)
    (tmp_path / "terraform").mkdir()
    state = tmp_path / "terraform" / "terraform.tfstate"
    eks = {"target": {"kind": "eks"}}
    assert plans.detect_existing(eks, str(tmp_path)) == {"installed": False}           # no file
    state.write_text(json.dumps({"version": 4, "resources": []}))                    # after terraform destroy
    assert plans.detect_existing(eks, str(tmp_path)) == {"installed": False}
    state.write_text(json.dumps({"version": 4, "resources": [{"type": "aws_vpc"}]}))
    assert plans.detect_existing(eks, str(tmp_path)) == {"installed": True}


def test_eks_services_get_their_own_load_balancers():
    from installer import platform
    assert yaml.safe_load(platform.argocd_values("pw-12345678", public=True))["server"]["service"]["type"] == "LoadBalancer"
    assert "server" not in yaml.safe_load(platform.argocd_values("pw-12345678"))
    g = yaml.safe_load(platform.monitoring_values(public=True))["grafana"]
    assert g["service"]["type"] == "LoadBalancer"
    assert "service" not in yaml.safe_load(platform.monitoring_values())["grafana"]


def test_services_show_public_addresses_on_eks(monkeypatch):
    import httpx
    from installer import services
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200))
    st = services.status({"Jenkins": "http://j.elb.amazonaws.com:8080/", "Argo CD": "http://a.elb.amazonaws.com/",
                          "Grafana": "http://g.elb.amazonaws.com/"})
    assert st["public"] and all(s["up"] and "amazonaws.com" in s["url"] for s in st["services"])
    assert not any("localhost" in s["url"] for s in st["services"])


def test_terraform_is_retried_on_a_dns_blip(monkeypatch, tmp_path):
    from installer import plans, runner
    calls = []
    run = runner.Run(kind="destroy", steps=[])
    ctx = runner.Ctx(run, {}, str(tmp_path))

    def sh(args, **kw):
        calls.append(args)
        if len(calls) == 1:
            ctx.log("dial tcp: lookup elasticfilesystem.us-east-1.amazonaws.com on 127.0.0.11:53: no such host")
            raise runner.StepFailed("terraform destroy failed")
        return "ok"
    ctx.sh = sh
    monkeypatch.setattr(plans.time, "sleep", lambda s: None)
    assert plans.terraform(ctx, ["destroy"], tmp_path, {}) == "ok" and len(calls) == 2
    # a real error is not retried
    calls.clear()

    def bad(args, **kw):
        calls.append(args)
        ctx.log("Error: invalid configuration")
        raise runner.StepFailed("terraform failed")
    ctx.sh = bad
    try:
        plans.terraform(ctx, ["destroy"], tmp_path, {})
    except runner.StepFailed:
        pass
    assert len(calls) == 1
