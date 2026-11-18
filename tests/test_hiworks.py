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


class MacKeychainTests(Base):
    """macOS /usr/bin/security 경로 — 실제 키체인은 건드리지 않고 subprocess 를 가짜로 바꾼다."""

    def test_memory_keyring_is_not_mac_path(self):
        self.assertFalse(hiworks.mac_security(keyring.get_keyring()))

    def test_quote_escapes_backslash_and_quote(self):
        self.assertEqual(hiworks._security_quote('a"b\\c'), '"a\\"b\\\\c"')

    def test_read_timeout_explains_prompt(self):
        import subprocess
        with patch.object(hiworks.subprocess, "run", side_effect=subprocess.TimeoutExpired("security", 60)):
            with self.assertRaises(hiworks.HiworksError) as cm:
                hiworks.mac_read("a@x.com")
        self.assertIn("항상 허용", str(cm.exception))

    def test_read_strips_one_newline_and_missing_is_none(self):
        ok = SimpleNamespace(returncode=0, stdout="pw \n", stderr="")
        with patch.object(hiworks.subprocess, "run", return_value=ok):
            self.assertEqual(hiworks.mac_read("a@x.com"), "pw ")
        missing = SimpleNamespace(returncode=44, stdout="", stderr="not found")
        with patch.object(hiworks.subprocess, "run", return_value=missing):
            self.assertIsNone(hiworks.mac_read("a@x.com"))

    def test_write_sends_password_on_stdin_not_argv(self):
        calls = []

        def fake(cmd, **kw):
            calls.append((cmd, kw))
            return SimpleNamespace(returncode=0, stdout="s3cr\"et\n", stderr="")
        with patch.object(hiworks.subprocess, "run", side_effect=fake):
            hiworks.mac_write("a@x.com", 's3cr"et')
        cmd, kw = calls[0]
        self.assertEqual(cmd, [hiworks.SECURITY_BIN, "-i"])
        self.assertNotIn("s3cr", " ".join(cmd))
        self.assertIn('-T /usr/bin/security', kw["input"])
        self.assertIn('-w "s3cr\\"et"', kw["input"])


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
    """URL 끝부분으로 가짜 응답을 고른다. calls 에 (method, url, json 또는 폼 data) 를 남긴다.
    응답 자리에 함수를 두면 요청 kw 로 불러 응답을 만든다(상태가 바뀌는 서버 흉내)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def handler(self, method):
        def call(_self, url, **kw):
            self.calls.append((method, url, kw["json"] if "json" in kw else kw.get("data")))
            for key, resp in self.routes.items():
                m, frag = key.split(" ", 1)
                if m == method and frag in url:
                    return resp(kw) if callable(resp) else resp
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



class VacationRequestTests(ApiBase):
    CHECK_OK = {"data": {"available_user_nos": [5], "remain_days_exceptions": [], "date_exceptions": []}}
    CHECK_DUP = {"data": {"available_user_nos": [], "remain_days_exceptions": [],
                          "date_exceptions": [{"date": "2026-12-21", "vacation_users": [5]}]}}

    def routes(self, check):
        return {**HrTests.DIR,
                "GET /me": FakeResponse(200, {"data": {"name": "홍길동", "user_id": "hong", "office_user_no": "100"}}),
                "GET /v1/vacation-types": FakeResponse(200, {"data": [{"id": 7, "title": "연차", "use_flag": "Y"}]}),
                "GET /forms/vacation-request/node/2": FakeResponse(200, {"data": {
                    "id": 9, "preserved_term": 5, "security_level": "C",
                    "line_users": [{"office_user_no": 100, "node_id": 2, "approval_type": "B"},
                                   {"office_user_no": False, "node_id": 2, "approval_type": "C"}]}}),
                "POST /vacation-request-check": FakeResponse(200, check),
                "POST /vacation-request": FakeResponse(200, {"data": {}})}

    def test_range_payload_uses_period_selection(self):
        self.serve(self.routes(self.CHECK_OK))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            p = hw.vacation_request_payload("2026-12-18", "2026-12-22", reason="a < b")
        self.assertEqual(p["period_selections"], [{"vacation_type_no": 7, "start_date": "2026-12-18", "end_date": "2026-12-22"}])
        self.assertEqual((p["details"], p["user_nos"], p["form_id"], p["node_id"]), ([], [100], 9, 2))
        self.assertEqual(p["line_users"], [{"office_user_no": 100, "node_id": 2, "approval_type": "B"}])  # 빈 자리는 뺀다
        self.assertEqual(p["comment"], "a &lt; b")
        self.assertNotIn("year", p)

    def test_half_day_payload(self):
        self.serve(self.routes(self.CHECK_OK))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            p = hw.vacation_request_payload("2026-12-23", half="pm")
            with self.assertRaises(hiworks.HiworksError):
                hw.vacation_request_payload("2026-12-23", "2026-12-24", half="am")
        self.assertEqual(p["details"], [{"vacation_type_no": 7, "vacation_date": "2026-12-23", "time_type": "H",
                                         "start_time": "14:00:00", "end_time": "18:00:00", "hours": 4.0}])
        self.assertEqual(p["year"], "2026")

    def test_failed_check_never_submits(self):
        srv = self.serve(self.routes(self.CHECK_DUP))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            p = hw.vacation_request_payload("2026-12-21")
            with self.assertRaises(hiworks.HiworksError) as cm:
                hw.request_vacation(p)
        self.assertIn("이미 휴가가 신청된 날", str(cm.exception))
        self.assertFalse([c for c in srv.calls if c[1].endswith("/vacation-request")])

    def test_passed_check_submits_wrapped_payload_once(self):
        srv = self.serve(self.routes(self.CHECK_OK))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            p = hw.vacation_request_payload("2026-12-21")
            hw.request_vacation(p)
        sent = [c for c in srv.calls if c[1].endswith("/vacation-request")]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2], {"data": p})

    def test_unknown_type_and_reversed_dates_refused(self):
        self.serve(self.routes(self.CHECK_OK))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.vacation_request_payload("2026-12-21", vtype="없는휴가")
            with self.assertRaises(hiworks.HiworksError):
                hw.vacation_request_payload("2026-12-22", "2026-12-21")



class ApprovalLineTests(ApiBase):
    CHECK_OK = VacationRequestTests.CHECK_OK
    routes = VacationRequestTests.routes

    def form_routes(self, method="BCF"):
        r = self.routes(self.CHECK_OK)
        r["GET /forms/vacation-request/node/2"] = FakeResponse(200, {"data": {
            "id": 9, "approval_method": method, "line_users": [
                {"office_user_no": 100, "node_id": 2, "approval_type": "B"},
                {"office_user_no": 200, "node_id": 3, "approval_type": "F"}]}})
        r["GET /v1/employees"] = FakeResponse(200, {"data": [
            {"id": 100, "name": "홍길동", "user_id": "hong", "active": "Y"},
            {"id": 200, "name": "참조인", "user_id": "ref", "active": "Y"},
            {"id": 300, "name": "김결재", "user_id": "kim1", "active": "Y"},
            {"id": 301, "name": "김결재", "user_id": "kim2", "active": "Y"},
            {"id": 400, "name": "이처리", "user_id": "lee", "active": "Y"}]})
        r["GET /v1/members"] = FakeResponse(200, {"data": [{"node_id": 2, "office_user_no": 100},
                                                           {"node_id": 3, "office_user_no": 300},
                                                           {"node_id": 3, "office_user_no": 400}]})
        return r

    def test_added_people_go_after_defaults_in_bcf_order(self):
        self.serve(self.form_routes())
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            p = hw.vacation_request_payload("2026-12-21", approvers=["kim1"], processors="이처리", refs=["참조인", "lee"])
        self.assertEqual([(u["approval_type"], u["office_user_no"]) for u in p["line_users"]],
                         [("B", 100), ("B", 300), ("C", 400), ("F", 200), ("F", 400)])
        self.assertEqual(p["line_users"][1]["node_id"], 3)

    def test_same_name_twice_must_use_id(self):
        self.serve(self.form_routes())
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError) as cm:
                hw.vacation_request_payload("2026-12-21", approvers=["김결재"])
        self.assertIn("kim1", str(cm.exception))

    def test_role_not_in_form_method_is_refused(self):
        self.serve(self.form_routes(method="BF"))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.vacation_request_payload("2026-12-21", processors=["lee"])

    def test_self_cannot_be_added_as_approver(self):
        self.serve(self.form_routes())
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.vacation_request_payload("2026-12-21", approvers=["hong"])


class WorkCheckTests(ApiBase):
    def status(self, start="Y", end="N"):
        return FakeResponse(200, {"data": {"date": "2026-10-01", "work_status": "출근전", "start_at": "0000-00-00 00:00:00",
                                           "end_at": "0000-00-00 00:00:00", "enable_start": start, "enable_end": end}})

    def test_check_in_posts_type_1(self):
        srv = self.serve({"GET /web/user-work-info": self.status(), "POST /web/time-record": FakeResponse(200, {})})
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            hw.record_work("in")
        posts = [c for c in srv.calls if c[0] == "POST"]
        self.assertEqual(posts[0][2], {"data": {"type": "1"}})

    def test_check_out_refused_when_not_enabled(self):
        srv = self.serve({"GET /web/user-work-info": self.status(end="N")})
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.record_work("out")
        self.assertFalse([c for c in srv.calls if c[0] == "POST"])

    def test_zero_dates_become_none(self):
        self.serve({"GET /web/user-work-info": self.status()})
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            st = hw.work_status()
        self.assertEqual((st["check_in"], st["can_check_in"], st["can_check_out"]), (None, True, False))



class ApprovalDocumentTests(ApiBase):
    DOCS = {"data": [{"id": 1, "document_code": "D-1", "list_status": "PROGRESS", "complete_date": None},
                     {"id": 2, "document_code": "D-2", "list_status": None, "complete_date": "2026-09-01 10:00:00"},
                     {"id": 3, "document_code": "D-3", "list_status": "RETURN", "complete_date": None}]}

    def test_status_derivation_and_filter(self):
        self.serve({"GET /my-documents": FakeResponse(200, self.DOCS)})
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            all_ = hw.approval_documents()
            done = hw.approval_documents(status="완료")
        self.assertEqual([x["status"] for x in all_["items"]], ["진행", "완료", "반려"])
        self.assertEqual([x["code"] for x in done["items"]], ["D-2"])
        self.assertIn("/x.com/approval/document/view/1", all_["items"][0]["url"])

    def test_box_maps_to_server_filter_and_all_sends_none(self):
        srv = self.serve({"GET /my-documents": FakeResponse(200, {"data": []})})
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            hw.approval_documents("read")
            hw.approval_documents("all")
            with self.assertRaises(hiworks.HiworksError):
                hw.approval_documents("없는함")
        urls = [c[1] for c in srv.calls]
        self.assertIn("filter[box_status][in]=CIRCULATION,CC", urls[0])
        self.assertNotIn("box_status", urls[1])


class ApprovalPendingTests(ApiBase):
    ROW = {"checkbox": '<label><input type="checkbox" class="js-checkbox-approval-lists" value="55"></label>',
           "document_code": "<div title='D-7'>D-7</div>",
           "title": "<div title=\"t\"><a onclick=\"ApprovalDocument.getView('7')\">휴가 신청 &amp; 연차</a></div>",
           "register": '<div title="기안자">기안자</div>', "type": '<div title="신청">신청</div>',
           "document_regdate": "2026-01-02", "document_regdate_title": "2026-01-02 09:00:00",
           "link": "javascript:ApprovalDocument.getView('7', 'W');"}

    def server(self, approve_result="SUCCESS", clears=True):
        """대기 목록은 승인 호출 전엔 ROW 한 줄(어느 쪽이든 같은 줄), 승인이 받아들여지고 clears 면 빈 목록."""
        state = {"waiting": True}

        def ajax(kw):
            d = kw.get("data") or {}
            if d.get("pMenu") == "get_document_list":
                rows = [self.ROW] if state["waiting"] else []
                return FakeResponse(200, {"resultCode": "SUCCESS", "result": rows, "message": "문서가 존재하지 않습니다."})
            if d.get("pMenu") == "approval_action":
                if approve_result == "SUCCESS" and clears:
                    state["waiting"] = False
                return FakeResponse(200, {"resultCode": approve_result, "message": "처리 결과"})
            return FakeResponse(200, {"resultCode": "FAIL"})
        return self.serve({"POST /approval/document_ajax/": ajax})

    def menus(self, srv):
        return [c[2].get("pMenu") for c in srv.calls if isinstance(c[2], dict)]

    def test_pending_parses_html_rows_and_stops_on_repeated_page(self):
        srv = self.server()
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            out = hw.approval_pending()
        self.assertEqual(out["count"], 1)  # 같은 줄이 다음 쪽에도 오면 거기서 멈춘다
        item = out["items"][0]
        self.assertEqual((item["document_no"], item["approval_no"], item["code"], item["title"], item["drafter"]),
                         (7, 55, "D-7", "휴가 신청 & 연차", "기안자"))
        self.assertIn("/x.com/approval/document/view/7", item["url"])
        self.assertEqual([c[2]["pStatus"] for c in srv.calls], ["W", "W"])

    def test_approve_sends_my_approval_no_and_confirms_it_left_waiting(self):
        srv = self.server()
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            out = hw.approve_documents([7])
        action = [c[2] for c in srv.calls if isinstance(c[2], dict) and c[2].get("pMenu") == "approval_action"]
        self.assertEqual([a["pNo[]"] for a in action], [["55"]])  # 문서 번호가 아니라 내 결재 칸 번호
        self.assertIsNone(out["approved"][0]["my_box"])  # 상세를 못 읽으면 처리 여부를 모른다(성공으로 치지 않음)
        self.assertEqual(([x["document_no"] for x in out["approved"]], out["not_approved"]), ([7], []))

    def test_document_not_waiting_sends_nothing(self):
        srv = self.server()
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.approve_documents([7, 8])  # 8 은 내 차례가 아니다 — 7 도 보내지 않는다
        self.assertNotIn("approval_action", self.menus(srv))

    def test_success_message_alone_is_not_trusted(self):
        self.server(clears=False)
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            out = hw.approve_documents([7])
        self.assertEqual((out["approved"], [x["document_no"] for x in out["not_approved"]]), ([], [7]))

    def test_server_refusal_raises(self):
        self.server(approve_result="FAIL")
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.approve_documents([7])

    def test_changed_row_format_raises_instead_of_guessing(self):
        with self.assertRaises(hiworks.HiworksError):
            hiworks.parse_pending_row({"checkbox": "", "title": "getView('7')"}, "x.com")

    def test_document_no_comes_from_link_not_drafter_title(self):
        row = dict(self.ROW, title="<div title=\"getView('9')\"><a onclick=\"ApprovalDocument.getView('7')\">getView('9')</a></div>")
        self.assertEqual(hiworks.parse_pending_row(row, "x.com")["document_no"], 7)
        with self.assertRaises(hiworks.HiworksError):  # 제목 링크와 link 가 다른 문서를 가리키면 거절
            hiworks.parse_pending_row(dict(self.ROW, link="javascript:ApprovalDocument.getView('9', 'W');"), "x.com")

    def test_same_document_twice_in_waiting_list_stops(self):
        other = dict(self.ROW, checkbox=self.ROW["checkbox"].replace('"55"', '"56"'))
        state = {"n": 0}

        def ajax(kw):
            state["n"] += 1
            return FakeResponse(200, {"resultCode": "SUCCESS", "result": [self.ROW, other] if state["n"] == 1 else []})
        srv = self.serve({"POST /approval/document_ajax/": ajax})
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.approve_documents([7])
        self.assertNotIn("approval_action", self.menus(srv))


class SavedLineTests(ApiBase):
    CHECK_OK = VacationRequestTests.CHECK_OK
    routes = VacationRequestTests.routes
    form_routes = ApprovalLineTests.form_routes

    def test_saved_line_goes_first_and_can_be_skipped(self):
        hiworks.save_vacation_line({"approvers": ["kim1"], "processors": [], "refs": ["lee"]})
        self.serve(self.form_routes())
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            p = hw.vacation_request_payload("2026-12-21", refs=["참조인"])
            q = hw.vacation_request_payload("2026-12-21", use_saved_line=False)
        self.assertEqual([(u["approval_type"], u["office_user_no"]) for u in p["line_users"]],
                         [("B", 100), ("B", 300), ("F", 200), ("F", 400)])
        self.assertEqual([(u["approval_type"], u["office_user_no"]) for u in q["line_users"]], [("B", 100), ("F", 200)])

    def test_clearing_removes_key_and_keeps_email(self):
        hiworks.write_private(hiworks.config_file(), {"email": "a@x.com"})
        hiworks.save_vacation_line({"approvers": ["kim1"]})
        self.assertEqual(hiworks.saved_vacation_line()["approvers"], ["kim1"])
        hiworks.save_vacation_line({"approvers": [], "processors": [], "refs": []})
        self.assertEqual(json.loads(hiworks.config_file().read_text()), {"email": "a@x.com"})



class ApprovalLineLookupTests(ApiBase):
    VIEW = ("<script>ApprovalProcess._documentNo = '9'; ApprovalProcess._firstLine = '100,200'; "
            "ApprovalProcess._secondLine = ''; ApprovalProcess._thirdLine = '300'; ApprovalProcess._fourthLine = ''; "
            "ApprovalProcess._approvalMethod = 'BCF'; ApprovalProcess._registerNo = '100';</script>")
    MISSING = '<script>\nalert("존재하지 않은 문서입니다.");\ndocument.location.href="/x.com/approval/document";</script>'

    def dir_routes(self):
        return {**HrTests.DIR, "GET /v1/employees": FakeResponse(200, {"data": [
            {"id": 100, "name": "기안자", "active": "Y"}, {"id": 200, "name": "결재자", "active": "Y"},
            {"id": 300, "name": "참조자", "active": "Y"}]})}

    def test_lines_map_to_method_letters(self):
        r = self.dir_routes(); r["GET /approval/document/view/9"] = FakeResponse(200, "")
        r["GET /approval/document/view/9"].body = self.VIEW.encode()
        self.serve(r)
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            out = hw.approval_line(9)
        self.assertEqual((out["approval_method"], out["drafter"]), ("BCF", "기안자"))
        self.assertEqual([(l["line"], l["role"], [p["name"] for p in l["people"]]) for l in out["lines"]],
                         [(1, "결재(신청)", ["기안자", "결재자"]), (3, "참조", ["참조자"])])

    def test_missing_document_raises_with_server_message(self):
        r = self.dir_routes(); r["GET /approval/document/view/8"] = FakeResponse(200, "")
        r["GET /approval/document/view/8"].body = self.MISSING.encode()
        self.serve(r)
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError) as cm:
                hw.approval_line(8)
        self.assertIn("존재하지 않은 문서", str(cm.exception))

    def test_history_keeps_rows_when_line_unavailable(self):
        r = self.dir_routes()
        r["GET /my-vacations/use-details"] = FakeResponse(200, {"data": [{"document_no": 8, "approval_status": "결재완료",
            "vacation_types": [{"vacation_type_name": "연차", "days": "2", "date_range_start": "2026-09-28", "date_range_end": "2026-09-29"}]}]})
        r["GET /approval/document/view/8"] = FakeResponse(200, ""); r["GET /approval/document/view/8"].body = self.MISSING.encode()
        self.serve(r)
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            h = hw.my_vacation_history(2026, with_lines=True)
        self.assertEqual((h[0]["start"], h[0]["approval_line"]), ("2026-09-28", None))
        self.assertIn("존재하지 않은 문서", h[0]["approval_line_error"])



class MailExtraTests(ApiBase):
    def mail_routes(self):
        return {
            "POST /mails/cert-key": FakeResponse(200, {"data": "CK"}),
            "GET /senders": FakeResponse(200, {"data": [{"no": 7, "address": "a@x.com", "is_default": True}]}),
            "POST /mails/send": FakeResponse(200, {"data": {"send_type": "NORMAL"}}),
            "GET /mails/5": FakeResponse(200, {"data": {"no": 5, "from": "B <b@x.com>", "to_address": ["a@x.com"],
                                                       "subject": "원래 제목", "message": {"content": "<p>원문</p>",
                                                       "attachments": [{"part_id": "1.2", "name": "f.txt", "size": 3}]}}}),
        }

    def sent_body(self, srv):
        return [c for c in srv.calls if c[1].endswith("/mails/send")][0][2]

    def test_reply_sets_flags_subject_and_quotes_original(self):
        srv = self.serve(self.mail_routes())
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            hw.send_mail("b@x.com", None, "답장", reply_to=5)
        b = self.sent_body(srv)
        self.assertEqual((b["is_reply"], b["is_forward"], b["original_mail_no"], b["response_target_mail_no"]), (True, False, 5, 5))
        self.assertEqual(b["subject"], "RE: 원래 제목")
        self.assertIn("<p>원문</p>", b["content"])
        self.assertEqual(b["original_temp_part_id_list"], [])

    def test_forward_carries_original_attachments(self):
        srv = self.serve(self.mail_routes())
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            out = hw.send_mail("c@x.com", None, "전달", forward_of=5)
        b = self.sent_body(srv)
        self.assertEqual((b["is_forward"], b["subject"], b["original_temp_part_id_list"]), (True, "FW: 원래 제목", ["1.2"]))
        self.assertEqual(out["forwarded_attachments"], 1)

    def test_missing_attachment_file_refused_before_any_call(self):
        srv = self.serve(self.mail_routes())
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.send_mail("b@x.com", "s", "b", attachments=["/no/such/file.txt"])
        self.assertEqual(srv.calls, [])

    def test_reply_and_forward_together_refused(self):
        self.serve(self.mail_routes())
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.send_mail("b@x.com", "s", "b", reply_to=5, forward_of=5)

    def test_attachment_meta_key_variants(self):
        self.assertEqual(hiworks.summarize_attachment({"partId": "2", "file_name": "a.pdf", "file_size": 9})["name"], "a.pdf")


class CalendarTimeTests(unittest.TestCase):
    def test_local_to_utc_uses_machine_timezone(self):
        with patch.dict(os.environ, {"TZ": "Asia/Seoul"}):
            import time as _t; _t.tzset()
            self.assertEqual(hiworks.local_to_utc("2026-10-02 23:00", False, False), "2026-10-02T14:00:00Z")
            self.assertEqual(hiworks.local_to_utc("2026-10-02", True, True), "2026-10-02T14:59:00Z")
            self.assertEqual(hiworks.utc_to_local("2026-10-02T14:30:00Z"), "2026-10-02 23:30")
        import time as _t; _t.tzset()

    def test_bad_time_format(self):
        with self.assertRaises(hiworks.HiworksError):
            hiworks.local_to_utc("10/2 3pm", False, False)


class VacationCancelTests(ApiBase):
    def routes(self, days):
        r = VacationRequestTests.routes(self, VacationRequestTests.CHECK_OK)
        r["GET /vacation-request-calendar"] = FakeResponse(200, {"data": {"calendar_data": [
            {"date": d, "vacation_request_details": [{"request_detail_id": 900 + i, "vacation_type_title": "연차", "time_type": "D", "days": 1}]}
            for i, d in enumerate(days)] + [{"date": "2026-12-26", "vacation_request_details": []}]}})
        r["POST /vacation-cancel"] = FakeResponse(200, {"data": {}})
        return r

    def test_payload_lists_detail_numbers_and_form_line(self):
        self.serve(self.routes(["2026-12-21", "2026-12-22"]))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            p, days = hw.vacation_cancel_payload("2026-12-21", "2026-12-22", reason="사정")
        self.assertEqual(p["vacation_request_detail_nos"], [900, 901])
        self.assertEqual((p["form_id"], p["node_id"], p["year"], p["comment"]), (9, 2, "2026", "사정"))
        self.assertNotIn("period_selections", p)
        self.assertEqual([d["date"] for d in days], ["2026-12-21", "2026-12-22"])

    def test_no_leave_in_range_is_refused(self):
        self.serve(self.routes([]))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.vacation_cancel_payload("2026-12-21")

    def test_cancel_posts_wrapped_payload_once(self):
        srv = self.serve(self.routes(["2026-12-21"]))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            p, _ = hw.vacation_cancel_payload("2026-12-21")
            hw.cancel_vacation(p)
        sent = [c for c in srv.calls if c[1].endswith("/vacation-cancel")]
        self.assertEqual((len(sent), sent[0][2]), (1, {"data": p}))


class TodayTests(ApiBase):
    def test_one_failing_part_does_not_break_others(self):
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with patch.object(hw, "list_mails", return_value={"items": [
                    {"no": 1, "from": "x", "subject": "s", "received": "t", "unread": True},
                    {"no": 2, "from": "y", "subject": "t", "received": "t", "unread": False}]}), \
                 patch.object(hw, "approval_counts", side_effect=hiworks.HiworksApiError(500, "ERR")), \
                 patch.object(hw, "schedules", return_value=[]), \
                 patch.object(hw, "work_status", return_value={"status": "출근전"}), \
                 patch.object(hw, "me", return_value={"office_user_no": 1}), \
                 patch.object(hw, "_directory", return_value={"belongs": {1: [2]}, "nodes": {2: {"node_name": "개발팀"}}}), \
                 patch.object(hw, "vacation_calendar", return_value=[
                     {"name": "동료", "office_user_no": 3, "departments": ["개발팀"], "type": "연차", "start": "2000-01-01",
                      "end": "2999-12-31", "full_day": True, "start_time": None, "end_time": None}]):
                out = hw.today()
        self.assertEqual(out["unread_mail"]["count_in_latest_50"], 1)
        self.assertIn("error", out["approval"])
        self.assertEqual([x["name"] for x in out["team_on_leave"]], ["동료"])


def detail_view(no=1000001, stamps=("2026-11-10", "2026-11-10", ""), complete="", rows=None, form="공통 > 휴가신청서",
                body_extra="", script_extra=""):
    """합성 문서 보기 화면 — 실제 화면 순서(머리 표·결재 칸 → 본문 contents after → 의견 탭 → ApprovalProcess 스크립트)만 본뜸."""
    rows = rows or [("연차", "2026년 11월 27일(금) ~ 2026년 12월 02일(수)", "4일")]
    st = "".join(f'<td class="stamp"> <img src="/x.com/approval/sign/approval/B/2/{n}" alt="가">'
                 + (f'<p class="date" title="{d} 10:00:00">{d} </p>' if d else "") + " </td>"
                 for n, d in zip((100, 200, 300), stamps))
    body = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in rows)
    return (f"<h1>휴가신청서</h1><table><tr><th>문서 종류</th><td>{form}</td><th>문서 번호</th>"
            f"<td class=\"gt-pr-10\">X-휴가-1</td></tr><tr><th>기안 일시</th><td>2026-11-10 10:00:00</td>"
            f"<th>완료 일시</th><td>{complete}</td></tr></table>"
            f"<table><tr>{st}<td class=\"stamp\"> </td></tr></table>"
            '<div class="contents after"><div class="hw-approval-docs"><section><div class="section-title">사유</div>'
            f'<div class="section-data">개인 사유 문장{body_extra}</div></section>'
            f'<table class="setting-table"><thead><tr><th>휴가 종류</th><th>신청일</th><th>일수</th></tr></thead><tbody>{body}</tbody></table></div></div>'
            '<div class="approval-comment-tab" id="approvalCommentsTab"></div>'
            f"<script>ApprovalProcess._documentNo = '{no}'; ApprovalProcess._firstLine = '100,200,300'; "
            "ApprovalProcess._secondLine = ''; ApprovalProcess._thirdLine = '400'; ApprovalProcess._approvalMethod = 'BCF'; "
            f"ApprovalProcess._registerNo = '100'; ApprovalProcess._documentType = 'hr_vacation';{script_extra}</script>")


class ApprovalDetailTests(ApiBase):
    def routes(self, view, status="PROGRESS", no=1000001):
        r = {f"GET /approval/document/view/{no}": FakeResponse(200, ""),
             "GET /my-documents": FakeResponse(200, {"data": [{"id": no, "document_code": "X-휴가-1",
                                                              "list_status": status, "complete_date": None}]})}
        r[f"GET /approval/document/view/{no}"].body = view.encode()
        return r

    def test_progress_document_fields_and_boxes(self):
        self.serve(self.routes(detail_view()))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            d = hw.approval_detail(1000001)
        self.assertEqual((d["form"], d["leave_kind"], d["start"], d["end"], d["days"], d["person"], d["doc_state"]),
                         ("휴가신청서", "연차", "2026-11-27", "2026-12-02", 4.0, "100", "진행"))
        self.assertEqual(d["boxes"], [{"hw": "100", "processed": True}, {"hw": "200", "processed": True},
                                      {"hw": "300", "processed": False}])
        self.assertEqual(d["turn_hw"], "300")
        self.assertNotIn("사유", json.dumps(d, ensure_ascii=False))  # 사유·제목은 돌려주지 않는다

    def test_complete_and_return_states(self):
        self.serve(self.routes(detail_view(stamps=("2026-11-10",) * 3, complete="2026-11-11 09:00:00"), status=None))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            self.assertEqual(hw.approval_detail(1000001)["doc_state"], "완료")
        self.serve(self.routes(detail_view(stamps=("2026-11-10", "", "")), status="RETURN"))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            d = hw.approval_detail(1000001)
        self.assertEqual((d["doc_state"], d["turn_hw"]), ("반려", None))

    def test_progress_view_without_kind_cell_uses_document_type(self):
        # 실측 2026-11-18: 진행 중 휴가 문서 보기 화면에 머리 「문서 종류」 칸이 없어 form 이 None 이었다 —
        # 신뢰 영역의 결재 변수 documentType=hr_vacation 으로 휴가신청서를 판정한다
        v = detail_view().replace("<th>문서 종류</th><td>공통 > 휴가신청서</td>", "")
        self.serve(self.routes(v))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            d = hw.approval_detail(1000001)
        self.assertEqual((d["form"], d["leave_kind"], d["doc_state"]), ("휴가신청서", "연차", "진행"))

    def test_kind_cell_absent_and_not_vacation_stays_none(self):
        v = detail_view().replace("<th>문서 종류</th><td>공통 > 휴가신청서</td>", "").replace("hr_vacation", "expense")
        self.serve(self.routes(v))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            self.assertIsNone(hw.approval_detail(1000001)["form"])

    def test_half_day_times(self):
        self.serve(self.routes(detail_view(rows=[("오전반차", "2026년 11월 26일(목) 09:00 ~ 13:00", "0.5일")])))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            d = hw.approval_detail(1000001)
        self.assertEqual((d["leave_kind"], d["start"], d["end"], d["start_time"], d["end_time"], d["days"]),
                         ("오전반차", "2026-11-26", "2026-11-26", "09:00", "13:00", 0.5))

    def test_non_leave_form(self):
        v = detail_view(form="공통 > 지출결의서").replace("hr_vacation", "expense")
        self.serve(self.routes(v))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            d = hw.approval_detail(1000001)
        self.assertEqual((d["form"], d["leave_kind"], d["start"]), ("지출결의서", None, None))

    def test_body_cannot_forge_stamps_or_dates(self):
        # 기안자 본문에 가짜 도장 칸·휴가 표를 넣어도 결재 칸·기간은 고정 영역에서만 읽는다(보안 검토 2026-11-18)
        fake = ('<table><tr><td class="stamp"><p class="date">2026-11-18 </p></td></tr></table>'
                '<table class="setting-table"><tbody><tr><td>연차</td><td>2027년 01월 01일(금)</td><td>1일</td></tr></tbody></table>')
        self.serve(self.routes(detail_view(stamps=("2026-11-10", "", ""), body_extra=fake)))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):  # 본문 안 휴가 표가 둘 — 어느 것이 진짜인지 몰라 멈춤
                hw.approval_detail(1000001)
        self.serve(self.routes(detail_view(stamps=("2026-11-10", "", ""), body_extra=fake[:fake.index("<table class")])))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            d = hw.approval_detail(1000001)
        self.assertEqual([b["processed"] for b in d["boxes"]], [True, False, False])

    def test_fake_comment_marker_cannot_hide_real_table(self):
        fake = ('<table class="setting-table"><tbody><tr><td>연차</td><td>2027년 01월 01일(금)</td><td>1일</td></tr></tbody></table>'
                '<div id="approvalCommentsTab"></div>')
        self.serve(self.routes(detail_view(body_extra=fake)))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.approval_detail(1000001)

    def test_table_after_comment_tab_refused(self):
        v = detail_view(rows=[("연차", "2026년 11월 27일(금)", "1일")]).replace(
            '<table class="setting-table">', '<table class="x">', 1).replace(
            '<div class="approval-comment-tab" id="approvalCommentsTab"></div>',
            '<div class="approval-comment-tab" id="approvalCommentsTab"></div><table class="setting-table"><tbody>'
            '<tr><td>연차</td><td>2027년 01월 01일(금)</td><td>1일</td></tr></tbody></table>')
        self.serve(self.routes(v))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.approval_detail(1000001)

    def test_same_value_vars_in_body_refused(self):
        self.serve(self.routes(detail_view(body_extra="ApprovalProcess._registerNo = '100';")))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.approval_detail(1000001)

    def test_conflicting_approval_vars_refused(self):
        v = detail_view(body_extra="ApprovalProcess._registerNo = '999';")
        self.serve(self.routes(v))
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with self.assertRaises(hiworks.HiworksError):
                hw.approval_detail(1000001)

    def test_whoami_returns_person_number(self):
        self.serve({"GET /me": FakeResponse(200, {"data": {"name": "가나", "user_id": "u1", "office_user_no": "300"}})})
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            me = hw.me()
        self.assertEqual((me["office_user_no"], me["user_id"]), (300, "u1"))

    def test_approve_reports_my_box(self):
        state = {"done": False}
        pending_row = {"checkbox": '<label><input type="checkbox" class="js-checkbox-approval-lists" value="77"></label>',
                       "link": "javascript:ApprovalDocument.getView('1000001')", "title": "t", "register": "r"}

        def pending(_kw):
            return FakeResponse(200, {"resultCode": "SUCCESS", "result": [] if state["done"] else [pending_row]})

        def action(kw):
            if kw["data"].get("pMenu") == "approval_action":
                state["done"] = True
                return FakeResponse(200, {"resultCode": "SUCCESS", "message": "ok"})
            return pending(kw)

        r = self.routes(detail_view(stamps=("2026-11-10", "2026-11-10", "2026-11-18")))
        r["POST /approval/document_ajax"] = action
        r["GET /v1/employees/me"] = FakeResponse(200, {"data": {"id": 300}})
        self.serve(r)
        with hiworks.Hiworks(username="a@x.com", password="pw") as hw:
            with patch.object(hiworks.Hiworks, "_my_person_no", return_value="300"):
                out = hw.approve_documents([1000001])
        self.assertEqual(out["approved"][0]["my_box"], True)
        self.assertEqual(out["approved"][0]["doc_state"], "진행")


if __name__ == "__main__":
    unittest.main()
