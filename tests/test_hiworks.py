# 실행: uv run --no-project --with 'scrapling[fetchers]>=0.4.11' --with 'keyring>=25' python -m unittest discover -s tests
# 네트워크·실계정·실제 키체인 없이 돈다(메모리 키체인 · 가짜 응답). 틀린 비번·OTP 를 실계정으로 시험하지 않으려는 것.
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import keyring
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "hiworks.py"
spec = importlib.util.spec_from_file_location("hiworks", SOURCE)
hiworks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hiworks)


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise PasswordDeleteError(username)
        del self.items[(service, username)]


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body = status, json.dumps(body).encode()


def errors(title):
    return {"errors": [{"title": title}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {"HIWORKS_HOME": self.tmp.name}
        p = patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        for k in ("HIWORKS_USERNAME", "HIWORKS_PASSWORD"):
            os.environ.pop(k, None)
        self.kr = MemoryKeyring()
        prev = keyring.get_keyring()
        keyring.set_keyring(self.kr)
        self.addCleanup(keyring.set_keyring, prev)

    def fake_posts(self, *responses):
        """Hiworks 세션 클래스의 post 를 순서대로 가짜 응답으로 바꾼다(__slots__ 라 클래스에 건다)."""
        calls = []
        seq = iter(responses)

        def post(_self, url, **kw):
            calls.append((url, kw.get("json")))
            return next(seq)
        from scrapling.engines.static import _SyncSessionLogic
        p = patch.object(_SyncSessionLogic, "post", post)
        p.start()
        self.addCleanup(p.stop)
        return calls


class ParseTests(unittest.TestCase):
    def test_200_returns_data(self):
        self.assertTrue(hiworks.parse_login_response(200, {"data": {"is_v3_product": True}})["is_v3_product"])

    def test_202_raises_step(self):
        with self.assertRaises(hiworks.HiworksStepRequired) as cm:
            hiworks.parse_login_response(202, errors("REQUIRE_OTP_VALIDATION"))
        self.assertEqual(cm.exception.title, "REQUIRE_OTP_VALIDATION")

    def test_4xx_raises_with_title(self):
        with self.assertRaises(hiworks.HiworksLoginError) as cm:
            hiworks.parse_login_response(400, errors("ACCOUNT_NOT_FOUND"))
        self.assertEqual(cm.exception.title, "ACCOUNT_NOT_FOUND")

    def test_5xx_without_body(self):
        with self.assertRaises(hiworks.HiworksLoginError):
            hiworks.parse_login_response(502, None)

    def test_liveness_html(self):
        self.assertFalse(hiworks.is_logged_in_html('location.href="https://login.office.hiworks.com/x.com"'))
        self.assertTrue(hiworks.is_logged_in_html('location.href="https://boards.office.hiworks.com/board"'))
        self.assertFalse(hiworks.is_logged_in_html("<html>점검 중</html>"))


class LoginTests(Base):
    def test_otp_flow_posts_code_once(self):
        calls = self.fake_posts(FakeResponse(202, errors("REQUIRE_OTP_VALIDATION")), FakeResponse(200, {"data": {}}))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            hw.login(lambda: "123456")
        self.assertEqual([c[0].rsplit("/", 1)[1] for c in calls], ["login", "otp"])
        self.assertEqual(calls[1][1], {"otp_code": "123456"})
        self.assertTrue(hiworks.session_file().exists())

    def test_otp_without_provider_raises_step(self):
        self.fake_posts(FakeResponse(202, errors("REQUIRE_OTP_VALIDATION")))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksStepRequired):
                hw.login(None)
            self.assertTrue(hw.password_ok)

    def test_password_change_step_is_not_handled(self):
        self.fake_posts(FakeResponse(202, errors("REQUIRE_PASSWORD_CHANGE")))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksStepRequired):
                hw.login(lambda: "123456")

    def test_wrong_otp_raises_and_no_session(self):
        self.fake_posts(FakeResponse(202, errors("REQUIRE_OTP_VALIDATION")), FakeResponse(400, errors("OTP_MISMATCH")))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksLoginError):
                hw.login(lambda: "000000")
        self.assertFalse(hiworks.session_file().exists())

    def test_missing_credentials_points_to_setup(self):
        with self.assertRaises(hiworks.HiworksError) as cm:
            hiworks.Hiworks()
        self.assertIn("setup", str(cm.exception))

    def test_password_comes_from_keyring(self):
        hiworks.store_credentials("a@x.com", "from-keyring")
        calls = self.fake_posts(FakeResponse(200, {"data": {}}))
        with hiworks.Hiworks() as hw:
            hw.login()
        self.assertEqual(calls[0][1]["password"], "from-keyring")

    def test_ensure_reuses_alive_session(self):
        hiworks.store_credentials("a@x.com", "pw")
        with hiworks.Hiworks() as hw:
            hw._jar().set("PHPSESSID", "v", domain=".hiworks.com")
            hw.save()
        with hiworks.Hiworks() as hw:
            with patch.object(hw, "is_alive", return_value=True), patch.object(hw, "login") as login:
                self.assertEqual(hw.ensure(), "reused")
                login.assert_not_called()


