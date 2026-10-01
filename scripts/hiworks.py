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
  mail boxes|list|read|send|delete       메일 — 결과 JSON. send·delete 는 --yes 없으면 미리보기만
  vacation                               내 휴가 종류별 발생·사용·잔여(JSON)
  leave-calendar [--month YYYY-MM] [--name 이름] [--dept 부서]
                                         전사 휴가 캘린더 — 누가 언제 휴가인지(연속된 날은 기간으로 묶음)
  org                                    조직도 — 부서 트리와 구성원
  person <이름|아이디>                   직원 찾기 — 본인이 공개한 항목만
  work status|in|out [--yes]              근무 체크 — 상태, 출근·퇴근 기록(지금 시각, --yes 때만)
  approval list [--box all|writer|approval|refer|read|reading|return|temp] [--status 진행|완료|반려] [--search 말]
  approval count                         전자결재 — 내 문서 목록·상태, 결재할 문서 수
  vacation-line show|set|add|remove|clear [--approver 이름] [--processor 이름] [--ref 이름]
                                         휴가 결재선 저장 — 휴가 신청 때 자동으로 들어간다(config.json, 아이디로 저장)
  vacation-request --start D [--end D] [--half am|pm] [--reason …] [--yes]
                                         휴가 신청 — 서버 사전 검사 + 미리보기, --yes 때만 실제 신청
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
세션 쿠키: 앱 호스트마다 부하분산 쿠키 lbg_* 가 붙고, 이게 세션이 저장된 서버를 고정한다. 저장하지 않으면 다음 실행이
다른 서버로 가서 세션이 죽은 것처럼 보인다 — 그래서 세션이 확인된 실행은 종료 때 쿠키를 전부 저장한다.

메일 API(mail-api.office.hiworks.com/v2, Origin mails.office.hiworks.com — 웹메일 번들에서 발굴·실측):
- GET /mailboxes → b0 받은·b1 보낸·b2 보낼·b3 임시·b4 스팸·b5 휴지통(+사용자 메일함)
- POST /mails/search?page[limit]=N&page[offset]=M  본문 {"mailbox_id", "subject", "from"}(평평한 키) → meta.page.total + data[]
- GET /mails/{no} → data.message.content(HTML)·attachments. 읽음 표시는 안 바뀐다
- 발송: POST /mails/cert-key → data(문자열) · GET /senders → 발신자 no(is_default) ·
  POST /mails/send {cert_key, sender_no, to[], cc[], bcc[], subject, content(HTML), is_text_mode:false, is_save:true, …}
  sender_no 는 /me/addresses 의 address_no 가 아니다(그걸 넣으면 404 sender entity not found)
- 삭제: 휴지통 이동 POST /mails/move-bulk {"mailbox_id":"b5","no_list":[…]} · 완전 삭제 POST /mails/delete-bulk {"no_list":[…]}

인사 API(Origin hr-work.office.hiworks.com — hr-work 번들에서 발굴·실측):
- 나: GET cache-api.office.hiworks.com/me → office_user_no(계정 번호 user_no 와 다르다. 남의 번호로 휴가 요약을 부르면 401)
- 내 휴가: GET work-api.office.hiworks.com/v1/vacation-types → /v1/user-vacation-days/office-users/{no}/vacation-types/{id}/summary
- 조직도: GET hr-api.office.hiworks.com/v1/organizations(node_id·parent_node_id·lft) · /v1/members(node_id↔office_user_no) ·
  /v1/employees?page[limit]=… · /v1/positions · /v1/jobs. 직원 휴대폰·이메일·입사일은 본인 공개 플래그(…_visible)가 Y 일 때만 내보낸다
- 전사 휴가 캘린더: GET hr-work-api.office.hiworks.com/v4/vacation-calendar?filter[year]&filter[month][&filter[node]][&filter[search]]
  → 날짜별 행(office_user_no·vacation_type_title·type days|hours·approval_status)
- 근무 체크(hr-timecheck-api /v4): GET web/user-work-info → date·work_status·start_at/end_at("0000-…"=없음)·
  enable_start/enable_end(Y 면 지금 누를 수 있음) · POST web/time-record {"data":{"type":"1"}}=출근 · "2"=퇴근(하루 한 번)
- 전자결재(approval-api.office.hiworks.com/v5, Origin approval.office.hiworks.com):
  GET my-documents?[filter[box_status][in]=WRITER|APPROVAL|REFER|CIRCULATION,CC|AUTH_READ|RETURN]&page[offset]&page[limit]
  (필터 없으면 전부) · GET my-documents/count · GET temp-documents. 상태: list_status(PROGRESS 등)가 있으면 그것,
  없으면 complete_date 가 있으면 완료. 결재할 문서 수: POST approval.office.hiworks.com/{domain}/approval/document_ajax/
  pMenu=get_approval_count(폼 전송) → result.w 대기·e 예정·p 진행·v 확인·a 전체
- 휴가 신청(hr-work-api /v4, 쓰기 본문은 {"data": …}):
  GET forms/vacation-request/node/{node_id} → 양식 id·보존 기간·보안 등급·기본 결재선 line_users(approval_type B 신청·C 처리·F 참조)
  POST vacation-request-check → available_user_nos·…_exceptions·date_exceptions[].…_users (신청서를 만들지 않는 사전 검사.
       잔여 일수 초과는 이 검사가 막지 않았다 — 2026-10-01 실측, 그래서 미리보기가 따로 경고한다)
  POST vacation-request {form_id, node_id, preserved_term, security_level, line_users[], comment, user_nos:[office_user_no],
       period_selections:[{vacation_type_no, start_date, end_date}] (종일 기간) | year + details:[{…, time_type:"H", start_time,
       end_time, hours}] (시간 단위)}. 결재선에 알림이 가는 실제 신청이다
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
MAIL_API = "https://mail-api.office.hiworks.com/v2"
MAIL_ORIGIN = "https://mails.office.hiworks.com"
TRASH_MAILBOX = "b5"
HR_ORIGIN = "https://hr-work.office.hiworks.com"
CACHE_API = "https://cache-api.office.hiworks.com"
WORK_API = "https://work-api.office.hiworks.com"
HR_API = "https://hr-api.office.hiworks.com"
HR_WORK_API = "https://hr-work-api.office.hiworks.com/v4"
TIMECHECK_API = "https://hr-timecheck-api.office.hiworks.com/v4"
APPROVAL_API = "https://approval-api.office.hiworks.com/v5"
APPROVAL_ORIGIN = "https://approval.office.hiworks.com"
# 내 문서함 이름 → box_status 필터(웹 화면 approval.js boxStatusFilterNaming 과 같다). all 은 필터 없이 전부.
APPROVAL_BOXES = {"writer": "WRITER", "approval": "APPROVAL", "refer": "REFER", "read": "CIRCULATION,CC",
                  "reading": "AUTH_READ", "return": "RETURN"}
