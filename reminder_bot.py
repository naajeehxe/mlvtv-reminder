"""
MLVTV 영상 제출 독촉 봇 (v4)
- 상태가 '제출완료'가 아닌 행을 모아 #general_student 채널에 '미제출 현황' 1개 글로 게시
- 각 줄에 @멘션 + 얼마나 밀렸는지(예: 3주 경과 / 마감 2일 전) 표시
- 매주 실행하도록 스케줄하면 => 제출완료 될 때까지 매주 리마인드
- DRY_RUN=1 이면 슬랙에 아무것도 안 올리고 터미널에만 미리보기 출력

필요:  pip install requests slack_sdk
환경변수:
  DRY_RUN                 (1이면 발송 안 함. 기본 0)
  NOTION_TOKEN, NOTION_DATABASE_ID
  SLACK_BOT_TOKEN, SLACK_CHANNEL_ID   (#general_student 채널 ID)
"""

import os
from datetime import date, datetime

import requests
from slack_sdk import WebClient

# ---------------------------------------------------------------- 설정
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
NOTION_VERSION = "2025-09-03"

slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
CHANNEL = os.environ["SLACK_CHANNEL_ID"]  # #general_student 채널 ID (C...)

# 문구에 넣을 링크 (여기만 바꾸면 됨)
MYBOX_LINK = "https://mybox.naver.com/main/web/shared?resourceKey=aGtpbWN2bWx8MzQ3MjUzMjEzODk2MjkyNDM2MXxEfDEzMzY3Mzcw"
NOTION_LINK = "https://www.notion.so/325a6dfcb578468d8f2d474c3f9c8cd5?v=2c966beed4be4920b76169d61e207383"

# 독촉이 3단계 캐릭터 이미지 (GitHub raw URL로 교체하세요)
DOKCHOK_IMG = {
    1: "https://raw.githubusercontent.com/naajeehxe/mlvtv-reminder/main/lv1_icon.png",  # 1주 경과
    2: "https://raw.githubusercontent.com/naajeehxe/mlvtv-reminder/main/lv2_icon.png",  # 2주 경과
    3: "https://raw.githubusercontent.com/naajeehxe/mlvtv-reminder/main/lv3_icon.png",  # 3주+ 경과
}


def dokchok_level(rows_with_todo):
    """마감 초과 건 기준으로 독촉이 단계 반환. 초과 없으면 0(아이콘 없음)."""
    max_over = 0
    for page, _ in rows_with_todo:
        deadline = prop_date(page, P_DEADLINE)
        if deadline:
            over = -days_until(deadline)   # 마감 지난 일수(+면 경과)
            max_over = max(max_over, over)
    if max_over >= 21:
        return 3
    if max_over >= 14:
        return 2
    if max_over >= 1:
        return 1
    return 0   # 마감 초과 건 없음 -> 기본 아이콘


P_TITLE, P_DEADLINE, P_SLACK_ID = "제목", "MLVTV 마감", "Slack ID"
P_ASSIGNEE = "담당자"
P_VENUE = "학회"
STATUS_DONE = "제출 완료"

# 체크할 제출 항목: (표시이름, Notion 속성명)
DELIVERABLES = [
    ("영상", "영상 상태"),
    ("코드", "코드 상태"),
    ("poster PDF", "poster PDF 상태"),
]

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------- Slack 이름->ID 매핑
_slack_directory = None  # {소문자이름: 멤버ID}


def build_slack_directory():
    """워크스페이스 구성원의 실명/표시이름 -> 멤버ID 딕셔너리를 만든다."""
    directory = {}
    cursor = None
    while True:
        resp = slack.users_list(cursor=cursor, limit=200)
        for m in resp["members"]:
            if m.get("deleted") or m.get("is_bot"):
                continue
            prof = m.get("profile", {})
            for key in (m.get("name"), prof.get("real_name"),
                        prof.get("display_name")):
                if key:
                    directory[key.strip().lower()] = m["id"]
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return directory


def resolve_slack_id(slack_id, assignee):
    """Slack ID가 있으면 그대로, 없으면 담당자 이름으로 자동 매핑."""
    global _slack_directory
    if slack_id:
        return slack_id
    if not assignee:
        return ""
    if _slack_directory is None:
        _slack_directory = build_slack_directory()
    return _slack_directory.get(assignee.strip().lower(), "")


# ---------------------------------------------------------------- Notion
def get_data_source_id(database_id):
    r = requests.get(f"https://api.notion.com/v1/databases/{database_id}",
                     headers=HEADERS)
    r.raise_for_status()
    return r.json()["data_sources"][0]["id"]


def query_all(data_source_id):
    """모든 행을 가져온다(미완 판단은 파이썬에서)."""
    results, cursor = [], None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/data_sources/{data_source_id}/query",
            headers=HEADERS, json=payload)
        r.raise_for_status()
        data = r.json()
        results.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def pending_items(page):
    """이 행에서 아직 '제출완료'가 아닌 항목 이름 목록. 예: ['영상', 'poster PDF']"""
    todo = []
    for label, prop in DELIVERABLES:
        if prop_text(page, prop) != STATUS_DONE:
            todo.append(label)
    return todo


