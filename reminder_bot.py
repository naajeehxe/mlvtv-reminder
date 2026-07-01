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

P_TITLE, P_DEADLINE, P_SLACK_ID, P_STATUS = "제목", "MLVTV 마감", "Slack ID", "상태"
P_ASSIGNEE = "담당자"
P_VENUE = "학회"
STATUS_DONE = "제출완료"

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


def query_pending(data_source_id):
    payload = {"filter": {"property": P_STATUS,
                          "select": {"does_not_equal": STATUS_DONE}}}
    r = requests.post(
        f"https://api.notion.com/v1/data_sources/{data_source_id}/query",
        headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()["results"]


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


def build_channel_message(rows):
    lines = ["📋 *[MLVTV] 이번 주 영상 미제출 현황*", ""]
    for page in rows:
        title = prop_text(page, P_TITLE)
        slack_id = prop_text(page, P_SLACK_ID)
        assignee = prop_text(page, P_ASSIGNEE)
        venue = prop_text(page, P_VENUE)
        deadline = prop_date(page, P_DEADLINE)

        uid = resolve_slack_id(slack_id, assignee)
        if uid:
            who = f"<@{uid}>"
        else:
            who = assignee or "(담당자 미지정)"
            if assignee:
                print(f"  ⚠️ '{assignee}' Slack 계정을 못 찾음 "
                      f"(이름이 Slack 표시이름과 다르거나 미가입). 멘션 없이 표시.")
        venue_tag = f"[{venue}] " if venue else ""
        lines.append(f"• {who}  {venue_tag}'{title}' — {status_label(deadline)}")
    lines.append("")
    lines.append(f"발표 슬라이드(pptx)에 발표 녹화를 넣은 형태로 저장해, "
                 f"<{MYBOX_LINK}|Mybox 폴더>에 업로드 부탁드려요. "
                 f"업로드 후 <{NOTION_LINK}|Notion>에서 상태를 '제출완료'로 바꿔주세요 🙏")
    return "\n".join(lines)


# ---------------------------------------------------------------- Slack (DRY_RUN 지원)
def send(channel, text):
    if DRY_RUN:
        print(f"[DRY_RUN] 슬랙에 올리지 않음. 아래는 미리보기입니다.\n-> {channel}\n{text}")
        return
    slack.chat_postMessage(channel=channel, text=text)


# ---------------------------------------------------------------- 메인
def main():
    if DRY_RUN:
        print(">>> DRY_RUN 모드: 슬랙엔 아무것도 안 올라갑니다. 터미널 미리보기만.\n")

    ds = get_data_source_id(DATABASE_ID)
    rows = query_pending(ds)
    print(f"미제출 {len(rows)}건 확인\n")

    if not rows:
        print("미제출 없음 — 알림 보낼 것 없음 ✅")
        return

    send(CHANNEL, build_channel_message(rows))


if __name__ == "__main__":
    main()