APPROVAL_STATUS = {"PROGRESS": "진행", "COMPLETE": "완료", "COMPLETED": "완료", "RETURN": "반려", "RETURNED": "반려",
                   "WAIT": "대기", "WAITING": "대기", "CANCEL": "취소", "HOLD": "보류"}
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


class HiworksApiError(HiworksError):
    """로그인 뒤 API 호출이 4xx·5xx 로 거절됐다."""

    def __init__(self, status: int, title: str | None, message: str | None = None):
        self.status, self.title, self.message = status, title, message
        super().__init__(f"API 거절 HTTP {status} {title or ''} {message or ''}".strip())


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


def saved_vacation_line() -> dict:
    """저장해 둔 휴가 결재선 {"approvers": [...], "processors": [...], "refs": [...]} — 값은 직원 아이디(user_id)."""
    line = read_json(config_file()).get("vacation_line") or {}
    return {k: list(line.get(k) or []) for k in ("approvers", "processors", "refs")}


def save_vacation_line(line: dict) -> None:
    cfg = read_json(config_file())
    if any(line.get(k) for k in ("approvers", "processors", "refs")):
        cfg["vacation_line"] = {k: list(line.get(k) or []) for k in ("approvers", "processors", "refs")}
    else:
        cfg.pop("vacation_line", None)
    write_private(config_file(), cfg)


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
        self.established = False  # 이 실행에서 살아 있는 세션을 확인했는가 — 종료 때 쿠키 자동 저장 조건
        self._fs = self._s = None
        self._cache: dict = {}

    def __enter__(self) -> "Hiworks":
        from scrapling.fetchers import FetcherSession
        logging.getLogger("scrapling").setLevel(logging.WARNING)  # import 가 INFO 로 다시 올리므로 그 뒤에 내린다
        self._fs = FetcherSession(impersonate="chrome", retries=1)
        self._s = self._fs.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        # 세션이 확인된 실행이면 종료 때 쿠키를 전부 저장한다. 앱 호스트마다 붙는 부하분산 쿠키(lbg_*)가 세션이 사는
        # 서버를 고정한다 — 이걸 버리면 다음 실행이 다른 서버로 가서 멀쩡한 세션이 죽은 것으로 보인다(2026-10-01 실측).
        if self._fs and self.established and any(c.name == "PHPSESSID" for c in self._jar().jar):
            self.save()
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
        self.established = True
        self.save()
        return data

    def is_alive(self) -> bool:
        r = self._s.get(LIVENESS_URL.format(domain=self.office_domain))
        alive = r.status == 200 and is_logged_in_html(r.body.decode("utf-8", "ignore"))
        self.established = self.established or alive
        return alive

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

    # ---------- 공통 ----------

    def _api(self, method: str, url: str, origin: str, *, body=None, accept: str = "application/json"):
        """JSON API 호출. 4xx·5xx 는 HiworksApiError. 쓰기 요청도 재시도하지 않는다(retries=1)."""
        headers = {"Origin": origin, "Referer": f"{origin}/", "accept": accept}
        kw = {"headers": headers, "retries": 1}
        if body is not None:
            kw["json"] = body
        r = getattr(self._s, method.lower())(url, **kw)
        try:
            data = json.loads(r.body) if r.body else {}
        except ValueError:
            data = {}
        if r.status >= 400:
            err = ((data.get("errors") or [{}])[0] or {}) if isinstance(data, dict) else {}
            raise HiworksApiError(r.status, err.get("title") or err.get("code"), err.get("message"))
        return data

    def _memo(self, key: str, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # ---------- 메일 ----------

    def _mail(self, method: str, path: str, body=None):
        return self._api(method, f"{MAIL_API}{path}", MAIL_ORIGIN, body=body)

    def mailboxes(self) -> list[dict]:
        return [{"id": b["no"], "name": b["name"]} for b in self._mail("GET", "/mailboxes").get("data", [])]

    def list_mails(self, mailbox: str = "b0", limit: int = 20, offset: int = 0,
                   subject: str | None = None, sender: str | None = None) -> dict:
        """메일 목록. mailbox 는 b0 받은·b1 보낸·b2 보낼·b3 임시·b4 스팸·b5 휴지통 또는 사용자 메일함 id."""
        body = {"mailbox_id": mailbox}
        if subject:
            body["subject"] = subject
        if sender:
            body["from"] = sender
        d = self._mail("POST", f"/mails/search?page[limit]={int(limit)}&page[offset]={int(offset)}", body)
        page = (d.get("meta") or {}).get("page") or {}
        return {"total": page.get("total"), "offset": page.get("offset"), "limit": page.get("limit"),
                "items": [summarize_mail(m) for m in d.get("data", [])]}

    def get_mail(self, no: int) -> dict:
        """메일 본문. API 로 읽어도 읽음 표시는 바뀌지 않는다(2026-10-01 실측)."""
        m = self._mail("GET", f"/mails/{int(no)}").get("data") or {}
        out = summarize_mail(m)
        msg = m.get("message") or {}
        out["content_html"] = msg.get("content", "")
        out["content_text"] = html_to_text(msg.get("content", ""))
        out["attachments"] = [{k: a.get(k) for k in ("part_id", "name", "size", "content_type") if k in a}
                              for a in msg.get("attachments", [])]
        return out

    def send_mail(self, to, subject: str, content: str, cc=(), bcc=(), html: bool = False) -> dict:
        """메일 발송. 실패해도 재시도하지 않는다 — 서버가 받은 뒤 끊긴 경우 다시 보내면 두 통이 된다."""
        to, cc, bcc = as_list(to), as_list(cc), as_list(bcc)
        if not to:
            raise HiworksError("받는 사람(to)이 비었습니다.")
        cert_key = self._mail("POST", "/mails/cert-key").get("data")
        senders = self._mail("GET", "/senders").get("data", [])  # sender_no 는 주소 번호가 아니라 발신자 번호(/senders 의 no)
        sender = next((x for x in senders if x.get("is_default")), senders[0] if senders else None)
        if not cert_key or sender is None:
            raise HiworksError("발송 준비(cert-key·보내는 사람)를 받지 못했습니다.")
        body = {
            "cert_key": cert_key, "sender_no": sender["no"], "to": to, "cc": cc, "bcc": bcc,
            "subject": subject or "[제목없음]", "content": content if html else text_to_html(content),
            "is_important": False, "original_mail_no": None, "original_temp_part_id_list": [],
            "is_encrypt": False, "is_separate_send": False, "reserve_date": None, "is_text_mode": False,
            "is_reply": False, "is_forward": False, "response_target_mail_no": None,
            "is_receipt_confirm": True, "is_save": True,
        }
        d = self._mail("POST", "/mails/send", body).get("data") or {}
        return {"sent": True, "from": sender.get("address"), "to": to, "cc": cc, "bcc": bcc, "subject": body["subject"],
                "send_type": d.get("send_type")}

    def delete_mails(self, nos, permanent: bool = False) -> dict:
        """기본은 휴지통(b5)으로 이동. permanent=True 면 완전 삭제(복구 불가)."""
        no_list = [int(n) for n in as_list(nos)]
        if not no_list:
            raise HiworksError("삭제할 메일 번호가 없습니다.")
        if permanent:
            self._mail("POST", "/mails/delete-bulk", {"no_list": no_list})
        else:
            self._mail("POST", "/mails/move-bulk", {"mailbox_id": TRASH_MAILBOX, "no_list": no_list})
        return {"deleted": no_list, "permanent": permanent}

    # ---------- 인사(나·휴가·조직도) ----------

    def me(self) -> dict:
        d = self._memo("me", lambda: self._api("GET", f"{CACHE_API}/me", HR_ORIGIN).get("data") or {})
        return {"name": d.get("name"), "user_id": d.get("user_id"), "office_user_no": int(d["office_user_no"])}

    def vacation_types(self) -> list[dict]:
        return self._memo("vtypes", lambda: [
            t for t in self._api("GET", f"{WORK_API}/v1/vacation-types?page[limit]=200", HR_ORIGIN).get("data", [])
            if t.get("use_flag") == "Y"])

    def my_vacation(self) -> dict:
        """내 휴가 종류별 발생·사용·잔여."""
        me = self.me()
        rows = []
        for t in self.vacation_types():
            s = self._api("GET", f"{WORK_API}/v1/user-vacation-days/office-users/{me['office_user_no']}"
                                 f"/vacation-types/{t['id']}/summary", HR_ORIGIN).get("data") or {}
            if not any(s.get(k) for k in ("created_total_hours", "used_total_hours", "remaining_total_hours")):
                continue  # 발생도 사용도 없는 종류는 뺀다
            rows.append({"type": t["title"], "created_days": s.get("created_days"), "created_hours": s.get("created_hours"),
                         "used_days": s.get("used_days"), "used_hours": s.get("used_hours"),
                         "remaining_days": s.get("remaining_days"), "remaining_hours": s.get("remaining_hours")})
        return {"name": me["name"], "vacations": rows}

    def _hr(self, path: str) -> list:
        return self._api("GET", f"{HR_API}{path}", HR_ORIGIN).get("data", [])

    def _directory(self) -> dict:
        """조직·소속·직원·직위·직책을 한 번에 읽어 둔다(실행당 1회)."""
        def build():
            nodes = {n["node_id"]: n for n in self._hr("/v1/organizations")}
            positions = {p["id"]: p["code_content"] for p in self._hr("/v1/positions")}
            jobs = {j["id"]: j["code_content"] for j in self._hr("/v1/jobs")}
            people = {e["id"]: e for e in self._hr("/v1/employees?page[limit]=10000")}
            belongs: dict[int, list[int]] = {}
            for m in self._hr("/v1/members"):
                belongs.setdefault(m["office_user_no"], []).append(m["node_id"])
            return {"nodes": nodes, "positions": positions, "jobs": jobs, "people": people, "belongs": belongs}
        return self._memo("dir", build)

    def _person(self, no: int, d: dict) -> dict:
        e = d["people"].get(no) or {}
        return public_profile(e, d, no)

    def org_chart(self) -> dict:
        """조직도 — 부서 트리와 부서별 구성원(이름·직위·직책)."""
        d = self._directory()
        members: dict[int, list[dict]] = {}
        for no, node_ids in d["belongs"].items():
            e = d["people"].get(no)
            if not e or e.get("active") != "Y" or e.get("del_flag") == "Y":
                continue
            for nid in node_ids:
                members.setdefault(nid, []).append({"name": e.get("name"), "position": d["positions"].get(e.get("position_no")),
                                                    "job": d["jobs"].get(e.get("job_no"))})
        children: dict = {}
        for n in d["nodes"].values():
            children.setdefault(n["parent_node_id"], []).append(n)

        def tree(n):
            kids = sorted(children.get(n["node_id"], []), key=lambda x: x["lft"])
            return {"id": n["node_id"], "name": n["node_name"], "members": members.get(n["node_id"], []),
                    "children": [tree(k) for k in kids]}
        roots = [n for n in d["nodes"].values() if n["parent_node_id"] not in d["nodes"]]
        return {"departments": [tree(r) for r in sorted(roots, key=lambda x: x["lft"])]}

    def find_people(self, query: str) -> list[dict]:
        """이름·아이디·영문 이름으로 직원 찾기(부분 일치). 본인이 공개하지 않은 항목은 빼고 돌려준다."""
        q = query.strip().lower()
        if not q:
            raise HiworksError("찾을 이름이 비었습니다.")
        d = self._directory()
        hits = [no for no, e in d["people"].items() if e.get("active") == "Y" and e.get("del_flag") != "Y"
                and any(q in (e.get(k) or "").lower() for k in ("name", "user_id", "english_name"))]
        return [self._person(no, d) for no in hits]

    def vacation_calendar(self, year: int, month: int, name: str | None = None, department: str | None = None) -> list[dict]:
        """전사 휴가 캘린더 — 그 달에 휴가를 쓴(쓸) 사람과 날짜. 사람별로 연속된 날을 기간으로 묶는다."""
        d = self._directory()
        url = f"{HR_WORK_API}/vacation-calendar?filter[year]={int(year)}&filter[month]={int(month)}&page[limit]=600"
        if department:
            url += f"&filter[node]={self._node_id(department, d)}"
        rows = self._api("GET", url, HR_ORIGIN).get("data", [])
        out = []
        for r in rows:
            e = d["people"].get(r.get("office_user_no")) or {}
            out.append({"name": e.get("name"), "office_user_no": r.get("office_user_no"),
                        "departments": [d["nodes"][n]["node_name"] for n in d["belongs"].get(r.get("office_user_no"), []) if n in d["nodes"]],
                        "date": r.get("date"), "type": r.get("vacation_type_title"), "unit": r.get("type"), "days": r.get("days"),
                        "hours": r.get("hours"), "start_time": r.get("start_time"), "end_time": r.get("end_time"),
                        "approval_status": r.get("approval_status")})
        if name:
            q = name.strip().lower()
            out = [o for o in out if q in (o["name"] or "").lower()]
        return group_vacation_periods(out)

    # ---------- 휴가 신청 ----------

    def _hr_work(self, method: str, path: str, data=None):
        body = None if data is None else {"data": data}  # hr-work-api 쓰기 요청은 {"data": …} 로 감싼다
        return self._api(method, f"{HR_WORK_API}/{path}", HR_ORIGIN, body=body)

    def vacation_request_payload(self, start: str, end: str | None = None, vtype: str = "연차", half: str | None = None,
                                 start_time: str | None = None, end_time: str | None = None, reason: str = "",
                                 department: str | None = None, approvers=(), processors=(), refs=(),
                                 use_saved_line: bool = True) -> dict:
        """휴가 신청 본문을 만든다(보내지 않음). 종일은 기간(start~end, 주말·공휴일은 서버가 뺀다),
        반차는 하루만 — half="am"(09~13시)·"pm"(14~18시) 또는 start_time·end_time 직접 지정.
        결재선은 양식 기본선에 이름으로 추가한다 — approvers 신청 라인 결재자(B, 적은 순서대로)·processors 처리(C)·refs 참조(F).
        use_saved_line 이면 저장해 둔 결재선(vacation-line)을 먼저 넣고 그 뒤에 이번 추가분을 붙인다."""
        if use_saved_line:
            saved = saved_vacation_line()
            approvers = saved["approvers"] + as_list(approvers)
            processors = saved["processors"] + as_list(processors)
            refs = saved["refs"] + as_list(refs)
        from datetime import date, datetime
        me, d = self.me(), self._directory()
        types = {t["title"]: t for t in self.vacation_types()}
        if vtype not in types:
            raise HiworksError(f"휴가 종류 '{vtype}' 가 없습니다(가능: {', '.join(types)}).")
        s_date, e_date = date.fromisoformat(start), date.fromisoformat(end or start)
        if e_date < s_date:
            raise HiworksError("끝 날짜가 시작 날짜보다 앞입니다.")
        my_nodes = d["belongs"].get(me["office_user_no"], [])
        if department:
            node_id = self._node_id(department, d)
            if node_id not in my_nodes:
                raise HiworksError(f"'{department}' 는 내 소속 부서가 아닙니다.")
        elif my_nodes:
            node_id = max(my_nodes, key=lambda n: d["nodes"].get(n, {}).get("depth", 0))  # 가장 아래 부서(팀)
        else:
            raise HiworksError("소속 부서를 찾지 못했습니다.")
        form = self._hr_work("GET", f"forms/vacation-request/node/{node_id}").get("data") or {}
        payload = {
            "form_id": form.get("id"), "node_id": node_id, "preserved_term": form.get("preserved_term"),
            "security_level": form.get("security_level"), "comment": html_escape_lines(reason)[:1000],
            "line_users": self._approval_line(form, me["office_user_no"], d, approvers, processors, refs),
            "user_nos": [me["office_user_no"]],
        }
        type_no = types[vtype]["id"]
        if half or start_time or end_time:
            if s_date != e_date:
                raise HiworksError("시간 단위(반차) 휴가는 하루씩만 신청합니다.")
            st, et = {"am": ("09:00", "13:00"), "pm": ("14:00", "18:00")}.get(half or "", (start_time, end_time))
            if not (st and et):
                raise HiworksError("반차는 --half am|pm 또는 --start-time·--end-time 을 주세요.")
            hours = (datetime.strptime(et, "%H:%M") - datetime.strptime(st, "%H:%M")).seconds / 3600
            payload["year"] = str(s_date.year)
            payload["details"] = [{"vacation_type_no": type_no, "vacation_date": s_date.isoformat(), "time_type": "H",
                                   "start_time": f"{st}:00", "end_time": f"{et}:00", "hours": hours}]
        else:
            payload["details"] = []
            payload["period_selections"] = [{"vacation_type_no": type_no, "start_date": s_date.isoformat(),
                                             "end_date": e_date.isoformat()}]
        return payload

    def _approval_line(self, form: dict, me_no: int, d: dict, approvers, processors, refs) -> list[dict]:
        """양식 기본 결재선 + 추가 인원. 화면과 같이 B → C → F 순으로 싣고, 같은 역할에 같은 사람은 한 번만."""
        allowed = set(form.get("approval_method") or "BCF")
        lines = {t: [{"office_user_no": u["office_user_no"], "node_id": u["node_id"], "approval_type": t}
                     for u in form.get("line_users", []) if u.get("office_user_no") and u.get("approval_type") == t]
                 for t in "BCF"}
        for t, names in (("B", approvers), ("C", processors), ("F", refs)):
            names = as_list(names)
            if names and t not in allowed:
                raise HiworksError(f"이 양식은 {APPROVAL_ROLE[t]} 결재선을 쓰지 않습니다(결재 방식 {form.get('approval_method')}).")
            for name in names:
                no = self._resolve_person(name, d)
                if t == "B" and no == me_no:
                    raise HiworksError("본인은 결재자로 추가할 수 없습니다.")
                if any(x["office_user_no"] == no for x in lines[t]):
                    continue
                nodes = d["belongs"].get(no) or []
                node = max(nodes, key=lambda n: d["nodes"].get(n, {}).get("depth", 0)) if nodes else None
                lines[t].append({"office_user_no": no, "node_id": node, "approval_type": t})
        return lines["B"] + lines["C"] + lines["F"]

    def resolve_user_ids(self, names) -> list[dict]:
        """이름·아이디 목록 → [{"user_id", "name"}] (저장용 — 이름이 같은 사람이 생겨도 흔들리지 않게 아이디로 둔다)."""
        d = self._directory()
        out = []
        for n in as_list(names):
            no = self._resolve_person(n, d)
            e = d["people"][no]
            out.append({"user_id": e.get("user_id"), "name": e.get("name")})
        return out

    def _resolve_person(self, name: str, d: dict) -> int:
        """이름(또는 아이디)을 재직 중인 직원 한 명으로 정한다. 같은 이름이 여럿이면 후보를 보여 주고 멈춘다."""
        q = name.strip()
        active = {no: e for no, e in d["people"].items() if e.get("active") == "Y" and e.get("del_flag") != "Y"}
        hits = [no for no, e in active.items() if q in (e.get("name"), e.get("user_id"))]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise HiworksError(f"'{name}' 와 이름이 같은 재직자가 없습니다.")
        cands = ", ".join(f"{active[n]['name']}({active[n].get('user_id')}, "
                          f"{'/'.join(d['nodes'][x]['node_name'] for x in d['belongs'].get(n, []) if x in d['nodes'])})" for n in hits)
        raise HiworksError(f"'{name}' 가 여러 명입니다 — 아이디로 지정하세요: {cands}")

    def check_vacation_request(self, payload: dict) -> dict:
        """서버 사전 검사(신청서는 만들지 않는다). ok=False 면 problems 에 사유."""
        r = self._hr_work("POST", "vacation-request-check", payload).get("data") or {}
        problems = [VACATION_CHECK_REASONS.get(k, k) for k, v in r.items()
                    if k.endswith("_exceptions") and v and k != "date_exceptions"]
        for ex in r.get("date_exceptions") or []:
            for k, v in ex.items():
                if k.endswith("_users") and v:
                    problems.append(f"{ex.get('date', '')} {VACATION_CHECK_REASONS.get(k, k)}".strip())
        ok = bool(r.get("available_user_nos")) and not problems
        return {"ok": ok, "problems": problems}

    def request_vacation(self, payload: dict) -> dict:
        """휴가 신청서를 실제로 올린다 — 결재선에 알림이 간다. 사전 검사를 통과한 본문만 보낸다. 재시도 없음."""
        check = self.check_vacation_request(payload)
        if not check["ok"]:
            raise HiworksError("휴가를 신청할 수 없습니다 — " + "; ".join(check["problems"] or ["사전 검사 실패"]))
        self._hr_work("POST", "vacation-request", payload)
        return {"requested": True, "details": payload.get("details") or payload.get("period_selections")}

    # ---------- 근무 체크(출근·퇴근) ----------

    def work_status(self) -> dict:
        """오늘 근무 체크 상태. 출근·퇴근 기록과 지금 누를 수 있는지."""
        w = self._api("GET", f"{TIMECHECK_API}/web/user-work-info", HR_ORIGIN).get("data") or {}
        t = lambda v: None if not v or v.startswith("0000") else v
        return {"date": w.get("date"), "status": w.get("work_status"), "check_in": t(w.get("start_at")),
                "check_out": t(w.get("end_at")), "can_check_in": w.get("enable_start") == "Y",
                "can_check_out": w.get("enable_end") == "Y",
                "records": [{"type": x.get("type"), "time": x.get("time"), "title": x.get("title")} for x in w.get("details") or []]}

    def record_work(self, kind: str) -> dict:
        """kind="in" 출근 · "out" 퇴근 — 지금 시각으로 기록된다. 퇴근은 하루에 한 번만. 재시도 없음."""
        if kind not in ("in", "out"):
            raise HiworksError("kind 는 in 또는 out 입니다.")
        before = self.work_status()
        if not before["can_check_in" if kind == "in" else "can_check_out"]:
            raise HiworksError(f"지금은 {'출근' if kind == 'in' else '퇴근'} 체크를 할 수 없습니다(현재 상태: {before['status']}).")
        self._api("POST", f"{TIMECHECK_API}/web/time-record", HR_ORIGIN, body={"data": {"type": "1" if kind == "in" else "2"}})
        return {"recorded": "출근" if kind == "in" else "퇴근", "status": self.work_status()}

    # ---------- 전자결재 ----------

    def approval_documents(self, box: str = "all", status: str | None = None, search: str | None = None,
                           limit: int = 30, offset: int = 0) -> dict:
        """내 전자결재 문서 목록 + 상태. box: all(전부)·writer 기안·approval 결재·refer 수신·read 회람/참조·
        reading 열람·return 반려·temp 임시저장. status 를 주면(진행·완료·반려 등) 그 상태만 남긴다."""
        if box == "temp":
            path = "/temp-documents"
            q = []
        else:
            path = "/my-documents"
            q = [] if box == "all" else [f"filter[box_status][in]={APPROVAL_BOXES[box]}"] if box in APPROVAL_BOXES else None
            if q is None:
                raise HiworksError(f"문서함 '{box}' 가 없습니다(가능: all, temp, {', '.join(APPROVAL_BOXES)}).")
        if search:
            from urllib.parse import quote
            q.append(f"filter[search_all][like]={quote(search)}")
        # 상태 필터는 서버에 없어 받아 와서 거른다 — 그래서 상태를 주면 한 번에 넉넉히(최대 200) 받는다.
        fetch = max(limit, 200) if status else limit
        q += [f"page[offset]={int(offset)}", f"page[limit]={int(fetch)}"]
        d = self._api("GET", f"{APPROVAL_API}{path}?{'&'.join(q)}", APPROVAL_ORIGIN, accept="application/json;charset=UTF-8")
        items = [summarize_document(x, self.office_domain) for x in d.get("data", [])]
        if status:
            items = [x for x in items if status in (x["status"], x["status_code"])]
        return {"box": box, "count": len(items[:limit]), "items": items[:limit]}

    def approval_counts(self) -> dict:
        """결재할 문서 수(대기·예정·진행·확인·전체)와 내 문서함 총 건수."""
        r = self._s.post(f"{APPROVAL_ORIGIN}/{self.office_domain}/approval/document_ajax/", retries=1,
                         data={"pMenu": "get_approval_count"},
                         headers={"Origin": APPROVAL_ORIGIN, "X-Requested-With": "XMLHttpRequest"})
        try:
            c = (json.loads(r.body) or {}).get("result") or {}
        except ValueError:
            c = {}
        total = self._api("GET", f"{APPROVAL_API}/my-documents/count", APPROVAL_ORIGIN).get("data", {}).get("count")
        names = {"w": "대기", "e": "예정", "p": "진행", "v": "확인", "a": "전체"}
        return {"to_do": {names[k]: c.get(k) for k in names if k in c}, "my_documents_total": total}

    def _node_id(self, department: str, d: dict) -> int:
        if str(department).isdigit():
            return int(department)
        hits = ([n for n in d["nodes"].values() if n["node_name"] == department]
                or [n for n in d["nodes"].values() if department in n["node_name"]])
        if len(hits) != 1:
            names = ", ".join(n["node_name"] for n in hits) or "없음"
            raise HiworksError(f"부서 '{department}' 를 하나로 정할 수 없습니다(후보: {names}).")
        return hits[0]["node_id"]


# ---------- API 응답 가공 ----------

def as_list(v) -> list:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            out.extend(as_list(x) if isinstance(x, str) else ([x] if x not in (None, "") else []))
        return out
    return [x.strip() for x in str(v).split(",") if x.strip()]


APPROVAL_ROLE = {"B": "결재(신청)", "C": "처리", "F": "참조"}

VACATION_CHECK_REASONS = {
    "remain_days_exceptions": "잔여 일수가 부족합니다",
    "request_days_limit_exceptions": "신청 가능 일수를 넘었습니다",
    "unavailable_user_exceptions": "신청할 수 없는 사용자입니다",
    "inactive_user_exceptions": "비활성 사용자입니다",
    "rest_user_exceptions": "휴직 중이라 신청할 수 없습니다",
    "invalid_joindate_user_exceptions": "입사일 정보 때문에 신청할 수 없습니다",
    "vacation_users": "이미 휴가가 신청된 날입니다",
    "duplicated_time_range_users": "이미 신청한 시간과 겹칩니다",
    "not_in_work_time_users": "근무시간 밖입니다",
    "in_rest_time_users": "휴게시간과 겹칩니다",
    "time_range_limit_users": "하루 시간제 휴가 한도를 넘었습니다",
    "start_end_time_limit_users": "시간제 휴가는 출퇴근 시간에 붙여서만 쓸 수 있습니다",
}


def html_escape_lines(text: str) -> str:
    import html as _html
    return "<br>".join(_html.escape(line) for line in (text or "").strip().splitlines())


def summarize_document(x: dict, domain: str) -> dict:
    code = x.get("list_status")
    status = APPROVAL_STATUS.get(code, code) if code else ("완료" if x.get("complete_date") else "진행")
    return {"id": x.get("id"), "code": x.get("document_code"), "title": x.get("title"), "form": x.get("form_title"),
            "type": x.get("document_type"), "drafter": x.get("register_name"), "department": x.get("node_name"),
            "drafted": x.get("regist_date"), "completed": x.get("complete_date"), "status": status, "status_code": code,
            "my_role": x.get("approval_types") or [], "attachments": x.get("attached_file_flag") == "Y",
            "url": f"{APPROVAL_ORIGIN}/{domain}/approval/document/view/{x.get('id')}" if x.get("id") else None}


def summarize_mail(m: dict) -> dict:
    return {"no": m.get("no"), "mailbox": m.get("mailbox_id"), "from": m.get("from"), "to": m.get("to_address", []),
            "cc": m.get("cc_address", []), "subject": m.get("subject"), "received": m.get("received_date"),
            "unread": m.get("is_new"), "attachments": m.get("file_attached"), "size": m.get("size")}


def html_to_text(content: str) -> str:
    import html as _html
    t = re.sub(r"(?is)<(script|style).*?</\1>", "", content or "")
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", t)
    t = _html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in t.splitlines())).strip()


