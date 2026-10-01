---
name: hiworks
description: 하이웍스(Hiworks) 로그인 세션을 브라우저 없이 만들고 재사용한다(Scrapling). 하이웍스 로그인, 로그인된 상태로 하이웍스 페이지·내부 API 호출, 세션 확인, 하이웍스 계정 등록·삭제에 쓴다. 트리거 — "/hiworks", "하이웍스 로그인", "하이웍스 세션", "하이웍스 API", "hiworks 로그인", "하이웍스 계정 등록". 자격증명이 없으면 /hiworks:setup 으로 보낸다.
argument-hint: "[status|check|session|login|get <url>|forget]"
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

## Python 에서 쓰기

```python
import sys; sys.path.insert(0, f"{plugin_root}/scripts")
from hiworks import Hiworks, ask_otp
with Hiworks() as hw:            # 이메일·비밀번호는 키체인에서
    hw.ensure(ask_otp)           # 재사용 또는 로그인
    r = hw.get("https://...")    # scrapling Response (r.status, r.body, r.json())
```

실행 환경에 `scrapling[fetchers]>=0.4.11`, `keyring>=25` 가 있어야 한다(`uv run --with ...`).

## 로그인 API 메모

엔드포인트·응답 코드·세션 판정 근거는 `scripts/hiworks.py` 상단 docstring 이 정본이다.
