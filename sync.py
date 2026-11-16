#!/usr/bin/env python3
"""
Синхронизация «Депозиты в работе» из Bitrix24.

Забирает из Bitrix24 депозиты (сделки в рабочих стадиях), звонки Ringostat,
комментарии, подборы (ссылки на авто в комментариях), задачи «Торги» и
«Просчёт ремонта», офисы и менеджеров. Собирает файл для сайта:

  site/data.enc.json  — зашифрованный паролем (если задан DASH_PASSWORD)
  site/data.json      — открытый (только если пароль НЕ задан, для локальной проверки)

Переменные окружения:
  BITRIX_WEBHOOK   https://factum-group.bitrix24.eu/rest/<user_id>/<token>/
  DASH_PASSWORD    пароль для сайта (рекомендуется)
  SITE_URL         адрес сайта (необязательно; в GitHub Actions берётся из имени репозитория).
                   Нужен, чтобы забрать прошлые снимки для сравнения «к вчера».
"""
import base64
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- настройки
CATEGORY_ID = 0  # воронка «Default pipeline»
STAGES = [  # рабочие стадии депозитов, по порядку
    ("4", "Депозит. Торгуем то, что хочет клиент"),
    ("UC_23XX7W", "Депозит на возврате"),
    ("5", "Активно двигаем по параметрам"),
    ("6", "Близок к реализации"),
]
WIN_STAGE = "7"      # «Выиграли — ждём оплату»: попадание сюда считаем выигрышем
WIN_DAYS = 30        # за сколько дней брать выигранные сделки для аналитики
DAILY_DAYS = 45      # сколько дней хранить статистику «по дням»
TEAM_ROOT_DEPARTMENTS = {"75", "40", "89"}  # «Отдел продаж», «Отдел покупок», «B2B» — все их менеджеры в списках
TORG_GROUP_ID = 12   # проект «1.2 Торги»
CALC_GROUP_ID = 10   # проект «1.1 Расчет ремонта США/Канада»
TASKS_LOOKBACK_DAYS = 120  # сколько дней задач в проектах просматривать
TALK_MIN_SEC = 30          # звонок короче — «недозвон», не разговор
HIST_HOURLY_KEEP_H = 26    # почасовые снимки для сравнения «к вчера»
HIST_DAILY_KEEP_D = 14     # по одному снимку в день — для истории

# комментарии, которые пишет бот / очередь лидов — не считаем работой менеджера
SYSTEM_COMMENT = re.compile(r"manychat\.com|Выдан через очередь|Рука поднята", re.I)
# ссылка на авто в комментарии = подбор
CAR_LINK = re.compile(
    r"(copart\.com|iaai\.com|manheim\.com|encar\.com|che168\.com|bid\.cars|olx\.|999\.md|"
    r"plc\.ua|mobile\.de|otomoto|autoscout|auto\.ria)", re.I)
# направление сделки: торги и просчёты ремонта считаем только для США/Канады
REGION_LINKS = {
    "usa": re.compile(r"copart\.com|iaai\.com|manheim\.com|bid\.cars|autoastat|stat\.vin|bidfax", re.I),
    "kor": re.compile(r"encar\.com|kbchachacha|kcar\.com", re.I),
    "chn": re.compile(r"che168\.com|dongchedi|autohome\.com", re.I),
    "eur": re.compile(r"mobile\.de|otomoto|autoscout|olx\.pl|autobazar|willhaben|leboncoin|marktplaats", re.I),
}
REGION_WORDS = {  # слова в названии сделки или в значении поля «Направление»
    "usa": re.compile(r"сша|\busa\b|америк|канад|\bcanada\b", re.I),
    "kor": re.compile(r"коре|korea", re.I),
    "chn": re.compile(r"кита|\bchina\b|\bbyd\b", re.I),
    "eur": re.compile(r"европ|europe|\beu\b|германи", re.I),
}
REGION_RECENT_DAYS = 30
_NB = r"(?<![а-яёіїєґa-z])"  # начало слова (в т.ч. кириллица): «скорее» не считается «Кореей»
REGION_KW = {  # как менеджеры пишут направление в комментариях
    "usa": re.compile(_NB + r"(сша|америк[а-яёіїє]*|канад[а-яёіїє]*|usa)(?![a-z])", re.I),
    "kor": re.compile(_NB + r"(коре[яиюйеі][а-яёіїє]*|корейск[а-яёіїє]*|korea[a-z]*)", re.I),
    "chn": re.compile(_NB + r"(кита[йяюеї][а-яёіїє]*|китайск[а-яёіїє]*|china|byd|zeekr|geely|chery|haval|changan|voyah|xpeng|lixiang|li ?auto|leapmotor|denza|юань)(?![a-z])", re.I),
    "eur": re.compile(_NB + r"(европ[а-яёіїє]*|європ[а-яёіїє]*|германи[а-яёіїє]*|німеччин[а-яёіїє]*|eu)(?![a-z])", re.I),
}
REGION_NEG = re.compile(r"(не|ні|без)\s+(хоч|хоче|смотр|дивит|рассматр|розгляд|интерес|цікав|подход|підход|нужн|треба|надо)", re.I)
BBCODE = re.compile(r"\[/?[^\]]+\]")
URL = re.compile(r"https?://\S+")
VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
LOT_IN_LINK = re.compile(r"(?:copart\.com/lot/|VehicleDetail/)(\d{6,9})", re.I)
LOT_8 = re.compile(r"(?:lot/|VehicleDetail/|\b)(\d{8})\b")
PHONE = re.compile(r"\d[\d\s\-()]{8,}\d")