def text_to_html(text: str) -> str:
    import html as _html
    return "<div>" + "<br>".join(_html.escape(line) for line in (text or "").splitlines()) + "</div>"


def public_profile(e: dict, d: dict, no: int) -> dict:
    """직원 정보 — 본인이 공개(…_visible == "Y")한 항목만 담는다. 회사 전화·이메일 아이디·부서·직위는 공개 항목."""
    out = {"name": e.get("name"), "english_name": e.get("english_name") or None, "user_id": e.get("user_id"),
           "office_user_no": no, "position": d["positions"].get(e.get("position_no")), "job": d["jobs"].get(e.get("job_no")),
           "departments": [d["nodes"][n]["node_name"] for n in d["belongs"].get(no, []) if n in d["nodes"]],
           "phone": e.get("phone") or None, "on_leave": e.get("rest_flag") == "Y"}
    for field, flag in (("cell", "cell_visible"), ("email", "email_visible"), ("joindate", "joindate_visible")):
        if e.get(flag) == "Y" and e.get(field):
            out[field] = e[field]
    return out


def group_vacation_periods(rows: list[dict]) -> list[dict]:
    """같은 사람·같은 휴가 종류·같은 결재 상태의 종일 휴가가 주말만 사이에 두고 이어지면 한 기간(start~end)으로 묶는다.
    시간 단위(반차 등)는 날마다 따로 둔다. 공휴일은 모르므로 공휴일을 사이에 둔 휴가는 두 기간으로 나온다."""
    from datetime import date, timedelta

    def only_weekend_between(a: str, b: str) -> bool:
        d, end = date.fromisoformat(a) + timedelta(days=1), date.fromisoformat(b)
        while d < end:
            if d.weekday() < 5:
                return False
            d += timedelta(days=1)
        return True

    rows = sorted(rows, key=lambda r: (r["office_user_no"] or 0, r["type"] or "", r["date"] or ""))
    out: list[dict] = []
    for r in rows:
        full_day = r.get("unit") != "hours"
        prev = out[-1] if out else None
        if (prev and full_day and prev["full_day"] and prev["office_user_no"] == r["office_user_no"]
                and prev["type"] == r["type"] and prev["approval_status"] == r["approval_status"]
                and r["date"] and only_weekend_between(prev["end"], r["date"])):
            prev["end"] = r["date"]
            prev["days"] = (prev["days"] or 0) + (r["days"] or 0)
            continue
        out.append({"name": r["name"], "office_user_no": r["office_user_no"], "departments": r["departments"],
                    "type": r["type"], "start": r["date"], "end": r["date"], "full_day": full_day,
                    "days": r["days"], "hours": r["hours"],
                    "start_time": None if full_day else r["start_time"], "end_time": None if full_day else r["end_time"],
                    "approval_status": r["approval_status"]})
    return sorted(out, key=lambda p: (p["start"] or "", p["name"] or ""))


