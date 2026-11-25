"""
OBD-сканер на Flask.
Запуск:  python app.py   → откроется браузер http://127.0.0.1:5000
"""
import json
import os
import threading
import webbrowser
from datetime import datetime

from flask import Flask, jsonify, render_template, request, Response

import elm327
import vin as vinlib
from elm327 import AdapterError, Scanner

BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(BASE, "reports")
os.makedirs(REPORTS, exist_ok=True)

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True   # изменения страницы видны без перезапуска
scanner = Scanner()
last_scan = {"result": None, "time": None, "vin": None}
last_buyer = {"result": None, "time": None}
last_live = {"rows": None, "vehicle_id": None}
BRANDING = os.path.join(BASE, "branding.json")
BRAND_DEFAULT = {"brand": "Автоброкер", "site": "автоброкер38.рф", "city": "Иркутск", "phone": "", "inspector": ""}


ODO_FILE = os.path.join(BASE, "odometer_dids.json")


def load_odo_map():
    try:
        with open(ODO_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def odo_dids_for(car):
    """Где искать пробег для этой марки: найденные ранее + стандартные."""
    found = load_odo_map().get((car.get("make") or "").strip().lower(), [])
    return found + elm327.ODO_DEFAULT


def load_brand():
    try:
        with open(BRANDING, encoding="utf-8") as f:
            return {**BRAND_DEFAULT, **json.load(f)}
    except Exception:
        return dict(BRAND_DEFAULT)


VEHICLES = os.path.join(BASE, "vehicles.json")
store_lock = threading.Lock()


def load_store():
    try:
        with open(VEHICLES, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {"selected": None, "vehicles": []}
    st.setdefault("vehicles", [])
    return st


def save_store(st):
    tmp = VEHICLES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, VEHICLES)


def load_vehicle():
    st = load_store()
    return next((v for v in st["vehicles"] if v["id"] == st.get("selected")), st["vehicles"][0] if st["vehicles"] else {})


def vehicle_title(v):
    return " ".join(str(x) for x in (v.get("make"), v.get("model"), v.get("year")) if x)


def match_vin(vin):
    if not vin:
        return None
    return next((v for v in load_store()["vehicles"] if (v.get("vin") or "").upper() == vin.upper()), None)


def _slug(text):
    import re
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(str.maketrans(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя", "abvgdeejziiklmnoprstufhccss_y_eua"))).strip("-")
    return s or "car"


def api(fn):
    def wrapper(*a, **kw):
        try:
            return jsonify({"ok": True, **(fn(*a, **kw) or {})})
        except AdapterError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:  # noqa
            scanner.log("err", f"{type(e).__name__}: {e}")
            return jsonify({"ok": False, "error": f"Ошибка программы: {type(e).__name__}: {e}"}), 500
    wrapper.__name__ = fn.__name__
    return wrapper


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/vehicle")
@api
def vehicle():
    return {"vehicle": load_vehicle()}


@app.get("/api/vehicles")
@api
def vehicles():
    st = load_store()
    return {"vehicles": st["vehicles"], "selected": st.get("selected")}


@app.post("/api/vehicles")
@api
def vehicle_save():
    d = request.get_json(force=True)
    fields = ("make", "model", "year", "vin", "body", "plate", "manufacturer", "engine", "fuel", "voltage", "protocol", "notes")
    car = {k: (str(d.get(k) or "").strip()) for k in fields}
    if not car["make"] or not car["model"]:
        raise AdapterError("Укажите марку и модель")
    car["vin"] = car["vin"].upper().replace(" ", "")
    if car["year"].isdigit():
        car["year"] = int(car["year"])
    with store_lock:
        st = load_store()
        if d.get("id") and any(v["id"] == d["id"] for v in st["vehicles"]):
            car["id"] = d["id"]
            st["vehicles"] = [car if v["id"] == car["id"] else v for v in st["vehicles"]]
        else:
            base = _slug(f"{car['make']}-{car['model']}-{car['year']}")
            cid, n = base, 2
            while any(v["id"] == cid for v in st["vehicles"]):
                cid, n = f"{base}-{n}", n + 1
            car["id"] = cid
            st["vehicles"].append(car)
        st["selected"] = car["id"]
        save_store(st)
    return {"vehicle": car}


@app.post("/api/vehicles/select")
@api
def vehicle_select():
    cid = request.get_json(force=True).get("id")
    with store_lock:
        st = load_store()
        if not any(v["id"] == cid for v in st["vehicles"]):
            raise AdapterError("Такой машины нет в списке")
        st["selected"] = cid
        save_store(st)
    return {"vehicle": load_vehicle()}


@app.post("/api/vehicles/delete")
@api
def vehicle_delete():
    cid = request.get_json(force=True).get("id")
    with store_lock:
        st = load_store()
        st["vehicles"] = [v for v in st["vehicles"] if v["id"] != cid]
        if st.get("selected") == cid:
            st["selected"] = st["vehicles"][0]["id"] if st["vehicles"] else None
        save_store(st)
    return {}


@app.get("/api/vin/<code>")
@api
def vin_decode(code):
    return {"vin": vinlib.decode(code)}


@app.get("/api/ports")
@api
def ports():
    return {"ports": elm327.list_ports()}


@app.get("/api/usb")
@api
def usb():
    import usb_scan
    try:
        res = usb_scan.scan()
    except Exception as e:  # noqa
        raise AdapterError(f"Не удалось получить список USB-устройств: {e}")
    scanner.log("info", "Поиск адаптера: " + res["verdict"])
    return {"usb": res, "ports": elm327.list_ports()}


@app.post("/api/connect")
@api
def connect():
    d = request.get_json(force=True)
    sel = load_vehicle()
    st = scanner.connect(d.get("port"), d.get("mode", "auto"), bool(d.get("demo")), d.get("baud") or None,
                         demo_vin=sel.get("vin"), demo_fuel=sel.get("fuel"))
    vin = None
    try:
        vin = scanner.read_vin()
    except Exception:
        pass
    match = match_vin(vin)
    note = None
    if vin and match and match["id"] != sel.get("id"):
        with store_lock:
            stv = load_store()
            stv["selected"] = match["id"]
            save_store(stv)
        note = f"По VIN это {vehicle_title(match)} — переключил на эту машину."
    elif vin and not match:
        dec = vinlib.decode(vin)
        note = (f"VIN из машины {vin} нет в списке ({dec.get('manufacturer', '')}, {dec.get('year') or '?'} г.). "
                f"Добавьте машину кнопкой «+».")
    elif vin and sel.get("vin") and vin.upper() == sel["vin"].upper():
        note = f"VIN совпал: {vehicle_title(sel)} ✔"
    scanner.log("info", note or "VIN из машины не прочитан")
    return {"status": st, "vin": vin, "vin_note": note, "vehicle": load_vehicle()}


@app.post("/api/disconnect")
@api
def disconnect():
    scanner.disconnect()
    return {}


@app.get("/api/status")
@api
def status():
    return {"status": scanner.status()}


@app.post("/api/scan")
@api
def scan():
    scanner.make = (load_vehicle().get("make") or "")
    res = scanner.read_dtcs()
    last_scan["result"], last_scan["time"] = res, datetime.now()
    try:
        last_scan["vin"] = scanner.read_vin()
    except Exception:
        pass
    car = load_vehicle()
    last_scan["vehicle"] = car
    fname = os.path.join(REPORTS, last_scan["time"].strftime(f"scan_{car.get('id', 'car')}_%Y-%m-%d_%H-%M-%S.json"))
    with open(fname, "w", encoding="utf-8") as f:
        json.dump({"time": last_scan["time"].isoformat(), "vin": last_scan["vin"], "vehicle": car,
                   "protocol": scanner.protocol_name, "demo": scanner.demo, **res}, f, ensure_ascii=False, indent=2)
    return {"scan": res, "saved": os.path.basename(fname)}


@app.post("/api/clear")
@api
def clear():
    return {"clear": scanner.clear_dtcs()}


@app.get("/api/live")
@api
def live():
    rows = scanner.live_data()
    last_live["rows"], last_live["vehicle_id"] = rows, load_vehicle().get("id")
    return {"rows": rows}


@app.get("/api/branding")
@api
def branding_get():
    return {"branding": load_brand()}


@app.post("/api/branding")
@api
def branding_save():
    d = request.get_json(force=True) or {}
    b = {k: str(d.get(k, "")).strip() for k in BRAND_DEFAULT}
    if not b["brand"]:
        b["brand"] = BRAND_DEFAULT["brand"]
    with open(BRANDING, "w", encoding="utf-8") as f:
        json.dump(b, f, ensure_ascii=False, indent=2)
    return {"branding": b}


@app.get("/api/report.pdf")
def report_pdf():
    r = last_scan["result"]
    if not r:
        return Response("Сначала выполните сканирование ошибок.", mimetype="text/plain; charset=utf-8", status=400)
    try:
        import report_pdf as rp
    except ImportError:
        return Response("Для PDF нужна библиотека reportlab. Закройте программу и запустите run.bat — он установит её сам "
                        "(или выполните: pip install reportlab).", mimetype="text/plain; charset=utf-8", status=500)
    v = last_scan.get("vehicle") or load_vehicle()
    b = last_buyer.get("result") if last_buyer.get("vehicle", {}).get("id") == v.get("id") else None
    live_rows = last_live["rows"] if last_live.get("vehicle_id") == v.get("id") else None
    data = rp.build_pdf(r, v, load_brand(), last_scan["time"], vin_ecu=last_scan.get("vin"),
                        protocol=scanner.protocol_name, demo=scanner.demo, buyer=b, live=live_rows)
    name = last_scan["time"].strftime(f"report_{v.get('id', 'car')}_%Y-%m-%d_%H-%M.pdf")
    with open(os.path.join(REPORTS, name), "wb") as f:
        f.write(data)
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={name}"})


@app.get("/api/dpf")
@api
def dpf():
    return {"dpf": scanner.dpf_status()}


@app.post("/api/buyer")
@api
def buyer():
    d = request.get_json(force=True) or {}
    car = load_vehicle()
    res = scanner.buyer_check(full_scan=bool(d.get("full")), expected_vin=car.get("vin") or None,
                              odo_dids=odo_dids_for(car), read_dtcs=bool(d.get("dtcs", True)))
    last_buyer["result"], last_buyer["time"], last_buyer["vehicle"] = res, datetime.now(), car
    fname = os.path.join(REPORTS, last_buyer["time"].strftime(f"check_{car.get('id', 'car')}_%Y-%m-%d_%H-%M-%S.json"))
    with open(fname, "w", encoding="utf-8") as f:
        json.dump({"time": last_buyer["time"].isoformat(), "vehicle": car, "demo": scanner.demo, **res},
                  f, ensure_ascii=False, indent=2)
    return {"buyer": res, "saved": os.path.basename(fname)}


@app.post("/api/odo_search")
@api
def odo_search():
    d = request.get_json(force=True) or {}
    try:
        km_target = float(str(d.get("km", "")).replace(" ", "").replace(",", "."))
    except ValueError:
        raise AdapterError("Введите пробег с приборной панели числом, например 45210")
    if km_target <= 0:
        raise AdapterError("Введите пробег с приборной панели")
    car = load_vehicle()
    b = last_buyer.get("result")
    if not b or last_buyer.get("vehicle", {}).get("id") != car.get("id") or not b.get("ecus"):
        raise AdapterError("Сначала сделайте «Полную проверку» этой машины — программа должна знать, какие блоки есть")
    want = set(x.upper() for x in d.get("ecus") or [])
    ecus = [e for e in b["ecus"] if not want or e["req"] in want]
    if not ecus:
        raise AdapterError("Выберите хотя бы один блок")
    matches = scanner.odo_search(km_target, ecus)
    if km_target < 1000:
        scanner.log("info", "Пробег меньше 1000 км — возможны случайные совпадения, проверяйте внимательно")
    return {"matches": matches[:40], "total": len(matches), "small_km": km_target < 1000}


@app.post("/api/odo_save")
@api
def odo_save():
    m = request.get_json(force=True) or {}
    car = load_vehicle()
    make = (car.get("make") or "").strip().lower()
    if not make:
        raise AdapterError("У машины не указана марка")
    entry = {"did": str(m["did"]).upper(), "offset": int(m["offset"]), "width": int(m["width"]),
             "endian": m["endian"], "scale": int(m["scale"]), "ecu": str(m["ecu"]).upper(),
             "note": f"найдено на {vehicle_title(car)} ({m.get('ecu_name', '')})"}
    data = load_odo_map()
    lst = [x for x in data.get(make, []) if not (x["did"] == entry["did"] and x.get("ecu") == entry["ecu"])]
    data[make] = [entry] + lst
    with open(ODO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    scanner.log("info", f"Запомнил место пробега для {car.get('make')}: блок {entry['ecu']}, DID {entry['did']}")
    return {"saved": entry, "make": car.get("make")}


last_doip = {"result": None, "time": None, "vehicle": None}

# постоянная DoIP-сессия: держим соединение открытым, чтобы живые данные обновлялись быстро
doip_sess = {"cl": None, "car": None}
doip_lock = threading.Lock()


def _doip_ensure(make):
    """Вернуть живой DoIP-клиент: переиспользовать открытый или найти+подключить заново.
       Возвращает (cl, car) или (None, errdict)."""
    import doip
    cl, car = doip_sess.get("cl"), doip_sess.get("car")
    if cl is not None and car is not None:
        try:
            cl.uds(car["logical_address"], b"\x3E\x00", timeout=1.0)   # TesterPresent — жив ли сокет
            return cl, car
        except Exception:
            try:
                cl.close()
            except Exception:
                pass
            doip_sess["cl"] = doip_sess["car"] = None
    cl, car = doip._find_and_connect(3.0, scanner.log, make)
    if cl is None:
        return None, car
    doip_sess["cl"], doip_sess["car"] = cl, car
    return cl, car


def _doip_close():
    cl = doip_sess.get("cl")
    if cl is not None:
        try:
            cl.close()
        except Exception:
            pass
    doip_sess["cl"] = doip_sess["car"] = None


@app.get("/api/network")
@api
def network_get():
    import doip
    return {"network": doip.network_info()}


@app.post("/api/doip")
@api
def doip_scan():
    """Найти машину по DoIP и прочитать ошибки — соединение остаётся открытым (постоянная сессия)."""
    import doip
    d = request.get_json(force=True) or {}
    deep = bool(d.get("deep"))
    with doip_lock:
        cl, car = _doip_ensure(load_vehicle().get("make"))
        if cl is None:
            return {"doip": car}
        r = doip.read_dtcs_on(cl, car, deep, scanner.log)
        res = {"ok": r.get("ok", True), "car": car, "ecus": r.get("ecus", []),
               "block_dtcs": r.get("block_dtcs", []), "vin": r.get("vin"),
               "part_number": r.get("part_number"), "serial": r.get("serial"),
               "ips": doip.local_ipv4s(), "network": doip.network_info(), "arp": doip.arp_neighbors()}
        if r.get("error"):
            res["error"] = r["error"]
    last_doip["result"], last_doip["time"], last_doip["vehicle"] = res, datetime.now(), load_vehicle()
    if res.get("ok"):
        fname = os.path.join(REPORTS, last_doip["time"].strftime("doip_%Y-%m-%d_%H-%M-%S.json"))
        with open(fname, "w", encoding="utf-8") as f:
            json.dump({"time": last_doip["time"].isoformat(), **res}, f, ensure_ascii=False, indent=2)
    return {"doip": res}


@app.post("/api/doip_disconnect")
@api
def doip_disconnect():
    with doip_lock:
        _doip_close()
    return {"ok": True}


@app.route("/api/doip_live", methods=["GET", "POST"])
@api
def doip_live():
    import doip
    with doip_lock:
        cl, car = _doip_ensure(load_vehicle().get("make"))
        if cl is None:
            return {"live": {"ok": False, "rows": [], "dids": [],
                             "error": (car or {}).get("error", "Нет связи по DoIP. Нажмите «Найти машину по DoIP».")},
                    "rows": []}
        r = doip.live_on(cl, car, scanner.log)
    note = "" if r["rows"] else ("Шлюз не отдаёт живые OBD-данные по DoIP напрямую. Для датчиков двигателя нужен "
                                 "адрес блока двигателя за шлюзом — сделайте «Глубокий перебор блоков».")
    last_live["rows"], last_live["vehicle_id"] = r["rows"], load_vehicle().get("id")
    return {"live": {"ok": bool(r["rows"] or r["dids"]), **r, "note": note, "car": car}, "rows": r["rows"]}


@app.post("/api/doip_buyer")
@api
def doip_buyer():
    import doip
    d = request.get_json(force=True) or {}
    deep = bool(d.get("full"))
    car0 = load_vehicle()
    with doip_lock:
        cl, car = _doip_ensure(car0.get("make"))
        if cl is None:
            raise AdapterError((car or {}).get("error", "Нет связи по DoIP. Сначала нажмите «Найти машину по DoIP»."))
        car = {**car, "vin": car.get("vin") or car0.get("vin")}
        res = doip.buyer_on(cl, car, car0.get("make"), deep, scanner.log)
    last_buyer["result"], last_buyer["time"], last_buyer["vehicle"] = res, datetime.now(), car0
    fname = os.path.join(REPORTS, last_buyer["time"].strftime(f"check_{car0.get('id', 'car')}_%Y-%m-%d_%H-%M-%S.json"))
    with open(fname, "w", encoding="utf-8") as f:
        json.dump({"time": last_buyer["time"].isoformat(), "vehicle": car0, "doip": True, **res}, f, ensure_ascii=False, indent=2)
    return {"buyer": res, "saved": os.path.basename(fname)}


@app.post("/api/doip_setip")
@api
def doip_setip():
    import doip
    d = request.get_json(force=True) or {}
    ip = str(d.get("ip") or "192.168.1.2").strip()
    if d.get("dhcp"):
        return {"result": doip.set_dhcp(log=scanner.log)}
    r = doip.set_static_ip(ip=ip, log=scanner.log)
    scan = None
    if r.get("ok"):
        import time as _t
        _t.sleep(1.5)
        scan = doip.scan_manual(deep=False, log=scanner.log, make=load_vehicle().get("make"))
        if scan.get("ok"):
            last_doip["result"], last_doip["time"], last_doip["vehicle"] = scan, datetime.now(), load_vehicle()
    return {"setip": r, "doip": scan}


@app.post("/api/doip_manual")
@api
def doip_manual():
    import doip
    d = request.get_json(force=True) or {}
    res = doip.scan_manual(deep=bool(d.get("deep")), log=scanner.log, make=load_vehicle().get("make"))
    if res.get("ok"):
        last_doip["result"], last_doip["time"], last_doip["vehicle"] = res, datetime.now(), load_vehicle()
    return {"doip": res}


@app.post("/api/doip_clear")
@api
def doip_clear():
    import doip
    d = request.get_json(force=True) or {}
    targets = d.get("targets")
    if not targets:
        r = last_doip.get("result") or {}
        if r.get("ecus"):
            targets = [e["addr"] for e in r["ecus"]]
    with doip_lock:
        cl, car = _doip_ensure(load_vehicle().get("make"))
        if cl is None:
            return {"clear": {"ok": False, "error": (car or {}).get("error", "Нет связи по DoIP")}}
        res = {**doip.clear_on(cl, car, targets, scanner.log), "car": car}
    return {"clear": res}


@app.get("/api/log")
@api
def log():
    return {"log": list(scanner.logbuf)}


@app.post("/api/raw")
@api
def raw():
    cmd = (request.get_json(force=True).get("cmd") or "").strip()
    if not cmd:
        raise AdapterError("Пустая команда")
    return {"lines": scanner.raw(cmd)}


@app.get("/api/report")
def report():
    r = last_scan["result"]
    if not r:
        return Response("Сначала выполните сканирование.", mimetype="text/plain; charset=utf-8")
    v = last_scan.get("vehicle") or load_vehicle()
    out = ["ОТЧЁТ О ДИАГНОСТИКЕ", "=" * 60,
           f"Дата:      {last_scan['time']:%d.%m.%Y %H:%M}",
           f"Автомобиль: {vehicle_title(v)}" + (f"  ({v['engine']})" if v.get("engine") else ""),
           f"VIN (из ЭБУ): {last_scan['vin'] or 'не прочитан'}   VIN (документы): {v.get('vin') or '—'}"
           + (f"   Кузов: {v['body']}" if v.get("body") else "") + (f"   Гос. номер: {v['plate']}" if v.get("plate") else ""),
           f"Протокол:  {scanner.protocol_name or '—'}" + ("   [ДЕМО-РЕЖИМ]" if scanner.demo else ""), ""]
    if r.get("lamps"):
        out.append("Лампы: " + ", ".join(f"{k} — {'ГОРИТ' if on else 'не горит'}" for k, on in r["lamps"].items()))
        out.append("")
    if not r["codes"]:
        out.append("Ошибок не найдено.")
    for i, c in enumerate(r["codes"], 1):
        out.append(f"{i}. {c['code']}  [{c['type']}]  блок: {c['ecu']}")
        out.append(f"   {c['description']}")
        out.append(f"   Система: {c['system']}   Важность: {dict(high='высокая', medium='средняя', low='низкая')[c['severity']]}")
        if c.get("hint"):
            out.append(f"   Совет: {c['hint']}")
        out.append("")
    b = last_buyer.get("result")
    if b and last_buyer.get("vehicle", {}).get("id") == v.get("id"):
        out += ["", "ПРОВЕРКА ПРИ ПОКУПКЕ (" + f"{last_buyer['time']:%d.%m.%Y %H:%M}" + ")", "-" * 60]
        for w in b["warnings"]:
            out.append("⚠ " + w)
        for n in b["notes"]:
            out.append("• " + n)
        for o in b["odometers"]:
            out.append(f"Пробег: {o['km']:,} км".replace(",", " ") + f" — {o['source']}")
        r = b["readiness"]
        for k, label in (("km_since_clear", "Пробег после сброса ошибок, км"), ("warmups_since_clear", "Прогревов после сброса"),
                         ("min_since_clear", "Минут работы после сброса"), ("obd_std", "Стандарт OBD")):
            if r.get(k) is not None:
                out.append(f"{label}: {r[k]}")
        if r["monitors"]:
            out.append("Готовность систем: " + "; ".join(f"{m['name']} — {'готов' if m['ready'] else 'НЕ готов'}" for m in r["monitors"]))
        for e in b["ecus"]:
            out.append(f"Блок {e['name']}: VIN {e.get('vin', '—')}, номер {e.get('part_number', '—')}, ПО {e.get('software', '—')}")
    body = "\r\n".join(out)
    name = last_scan["time"].strftime(f"report_{v.get('id', 'car')}_%Y-%m-%d_%H-%M.txt")
    return Response(body.encode("utf-8-sig"), mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


def migrate_old_vehicle():
    """Старый vehicle.json (одна машина) → vehicles.json (список)."""
    if os.path.exists(VEHICLES):
        return
    old = os.path.join(BASE, "vehicle.json")
    cars = []
    if os.path.exists(old):
        with open(old, encoding="utf-8") as f:
            o = json.load(f)
        cars.append({"id": "car-1", "make": o.get("make", ""), "model": f"{o.get('model', '')} {o.get('series', '')}".strip(),
                     "year": o.get("year", ""), "vin": o.get("vin", ""), "manufacturer": o.get("manufacturer", ""),
                     "engine": o.get("engine", ""), "fuel": "", "voltage": "", "protocol": "auto", "notes": ""})
    save_store({"selected": cars[0]["id"] if cars else None, "vehicles": cars})


def main():
    migrate_old_vehicle()
    import socket
    port = 5000
    url = f"http://127.0.0.1:{port}"
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            print("!" * 60)
            print("  Программа УЖЕ запущена в другом окне (порт 5000 занят).")
            print("  Закройте старое чёрное окно или завершите python.exe")
            print("  в Диспетчере задач (Ctrl+Shift+Esc) и запустите снова.")
            print("!" * 60)
            webbrowser.open(url)
            return
    print("=" * 60)
    print("  OBD-сканер запущен. Откройте в браузере:", url)
    print("  Остановить: Ctrl+C")
    print("=" * 60)
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # не засорять окно строками GET /api/log
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # use_reloader=False — иначе COM-порт откроется дважды
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