DAY_MS = 86_400_000
PAUSE = 0.35  # секунд между запросами (лимит Bitrix24 ~2 запроса/сек)

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(ROOT, "site")


# ---------------------------------------------------------------- Bitrix REST
class Bitrix:
    def __init__(self, webhook):
        if not webhook:
            sys.exit("Не задан BITRIX_WEBHOOK")
        self.base = webhook.rstrip("/") + "/"
        m = re.match(r"(https?://[^/]+)", self.base)
        self.portal = m.group(1) if m else ""
        self.calls = 0

    def call(self, method, params=None):
        body = json.dumps(params or {}).encode()
        for attempt in range(6):
            req = urllib.request.Request(self.base + method + ".json", data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                time.sleep(PAUSE)
                self.calls += 1
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                if e.code in (429, 503) or "QUERY_LIMIT_EXCEEDED" in text:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"{method}: HTTP {e.code} {text[:300]}")
            except (urllib.error.URLError, TimeoutError):
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"{method}: не удалось после повторов")

    def all(self, method, params=None):
        """Все страницы списка (по 50)."""
        params = dict(params or {})
        out, start = [], 0
        while True:
            params["start"] = start
            r = self.call(method, params)
            if "error" in r:
                raise RuntimeError(f"{method}: {r.get('error')} {r.get('error_description')}")
            res = r.get("result")
            if isinstance(res, dict):
                res = res.get("tasks", res.get("items", []))
            out.extend(res or [])
            if "next" not in r:
                return out
            start = r["next"]

    def batch(self, cmds):
        """cmds: {name: (method, params)} — до 50 команд за раз."""
        q = {name: m + "?" + php_query(p) for name, (m, p) in cmds.items()}
        r = self.call("batch", {"halt": 0, "cmd": q})
        res = r.get("result", {})
        return res.get("result", {}), res.get("result_next", {}), res.get("result_error", {})


def php_query(params):
    """{'filter': {'ID': [1,2]}} → filter[ID][0]=1&filter[ID][1]=2 (как http_build_query в PHP)."""
    pairs = []

    def walk(prefix, v):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(f"{prefix}[{k}]" if prefix else str(k), x)
        elif isinstance(v, (list, tuple)):
            for i, x in enumerate(v):
                walk(f"{prefix}[{i}]", x)
        else:
            pairs.append((prefix, "" if v is None else str(v)))

    walk("", params)
    return urllib.parse.urlencode(pairs)


def ms(s):
    if not s:
        return None
    try:
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


def clean_title(s):
    """Название сделки без телефонов клиента (любая группа цифр, где 9+ цифр)."""
    s = re.sub(r"\+?\(?\d[\d\s\-().]{6,}\d", lambda m: "" if len(re.sub(r"\D", "", m.group(0))) >= 9 else m.group(0), s or "")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(\s*[-—–,;:/|]\s*)+$", "", s)
    return re.sub(r"^(\s*[-—–,;:/|]\s*)+", "", s).strip()