# ---------- CLI ----------

def emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def weekdays_between(start: str, end: str) -> int:
    from datetime import date, timedelta
    d, e, n = date.fromisoformat(start), date.fromisoformat(end), 0
    while d <= e:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def vacation_request_preview(hw: "Hiworks", payload: dict, vtype: str) -> dict:
    """사람이 확인할 미리보기 — 기간·추정 일수·잔여·결재선·서버 사전 검사."""
    d = hw._directory()
    if payload.get("period_selections"):
        ps = payload["period_selections"][0]
        span, est = {"start": ps["start_date"], "end": ps["end_date"]}, weekdays_between(ps["start_date"], ps["end_date"])
        unit = "일(주말 뺀 추정 — 공휴일은 서버가 뺀다)"
    else:
        x = payload["details"][0]
        span, est, unit = {"date": x["vacation_date"], "time": f"{x['start_time'][:5]}~{x['end_time'][:5]}"}, x["hours"], "시간"
    remaining = next((v for v in hw.my_vacation()["vacations"] if v["type"] == vtype), None)
    out = {"type": vtype, **span, "estimate": f"{est}{unit}",
           "department": d["nodes"].get(payload["node_id"], {}).get("node_name"),
           "approval_line": [{"role": APPROVAL_ROLE.get(u["approval_type"], u["approval_type"]),
                              "name": (d["people"].get(u["office_user_no"]) or {}).get("name")} for u in payload["line_users"]],
           "reason": payload["comment"], "check": hw.check_vacation_request(payload)}
    if remaining:
        out["remaining_days"] = remaining["remaining_days"]
        if payload.get("period_selections") and est > (remaining["remaining_days"] or 0):
            out["warning"] = f"추정 {est}일이 잔여 {remaining['remaining_days']}일보다 많습니다(서버 사전 검사는 잔여를 막지 않았습니다)."
    return out


