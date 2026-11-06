"""Change requests: approval by someone else, ordered execution on devices, transcript without passwords."""

import pytest
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.config import settings
from app.routers import changes as changes_router
from app.services import changes as svc
from app.services.device_cli import CliError, CliSession, Target, TelnetStream
from tests.test_auth import add_user, auth_on, logged_in  # noqa: F401  (fixture)
from tests.test_configgen import make_service  # noqa: F401  (fixture)
from tests.test_validation import net  # noqa: F401  (fixture)

PASSWORD = "Dev1ce-P@ss"


class FakeDevice:
    """A Huawei-like CLI: echoes each line, answers with a prompt, rejects the commands in `reject`."""

    def __init__(self, name: str, reject: set[str] = frozenset()):
        self.name, self.reject, self.sent, self._out = name, reject, [], ""
        self._prompt = f"<{name}>"

    def send(self, data: str) -> None:
        command = data.rstrip("\n")
        if command:
            self.sent.append(command)
        if command == "system-view":
            self._prompt = f"[~{self.name}]"
        elif command == "return":
            self._prompt = f"<{self.name}>"
        reply = "Error: Unrecognized command found at '^' position.\n" if command in self.reject else ""
        self._out += f"{command}\n{reply}{self._prompt}"

    def read(self, timeout: float) -> str:
        out, self._out = self._out, ""
        return out

    def close(self) -> None:
        pass