def region_of_text(text):
    for code, rx in REGION_WORDS.items():
        if rx.search(text or ""):
            return code
    return None


def chunks(seq, n=50):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def phones9(text):
    return {p for p in (re.sub(r"\D", "", x)[-9:] for x in PHONE.findall(text or "")) if len(p) == 9}


def vins(text):
    up = (text or "").upper()
    return {v for v in VIN.findall(up) if re.search(r"\d", v) and re.search(r"[A-Z]", v)}


# ---------------------------------------------------------------- сбор данных
def collect(bx, now_ms):
    log = lambda *a: print(*a, flush=True)
    dep_stages = [s for s, _ in STAGES]

    # поле «Направление» в сделке (если его заведут в Bitrix24) — главный источник
    dir_field, dir_map = None, {}
    try:
        deal_fields = bx.call("crm.deal.fields").get("result") or {}
    except RuntimeError as e:
        log(f"Поля сделки не прочитаны: {e}")
        deal_fields = {}
    for code, f in deal_fields.items():
        label = f.get("formLabel") or f.get("listLabel") or f.get("title") or ""
        if code.startswith("UF_") and re.search(r"направлени", label, re.I):
            dir_field = code
            dir_map = {str(i["ID"]): region_of_text(i.get("VALUE")) for i in f.get("items") or []}
            log(f"Поле направления: {code} «{label}»")
            break
    select = ["ID", "TITLE", "STAGE_ID", "ASSIGNED_BY_ID", "DATE_CREATE", "MOVED_TIME", "CONTACT_ID"]
    if dir_field:
        select.append(dir_field)

    # 1) депозиты в работе
    deals = bx.all("crm.deal.list", {
        "filter": {"CATEGORY_ID": CATEGORY_ID, "STAGE_ID": dep_stages},
        "select": select,
    })
    dep_set = {d["ID"] for d in deals}
    log(f"Депозитов: {len(deals)}")

    # 2) выигранные: сделки, перешедшие в стадию выигрыша за WIN_DAYS дней (по истории стадий)
    win_since = dt.datetime.fromtimestamp((now_ms - WIN_DAYS * DAY_MS) / 1000, dt.timezone.utc)
    win_time = {}
    try:
        for h in bx.all("crm.stagehistory.list", {
                "entityTypeId": 2,
                "filter": {"STAGE_ID": WIN_STAGE, ">=CREATED_TIME": win_since.strftime("%Y-%m-%dT%H:%M:%S")},
                "select": ["OWNER_ID", "CREATED_TIME"]}):
            i, t = str(h["OWNER_ID"]), ms(h.get("CREATED_TIME"))
            if t and i not in dep_set:
                win_time[i] = min(win_time.get(i, t), t)
    except RuntimeError as e:
        log(f"История стадий недоступна: {e}")
    wins = []
    for part in chunks(sorted(win_time), 50):
        wins += bx.all("crm.deal.list", {"filter": {"ID": part}, "select": select})
    log(f"Выигранных за {WIN_DAYS} дн.: {len(wins)}")

    all_deals = deals + wins
    ids = [d["ID"] for d in all_deals]
    all_set = set(ids)

    # когда сделка впервые попала в депозитные стадии
    dep_start = {}
    for part in chunks(ids, 50):
        try:
            for h in bx.all("crm.stagehistory.list", {
                    "entityTypeId": 2, "filter": {"OWNER_ID": [int(x) for x in part], "STAGE_ID": dep_stages},
                    "select": ["OWNER_ID", "CREATED_TIME"]}):
                i, t = str(h["OWNER_ID"]), ms(h.get("CREATED_TIME"))
                if t:
                    dep_start[i] = min(dep_start.get(i, t), t)
        except RuntimeError as e:
            log(f"История стадий недоступна: {e}")
            break

    # звонки (Ringostat пишет их как дела-звонки CRM)
    calls = []
    for part in chunks(ids, 50):
        calls += bx.all("crm.activity.list", {
            "filter": {"OWNER_TYPE_ID": 2, "OWNER_ID": part, "TYPE_ID": 2},
            "select": ["ID", "OWNER_ID", "DIRECTION", "START_TIME", "END_TIME", "RESPONSIBLE_ID"],
        })
    log(f"Звонков: {len(calls)}")

    # комментарии — пачками по 50 сделок, потом догружаем длинные
    comments = {i: [] for i in ids}
    for part in chunks(ids, 50):
        cmds = {f"d{i}": ("crm.timeline.comment.list", {
            "filter": {"ENTITY_TYPE": "deal", "ENTITY_ID": i},
            "select": ["ID", "CREATED", "AUTHOR_ID", "COMMENT"]}) for i in part}
        res, nxt, err = bx.batch(cmds)
        for key, items in res.items():
            comments[key[1:]] = list(items or [])
        for key in nxt:
            i = key[1:]
            comments[i] = bx.all("crm.timeline.comment.list", {
                "filter": {"ENTITY_TYPE": "deal", "ENTITY_ID": i},
                "select": ["ID", "CREATED", "AUTHOR_ID", "COMMENT"]})
        if err:
            log("Ошибки комментариев:", list(err)[:5])
    log(f"Комментариев: {sum(len(v) for v in comments.values())}")

    # ключи для сопоставления задач со сделками: VIN / лот из комментариев, телефон клиента
    by_vin, by_lot, by_phone = {}, {}, {}
    for d in all_deals:
        for c in comments[d["ID"]]:
            t = c.get("COMMENT") or ""
            for v in vins(t):
                by_vin.setdefault(v, set()).add(d["ID"])
            for l in LOT_IN_LINK.findall(t):
                by_lot.setdefault(l, set()).add(d["ID"])
    contact_ids = sorted({d["CONTACT_ID"] for d in all_deals if d.get("CONTACT_ID") not in (None, "", "0")})
    contact_phones = {}
    for part in chunks(contact_ids, 50):
        for c in bx.all("crm.contact.list", {"filter": {"ID": part}, "select": ["ID", "PHONE"]}):
            contact_phones[c["ID"]] = {re.sub(r"\D", "", p.get("VALUE", ""))[-9:] for p in c.get("PHONE") or []}
    for d in all_deals:
        for p in contact_phones.get(d.get("CONTACT_ID"), ()):
            if len(p) == 9:
                by_phone.setdefault(p, set()).add(d["ID"])

    since = dt.datetime.fromtimestamp((now_ms - TASKS_LOOKBACK_DAYS * DAY_MS) / 1000, dt.timezone.utc)

    def match_group(group_id):
        tasks = bx.all("tasks.task.list", {
            "filter": {"GROUP_ID": group_id, ">=CREATED_DATE": since.strftime("%Y-%m-%d")},
            "select": ["ID", "TITLE", "DESCRIPTION", "CREATED_DATE", "UF_CRM_TASK", "CREATED_BY"],
        })
        task_log.extend((group_id, ms(t.get("createdDate")), str(t.get("createdBy") or "")) for t in tasks)
        found, linked = {}, 0
        for t in tasks:
            text = f"{t.get('title') or ''} {t.get('description') or ''}"
            hits = set()
            crm = t.get("ufCrmTask") or []
            if crm:
                linked += 1
            for x in crm:
                if x.startswith("D_") and x[2:] in all_set:
                    hits.add(x[2:])
            for v in vins(text):
                hits |= by_vin.get(v, set())
            for l in LOT_8.findall(text):
                hits |= by_lot.get(l, set())
            for p in phones9(text):
                hits |= by_phone.get(p, set())
            created = ms(t.get("createdDate"))
            if created is None:
                continue
            for i in hits:
                found.setdefault(i, []).append(created)
        log(f"Проект {group_id}: задач {len(tasks)}, привязано к CRM {linked}, найдено сделок {len(found)}")
        return found, {"tasks": len(tasks), "linked": linked}

    task_log = []  # (проект, когда создана, кто поставил) — для статистики по дням
    torgs, torg_stat = match_group(TORG_GROUP_ID)
    calcs, calc_stat = match_group(CALC_GROUP_ID)

    # менеджеры и офисы: все активные сотрудники продаж/реализации/B2B + ответственные по сделкам
    deps = {d["ID"]: d for d in bx.all("department.get", {})}
    def under(dep_id, roots):
        seen, x = set(), str(dep_id)
        while x and x not in seen:
            if x in roots:
                return True
            seen.add(x)
            x = str(deps.get(x, {}).get("PARENT") or "")
        return False
    team_deps = [int(i) for i in deps if under(i, TEAM_ROOT_DEPARTMENTS)]
    users_raw = []
    if team_deps:
        users_raw += bx.all("user.get", {"ACTIVE": True, "UF_DEPARTMENT": team_deps})
    have = {str(u["ID"]) for u in users_raw}
    mgr_ids = sorted({d["ASSIGNED_BY_ID"] for d in all_deals}, key=int)
    missing = [m for m in mgr_ids if str(m) not in have]
    for part in chunks(missing, 50):
        users_raw += bx.all("user.get", {"ID": part})
    clean = lambda s: re.sub(r"\s+", " ", (s or "").strip())
    users = {}
    for u in users_raw:
        dep = str((u.get("UF_DEPARTMENT") or [""])[0])
        parent = deps.get(dep, {}).get("PARENT")
        users[str(u["ID"])] = {
            "n": clean(f"{u.get('LAST_NAME') or ''} {u.get('NAME') or ''}") or f"ID {u['ID']}",
            "pos": clean(u.get("WORK_POSITION")),
            "office": clean(deps.get(dep, {}).get("NAME")) or "—",
            "grp": clean(deps.get(str(parent), {}).get("NAME")) if parent else "",
            "team": under(dep, TEAM_ROOT_DEPARTMENTS),
        }
    for m in mgr_ids:  # уволенные / недоступные
        users.setdefault(str(m), {"n": f"ID {m}", "pos": "", "office": "—", "grp": "", "team": False})
    log(f"Сотрудников в списке: {len(users)}")

    # итог по каждой сделке
    calls_by = {}
    for c in calls:
        calls_by.setdefault(c["OWNER_ID"], []).append(c)
    last = lambda a: max(a) if a else None
    n30 = lambda a: sum(1 for t in a if t and t >= now_ms - 30 * DAY_MS)

    def analyse(d):
        i, mgr = d["ID"], d["ASSIGNED_BY_ID"]
        call_t, talk_t = [], []
        for c in calls_by.get(i, []):
            s, e = ms(c.get("START_TIME")), ms(c.get("END_TIME"))
            if s is None:
                continue
            call_t.append(s)
            if e and (e - s) / 1000 >= TALK_MIN_SEC:
                talk_t.append(s)
        com_t, sel_t = [], []
        reg = {k: 0.0 for k in REGION_LINKS}  # «очки» направления по комментариям
        for c in comments[i]:
            text = c.get("COMMENT") or ""
            if SYSTEM_COMMENT.search(text):
                continue
            t = ms(c.get("CREATED"))
            w = 2 if t and t >= now_ms - REGION_RECENT_DAYS * DAY_MS else 1  # свежие комментарии важнее
            for code, rx in REGION_LINKS.items():
                reg[code] += 0.3 * w * len(rx.findall(text))
            plain = BBCODE.sub(" ", URL.sub(" ", text))
            for code, rx in REGION_KW.items():
                for m in rx.finditer(plain):
                    ctx = plain[max(0, m.start() - 30):m.end() + 30]
                    reg[code] += (-1 if REGION_NEG.search(ctx) else 1) * w
            if str(c.get("AUTHOR_ID")) == str(mgr):
                com_t.append(t)
            if CAR_LINK.search(text):
                sel_t.append(t)
        # направление: поле → название и комментарии → ссылки → США по умолчанию
        dir_code, dir_src = None, "auto"
        if dir_field:
            v = d.get(dir_field)
            v = v[0] if isinstance(v, list) and v else v
            dir_code = dir_map.get(str(v)) if v else None
            if dir_code:
                dir_src = "field"
        if not dir_code:
            for code, rx in REGION_KW.items():
                if rx.search(d.get("TITLE") or ""):
                    reg[code] += 10  # направление в названии сделки — сильный признак
            best = max(reg.values())
            if best > 0:
                top = [k for k, n in reg.items() if n == best]
                dir_code = "usa" if "usa" in top else top[0]
        if not dir_code:
            dir_code, dir_src = "usa", "default"
        return {"call": call_t, "talk": talk_t, "com": com_t, "sel": sel_t,
                "torg": torgs.get(i, []), "calc": calcs.get(i, []), "dir": dir_code, "dirSrc": dir_src}

    def count_between(ts, a, b):
        return sum(1 for t in ts if t and (a is None or t >= a) and (b is None or t <= b))

    rows = []
    for d in deals:
        i, x = d["ID"], analyse(d)
        start = dep_start.get(i) or ms(d.get("DATE_CREATE"))
        tot = {k: count_between(x[k], start, None) for k in ("call", "talk", "com", "sel", "torg", "calc")}
        rows.append({
            "id": int(i), "t": clean_title(d.get("TITLE")), "st": d["STAGE_ID"], "m": int(d["ASSIGNED_BY_ID"]),
            "dir": x["dir"], "dirSrc": x["dirSrc"],
            "cr": ms(d.get("DATE_CREATE")), "mv": ms(d.get("MOVED_TIME")), "ds": start,
            "call": {"last": last(x["call"]), "lastTalk": last(x["talk"]), "n30": n30(x["call"]), "talk30": n30(x["talk"])},
            "com": {"last": last(x["com"]), "n30": n30(x["com"])},
            "sel": {"last": last(x["sel"]), "n30": n30(x["sel"])},
            "torg": {"last": last(x["torg"]), "n30": n30(x["torg"])},
            "calc": {"last": last(x["calc"]), "n30": n30(x["calc"])},
            "tot": tot,  # сколько всего с момента попадания в депозит
        })

    win_rows = []
    for d in wins:
        i, x = d["ID"], analyse(d)
        won = win_time[i]
        start = dep_start.get(i)
        tot = {k: count_between(x[k], start, won) for k in ("call", "talk", "com", "sel", "torg", "calc")}
        win_rows.append({
            "id": int(i), "t": clean_title(d.get("TITLE")), "m": int(d["ASSIGNED_BY_ID"]),
            "dir": x["dir"], "dirSrc": x["dirSrc"], "won": won, "ds": start, "tot": tot,
        })

    # статистика по дням: кто сколько сделал (Киев). Звонки/комментарии/подборы — по депозитам и выигранным,
    # торги/просчёты — все задачи в проектах по постановщику.
    from zoneinfo import ZoneInfo
    kyiv = ZoneInfo("Europe/Kyiv")
    day_of = lambda ts: dt.datetime.fromtimestamp(ts / 1000, kyiv).strftime("%Y-%m-%d")
    since_daily = now_ms - DAILY_DAYS * DAY_MS
    daily = {}
    def bump(ts, who, idx):
        if not ts or ts < since_daily or not who:
            return
        row = daily.setdefault(day_of(ts), {}).setdefault(str(who), [0, 0, 0, 0, 0, 0])
        row[idx] += 1  # 0 звонки, 1 разговоры, 2 комментарии, 3 подборы, 4 торги, 5 просчёты
    deal_mgr = {d["ID"]: d["ASSIGNED_BY_ID"] for d in all_deals}
    for c in calls:
        s, e = ms(c.get("START_TIME")), ms(c.get("END_TIME"))
        who = c.get("RESPONSIBLE_ID") or deal_mgr.get(c.get("OWNER_ID"))
        bump(s, who, 0)
        if s and e and (e - s) / 1000 >= TALK_MIN_SEC:
            bump(s, who, 1)
    for i, items in comments.items():
        for c in items:
            text = c.get("COMMENT") or ""
            if SYSTEM_COMMENT.search(text):
                continue
            ts, who = ms(c.get("CREATED")), c.get("AUTHOR_ID")
            bump(ts, who, 2)
            if CAR_LINK.search(text):
                bump(ts, who, 3)
    for gid, ts, who in task_log:
        bump(ts, who, 4 if gid == TORG_GROUP_ID else 5)

    return {
        "generated": now_ms,
        "portal": bx.portal,
        "stages": [{"id": s, "name": n} for s, n in STAGES],
        "users": users,
        "deals": rows,
        "wins": win_rows,
        "daily": daily,
        "winDays": WIN_DAYS,
        "meta": {"torg": torg_stat, "calc": calc_stat, "api_calls": bx.calls},
    }