class SetupTests(Base):
    def run_setup(self, stdin_pw, *responses, otp="123456"):
        self.fake_posts(*responses)
        args = SimpleNamespace(email="a@x.com", password_stdin=True, no_prompt=False, force=False, ip_level="1")
        with patch("sys.stdin", io.StringIO(stdin_pw + "\n")), patch.object(hiworks, "ask_otp", return_value=otp):
            return hiworks.cmd_setup(args)

    def test_success_stores_password_in_keyring_not_file(self):
        self.assertEqual(self.run_setup("secret-pw", FakeResponse(200, {"data": {}})), 0)
        self.assertEqual(self.kr.items[("hiworks", "a@x.com")], "secret-pw")
        cfg = hiworks.config_file()
        self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)
        for f in Path(self.tmp.name).iterdir():
            self.assertNotIn("secret-pw", f.read_text(), f.name)

    def test_wrong_password_is_not_stored(self):
        self.assertEqual(self.run_setup("bad", FakeResponse(400, errors("ACCOUNT_NOT_FOUND"))), 2)
        self.assertEqual(self.kr.items, {})
        self.assertFalse(hiworks.config_file().exists())

    def test_correct_password_wrong_otp_is_stored_with_warning(self):
        rc = self.run_setup("pw", FakeResponse(202, errors("REQUIRE_OTP_VALIDATION")), FakeResponse(400, errors("OTP_MISMATCH")))
        self.assertEqual(rc, 1)
        self.assertEqual(self.kr.items[("hiworks", "a@x.com")], "pw")

    def test_no_usable_keyring_stops_before_asking(self):
        from keyring.backends import fail
        keyring.set_keyring(fail.Keyring())
        with patch.object(hiworks, "ask") as ask:
            with self.assertRaises(hiworks.HiworksError):
                hiworks.cmd_setup(SimpleNamespace(email=None, password_stdin=False, no_prompt=False, force=False, ip_level="1"))
            ask.assert_not_called()

    def test_already_registered_and_alive_asks_nothing(self):
        hiworks.store_credentials("a@x.com", "pw")
        with patch.object(hiworks.Hiworks, "ensure", return_value="reused") as ensure, \
                patch.object(hiworks, "ask") as ask, patch("sys.stdin", io.StringIO("")) as stdin:
            rc = hiworks.cmd_setup(SimpleNamespace(email=None, password_stdin=False, no_prompt=False, force=False, ip_level="1"))
        self.assertEqual(rc, 0)
        ensure.assert_called_once()
        ask.assert_not_called()

    def test_stale_stored_password_is_replaced(self):
        hiworks.store_credentials("a@x.com", "old")
        calls = self.fake_posts(FakeResponse(400, errors("ACCOUNT_NOT_FOUND")), FakeResponse(200, {"data": {}}))
        args = SimpleNamespace(email=None, password_stdin=True, no_prompt=False, force=False, ip_level="1")
        with patch("sys.stdin", io.StringIO("new\n")):
            self.assertEqual(hiworks.cmd_setup(args), 0)
        self.assertEqual([c[1]["password"] for c in calls], ["old", "new"])
        self.assertEqual(self.kr.items[("hiworks", "a@x.com")], "new")

    def test_force_skips_stored_password(self):
        hiworks.store_credentials("a@x.com", "old")
        calls = self.fake_posts(FakeResponse(200, {"data": {}}))
        args = SimpleNamespace(email=None, password_stdin=True, no_prompt=False, force=True, ip_level="1")
        with patch("sys.stdin", io.StringIO("new\n")):
            self.assertEqual(hiworks.cmd_setup(args), 0)
        self.assertEqual([c[1]["password"] for c in calls], ["new"])

    def test_forget_removes_everything(self):
        self.run_setup("pw", FakeResponse(200, {"data": {}}))
        hiworks.forget_credentials()
        self.assertEqual(self.kr.items, {})
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])