def prop_text(page, name):
    p = page["properties"].get(name)
    if not p:
        return ""
    t = p["type"]
    if t == "title":
        return "".join(x["plain_text"] for x in p["title"])
    if t == "rich_text":
        return "".join(x["plain_text"] for x in p["rich_text"])
    if t == "select":
        return p["select"]["name"] if p["select"] else ""
    return ""


def prop_date(page, name):
    d = page["properties"].get(name, {}).get("date")
    return d["start"] if d else None


def days_until(deadline_str):
    return (datetime.fromisoformat(deadline_str).date() - date.today()).days


# ---------------------------------------------------------------- 문구
def status_label(deadline):
    """마감 대비 얼마나 밀렸는지 라벨."""
    if not deadline:
        return "미제출 상태입니다"
    d = days_until(deadline)          # +면 남음, -면 경과
    if d > 0:
        return f"마감 {d}일 전입니다"
    if d == 0:
        return "오늘이 마감입니다 ⏰"
    over = -d
    if over >= 7:
        weeks = over // 7
        days = over % 7
        if days:
            return f"{weeks}주 {days}일 경과했습니다"
        return f"{weeks}주 경과했습니다"
    return f"{over}일 경과했습니다"


def _who_label(page):
    slack_id = prop_text(page, P_SLACK_ID)
    assignee = prop_text(page, P_ASSIGNEE)
    uid = resolve_slack_id(slack_id, assignee)
    if uid:
        return f"<@{uid}>"
    if assignee:
        print(f"  ⚠️ '{assignee}' Slack 계정을 못 찾음 "
              f"(이름이 Slack 표시이름과 다르거나 미가입). 멘션 없이 표시.")
    return assignee or "(담당자 미지정)"


def _fmt_date(deadline):
    return deadline.replace("-", ".") if deadline else "마감일 미정"


def build_channel_message(rows_with_todo):
    # 마감 전(upcoming) / 마감 초과(overdue)로 분류
    upcoming, overdue = [], []
    for page, todo in rows_with_todo:
        deadline = prop_date(page, P_DEADLINE)
        d = days_until(deadline) if deadline else 0
        (overdue if d < 0 else upcoming).append((page, todo))

    lines = ["📋 *논문 자료 취합 리마인더*", ""]

    # 🚨 마감 초과 — 독촉이 강하게
    if overdue:
        lines.append("🚨 *마감 초과*")
        for page, todo in overdue:
            title = prop_text(page, P_TITLE)
            venue = prop_text(page, P_VENUE)
            deadline = prop_date(page, P_DEADLINE)
            venue_tag = f"*[{venue}]* " if venue else ""
            lines.append(f"{_who_label(page)}  {venue_tag}{title} ({status_label(deadline)})")
            marks = [f"{'✅' if prop_text(page, prop) == STATUS_DONE else '❌'} {label}"
                     for label, prop in DELIVERABLES]
            lines.append("    " + "   ".join(marks))
        lines.append("")

    # ⏳ 마감 전 — 가볍게 안내
    if upcoming:
        lines.append("⏳ *마감 예정*")
        for page, todo in upcoming:
            title = prop_text(page, P_TITLE)
            venue = prop_text(page, P_VENUE)
            deadline = prop_date(page, P_DEADLINE)
            venue_tag = f"*[{venue}]* " if venue else ""
            lines.append(f"{_who_label(page)}  {venue_tag}{title} — "
                         f"*{_fmt_date(deadline)}*까지 업로드 부탁드립니다 🙏")
        lines.append("")

    lines.append(f"영상(pptx에 녹화를 첨부하여 제출)·코드·poster PDF를 "
                 f"<{MYBOX_LINK}|Mybox>에 업로드한 뒤, 반드시 "
                 f"<{NOTION_LINK}|Notion>에서 상태를 *`제출 완료`* 로 변경해야 합니다.")
    lines.append("_제출 완료로 변경하지 않으면 완료될 때까지 매주 리마인더가 발송됩니다._")
    return "\n".join(lines)


# ---------------------------------------------------------------- Slack (DRY_RUN 지원)
def send(channel, text, image_url=None):
    if DRY_RUN:
        extra = f"\n[봇 아이콘] {image_url}" if image_url else ""
        print(f"[DRY_RUN] 슬랙에 올리지 않음. 아래는 미리보기입니다.\n-> {channel}{extra}\n{text}")
        return
    kwargs = {"channel": channel, "text": text}
    if image_url:
        kwargs["icon_url"] = image_url          # 단계별 독촉이 아이콘만 교체
    slack.chat_postMessage(**kwargs)


# ---------------------------------------------------------------- 메인
def main():
    if DRY_RUN:
        print(">>> DRY_RUN 모드: 슬랙엔 아무것도 안 올라갑니다. 터미널 미리보기만.\n")

    ds = get_data_source_id(DATABASE_ID)
    rows = query_all(ds)

    # 하나라도 미완인 행만 추림
    pending = [(page, todo) for page in rows if (todo := pending_items(page))]
    print(f"전체 {len(rows)}건 중 미완 {len(pending)}건\n")

    if not pending:
        print("모두 제출완료 — 알림 보낼 것 없음 ✅")
        return

    level = dokchok_level(pending)     # 마감 초과 기준 1/2/3, 없으면 0
    img = DOKCHOK_IMG.get(level)       # 0이면 None -> 기본 아이콘
    print(f"독촉이 단계: lv{level}")
    send(CHANNEL, build_channel_message(pending), image_url=img)


if __name__ == "__main__":
    main()