# ---------------------------------------------------------------- шифрование
def encrypt(data: bytes, password: str, iterations=250_000):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(password.encode())
    ct = AESGCM(key).encrypt(iv, data, None)  # шифротекст + тег, как ждёт WebCrypto
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "iter": iterations, "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def decrypt(pkg, password):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    b = base64.b64decode
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b(pkg["salt"]),
                     iterations=pkg["iter"]).derive(password.encode())
    return AESGCM(key).decrypt(b(pkg["iv"]), b(pkg["ct"]), None)


# ---------------------------------------------------------------- история (для «к вчера»)
def site_url():
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"].rstrip("/") + "/"
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return None


def load_previous(password):
    """Прошлые данные (снимки, статистика по дням) берём из уже опубликованного data.enc.json — без коммитов."""
    url = site_url()
    if not url or not password:
        return {}
    try:
        req = urllib.request.Request(f"{url}data.enc.json?t={int(time.time())}", headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=30) as r:
            prev = json.loads(decrypt(json.loads(r.read().decode()), password))
        print(f"История: загружено снимков {len(prev.get('hist') or [])}, дней статистики {len(prev.get('daily') or {})}")
        return prev
    except Exception as e:  # первый запуск, смена пароля, сайт недоступен
        print(f"История: начинаем заново ({type(e).__name__})")
        return {}


