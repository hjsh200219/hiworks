---
name: hiworks
description: 하이웍스(Hiworks) 로그인 세션을 브라우저 없이 만들고 재사용하며(Scrapling) 메일 읽기·쓰기·삭제, 내 잔여 휴가, 전사 휴가 캘린더(다른 사람 휴가 기간), 조직도·직원 조회를 JSON 으로 돌려준다. 트리거 — "/hiworks", "하이웍스 로그인", "하이웍스 메일", "메일 보내줘", "메일 지워줘", "받은 메일", "잔여 휴가", "남은 연차", "누구 휴가", "휴가 캘린더", "조직도", "OO 연락처", "OO 부서". 자격증명이 없으면 그 자리에서 setup 을 실행한다.
argument-hint: "[mail|vacation|leave-calendar|org|person|status|check|session|forget]"
allowed-tools: Bash(uv run:*)
---

# hiworks — 하이웍스 로그인 세션

모든 명령은 `uv run "${CLAUDE_PLUGIN_ROOT}/scripts/hiworks.py" <명령>` 이다(Windows 도 같음).
아래에서는 `HW` 로 줄여 쓴다.

| 명령 | 하는 일 | 네트워크 |
|---|---|---|
| `HW status` | 등록 상태(이메일·비밀번호 저장 여부·세션 파일). 값은 출력 안 함 | 없음 |
| `HW check` | 저장 세션이 살아 있는지만 확인. 로그인하지 않음. 죽었으면 exit 1 | GET 1회 |
| `HW session` | 살아 있으면 재사용, 아니면 로그인 1회 | GET 1회 (+ 로그인) |
| `HW login` | 저장 세션을 무시하고 새로 로그인 | 로그인 |
| `HW get <url>` | 세션으로 인증된 GET, 본문 앞 2000자(`--max N`) | GET |
| `HW forget` | 키체인 항목·설정·세션 삭제 | 없음 |
| `HW mail boxes` | 메일함 목록(b0 받은·b1 보낸·b2 보낼·b3 임시·b4 스팸·b5 휴지통) | GET |
| `HW mail list [--box b0] [--limit 20] [--offset 0] [--subject 말] [--from 주소]` | 메일 목록 `{total, items[]}` | POST |
| `HW mail read <no> [--html]` | 메일 본문(`content_text`). 읽음 표시는 안 바뀐다 | GET |
| `HW mail send --to a,b --subject 제목 (--body 본문 \| --body-file 파일) [--cc] [--bcc] [--html] [--yes]` | 발송. **`--yes` 없으면 미리보기만** | POST |
| `HW mail delete <no>... [--permanent] [--yes]` | 기본 휴지통 이동, `--permanent` 는 완전 삭제. **`--yes` 없으면 미리보기만** | POST |
| `HW vacation` | 내 휴가 종류별 발생·사용·잔여 | GET |
| `HW leave-calendar [--month YYYY-MM] [--name 이름] [--dept 부서]` | 전사 휴가 캘린더 — 누가 언제 휴가인지(연속된 종일 휴가는 기간으로 묶음) | GET |
| `HW org` | 조직도 — 부서 트리와 구성원(이름·직위·직책) | GET |
| `HW person <이름\|아이디>` | 직원 찾기 — 부서·직위·회사 전화, 본인이 공개한 경우만 휴대폰·이메일·입사일 | GET |

## 순서

1. `HW status` — 「등록 안 됨」이면 사용자를 `/hiworks:setup` 으로 돌려보내지 말고 **그 자리에서** `HW setup` 을 실행한다(Bash `timeout` 600000, 실행 직전 「화면에 Hiworks 입력 창이 뜹니다」 한 줄 안내). **비밀번호를 채팅으로 묻지 않는다.**
2. 로그인이 필요한 일이면 `HW session` (필요할 때만 로그인한다 — `login` 을 습관적으로 부르지 않는다).
3. 그 뒤 `HW get <url>` 또는 Python 에서 모듈로 쓴다.

## 지킬 것

- **로그인·인증 코드는 실패해도 자동 재시도하지 않는다.** 틀린 비밀번호·인증 코드 횟수가 쌓이면 계정이 잠긴다. 실패 사유를 사용자에게 알리고 멈춘다.
- 2단계 인증 계정은 `session`/`login` 때 인증 코드 입력 창이 뜬다. 이 두 명령은 Bash 도구 `timeout` 을 600000 으로 준다(사람이 입력하는 동안 기본 2분에 끊기지 않게). 사람이 없는 자동 실행이면 `--no-prompt` 를 붙여 창 대신 실패하게 한다.
- `202` 계열 사유(2단계 인증 설정·비밀번호 변경·로그인 제한)는 이 도구가 처리하지 않는다 — 브라우저에서 하이웍스에 로그인해 끝내도록 안내한다.
- 세션 쿠키 값(`~/.hiworks/session.json`)을 화면에 출력하지 않는다.
- IP 보안 기본값은 1단계(C클래스 대역)다. 네트워크가 바뀌면 세션이 죽고 `session` 이 다시 로그인한다.

## 메일 보내기·지우기 규칙

- **보내기 전에 받는 사람·제목·본문을 사용자에게 보여 주고 확인을 받은 뒤에만 `--yes` 를 붙인다.** 먼저 `--yes` 없이 실행해 미리보기 JSON 을 보여 줘도 된다.
- 발송이 실패해도 자동으로 다시 보내지 않는다 — 서버가 이미 받았다면 두 통이 된다. 보낸 메일함(`--box b1`)에서 확인한 뒤 사용자에게 묻는다.
- 삭제는 기본이 휴지통 이동이다. `--permanent`(복구 불가)는 사용자가 «완전 삭제»를 분명히 말했을 때만 쓴다.
- 「전부」「모두」 지우기 요청이면 `mail list` 로 번호를 뽑아 개수와 제목 몇 개를 보여 주고 확인을 받은 뒤 지운다.

## 다른 사람 정보

- `person`·`org`·`leave-calendar` 는 회사 하이웍스에서 그 계정이 볼 수 있는 정보만 돌려준다. 휴대폰·이메일·입사일은 그 직원이 공개로 해 둔 경우에만 나온다.
- 물어본 것에 필요한 만큼만 답한다(예: 휴가 기간을 물으면 기간만). 결과를 통째로 붙여 넣지 않는다.

## Python 에서 쓰기

```python
import sys; sys.path.insert(0, f"{plugin_root}/scripts")
from hiworks import Hiworks, ask_otp
with Hiworks() as hw:            # 이메일·비밀번호는 키체인에서
    hw.ensure(ask_otp)           # 재사용 또는 로그인
    hw.list_mails("b0", limit=20)                 # {total, items}
    hw.get_mail(no)                               # 본문
    hw.send_mail(["b@x.com"], "제목", "본문")      # 실제 발송 — 호출 전에 사람 확인
    hw.delete_mails([no])                         # 휴지통 이동, permanent=True 는 완전 삭제
    hw.my_vacation(); hw.vacation_calendar(2026, 10, name="홍")
    hw.org_chart(); hw.find_people("홍길동")
    r = hw.get("https://...")                     # 그 밖의 하이웍스 API — scrapling Response
```

실행 환경에 `scrapling[fetchers]>=0.4.11`, `keyring>=25` 가 있어야 한다(`uv run --with ...`).

## 로그인 API 메모

엔드포인트·응답 코드·세션 판정 근거는 `scripts/hiworks.py` 상단 docstring 이 정본이다.
