from __future__ import annotations

import re

from app.routers.auth import reset_login_limiter
from tests.helpers import EMAILS, login
from scripts.seed import DEMO_PASSWORD

STRONG = "Correct-horse-9"


async def test_first_user_becomes_owner_and_registration_then_closes(client):
    r = await client.post("/auth/register", json={"email": "Boss@Example.com", "password": STRONG, "name": "Boss", "org_name": "Acme"})
    assert r.status_code == 201
    assert r.json()["user"]["role"] == "OWNER" and r.json()["user"]["email"] == "boss@example.com"
    r = await client.post("/auth/register", json={"email": "two@example.com", "password": STRONG, "name": "Two"})
    assert r.status_code == 403


async def test_weak_password_rejected(client):
    r = await client.post("/auth/register", json={"email": "a@example.com", "password": "short1", "name": "A"})
    assert r.status_code == 422
    r = await client.post("/auth/register", json={"email": "a@example.com", "password": "alllettersnodigits", "name": "A"})
    assert r.status_code == 422


async def test_login_failures_are_indistinguishable(seeded):
    c = seeded
    a = await c.post("/auth/login", json={"email": EMAILS["staff"], "password": "wrong-password-1"})
    b = await c.post("/auth/login", json={"email": "nobody@calibrex.demo", "password": "wrong-password-1"})
    assert a.status_code == b.status_code == 401 and a.text == b.text


async def test_login_is_rate_limited(seeded, monkeypatch):
    c = seeded
    monkeypatch.setattr(c.app.state.settings, "login_max_attempts", 5)
    reset_login_limiter()
    codes = [(await c.post("/auth/login", json={"email": EMAILS["staff"], "password": "wrong-password-1"})).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    reset_login_limiter()


async def test_me_requires_a_valid_token(seeded):
    c = seeded
    assert (await c.get("/auth/me")).status_code == 401
    assert (await c.get("/auth/me", headers={"Authorization": "Bearer garbage"})).status_code == 401
    h = await login(c, "manager")
    assert (await c.get("/auth/me", headers=h)).json()["role"] == "MANAGER"


async def test_refresh_rotates_and_replay_kills_the_session(seeded):
    c = seeded
    await login(c, "staff")
    name = c.app.state.settings.refresh_cookie_name
    old = c.cookies.get(name)
    assert old
    r = await c.post("/auth/refresh")
    assert r.status_code == 200 and r.json()["access_token"]
    new = c.cookies.get(name)
    assert new and new != old

    c.cookies.clear()  # replay with an explicit cookie only, so the jar cannot interfere
    assert (await c.post("/auth/refresh", cookies={name: old})).status_code == 401  # a rotated token coming back means it leaked
    c.cookies.clear()
    assert (await c.post("/auth/refresh", cookies={name: new})).status_code == 401  # ...and the whole family is revoked, newest token included


async def test_logout_all_kills_existing_access_tokens(seeded):
    c = seeded
    h = await login(c, "staff")
    assert (await c.post("/auth/logout-all", headers=h)).status_code == 204
    assert (await c.get("/auth/me", headers=h)).status_code == 401
    assert (await c.post("/auth/refresh")).status_code == 401


async def test_invite_accept_flow_and_role(seeded):
    c = seeded
    owner = await login(c, "owner")
    r = await c.post("/users/invite", headers=owner, json={"email": "New.Person@example.com", "role": "MANAGER", "name": "New Person"})
    assert r.status_code == 201 and r.json()["pending"] is True
    token = r.json()["invite_token"]

    assert (await c.post("/auth/login", json={"email": "new.person@example.com", "password": STRONG})).status_code == 401  # not active yet
    assert (await c.get(f"/invites/{token}")).json() == {"email": "new.person@example.com", "role": "MANAGER"}
    assert (await c.post(f"/invites/{token}/accept", json={"password": "weak"})).status_code == 422
    r = await c.post(f"/invites/{token}/accept", json={"password": STRONG})
    assert r.status_code == 201 and r.json()["user"]["role"] == "MANAGER"
    assert (await c.post(f"/invites/{token}/accept", json={"password": STRONG})).status_code == 400  # single use
    assert (await login(c, "new.person@example.com", STRONG))


async def test_password_recovery_is_single_use_and_revokes_existing_sessions(seeded):
    c = seeded
    owner = await login(c, "owner")
    staff_before = await login(c, "staff")

    class MailCapture:
        messages = []

        async def send(self, to, subject, body):
            self.messages.append((to, subject, body))

    mail = MailCapture()
    c.app.state.mailer = mail
    reset_login_limiter()
    requested = await c.post("/auth/password/forgot", json={"email": EMAILS["staff"]})
    unknown = await c.post("/auth/password/forgot", json={"email": "unknown@example.com"})
    assert requested.status_code == unknown.status_code == 202
    assert requested.json() == unknown.json()
    assert len(mail.messages) == 1 and mail.messages[0][0] == EMAILS["staff"]
    token = re.search(r"/\#/reset-password/([A-Za-z0-9_-]+)", mail.messages[0][2]).group(1)

    weak = await c.post("/auth/password/reset", json={"token": token, "password": "weak"})
    assert weak.status_code == 422
    changed = await c.post("/auth/password/reset", json={"token": token, "password": "New-strong-pass-9"})
    assert changed.status_code == 204
    assert (await c.get("/auth/me", headers=owner)).status_code == 200  # unrelated sessions remain active
    assert (await c.get("/auth/me", headers=staff_before)).status_code == 401
    staff = await login(c, "staff")
    assert (await c.get("/auth/me", headers=staff)).status_code == 200
    assert (await c.post("/auth/password/reset", json={"token": token, "password": "Other-strong-pass-8"})).status_code == 400


async def test_deactivation_and_demotion_take_effect_immediately(seeded):
    c = seeded
    owner, staff, mgr = await login(c, "owner"), await login(c, "staff"), await login(c, "manager")
    users = {u["email"]: u for u in (await c.get("/users", headers=owner)).json()}

    r = await c.patch(f"/users/{users[EMAILS['staff']]['id']}", headers=owner, json={"active": False})
    assert r.status_code == 200
    assert (await c.get("/auth/me", headers=staff)).status_code == 401

    assert (await c.get("/approvals", headers=mgr)).status_code == 200
    await c.patch(f"/users/{users[EMAILS['manager']]['id']}", headers=owner, json={"role": "AUDITOR"})
    assert (await c.get("/approvals", headers=mgr)).status_code == 401  # old token died with the role change
    mgr2 = await login(c, "manager")
    assert (await c.get("/approvals", headers=mgr2)).status_code == 403


async def test_there_is_always_one_active_owner(seeded):
    c = seeded
    owner = await login(c, "owner")
    me = (await c.get("/auth/me", headers=owner)).json()
    assert (await c.patch(f"/users/{me['id']}", headers=owner, json={"role": "MANAGER"})).status_code == 409
    assert (await c.patch(f"/users/{me['id']}", headers=owner, json={"active": False})).status_code == 409


async def test_only_owner_manages_users(seeded):
    c = seeded
    for who in ("manager", "staff", "auditor"):
        h = await login(c, who)
        assert (await c.get("/users", headers=h)).status_code == 403
        assert (await c.post("/users/invite", headers=h, json={"email": "x@example.com", "role": "STAFF"})).status_code == 403
