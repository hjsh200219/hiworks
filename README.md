# hiworks — 하이웍스 로그인 세션 플러그인

하이웍스(Hiworks) 로그인을 브라우저 없이 처리하고, 메일 읽기·쓰기·삭제, 내 잔여 휴가·휴가 신청, 다른 사람 휴가 기간(전사 휴가 캘린더), 조직도·직원 조회를 JSON API 로 제공하는 Claude Code 플러그인입니다.
[Scrapling](https://github.com/D4Vinci/Scrapling) 으로 하이웍스 로그인 API 를 직접 호출하고, 세션을 저장해 재사용합니다.

- **비밀번호는 OS 키체인에만** 저장합니다(macOS 키체인 · Windows 자격 증명 관리자 · Linux Secret Service).
- **비밀번호는 채팅으로 입력하지 않습니다.** 터미널 프롬프트(화면에 안 찍힘) 또는 OS 비밀번호 창으로만 받습니다.
- 비밀번호가 맞을 때만 저장합니다. 로그인·인증 코드는 실패해도 자동 재시도하지 않습니다(계정 잠김 방지).
- 2단계 인증(OTP) 계정이면 인증 코드 6자리를 입력 창으로 받습니다.

## 설치 — 두 단계

필요한 것: [uv](https://docs.astral.sh/uv/) (Python 과 의존성은 uv 가 알아서 받습니다)

**1. 설치** — 터미널에 한 줄:

```bash
claude plugin marketplace add hjsh200219/hiworks && claude plugin install hiworks@hiworks
```

**2. 등록** — Claude Code 에서 `/hiworks:setup` (이메일을 인자로 주면 입력 창 하나가 줄어듭니다: `/hiworks:setup 아이디@회사도메인`).
입력 창에 비밀번호를 넣으면 끝입니다. 이미 등록돼 있으면 창 없이 바로 끝납니다.
`/hiworks:setup` 을 안 불러도 "하이웍스 로그인해줘" 라고 하면 등록이 안 된 경우 그 자리에서 입력 창을 띄웁니다.

### 팀 전체에 자동 설치

팀이 같이 쓰는 저장소의 `.claude/settings.json` 에 아래를 넣으면, 그 폴더를 신뢰한 사람에게 Claude Code 가 플러그인 설치를 제안합니다(1단계 생략):

```json
{
  "extraKnownMarketplaces": {
    "hiworks": { "source": { "source": "github", "repo": "hjsh200219/hiworks" } }
  },
  "enabledPlugins": { "hiworks@hiworks": true }
}
```

## 사용

Claude 에게 "하이웍스 로그인 세션 만들어줘", "하이웍스 세션 살아 있어?" 처럼 말하거나 `/hiworks:hiworks` 를 부르면 됩니다.
터미널에서 직접 쓸 때:

```bash
HW="uv run <플러그인 경로>/scripts/hiworks.py"
$HW setup [--email 아이디@회사도메인] # 등록(비밀번호는 프롬프트/입력 창) · 이미 등록돼 있으면 확인만 · --force 로 새로
$HW status                          # 등록 상태(값은 출력 안 함)
$HW session                         # 세션 재사용 또는 로그인 1회
$HW check                           # 세션 생존 확인(로그인 안 함)
$HW get <url>                       # 로그인된 상태로 GET
$HW forget                          # 키체인 항목·설정·세션 삭제

# 메일 (결과는 JSON)
$HW mail list [--box b0] [--subject 말] [--from 주소] [--limit 20] [--offset 0]
$HW mail read <번호>
$HW mail send --to a@x.com --subject 제목 --body 본문 --yes      # --yes 없으면 미리보기만
$HW mail delete <번호>... [--permanent] --yes                     # 기본 휴지통 이동

# 인사
$HW vacation                                   # 내 휴가 발생·사용·잔여
$HW leave-calendar --month 2026-10 [--name 홍] [--dept 개발팀]   # 누가 언제 휴가인지
$HW org                                        # 조직도
$HW person 홍길동                               # 직원 찾기(본인이 공개한 연락처만)
$HW vacation-request --start 2026-12-21 [--end 2026-12-24] [--half am|pm] --reason 사유 --yes   # 휴가 신청(--yes 없으면 미리보기)
```

플러그인 경로는 `~/.claude/plugins/cache/hiworks/hiworks/<버전>/` 입니다.

## 저장 위치

| 무엇 | 어디 |
|---|---|
| 비밀번호 | OS 키체인, 서비스명 `hiworks`, 계정명 = 이메일 |
| 이메일 | `~/.hiworks/config.json` (권한 600) |
| 세션 쿠키 | `~/.hiworks/session.json` (권한 600) |

`HIWORKS_HOME` 으로 폴더를, `HIWORKS_KEYRING_SERVICE` 로 키체인 서비스명을 바꿀 수 있습니다.
키체인이 없는 환경(Linux 서버 등)에서는 환경변수 `HIWORKS_USERNAME`/`HIWORKS_PASSWORD` 를 쓰세요 — 저장된 값보다 우선합니다.

## 알아 둘 것

- **macOS 키체인 접근 창**: 키체인 항목은 uv 가 설치한 Python 이 만듭니다. uv 가 Python 을 새 버전으로 바꾸면 "python 이(가) 키체인 접근을 원합니다" 창이 한 번 뜰 수 있습니다 — 「항상 허용」을 누르세요.
- **원격 접속 중 등록**: 휴대폰 Remote Control 처럼 컴퓨터 화면을 볼 수 없으면 비밀번호 창도 볼 수 없습니다. 그때는 컴퓨터 앞에서 터미널로 `setup` 을 실행하세요.
- **IP 보안**: 기본은 하이웍스 브라우저 로그인과 같은 1단계(C클래스 대역)입니다. 네트워크가 바뀌면 세션이 끊기고 `session` 이 다시 로그인합니다. `--ip-level -1|1|2` 로 바꿀 수 있습니다.
- 2단계 인증 설정·비밀번호 변경 요구·로그인 제한 확인은 처리하지 않습니다. 브라우저에서 하이웍스에 로그인해 끝내세요.

## 개인정보

`person`·`org`·`leave-calendar` 는 로그인한 계정이 하이웍스 화면에서 볼 수 있는 범위만 돌려줍니다. 휴대폰·이메일·입사일은 그 직원이 공개로 설정한 경우에만 내보냅니다. 결과를 회사 밖으로 옮기지 마세요.

## 라이선스

MIT. 하이웍스(가비아)와 무관한 개인 프로젝트이며, 하이웍스 웹 로그인 화면이 쓰는 API 를 그대로 호출합니다 — 하이웍스가 API 를 바꾸면 동작하지 않을 수 있습니다.

## 개발

```bash
uv run --no-project --with 'scrapling[fetchers]>=0.4.11' --with 'keyring>=25' python -m unittest discover -s tests
```

테스트는 네트워크·실계정·실제 키체인 없이 돕니다(메모리 키체인 · 가짜 응답).
로그인 API 와 응답 코드, 세션 판정 근거는 `scripts/hiworks.py` 상단 docstring 에 있습니다.
배포할 때는 `.claude-plugin/plugin.json` 의 `version` 을 올려야 사용자의 `claude plugin update` 가 새 버전을 받습니다.
