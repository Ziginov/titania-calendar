#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

API_URL = (
    "https://titania2-gateway-tuotanto-e1.cgisaas.fi/"
    "titania-planning-api/assignments/employee-aggregate"
)
AUTH_BASE = "https://titania2-auth-tuotanto-e1.cgisaas.fi"
REALM = "esperi"
TOKEN_URL = f"{AUTH_BASE}/realms/{REALM}/protocol/openid-connect/token"

TENANT_ID = "esperi"
CLIENT_ID = "SELF-SERVICES-EMPLOYEE"
OIDC_CLIENT_ID = "mobile-sso"

OUTPUT_ICS = Path("titania_work.ics")
SNAPSHOT_FILE = Path("titania_snapshot.json")
ENCRYPTED_REFRESH_FILE = Path("titania_refresh_token.enc")

# Данные для приблизительной статистики/зарплаты.
MONTHLY_SALARY = 2415.76
EVENING_RATE = 2.22
NIGHT_RATE = 5.93
SATURDAY_RATE = 3.71
SUNDAY_RATE = 14.82

TAX_RATE = 0.105
PENSION_RATE = 0.073
UNEMPLOYMENT_RATE = 0.0089


def post_form(url: str, data: dict) -> dict:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Auth HTTP {exc.code}: {body[:400]}") from exc


def decrypt_refresh_token() -> str:
    key = os.environ.get("TITANIA_TOKEN_KEY", "").strip()
    if not key:
        raise RuntimeError("Нет GitHub Secret TITANIA_TOKEN_KEY.")
    if not ENCRYPTED_REFRESH_FILE.exists():
        raise RuntimeError("Нет titania_refresh_token.enc в репозитории.")

    token = Fernet(key.encode("ascii")).decrypt(
        ENCRYPTED_REFRESH_FILE.read_bytes()
    ).decode("utf-8")
    return token.strip()


def encrypt_refresh_token(refresh_token: str) -> None:
    key = os.environ["TITANIA_TOKEN_KEY"].strip()
    encrypted = Fernet(key.encode("ascii")).encrypt(refresh_token.encode("utf-8"))
    ENCRYPTED_REFRESH_FILE.write_bytes(encrypted)


def refresh_access_token(refresh_token: str) -> tuple[str, str]:
    result = post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": OIDC_CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
    access_token = result.get("access_token")
    new_refresh = result.get("refresh_token")
    if not access_token or not new_refresh:
        raise RuntimeError("Titania auth не вернула access_token/refresh_token.")
    return access_token, new_refresh


def fetch_schedule(token: str, start: date, end: date) -> dict:
    params = urllib.parse.urlencode(
        {
            "startDate": start.strftime("%Y%m%d"),
            "endDate": end.strftime("%Y%m%d"),
            "enrichWithModifiedDates": "true",
        }
    )
    request = urllib.request.Request(
        f"{API_URL}?{params}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Client-Id": CLIENT_ID,
            "X-TenantId": TENANT_ID,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Titania API HTTP {exc.code}: {body[:400]}") from exc


def load_telegram_config() -> dict | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return {"bot_token": token, "chat_id": chat_id}


def send_telegram_message(text: str) -> None:
    config = load_telegram_config()
    if not config:
        print("Telegram secrets не заданы — уведомление пропущено.")
        return
    telegram_api_call(
        config["bot_token"],
        "sendMessage",
        {"chat_id": config["chat_id"], "text": text},
    )


def upload_ics_to_github() -> bool:
    # В GitHub Actions файл коммитится самим workflow после выполнения скрипта.
    print("GitHub Actions: календарь будет сохранён коммитом workflow.")
    return True

def ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\r\n", r"\n")
        .replace("\n", r"\n")
    )


