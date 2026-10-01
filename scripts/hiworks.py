#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["scrapling[fetchers]>=0.4.11", "keyring>=25"]
# ///
"""hiworks — 하이웍스(Hiworks) 로그인 세션을 브라우저 없이 만들고 재사용한다(Scrapling).

실행은 항상 `uv run <이 파일> <명령>` — 의존성은 위 inline 메타데이터로 uv 가 붙인다(Windows 도 동일).

  setup [--email E] [--password-stdin] [--force]
                                         자격증명 등록. 로그인 1회로 확인한 뒤에만 저장한다.
                                         이미 등록돼 있으면 창 없이 세션만 확인(비번이 바뀌어 거절되면 새로 받음)
  status                                 등록 상태(값은 출력하지 않음 · 네트워크 안 씀)
  check                                  저장 세션이 살아 있는지만 본다(로그인 안 함) · 죽었으면 exit 1
  session [--no-prompt]                  살아 있으면 재사용, 죽었으면 1회 로그인(2단계 인증 코드는 입력 창)
  login [--no-prompt]                    저장 세션을 무시하고 새로 로그인
  get <url>                              세션으로 인증된 GET(API 탐색용)
  forget                                 키체인 항목·설정·세션 파일 삭제

자격증명 보관:
- 이메일(비밀 아님) → $HIWORKS_HOME/config.json (기본 ~/.hiworks, 0600)
- 비밀번호 → OS 키체인(keyring: macOS 키체인 · Windows 자격 증명 관리자 · Linux Secret Service), 서비스명 "hiworks"
- 세션 쿠키 → $HIWORKS_HOME/session.json (0600)
- 환경변수 HIWORKS_USERNAME / HIWORKS_PASSWORD 가 있으면 그쪽이 우선한다(자동화용 탈출구).
비밀번호는 명령행 인자로 받지 않는다(프로세스 목록에 남는다). 입력 경로는 셋뿐이다 —
터미널이면 getpass, 터미널이 없으면(Claude Code 의 Bash 도구) OS 비밀번호 창(macOS osascript · Windows Get-Credential),
자동화면 --password-stdin.

로그인 API(2026-10-01 login.office.hiworks.com 번들에서 발굴·실측):
- POST https://auth-api.office.hiworks.com/office-web/login
  JSON {"id": "<id>@<office_domain>", "password": ..., "ip_security_level": "1"} · Origin/Referer login.office.hiworks.com
- 200 {"data": {"office_main", "is_v3_product"}} → 성공. 쿠키 PHPSESSID·h_officeid(.hiworks.com)
- 202 {"errors": [{"title"}]} → 비번은 맞고 단계가 남음. REQUIRE_OTP_VALIDATION 이면
  POST /office-web/otp {"otp_code": "<6자리>"} — 응답 판정은 로그인과 같다. 그 밖(OTP 설정·비번 변경·로그인 제한)은 브라우저 몫.
- 4xx {"errors": [{"title": "ACCOUNT_NOT_FOUND" ...}]} → 실패. 틀린 비번·OTP 는 횟수가 쌓여 잠긴다
  (ALLOWED_OTP_MISMATCH_COUNT_EXCEED) — 그래서 어떤 단계도 재시도하지 않고 요청도 retries=1(=1회 시도)이다.
- ip_security_level: "-1" 사용 안 함 · "1" C클래스 대역(브라우저 기본) · "2" IP 고정.
  "1" 이면 네트워크(공인 IP 대역)가 바뀔 때 저장 세션이 죽는다 → session 이 다시 로그인한다.

세션 생존 판정: board.office.hiworks.com/<domain>/bbs/board/board_list 는 로그인 여부와 무관하게 HTTP 200 이고
본문 document.location.href 가 갈린다 — 비로그인 login.office.hiworks.com, 로그인이면 다른 앱 호스트.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

AUTH_API = "https://auth-api.office.hiworks.com"
LOGIN_ORIGIN = "https://login.office.hiworks.com"
LIVENESS_URL = "https://board.office.hiworks.com/{domain}/bbs/board/board_list"
KEYRING_SERVICE = os.environ.get("HIWORKS_KEYRING_SERVICE", "hiworks")
STEP_TITLES = {
    "REQUIRE_OTP_VALIDATION": "2단계 인증 코드 입력이 필요합니다",
    "REQUIRE_OTP_SETTING": "2단계 인증 설정이 필요합니다(브라우저에서 하이웍스에 로그인해 설정하세요)",
    "REQUIRE_PASSWORD_CHANGE": "비밀번호 변경이 필요합니다(브라우저에서 하이웍스에 로그인해 변경하세요)",
    "LOGIN_RESTRICTION_CHECK": "로그인 제한 확인이 필요합니다(브라우저에서 하이웍스에 로그인해 확인하세요)",
}
SETUP_HINT = "자격증명이 없습니다. 먼저 `/hiworks:setup` 또는 `uv run <플러그인>/scripts/hiworks.py setup` 을 실행하세요."


def home_dir() -> Path:
    return Path(os.environ.get("HIWORKS_HOME") or Path.home() / ".hiworks")


def config_file() -> Path:
    return home_dir() / "config.json"


def session_file() -> Path:
    return home_dir() / "session.json"


class HiworksError(Exception):
    pass


class HiworksLoginError(HiworksError):
    """로그인·OTP 거절(4xx·5xx). title 은 하이웍스 오류 코드(ACCOUNT_NOT_FOUND 등)."""

    def __init__(self, status: int, title: str | None):
        self.status, self.title = status, title
        super().__init__(f"로그인 실패 HTTP {status} {title or ''}".strip())


class HiworksStepRequired(HiworksError):
    """202 — 비번은 맞았지만 남은 단계가 있다."""

    def __init__(self, title: str | None, values: dict | None = None):
        self.title, self.values = title, values or {}
        super().__init__(f"{title}: {STEP_TITLES.get(title or '', '알 수 없는 추가 단계입니다')}")


# ---------- 파일 ----------

def write_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


# ---------- 자격증명 ----------

def keyring_backend():
    """쓸 수 있는 키체인 백엔드를 돌려준다. 없으면(Linux 헤드리스 등) HiworksError."""
    import keyring
    kr = keyring.get_keyring()
    name = f"{type(kr).__module__}.{type(kr).__name__}"
    if "fail" in name or "null" in name or getattr(kr, "priority", 1) <= 0:
        raise HiworksError(f"이 컴퓨터에서 쓸 수 있는 키체인이 없습니다({name}). "
                           "환경변수 HIWORKS_USERNAME/HIWORKS_PASSWORD 로 대신 지정하세요.")
    return kr


def configured_email() -> tuple[str | None, str]:
    """(이메일, 출처)."""
    if os.environ.get("HIWORKS_USERNAME"):
        return os.environ["HIWORKS_USERNAME"].strip(), "환경변수 HIWORKS_USERNAME"
    email = read_json(config_file()).get("email")
    return (email, str(config_file())) if email else (None, "")


def stored_password(email: str) -> tuple[str | None, str]:
    """(비밀번호, 출처). 값은 호출부가 출력하지 않는다."""
    if os.environ.get("HIWORKS_PASSWORD"):
        return os.environ["HIWORKS_PASSWORD"], "환경변수 HIWORKS_PASSWORD"
    kr = keyring_backend()
    pw = kr.get_password(KEYRING_SERVICE, email)
    return (pw, f"키체인({type(kr).__name__}) 서비스 {KEYRING_SERVICE}") if pw else (None, "")


def store_credentials(email: str, password: str) -> None:
    keyring_backend().set_password(KEYRING_SERVICE, email, password)
    write_private(config_file(), {"email": email, "saved_at": int(time.time())})


def forget_credentials() -> list[str]:
    done = []
    email = read_json(config_file()).get("email")
    if email:
        try:
            keyring_backend().delete_password(KEYRING_SERVICE, email)
            done.append(f"키체인 항목({KEYRING_SERVICE} / {email})")
        except Exception:  # noqa: BLE001 — 항목이 없거나 백엔드가 없으면 지울 것도 없다
            pass
    for p in (config_file(), session_file()):
        if p.exists():
            p.unlink()
            done.append(str(p))
    return done


# ---------- 입력 ----------

def _osascript_dialog(message: str, hidden: bool) -> str:
    esc = message.replace("\\", "\\\\").replace('"', '\\"')
    script = (f'display dialog "{esc}" default answer "" '
              f'{"with hidden answer " if hidden else ""}with title "Hiworks" '
              'buttons {"취소", "확인"} default button "확인" cancel button "취소"\n'
              "text returned of result")
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise HiworksError("입력을 취소했습니다.")
    return r.stdout.rstrip("\n")


def _windows_dialog(message: str, hidden: bool, username: str | None) -> str:
    msg = message.replace("'", "''")
    if hidden:
        user = (username or "").replace("'", "''")
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
              f"$c=Get-Credential -UserName '{user}' -Message '{msg}';"
              "if(-not $c){exit 1};[Console]::Out.Write($c.GetNetworkCredential().Password)")
    else:
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;Add-Type -AssemblyName Microsoft.VisualBasic;"
              f"$v=[Microsoft.VisualBasic.Interaction]::InputBox('{msg}','Hiworks');"
              "if(-not $v){exit 1};[Console]::Out.Write($v)")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=600)
    if r.returncode != 0:
        raise HiworksError("입력을 취소했습니다.")
    return r.stdout.decode("utf-8", "replace")


def ask(message: str, hidden: bool, username: str | None = None) -> str:
    """사람에게 값을 받는다. 터미널 → getpass/input, 아니면 OS 입력 창. 채팅·명령행 인자는 쓰지 않는다."""
    if sys.stdin.isatty():
        return getpass.getpass(f"{message}: ") if hidden else input(f"{message}: ")
    if sys.platform == "darwin":
        return _osascript_dialog(message, hidden)
    if os.name == "nt":
        return _windows_dialog(message, hidden, username)
    raise HiworksError("입력 창을 띄울 수 없습니다. 터미널에서 직접 `uv run <플러그인>/scripts/hiworks.py setup` 을 실행하세요.")


def ask_otp() -> str:
    code = ask("하이웍스 2단계 인증 코드(숫자 6자리)", hidden=False).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise HiworksError("2단계 인증 코드는 숫자 6자리여야 합니다.")
    return code


# ---------- 로그인 ----------

def parse_login_response(status: int, body: dict | None) -> dict:
    """로그인·OTP 응답을 판정한다. 성공이면 data, 아니면 예외. 상태 코드와 errors[0].title 로만 가른다."""
    body = body or {}
    err = (body.get("errors") or [{}])[0] or {}
    if status == 200:
        return body.get("data") or {}
    if status == 202:
        raise HiworksStepRequired(err.get("title"), err.get("values"))
    raise HiworksLoginError(status, err.get("title"))


def redirect_host(html: str) -> str | None:
    m = re.search(r'location\.href\s*=\s*["\']([^"\']+)', html)
    return urlparse(m.group(1)).hostname if m else None


def is_logged_in_html(html: str) -> bool:
    host = redirect_host(html)
    return host is not None and host != "login.office.hiworks.com"


class Hiworks:
    def __init__(self, username: str | None = None, password: str | None = None,
                 ip_security_level: str = "1", session_path: Path | None = None):
        self.username = username or configured_email()[0]
        if not self.username:
            raise HiworksError(SETUP_HINT)
        if "@" not in self.username:
            raise HiworksError("이메일은 아이디@회사도메인 형식이어야 합니다.")
        self.office_domain = self.username.split("@", 1)[1]
        self._password = password
        self.ip_security_level = ip_security_level
        self.session_path = session_path or session_file()
        self.password_ok = False  # 1단계(비밀번호)가 200·202 로 통과했는가 — setup 이 저장 여부를 가른다
        self._fs = self._s = None

    def __enter__(self) -> "Hiworks":
        from scrapling.fetchers import FetcherSession
        logging.getLogger("scrapling").setLevel(logging.WARNING)  # import 가 INFO 로 다시 올리므로 그 뒤에 내린다
        self._fs = FetcherSession(impersonate="chrome", retries=1)
        self._s = self._fs.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        if self._fs:
            self._fs.__exit__(*exc)
        self._fs = self._s = None

    # scrapling 0.4.11 FetcherSession 에는 쿠키 공개 API 가 없다 — 내부 curl_cffi 세션을 직접 만지는 곳은 여기뿐.
    def _jar(self):
        return self._s._curl_session.cookies

    def cookies(self) -> list[dict]:
        return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                 "secure": c.secure, "expires": c.expires} for c in self._jar().jar]

    def load(self) -> bool:
        data = read_json(self.session_path)
        if data.get("username") != self.username:
            return False
        jar = self._jar()
        for c in data.get("cookies", []):
            jar.set(c["name"], c["value"], domain=c["domain"], path=c.get("path") or "/", secure=c.get("secure", False))
        return True

    def save(self) -> Path:
        write_private(self.session_path, {"username": self.username, "ip_security_level": self.ip_security_level,
                                          "saved_at": int(time.time()), "cookies": self.cookies()})
        return self.session_path

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict | None]:
        r = self._s.post(f"{AUTH_API}{path}", retries=1, json=payload,
                         headers={"Origin": LOGIN_ORIGIN, "Referer": f"{LOGIN_ORIGIN}/", "accept": "application/json"})
        try:
            return r.status, json.loads(r.body or b"{}")
        except ValueError:
            return r.status, None

    def login(self, otp_provider: Callable[[], str] | None = None) -> dict:
        """1회 로그인하고 세션을 저장한다. 실패는 예외 — 재시도 금지(틀린 비번·OTP 횟수가 쌓인다)."""
        password = self._password or stored_password(self.username)[0]
        if not password:
            raise HiworksError(SETUP_HINT)
        self._jar().clear()
        self.password_ok = False
        status, body = self._post_json("/office-web/login", {
            "id": self.username, "password": password, "ip_security_level": self.ip_security_level})
        try:
            data = parse_login_response(status, body)
            self.password_ok = True
        except HiworksStepRequired as e:
            self.password_ok = True
            if e.title != "REQUIRE_OTP_VALIDATION" or otp_provider is None:
                raise
            status, body = self._post_json("/office-web/otp", {"otp_code": otp_provider()})
            data = parse_login_response(status, body)
        self.save()
        return data

    def is_alive(self) -> bool:
        r = self._s.get(LIVENESS_URL.format(domain=self.office_domain))
        return r.status == 200 and is_logged_in_html(r.body.decode("utf-8", "ignore"))

    def ensure(self, otp_provider: Callable[[], str] | None = None) -> str:
        """저장 세션이 살아 있으면 "reused", 아니면 1회 로그인하고 "login"."""
        if self.load() and self.is_alive():
            return "reused"
        self.login(otp_provider)
        return "login"

    def get(self, url: str, **kw):
        return self._s.get(url, **kw)

    def post(self, url: str, **kw):
        return self._s.post(url, **kw)


# ---------- CLI ----------

def cmd_setup(a) -> int:
    keyring_backend()  # 저장할 곳이 없으면 비밀번호를 묻기 전에 멈춘다
    otp = None if a.no_prompt else ask_otp
    email = a.email or configured_email()[0]
    if email and not a.force:
        # 이미 등록돼 있으면 창을 띄우지 않는다. 저장된 비밀번호가 바뀌어 거절될 때만 새로 받는다.
        stored = stored_password(email)[0]
        if stored:
            with Hiworks(username=email, password=stored, ip_security_level=a.ip_level) as hw:
                try:
                    how = hw.ensure(otp)
                    print(f"✓ 이미 등록돼 있습니다 · {email} · {'세션 재사용' if how == 'reused' else '새로 로그인'}")
                    return 0
                except HiworksLoginError:
                    if hw.password_ok:  # 비번은 맞고 인증 코드가 틀렸다 — 비밀번호를 다시 받을 일이 아니다
                        raise
                    print("저장된 비밀번호로 로그인하지 못했습니다 — 새 비밀번호를 받습니다.", file=sys.stderr)
    email = (email or ask("하이웍스 이메일(아이디@회사도메인)", hidden=False)).strip()
    if "@" not in email:
        raise HiworksError("이메일은 아이디@회사도메인 형식이어야 합니다.")
    password = sys.stdin.read().rstrip("\r\n") if a.password_stdin else ask(f"{email} 하이웍스 비밀번호", hidden=True, username=email)
    if not password:
        raise HiworksError("비밀번호가 비어 있습니다.")
    with Hiworks(username=email, password=password, ip_security_level=a.ip_level) as hw:
        try:
            hw.login(otp)
        except HiworksError as e:
            if not hw.password_ok:
                print(f"✗ 저장하지 않았습니다 — {e}", file=sys.stderr)
                return 2
            store_credentials(email, password)
            print(f"△ 비밀번호를 확인해 키체인에 저장했습니다 · {email}")
            print(f"  다만 세션은 아직 없습니다 — {e}")
            return 1
    store_credentials(email, password)
    print(f"✓ 등록 완료 · {email} · 비밀번호는 키체인(서비스 {KEYRING_SERVICE}), 세션은 {session_file()}")
    return 0


def cmd_status(_a) -> int:
    email, src = configured_email()
    if not email:
        print(f"등록 안 됨 · {SETUP_HINT}")
        return 1
    try:
        pw, pw_src = stored_password(email)
    except HiworksError as e:
        pw, pw_src = None, str(e)
    sess = read_json(session_file())
    print(f"이메일    {email}  ({src})")
    print(f"비밀번호  {'저장됨 · ' + pw_src if pw else '없음 · ' + (pw_src or SETUP_HINT)}")
    if sess.get("username") == email:
        saved = time.strftime("%Y-%m-%d %H:%M", time.localtime(sess.get("saved_at", 0)))
        print(f"세션      {session_file()} (저장 {saved}) — 살아 있는지는 `check`")
    else:
        print("세션      없음")
    return 0 if pw else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="hiworks", description="하이웍스 로그인 세션(Scrapling)")
    ap.add_argument("--ip-level", default="1", choices=["-1", "1", "2"],
                    help="ip_security_level — -1 사용 안 함 · 1 C클래스(기본) · 2 IP 고정")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="자격증명 등록(로그인 1회로 확인 후 키체인에 저장)")
    s.add_argument("--email")
    s.add_argument("--password-stdin", action="store_true", help="비밀번호를 표준입력에서 읽는다(자동화용)")
    s.add_argument("--no-prompt", action="store_true", help="2단계 인증 코드 입력 창을 띄우지 않는다")
    s.add_argument("--force", action="store_true", help="이미 등록돼 있어도 비밀번호를 새로 받는다")
    sub.add_parser("status", help="등록 상태(값은 출력 안 함)")
    sub.add_parser("check", help="저장 세션 생존 확인(로그인 안 함)")
    for name, h in (("session", "재사용 또는 1회 로그인"), ("login", "새로 로그인")):
        p = sub.add_parser(name, help=h)
        p.add_argument("--no-prompt", action="store_true", help="2단계 인증 코드가 필요하면 묻지 않고 실패")
    g = sub.add_parser("get", help="인증된 GET")
    g.add_argument("url")
    g.add_argument("--max", type=int, default=2000, help="출력할 본문 글자 수")
    sub.add_parser("forget", help="키체인 항목·설정·세션 삭제")
    a = ap.parse_args()

    try:
        if a.cmd == "setup":
            return cmd_setup(a)
        if a.cmd == "status":
            return cmd_status(a)
        if a.cmd == "forget":
            done = forget_credentials()
            print("삭제: " + (", ".join(done) if done else "지울 것이 없습니다"))
            return 0
        with Hiworks(ip_security_level=a.ip_level) as hw:
            otp = None if getattr(a, "no_prompt", False) else ask_otp
            if a.cmd == "check":
                alive = hw.load() and hw.is_alive()
                print(f"{'살아 있음' if alive else '죽음 또는 없음'} · {hw.username}")
                return 0 if alive else 1
            if a.cmd == "login":
                hw.login(otp)
                print(f"로그인 성공 · {hw.username} · 세션 {hw.session_path}")
                return 0
            how = hw.ensure(otp)
            if a.cmd == "session":
                print(f"{'저장 세션 재사용' if how == 'reused' else '새로 로그인'} · {hw.username}")
                return 0
            r = hw.get(a.url)
            hw.save()  # 앱별 lbg_* 쿠키도 다음 실행에 쓴다
            print(f"HTTP {r.status} {r.url}")
            print(r.body.decode("utf-8", "ignore")[: a.max])
            return 0 if r.status < 400 else 1
    except HiworksError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