def merge_daily(prev, cur, now_ms):
    """Сегодня и вчера пересчитываем; более ранние дни берём из прошлых данных —
    тогда в них остаются клиенты, которые с тех пор ушли из депозитов."""
    from zoneinfo import ZoneInfo
    kyiv = ZoneInfo("Europe/Kyiv")
    today = dt.datetime.fromtimestamp(now_ms / 1000, kyiv).date()
    keep_from = (today - dt.timedelta(days=DAILY_DAYS)).isoformat()
    fresh_from = (today - dt.timedelta(days=1)).isoformat()
    out = {}
    for day, v in (prev or {}).items():
        if keep_from <= day < fresh_from:
            out[day] = v
    for day, v in cur.items():
        if day >= fresh_from or day not in out:
            out[day] = v
    return {d: out[d] for d in sorted(out) if d >= keep_from}


def snapshot(data):
    """Компактный снимок: возраст последнего действия в часах (-1 = не было)."""
    g = data["generated"]
    h = lambda t: -1 if t is None else round((g - t) / 3_600_000)
    return {"g": g, "x": [[d["id"], d["m"], d["st"], h(d["call"]["last"]), h(d["call"]["lastTalk"]),
                           h(d["com"]["last"]), h(d["sel"]["last"]), h(d["torg"]["last"]), h(d["calc"]["last"]),
                           d.get("dir", "usa")]
                          for d in data["deals"]]}