def run_api_command(hw: "Hiworks", a) -> int:
    if a.cmd == "vacation":
        emit(hw.my_vacation())
    elif a.cmd == "org":
        emit(hw.org_chart())
    elif a.cmd == "person":
        emit(hw.find_people(a.query))
    elif a.cmd == "leave-calendar":
        from datetime import date
        ym = a.month or date.today().strftime("%Y-%m")
        if not re.fullmatch(r"\d{4}-\d{2}", ym):
            raise HiworksError("--month 는 YYYY-MM 형식입니다.")
        y, mo = map(int, ym.split("-"))
        emit({"month": ym, "periods": hw.vacation_calendar(y, mo, name=a.name, department=a.dept)})
    elif a.cmd == "approval":
        emit(hw.approval_counts() if a.action == "count"
             else hw.approval_documents(a.box, a.status, a.search, a.limit, a.offset))
    elif a.cmd == "vacation-line":
        line = saved_vacation_line()
        new = {"approvers": hw.resolve_user_ids(a.approver), "processors": hw.resolve_user_ids(a.processor),
               "refs": hw.resolve_user_ids(a.ref)}
        if a.action == "set":
            line = {k: [p["user_id"] for p in v] for k, v in new.items()}
        elif a.action == "add":
            for k, v in new.items():
                line[k] += [p["user_id"] for p in v if p["user_id"] not in line[k]]
        elif a.action == "remove":
            gone = {p["user_id"] for p in hw.resolve_user_ids(a.names)}
            line = {k: [u for u in v if u not in gone] for k, v in line.items()}
        elif a.action == "clear":
            line = {"approvers": [], "processors": [], "refs": []}
        if a.action != "show":
            save_vacation_line(line)
        d = hw._directory()
        by_id = {e.get("user_id"): e.get("name") for e in d["people"].values()}
        emit({role: [{"user_id": u, "name": by_id.get(u)} for u in ids] for role, ids in line.items()})
    elif a.cmd == "work":
        if a.action == "status":
            emit(hw.work_status())
        elif not a.yes:
            from datetime import datetime
            st = hw.work_status()
            can = st["can_check_in" if a.action == "in" else "can_check_out"]
            emit({"preview": {"action": "출근" if a.action == "in" else "퇴근", "will_record_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                              "possible": can, "current": st},
                  "note": "기록하지 않았습니다. 실제로 기록하려면 --yes" + (" (퇴근은 하루에 한 번만)" if a.action == "out" else "")})
            return 0 if can else 1
        else:
            emit(hw.record_work(a.action))
    elif a.cmd == "vacation-request":
        payload = hw.vacation_request_payload(a.start, a.end, a.type, a.half, a.start_time, a.end_time, a.reason, a.dept,
                                              approvers=a.approver, processors=a.processor, refs=a.ref,
                                              use_saved_line=not a.no_saved_line)
        preview = vacation_request_preview(hw, payload, a.type)
        if not a.yes:
            emit({"preview": preview, "note": "신청하지 않았습니다. 실제로 신청하려면 --yes (결재선에 알림이 갑니다)"})
            return 0 if preview["check"]["ok"] else 1
        emit({**hw.request_vacation(payload), "preview": preview})
    elif a.mail_cmd == "boxes":
        emit(hw.mailboxes())
    elif a.mail_cmd == "list":
        emit(hw.list_mails(a.box, a.limit, a.offset, a.subject, a.sender))
    elif a.mail_cmd == "read":
        m = hw.get_mail(a.no)
        if not a.html:
            m.pop("content_html", None)
        emit(m)
    elif a.mail_cmd == "send":
        body = Path(a.body_file).read_text(encoding="utf-8") if a.body_file else a.body
        preview = {"to": as_list(a.to), "cc": as_list(a.cc), "bcc": as_list(a.bcc), "subject": a.subject,
                   "html": a.html, "body_preview": body[:300]}
        if not a.yes:
            emit({"preview": preview, "note": "보내지 않았습니다. 실제로 보내려면 --yes"})
            return 0
        emit(hw.send_mail(a.to, a.subject, body, a.cc, a.bcc, html=a.html))
    elif a.mail_cmd == "delete":
        if not a.yes:
            emit({"preview": {"no_list": a.no, "permanent": a.permanent},
                  "note": "삭제하지 않았습니다. 실제로 지우려면 --yes" + (" (완전 삭제는 복구할 수 없습니다)" if a.permanent else "")})
            return 0
        emit(hw.delete_mails(a.no, permanent=a.permanent))
    return 0


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
    password = sys.stdin.read().rstrip("\r\n") if a.password_stdin else ask(f"{email} 하이웍스 비밀번호 (영문 입력 상태인지 확인하세요 — 입력값은 점으로만 보입니다)", hidden=True, username=email)
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

    m = sub.add_parser("mail", help="메일 — 결과는 JSON").add_subparsers(dest="mail_cmd", required=True)
    m.add_parser("boxes", help="메일함 목록")
    ml = m.add_parser("list", help="메일 목록")
    ml.add_argument("--box", default="b0", help="b0 받은(기본)·b1 보낸·b2 보낼·b3 임시·b4 스팸·b5 휴지통")
    ml.add_argument("--limit", type=int, default=20)
    ml.add_argument("--offset", type=int, default=0)
    ml.add_argument("--subject", help="제목에 들어간 말")
    ml.add_argument("--from", dest="sender", help="보낸 사람 주소")
    mr = m.add_parser("read", help="메일 본문(안 읽은 메일은 읽음으로 바뀐다)")
    mr.add_argument("no", type=int)
    mr.add_argument("--html", action="store_true", help="HTML 본문도 함께")
    ms = m.add_parser("send", help="메일 발송 — --yes 없으면 미리보기만")
    ms.add_argument("--to", required=True, help="받는 사람(쉼표로 여러 명)")
    ms.add_argument("--cc", default="")
    ms.add_argument("--bcc", default="")
    ms.add_argument("--subject", required=True)
    g2 = ms.add_mutually_exclusive_group(required=True)
    g2.add_argument("--body", help="본문(텍스트)")
    g2.add_argument("--body-file", help="본문 파일(텍스트, --html 이면 HTML)")
    ms.add_argument("--html", action="store_true", help="본문을 HTML 로 보낸다")
    ms.add_argument("--yes", action="store_true", help="실제로 보낸다")
    md = m.add_parser("delete", help="메일 삭제(기본 휴지통 이동) — --yes 없으면 미리보기만")
    md.add_argument("no", type=int, nargs="+")
    md.add_argument("--permanent", action="store_true", help="완전 삭제(복구 불가)")
    md.add_argument("--yes", action="store_true", help="실제로 삭제한다")

    sub.add_parser("vacation", help="내 휴가 종류별 발생·사용·잔여(JSON)")
    lc = sub.add_parser("leave-calendar", help="전사 휴가 캘린더 — 누가 언제 휴가인지(JSON)")
    lc.add_argument("--month", help="YYYY-MM (기본 이번 달)")
    lc.add_argument("--name", help="이름(부분 일치)")
    lc.add_argument("--dept", help="부서 이름(부분 일치) 또는 id")
    sub.add_parser("org", help="조직도 — 부서 트리와 구성원(JSON)")
    pp = sub.add_parser("person", help="직원 찾기 — 이름·아이디·영문 이름 부분 일치(JSON)")
    pp.add_argument("query")
    vl = sub.add_parser("vacation-line", help="휴가 결재선 저장 — 휴가 신청 때 자동으로 들어간다")
    vl.add_argument("action", choices=["show", "set", "add", "remove", "clear"],
                    help="show 보기 · set 통째로 바꾸기 · add 추가 · remove 빼기 · clear 지우기")
    vl.add_argument("names", nargs="*", help="remove 때 뺄 이름·아이디")
    vl.add_argument("--approver", action="append", default=[], help="결재자(B) — 적은 순서가 결재 순서")
    vl.add_argument("--processor", action="append", default=[], help="처리자(C)")
    vl.add_argument("--ref", action="append", default=[], help="참조자(F)")
    ap2 = sub.add_parser("approval", help="전자결재 — list 문서 목록·상태 · count 건수")
    ap2.add_argument("action", choices=["list", "count"])
    ap2.add_argument("--box", default="all", help="all(기본)·writer 기안·approval 결재·refer 수신·read 회람/참조·reading 열람·return 반려·temp 임시저장")
    ap2.add_argument("--status", help="상태로 거르기 — 진행·완료·반려 등")
    ap2.add_argument("--search", help="제목·내용 등 검색어")
    ap2.add_argument("--limit", type=int, default=30)
    ap2.add_argument("--offset", type=int, default=0)
    wk = sub.add_parser("work", help="근무 체크 — status 상태 · in 출근 · out 퇴근(in·out 은 --yes 없으면 미리보기만)")
    wk.add_argument("action", choices=["status", "in", "out"])
    wk.add_argument("--yes", action="store_true", help="실제로 기록한다(지금 시각)")
    vr = sub.add_parser("vacation-request", help="휴가 신청 — --yes 없으면 사전 검사·미리보기만")
    vr.add_argument("--start", required=True, help="YYYY-MM-DD")
    vr.add_argument("--end", help="YYYY-MM-DD(종일 기간의 마지막 날, 기본 시작일)")
    vr.add_argument("--type", default="연차", help="휴가 종류(기본 연차)")
    vr.add_argument("--half", choices=["am", "pm"], help="반차 — am 09~13시, pm 14~18시")
    vr.add_argument("--start-time", help="시간 단위 시작 HH:MM")
    vr.add_argument("--end-time", help="시간 단위 끝 HH:MM")
    vr.add_argument("--reason", default="", help="사유")
    vr.add_argument("--dept", help="신청 부서(기본 내 소속 중 가장 아래 부서)")
    vr.add_argument("--approver", action="append", default=[], help="결재자 추가(신청 라인 B) — 이름 또는 아이디, 여러 번·쉼표 가능, 적은 순서가 결재 순서")
    vr.add_argument("--processor", action="append", default=[], help="처리자 추가(C) — 이름 또는 아이디")
    vr.add_argument("--ref", action="append", default=[], help="참조자 추가(F) — 이름 또는 아이디")
    vr.add_argument("--no-saved-line", action="store_true", help="저장해 둔 결재선(vacation-line)을 넣지 않는다")
    vr.add_argument("--yes", action="store_true", help="실제로 신청한다(결재선에 알림이 간다)")
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
            if a.cmd in ("mail", "vacation", "leave-calendar", "org", "person", "vacation-request", "work",
                         "approval", "vacation-line"):
                return run_api_command(hw, a)
            if a.cmd == "session":
                print(f"{'저장 세션 재사용' if how == 'reused' else '새로 로그인'} · {hw.username}")
                return 0
            r = hw.get(a.url)
            print(f"HTTP {r.status} {r.url}")
            print(r.body.decode("utf-8", "ignore")[: a.max])
            return 0 if r.status < 400 else 1
    except HiworksError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