def format_local_dt(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%Y%m%dT%H%M%S")


def pretty_shift_title(activity_name: str, start_value: str) -> str:
    """Return a compact calendar title such as '☀️ Työvuoro' or '🌙 Työvuoro'."""
    start_dt = datetime.fromisoformat(start_value)
    icon = "☀️" if start_dt.hour < 11 else "🌙"
    return f"{icon} Työvuoro"


def extract_location(activity_name: str) -> str:
    """Convert 'Pirttilä vastuu aamu' to 'Pirttilä'."""
    name = (activity_name or "").strip()

    suffixes = (
        " vastuu aamu",
        " vastuu ilta",
        " vastuu yö",
        " vastuu yo",
        " hyppyri aamu",
        " hyppyri ilta",
        " hyppyri yö",
        " hyppyri yo",
        " aamu",
        " ilta",
        " yö",
        " yo",
    )

    lowered = name.casefold()
    for suffix in suffixes:
        if lowered.endswith(suffix.casefold()):
            name = name[: -len(suffix)].strip()
            break

    return name or "Työvuoro"

def format_time_range(start_value: str, end_value: str) -> str:
    start_dt = datetime.fromisoformat(start_value)
    end_dt = datetime.fromisoformat(end_value)
    return f"{start_dt:%H:%M}–{end_dt:%H:%M}"





def collect_work_shifts(payload: dict) -> tuple[list[dict], int, str | None]:
    people = payload.get("data")
    if not isinstance(people, list):
        raise RuntimeError("В ответе Titania нет списка data.")

    # В Titania approvedShifts/dayStatuses могут отставать от того,
    # что уже реально показывается сотруднику в Työviikkoni.
    # Поэтому для календаря берём WORK-смены напрямую из combinedShifts —
    # именно там находятся смены, которые Titania вернула для выбранного периода.
    shifts: list[dict] = []
    seen: set[str] = set()

    for person in people:
        for shift in person.get("combinedShifts", []):
            if shift.get("assignmentType") != "WORK":
                continue

            start = shift.get("startDateTime")
            end = shift.get("endDateTime")
            if not start or not end:
                continue

            key = "|".join(
                [
                    str(shift.get("plannedAssignmentId") or shift.get("id") or ""),
                    start,
                    end,
                    str(shift.get("activityCode") or ""),
                ]
            )
            if key in seen:
                continue

            seen.add(key)
            shifts.append(shift)

    shifts.sort(key=lambda item: item["startDateTime"])

    latest_visible_date = shifts[-1]["startDateTime"][:10] if shifts else None

    approved_cutoff: str | None = None
    for person in people:
        for shift in person.get("approvedShifts", []):
            start = shift.get("startDateTime")
            if start:
                d = start[:10]
                if approved_cutoff is None or d > approved_cutoff:
                    approved_cutoff = d

    if approved_cutoff:
        print(f"Titania: approvedShifts заканчивается на {approved_cutoff}")
    if latest_visible_date:
        print(f"Titania: последняя WORK-смена в combinedShifts = {latest_visible_date}")
    if approved_cutoff and latest_visible_date and latest_visible_date > approved_cutoff:
        print(
            "Titania: approvedShifts отстаёт — не используем его как границу."
        )

    # Ничего не отбрасываем по approvedShifts/dayStatuses.
    skipped_after_cutoff = 0
    return shifts, skipped_after_cutoff, latest_visible_date

def overlap_minutes(
    start: datetime,
    end: datetime,
    window_start_hour: int,
    window_end_hour: int,
) -> int:
    """Minutes overlapping a daily time window. Supports windows crossing midnight."""
    total = 0
    day = start.date() - timedelta(days=1)

    while day <= end.date():
        if window_start_hour < window_end_hour:
            win_start = datetime.combine(day, datetime.min.time()).replace(
                hour=window_start_hour
            )
            win_end = datetime.combine(day, datetime.min.time()).replace(
                hour=window_end_hour
            )
        else:
            win_start = datetime.combine(day, datetime.min.time()).replace(
                hour=window_start_hour
            )
            win_end = datetime.combine(day + timedelta(days=1), datetime.min.time()).replace(
                hour=window_end_hour
            )

        overlap_start = max(start, win_start)
        overlap_end = min(end, win_end)
        if overlap_end > overlap_start:
            total += int((overlap_end - overlap_start).total_seconds() // 60)

        day += timedelta(days=1)

    return total


def saturday_minutes(start: datetime, end: datetime) -> int:
    total = 0
    cursor = start
    while cursor < end:
        next_cursor = min(cursor + timedelta(minutes=1), end)
        if cursor.weekday() == 5 and 6 <= cursor.hour < 20:
            total += int((next_cursor - cursor).total_seconds() // 60)
        cursor = next_cursor
    return total


def sunday_minutes(start: datetime, end: datetime) -> int:
    total = 0
    cursor = start
    while cursor < end:
        next_cursor = min(cursor + timedelta(minutes=1), end)
        if cursor.weekday() == 6:
            total += int((next_cursor - cursor).total_seconds() // 60)
        cursor = next_cursor
    return total


def format_minutes(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    return f"{hours} ч {mins:02d} мин"


def calculate_statistics(shifts: list[dict]) -> dict:
    total_minutes = 0
    by_type = defaultdict(int)
    by_month = defaultdict(lambda: {"shifts": 0, "minutes": 0})
    by_location = defaultdict(lambda: {"shifts": 0, "minutes": 0})

    evening_minutes = 0
    night_minutes = 0
    sat_minutes = 0
    sun_minutes = 0

    for shift in shifts:
        start = datetime.fromisoformat(shift["startDateTime"])
        end = datetime.fromisoformat(shift["endDateTime"])

        minutes = max(0, int((end - start).total_seconds() // 60))
        total_minutes += minutes

        shift_type = shift.get("shiftType") or "OTHER"
        by_type[shift_type] += 1

        month_key = start.strftime("%Y-%m")
        by_month[month_key]["shifts"] += 1
        by_month[month_key]["minutes"] += minutes

        location = extract_location(
            shift.get("activityName") or "Työvuoro"
        )
        by_location[location]["shifts"] += 1
        by_location[location]["minutes"] += minutes

        evening_minutes += overlap_minutes(start, end, 18, 21)
        night_minutes += overlap_minutes(start, end, 21, 6)
        sat_minutes += saturday_minutes(start, end)
        sun_minutes += sunday_minutes(start, end)

    premium_amounts = {
        "evening": evening_minutes / 60 * EVENING_RATE,
        "night": night_minutes / 60 * NIGHT_RATE,
        "saturday": sat_minutes / 60 * SATURDAY_RATE,
        "sunday": sun_minutes / 60 * SUNDAY_RATE,
    }

    premium_total = sum(premium_amounts.values())
    gross_estimate = MONTHLY_SALARY + premium_total
    estimated_deductions = gross_estimate * (
        TAX_RATE + PENSION_RATE + UNEMPLOYMENT_RATE
    )
    net_estimate = gross_estimate - estimated_deductions

    return {
        "total_shifts": len(shifts),
        "total_minutes": total_minutes,
        "by_type": dict(by_type),
        "by_month": dict(by_month),
        "by_location": dict(by_location),
        "premium_minutes": {
            "evening": evening_minutes,
            "night": night_minutes,
            "saturday": sat_minutes,
            "sunday": sun_minutes,
        },
        "premium_amounts": premium_amounts,
        "premium_total": premium_total,
        "gross_estimate": gross_estimate,
        "net_estimate": net_estimate,
    }


def print_statistics(stats: dict) -> None:
    labels = {
        "MORNING_SHIFT": "Утренних",
        "EVENING_SHIFT": "Вечерних",
        "NIGHT_SHIFT": "Ночных",
        "DAY_SHIFT": "Дневных",
        "OTHER": "Других",
    }

    print("\n=== Статистика ===")
    print(f"Смен: {stats['total_shifts']}")
    print(f"Всего часов: {format_minutes(stats['total_minutes'])}")

    for key, label in labels.items():
        count = stats["by_type"].get(key, 0)
        if count:
            print(f"{label}: {count}")

    if stats["by_month"]:
        print("\nПо месяцам:")
        for month, data in sorted(stats["by_month"].items()):
            print(
                f"  {month}: {data['shifts']} смен, "
                f"{format_minutes(data['minutes'])}"
            )

    if stats["by_location"]:
        print("\nПо местам:")
        for location, data in sorted(
            stats["by_location"].items(),
            key=lambda item: (-item[1]["minutes"], item[0]),
        ):
            print(
                f"  {location}: {data['shifts']} смен, "
                f"{format_minutes(data['minutes'])}"
            )

    pm = stats["premium_minutes"]
    pa = stats["premium_amounts"]

    print("\n=== Прогноз доплат ===")
    print(
        f"Вечер 18–21: {format_minutes(pm['evening'])} "
        f"× {EVENING_RATE:.2f} € = {pa['evening']:.2f} €"
    )
    print(
        f"Ночь 21–06: {format_minutes(pm['night'])} "
        f"× {NIGHT_RATE:.2f} € = {pa['night']:.2f} €"
    )
    print(
        f"Суббота 06–20: {format_minutes(pm['saturday'])} "
        f"× {SATURDAY_RATE:.2f} € = {pa['saturday']:.2f} €"
    )
    print(
        f"Воскресенье: {format_minutes(pm['sunday'])} "
        f"× {SUNDAY_RATE:.2f} € = {pa['sunday']:.2f} €"
    )
    print(f"Доплаты всего: {stats['premium_total']:.2f} €")

    print("\n=== Оценка зарплаты ===")
    print(f"Базовый оклад: {MONTHLY_SALARY:.2f} €")
    print(f"Ориентировочный bruto: {stats['gross_estimate']:.2f} €")
    print(f"Очень приблизительный netto: {stats['net_estimate']:.2f} €")

    print("\nВажно:")
    print("- расчёт основан только на опубликованных сменах;")
    print("- праздничные доплаты пока не учитываются отдельно;")
    print("- отпускные, больничные, переработки и корректировки не учитываются;")
    print("- netto является только приблизительной оценкой.")


def telegram_api_call(token: str, method: str, payload: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers = {}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ошибка связи с Telegram: {exc.reason}") from exc

    if not result.get("ok"):
        raise RuntimeError(f"Telegram API: {result}")
    return result









def shift_record(shift: dict) -> dict:
    start = shift["startDateTime"]
    end = shift["endDateTime"]
    activity = shift.get("activityName") or "Työvuoro"

    source_id = str(
        shift.get("plannedAssignmentId")
        or shift.get("id")
        or hashlib.sha256(
            f"{start}|{end}|{activity}".encode("utf-8")
        ).hexdigest()[:24]
    )

    return {
        "id": source_id,
        "start": start,
        "end": end,
        "title": pretty_shift_title(activity, start),
        "location": extract_location(activity),
        "activity_code": shift.get("activityCode") or "",
    }


def load_snapshot() -> dict[str, dict]:
    if not SNAPSHOT_FILE.exists():
        return {}
    try:
        items = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        return {str(item["id"]): item for item in items}
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return {}


def save_snapshot(records: dict[str, dict]) -> None:
    ordered = sorted(records.values(), key=lambda item: item["start"])
    SNAPSHOT_FILE.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def compare_snapshots(
    old: dict[str, dict],
    new: dict[str, dict],
) -> tuple[list[dict], list[tuple[dict, dict]], list[dict]]:
    added = [new[key] for key in new.keys() - old.keys()]
    removed = [old[key] for key in old.keys() - new.keys()]

    changed: list[tuple[dict, dict]] = []
    for key in new.keys() & old.keys():
        old_record = old[key]
        new_record = new[key]
        comparable_fields = ("start", "end", "title", "location", "activity_code")
        if any(old_record.get(field) != new_record.get(field) for field in comparable_fields):
            changed.append((old_record, new_record))

    added.sort(key=lambda item: item["start"])
    removed.sort(key=lambda item: item["start"])
    changed.sort(key=lambda pair: pair[1]["start"])
    return added, changed, removed


def human_shift(record: dict) -> str:
    start = datetime.fromisoformat(record["start"])
    end = datetime.fromisoformat(record["end"])
    return (
        f"{record['title']} — {start:%d.%m.%Y}, "
        f"{start:%H:%M}–{end:%H:%M}"
    )


def build_change_message(
    added: list[dict],
    changed: list[tuple[dict, dict]],
    removed: list[dict],
    stats: dict,
) -> str:
    lines = ["📅 Изменения в Titania"]

    if added:
        lines.append(f"\n➕ Новые смены: {len(added)}")
        for record in added[:10]:
            lines.append(human_shift(record))
        if len(added) > 10:
            lines.append(f"…и ещё {len(added) - 10}")

    if changed:
        lines.append(f"\n✏️ Изменённые смены: {len(changed)}")
        for old_record, new_record in changed[:8]:
            lines.append(f"Было: {human_shift(old_record)}")
            lines.append(f"Стало: {human_shift(new_record)}")
        if len(changed) > 8:
            lines.append(f"…и ещё {len(changed) - 8}")

    if removed:
        lines.append(f"\n❌ Удалённые смены: {len(removed)}")
        for record in removed[:10]:
            lines.append(human_shift(record))
        if len(removed) > 10:
            lines.append(f"…и ещё {len(removed) - 10}")

    lines.extend(
        [
            "",
            f"Всего опубликовано: {stats['total_shifts']} смен",
            f"Часов: {format_minutes(stats['total_minutes'])}",
            f"Прогноз доплат: {stats['premium_total']:.2f} €",
            f"Ориентировочный bruto: {stats['gross_estimate']:.2f} €",
            f"Очень приблизительный netto: {stats['net_estimate']:.2f} €",
            "",
            "Календарь на GitHub обновлён.",
        ]
    )
    return "\n".join(lines)

def build_ics(shifts: list[dict]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Titania Calendar Exporter//RU//",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Titania — рабочие смены",
        "X-WR-TIMEZONE:Europe/Helsinki",
    ]

    for shift in shifts:
        start = shift["startDateTime"]
        end = shift["endDateTime"]

        activity = shift.get("activityName") or "Työvuoro"
        pretty_title = pretty_shift_title(activity, start)
        location = extract_location(activity)
        time_range = format_time_range(start, end)

        activity_code = shift.get("activityCode") or ""
        unit = shift.get("responsibleUnitCode") or ""
        shift_type = shift.get("shiftType") or ""

        raw_uid = "|".join(
            [
                str(shift.get("plannedAssignmentId") or shift.get("id") or ""),
                start,
                end,
                activity_code,
            ]
        )
        uid = (
            hashlib.sha256(raw_uid.encode("utf-8")).hexdigest()[:24]
            + "@titania-calendar"
        )

        description = [
            time_range,
            "",
            f"Yksikkö: {location}",
            f"Alkuperäinen nimi: {activity}",
            "Lähde: Titania",
        ]
        if activity_code:
            description.append(f"Код: {activity_code}")
        if shift_type:
            description.append(f"Тип: {shift_type}")
        if unit:
            description.append(f"Подразделение: {unit}")

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{stamp}",
                f"DTSTART;TZID=Europe/Helsinki:{format_local_dt(start)}",
                f"DTEND;TZID=Europe/Helsinki:{format_local_dt(end)}",
                f"SUMMARY:{ics_escape(pretty_title)}",
                f"LOCATION:{ics_escape(location)}",
                f"DESCRIPTION:{ics_escape(chr(10).join(description))}",
                "STATUS:CONFIRMED",
                "TRANSP:OPAQUE",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"



def main() -> None:
    print("=== Titania Cloud Update ===")

    start = date.today()
    end = start + timedelta(days=60)

    old_refresh = decrypt_refresh_token()
    access_token, new_refresh = refresh_access_token(old_refresh)
    print("✅ Titania: access_token обновлён через refresh_token.")

    # Сразу сохраняем ротированный refresh token в зашифрованный файл.
    encrypt_refresh_token(new_refresh)
    print("✅ Titania: новый refresh_token зашифрован для следующего запуска.")

    payload = fetch_schedule(access_token, start, end)
    shifts, skipped_after_cutoff, latest_published_date = collect_work_shifts(payload)
    stats = calculate_statistics(shifts)

    old_snapshot = load_snapshot()
    new_snapshot = {
        record["id"]: record
        for record in (shift_record(shift) for shift in shifts)
    }
    added, changed, removed = compare_snapshots(old_snapshot, new_snapshot)

    OUTPUT_ICS.write_text(build_ics(shifts), encoding="utf-8", newline="")
    upload_ics_to_github()

    if old_snapshot:
        if added or changed or removed:
            send_telegram_message(build_change_message(added, changed, removed, stats))
            print("✅ Telegram: отправлено уведомление об изменениях.")
        else:
            print("Telegram: изменений нет — молчим.")
    else:
        send_telegram_message(
            "✅ Titania cloud-автоматизация запущена.\n"
            f"Смен: {stats['total_shifts']}\n"
            f"Часов: {format_minutes(stats['total_minutes'])}"
        )

    save_snapshot(new_snapshot)

    print(f"✅ Смен выгружено: {len(shifts)}")
    print(f"✅ Последняя смена: {latest_published_date or 'не определена'}")
    print(f"✅ Период: {start.isoformat()} — {end.isoformat()}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"❌ Ошибка: {exc}")
        sys.exit(1)