class AskTests(unittest.TestCase):
    def test_no_tty_no_gui_refuses(self):
        with patch("sys.stdin", io.StringIO("")), patch.object(hiworks.sys, "platform", "linux"), \
                patch.object(hiworks.os, "name", "posix"):
            with self.assertRaises(hiworks.HiworksError):
                hiworks.ask("x", hidden=True)

    def test_mac_dialog_is_hidden_and_value_from_stdout(self):
        done = SimpleNamespace(returncode=0, stdout="pw\n")
        with patch("sys.stdin", io.StringIO("")), patch.object(hiworks.sys, "platform", "darwin"), \
                patch.object(hiworks.subprocess, "run", return_value=done) as run:
            self.assertEqual(hiworks.ask('비밀번호 "따옴표"', hidden=True), "pw")
        argv = run.call_args.args[0]
        self.assertIn("with hidden answer", argv[2])
        self.assertNotIn("pw", " ".join(argv))  # 값이 명령행에 실리지 않는다

    def test_mac_dialog_cancel(self):
        with patch("sys.stdin", io.StringIO("")), patch.object(hiworks.sys, "platform", "darwin"), \
                patch.object(hiworks.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")):
            with self.assertRaises(hiworks.HiworksError):
                hiworks.ask("x", hidden=True)

    def test_otp_must_be_six_digits(self):
        with patch.object(hiworks, "ask", return_value="12ab"):
            with self.assertRaises(hiworks.HiworksError):
                hiworks.ask_otp()



class FakeServer:
    """URL 끝부분으로 가짜 응답을 고른다. calls 에 (method, url, json) 을 남긴다."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def handler(self, method):
        def call(_self, url, **kw):
            self.calls.append((method, url, kw.get("json")))
            for key, resp in self.routes.items():
                m, frag = key.split(" ", 1)
                if m == method and frag in url:
                    return resp
            return FakeResponse(404, errors("NOT_FOUND"))
        return call


class ApiBase(Base):
    def serve(self, routes):
        from scrapling.engines.static import _SyncSessionLogic
        srv = FakeServer(routes)
        for m in ("get", "post"):
            p = patch.object(_SyncSessionLogic, m, srv.handler(m.upper()))
            p.start()
            self.addCleanup(p.stop)
        return srv


class MailTests(ApiBase):
    def test_send_uses_sender_no_not_address_no_and_escapes_text(self):
        srv = self.serve({
            "POST /mails/cert-key": FakeResponse(200, {"data": "CK"}),
            "GET /senders": FakeResponse(200, {"data": [{"no": 7, "address_no": 99, "address": "a@x.com", "is_default": True}]}),
            "POST /mails/send": FakeResponse(200, {"data": {"send_type": "NORMAL"}}),
        })
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            out = hw.send_mail("b@x.com, c@x.com", "제목", "1 < 2\n둘째 줄")
        body = [c for c in srv.calls if c[1].endswith("/mails/send")][0][2]
        self.assertEqual((body["cert_key"], body["sender_no"], body["to"]), ("CK", 7, ["b@x.com", "c@x.com"]))
        self.assertEqual(body["content"], "<div>1 &lt; 2<br>둘째 줄</div>")
        self.assertTrue(out["sent"])

    def test_send_without_recipient_is_refused_before_any_call(self):
        srv = self.serve({})
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.send_mail("", "s", "b")
        self.assertEqual(srv.calls, [])

    def test_delete_default_moves_to_trash(self):
        srv = self.serve({"POST /mails/move-bulk": FakeResponse(200, {})})
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            hw.delete_mails([1, 2])
        self.assertEqual(srv.calls[-1][1].rsplit("/", 1)[1], "move-bulk")
        self.assertEqual(srv.calls[-1][2], {"mailbox_id": "b5", "no_list": [1, 2]})

    def test_delete_permanent_uses_delete_bulk(self):
        srv = self.serve({"POST /mails/delete-bulk": FakeResponse(200, {})})
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            hw.delete_mails(3, permanent=True)
        self.assertEqual(srv.calls[-1][1].rsplit("/", 1)[1], "delete-bulk")
        self.assertEqual(srv.calls[-1][2], {"no_list": [3]})

    def test_list_sends_flat_filters_and_paging(self):
        srv = self.serve({"POST /mails/search": FakeResponse(200, {"meta": {"page": {"total": 1, "offset": 5, "limit": 2}},
                                                                    "data": [{"no": 1, "mailbox_id": "b1", "subject": "s"}]})})
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            out = hw.list_mails("b1", limit=2, offset=5, subject="s", sender="z@x.com")
        method, url, body = srv.calls[-1]
        self.assertIn("page[limit]=2&page[offset]=5", url)
        self.assertEqual(body, {"mailbox_id": "b1", "subject": "s", "from": "z@x.com"})
        self.assertEqual((out["total"], out["items"][0]["no"]), (1, 1))

    def test_api_error_raises(self):
        self.serve({"GET /mailboxes": FakeResponse(401, errors("Unauthorized"))})
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksApiError) as cm:
                hw.mailboxes()
        self.assertEqual(cm.exception.status, 401)


class HrTests(ApiBase):
    DIR = {
        "GET /v1/organizations": FakeResponse(200, {"data": [
            {"node_id": 1, "node_name": "회사", "parent_node_id": 0, "lft": 1},
            {"node_id": 2, "node_name": "개발팀", "parent_node_id": 1, "lft": 2},
            {"node_id": 3, "node_name": "개발지원팀", "parent_node_id": 1, "lft": 4}]}),
        "GET /v1/positions": FakeResponse(200, {"data": [{"id": 10, "code_content": "팀장"}]}),
        "GET /v1/jobs": FakeResponse(200, {"data": [{"id": 20, "code_content": "개발"}]}),
        "GET /v1/employees": FakeResponse(200, {"data": [
            {"id": 100, "name": "홍길동", "user_id": "hong", "active": "Y", "del_flag": "N", "position_no": 10, "job_no": 20,
             "phone": "000-0000", "cell": "010-0000-0000", "cell_visible": "N", "email": "h@x.com", "email_visible": "Y"},
            {"id": 101, "name": "퇴사자", "user_id": "gone", "active": "N", "del_flag": "N"}]}),
        "GET /v1/members": FakeResponse(200, {"data": [{"node_id": 2, "office_user_no": 100}, {"node_id": 2, "office_user_no": 101}]}),
    }

    def test_person_hides_fields_not_made_public(self):
        self.serve(self.DIR)
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            people = hw.find_people("홍")
        self.assertEqual(len(people), 1)
        p = people[0]
        self.assertEqual((p["position"], p["job"], p["departments"], p["email"]), ("팀장", "개발", ["개발팀"], "h@x.com"))
        self.assertNotIn("cell", p)

    def test_inactive_people_are_not_listed(self):
        self.serve(self.DIR)
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            self.assertEqual(hw.find_people("퇴사"), [])
            chart = hw.org_chart()
        dev = chart["departments"][0]["children"][0]
        self.assertEqual((dev["name"], [m["name"] for m in dev["members"]]), ("개발팀", ["홍길동"]))

    def test_department_exact_match_wins_over_partial(self):
        srv = self.serve({**self.DIR, "GET /vacation-calendar": FakeResponse(200, {"data": []})})
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            hw.vacation_calendar(2026, 10, department="개발팀")
            with self.assertRaises(hiworks.HiworksError):
                hw.vacation_calendar(2026, 10, department="개발")  # 개발팀·개발지원팀 둘 다 걸린다
        self.assertIn("filter[node]=2", [c for c in srv.calls if "vacation-calendar" in c[1]][0][1])

    def test_my_vacation_skips_empty_types(self):
        self.serve({
            "GET /me": FakeResponse(200, {"data": {"name": "나", "user_id": "me", "office_user_no": "5"}}),
            "GET /v1/vacation-types": FakeResponse(200, {"data": [{"id": 1, "title": "연차", "use_flag": "Y"},
                                                                  {"id": 2, "title": "월차", "use_flag": "Y"}]}),
            "GET /office-users/5/vacation-types/1/summary": FakeResponse(200, {"data": {"created_days": 15, "used_days": 6,
                                                                                       "remaining_days": 9, "created_total_hours": 120}}),
            "GET /office-users/5/vacation-types/2/summary": FakeResponse(200, {"data": {"created_total_hours": 0}}),
        })
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            out = hw.my_vacation()
        self.assertEqual([(v["type"], v["remaining_days"]) for v in out["vacations"]], [("연차", 9)])


class PeriodTests(unittest.TestCase):
    def row(self, date, unit="days", no=1, typ="연차", status="결재완료"):
        return {"name": "A", "office_user_no": no, "departments": [], "date": date, "type": typ, "unit": unit,
                "days": 1 if unit == "days" else 0, "hours": 0 if unit == "days" else 4, "start_time": "09:00",
                "end_time": "13:00", "approval_status": status}

    def test_weekend_gap_merges_weekday_gap_does_not(self):
        rows = [self.row("2026-10-08"), self.row("2026-10-09"), self.row("2026-10-12"),  # 목·금 + 주말 + 월
                self.row("2026-10-14")]                                                   # 화요일 근무 뒤 수요일
        p = hiworks.group_vacation_periods(rows)
        self.assertEqual([(x["start"], x["end"], x["days"]) for x in p], [("2026-10-08", "2026-10-12", 3), ("2026-10-14", "2026-10-14", 1)])

    def test_hours_and_different_people_stay_separate(self):
        p = hiworks.group_vacation_periods([self.row("2026-10-08"), self.row("2026-10-09", unit="hours"),
                                            self.row("2026-10-09", no=2)])
        self.assertEqual(len(p), 3)
        self.assertEqual([x["full_day"] for x in p if x["office_user_no"] == 1], [True, False])


class AutoSaveTests(Base):
    def test_cookies_saved_on_exit_after_established_session(self):
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            hw._jar().set("PHPSESSID", "s", domain=".hiworks.com")
            hw._jar().set("lbg_1", "node", domain="mail-api.office.hiworks.com")
            hw.established = True
        names = {c["name"] for c in json.loads(hiworks.session_file().read_text())["cookies"]}
        self.assertEqual(names, {"PHPSESSID", "lbg_1"})

    def test_nothing_saved_when_session_not_established(self):
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            hw._jar().set("PHPSESSID", "s", domain=".hiworks.com")
        self.assertFalse(hiworks.session_file().exists())


if __name__ == "__main__":
    unittest.main()
