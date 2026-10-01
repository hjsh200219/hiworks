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


if __name__ == "__main__":
    unittest.main()
