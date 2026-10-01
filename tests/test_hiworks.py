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


if __name__ == "__main__":
    unittest.main()