@pytest.fixture
def devices(monkeypatch, db, tmp_path):
    """Runs execute inline against the test DB; every connect goes to a FakeDevice named after the host."""
    created: dict[str, FakeDevice] = {}
    reject: set[str] = set()

    def connect(target, known_hosts, timeout, jump):
        created[target.host] = FakeDevice(target.host, reject)
        return created[target.host]

    monkeypatch.setattr(svc, "open_stream", connect)
    monkeypatch.setattr(svc, "start_background", lambda work: work())
    monkeypatch.setattr(changes_router, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    monkeypatch.setattr(settings, "device_known_hosts", str(tmp_path / "known_hosts"))
    monkeypatch.setattr(settings, "device_jump_host", None)
    return {"created": created, "reject": reject}


@pytest.fixture
def team(auth_on, db):  # noqa: F811
    add_user(db, "eng", "engineer")
    add_user(db, "apr", "approver")
    return {"eng": logged_in("eng"), "apr": logged_in("apr")}


@pytest.fixture
def change(team, make_service):  # noqa: F811
    service = make_service("vrp", "l2vpn")
    response = team["eng"].post("/api/changes", json={"service_id": service.id})
    assert response.status_code == 201, response.text
    return response.json()


def run(client, change_id: int, site: str, phase: str):
    return client.post(f"/api/changes/{change_id}/runs", json={
        "site": site, "phase": phase, "username": "noc1", "password": PASSWORD})


def test_should_refuse_approval_when_approver_is_the_creator(team, db, change):
    add_user(db, "eng2", "approver")
    creator_as_approver = db.query(m.User).filter_by(username="eng").one()
    creator_as_approver.role = "approver"
    db.commit()
    assert team["eng"].post(f"/api/changes/{change['id']}/approve", json={}).status_code == 403


def test_should_refuse_approval_when_role_is_engineer(db, change):
    add_user(db, "eng3", "engineer")
    assert logged_in("eng3").post(f"/api/changes/{change['id']}/approve", json={}).status_code == 403


def test_should_refuse_push_when_change_is_not_approved(team, change, devices):
    assert run(team["eng"], change["id"], "A", "push").status_code == 409


def test_should_allow_precheck_when_change_still_awaits_approval(team, change, devices):
    response = run(team["eng"], change["id"], "A", "precheck")
    assert response.status_code == 202 and team["eng"].get(
        f"/api/changes/{change['id']}/runs/{response.json()['id']}").json()["status"] == "ok"


def test_should_refuse_site_b_push_when_site_a_was_not_pushed(team, change, devices):
    team["apr"].post(f"/api/changes/{change['id']}/approve", json={})
    assert run(team["eng"], change["id"], "B", "push").status_code == 409


def test_should_send_frozen_config_in_order_when_push_runs(team, change, devices):
    team["apr"].post(f"/api/changes/{change['id']}/approve", json={})
    run(team["eng"], change["id"], "A", "push")
    detail = team["eng"].get(f"/api/changes/{change['id']}").json()
    site_a = detail["sites"][0]
    sent = devices["created"][site_a["host"]].sent
    assert sent[sent.index(site_a["config"][0]):] == site_a["config"]
    assert (detail["runs"][-1]["status"], detail["status"]) == ("ok", "in_progress")


def test_should_mark_change_implemented_when_both_sites_are_pushed(team, change, devices):
    team["apr"].post(f"/api/changes/{change['id']}/approve", json={})
    run(team["eng"], change["id"], "A", "push")
    run(team["eng"], change["id"], "B", "push")
    assert team["eng"].get(f"/api/changes/{change['id']}").json()["status"] == "implemented"


def test_should_stop_at_rejected_line_when_device_returns_error(team, change, devices):
    team["apr"].post(f"/api/changes/{change['id']}/approve", json={})
    config = team["eng"].get(f"/api/changes/{change['id']}").json()["sites"][0]["config"]
    devices["reject"].add(config[2])
    run_id = run(team["eng"], change["id"], "A", "push").json()["id"]
    host = team["eng"].get(f"/api/changes/{change['id']}").json()["sites"][0]["host"]
    assert config[3] not in devices["created"][host].sent
    result = team["eng"].get(f"/api/changes/{change['id']}/runs/{run_id}").json()
    assert result["status"] == "failed" and config[2] in result["error"]


def test_should_keep_password_out_of_transcript_when_run_finishes(team, change, devices):
    run_id = run(team["eng"], change["id"], "A", "precheck").json()["id"]
    log = team["eng"].get(f"/api/changes/{change['id']}/runs/{run_id}").json()["log"]
    assert log and PASSWORD not in log


def test_should_require_push_before_postcheck_when_site_untouched(team, change, devices):
    team["apr"].post(f"/api/changes/{change['id']}/approve", json={})
    assert run(team["eng"], change["id"], "A", "postcheck").status_code == 409


@pytest.mark.parametrize("line, name", [
    ("<JKT-PE1>", "JKT-PE1"), ("[~JKT-PE1-vsi-OXY_1480]", "JKT-PE1-vsi-OXY_1480"),
    ("RP/0/RSP0/CPU0:JKT-PE1(config)#", "JKT-PE1"), ("JKT-ME1(config-if)#", "JKT-ME1"),
])
def test_should_extract_device_name_when_reading_prompt(line, name):
    assert CliSession.prompt_name(line) == name


def test_should_not_take_ok_line_for_prompt_when_device_name_is_known():
    session = CliSession(FakeDevice("PE1"), Target("PE1", "ssh", "u", "p"), lambda text: None, 1)
    session.login()
    assert not session._is_prompt("show run\n[OK]") and session._is_prompt("show run\n<PE1>")


def test_should_refuse_telnet_run_when_telnet_is_disabled(team, change, devices, monkeypatch):
    monkeypatch.setattr(settings, "device_telnet_enabled", False)
    response = team["eng"].post(f"/api/changes/{change['id']}/runs", json={
        "site": "a", "phase": "precheck", "protocol": "telnet", "username": "u", "password": "p"})
    assert response.status_code == 422 and devices["created"] == {}


def test_should_refuse_option_and_strip_iac_when_telnet_server_negotiates():
    class Sock:
        sent = b""

        def sendall(self, data):
            Sock.sent += data

    stream = TelnetStream(Sock())
    assert stream._negotiate(bytes([255, 253, 24]) + b"login: ") == b"login: "
    assert Sock.sent == bytes([255, 252, 24])  # DO terminal-type -> WONT


def test_should_raise_when_device_never_shows_a_prompt():
    class Silent(FakeDevice):
        def send(self, data):
            pass

    with pytest.raises(CliError):
        CliSession(Silent("X"), Target("X", "ssh", "u", "p"), lambda text: None, 0.5).login()