def merge_history(hist, snap):
    """Почасовые снимки за последние ~сутки + один снимок в день (первый за день по Киеву)."""
    from zoneinfo import ZoneInfo
    kyiv = ZoneInfo("Europe/Kyiv")
    day = lambda g: dt.datetime.fromtimestamp(g / 1000, kyiv).strftime("%Y-%m-%d")
    all_snaps = sorted([s for s in hist if isinstance(s, dict) and "g" in s] + [snap], key=lambda s: s["g"])
    now = snap["g"]
    keep, seen_days = [], set()
    for s in all_snaps:
        d = day(s["g"])
        recent = now - s["g"] <= HIST_HOURLY_KEEP_H * 3_600_000
        first_of_day = d not in seen_days and now - s["g"] <= HIST_DAILY_KEEP_D * DAY_MS
        seen_days.add(d)
        if recent or first_of_day:
            keep.append(s)
    return keep


def main():
    bx = Bitrix(os.environ.get("BITRIX_WEBHOOK", ""))
    now_ms = int(time.time() * 1000)
    t0 = time.time()
    data = collect(bx, now_ms)
    password = os.environ.get("DASH_PASSWORD")
    prev = load_previous(password)
    data["hist"] = merge_history(prev.get("hist") or [], snapshot(data))
    data["daily"] = merge_daily(prev.get("daily"), data.get("daily") or {}, now_ms)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    os.makedirs(SITE_DIR, exist_ok=True)
    enc_path, plain_path = os.path.join(SITE_DIR, "data.enc.json"), os.path.join(SITE_DIR, "data.json")
    if password:
        with open(enc_path, "w") as f:
            json.dump(encrypt(raw, password), f)
        if os.path.exists(plain_path):
            os.remove(plain_path)
        print(f"Записан {enc_path} (зашифрован)")
    else:
        with open(plain_path, "wb") as f:
            f.write(raw)
        print(f"ВНИМАНИЕ: DASH_PASSWORD не задан, записан открытый {plain_path}")
    print(f"Готово за {time.time() - t0:.0f} с, запросов к API: {bx.calls}")


if __name__ == "__main__":
    main()
