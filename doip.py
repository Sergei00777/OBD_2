"""
DoIP (Diagnostics over IP, ISO 13400) — чтение по Ethernet-кабелю (OBD→RJ45→USB, чип ASIX).
Для машин, где блоки закрыты шлюзом и недоступны через обычный ELM327: Li (Lixiang), Zeekr, новые EV.

Только чтение: опознание машины, активация маршрута, чтение VIN и ошибок (UDS сервис 19/22).
Защиту доступа (seed-key) не обходим — если блок требует авторизацию, просто фиксируем отказ.

Чистые сокеты, без сторонних библиотек.
"""
import socket
import struct
import time

import dtc_db
from elm327 import decode_uds_dtc

DOIP_PORT = 13400
BROADCAST_DSTS = ("255.255.255.255", "192.168.1.255", "169.254.255.255")
PV = 0x02                 # ISO 13400-2:2012
TESTER = 0x0E80           # логический адрес тестера (стандартный диапазон)

# типы сообщений DoIP
T_VEHICLE_ID_REQ = 0x0001
T_VEHICLE_ANNOUNCE = 0x0004
T_ROUTING_ACT_REQ = 0x0005
T_ROUTING_ACT_RESP = 0x0006
T_ALIVE_REQ = 0x0007
T_ALIVE_RESP = 0x0008
T_DIAG = 0x8001
T_DIAG_ACK = 0x8002
T_DIAG_NACK = 0x8003

ROUTING_CODES = {
    0x00: "отказано: неизвестный адрес источника", 0x01: "отказано: все сокеты заняты",
    0x02: "отказано: адрес источника уже активен на другом сокете", 0x03: "отказано: адрес источника уже зарегистрирован",
    0x04: "отказано: нужна аутентификация", 0x05: "отказано: нужно подтверждение (vehicle confirmation)",
    0x06: "отказано: неподдерживаемый тип активации", 0x10: "успешно", 0x11: "успешно, нужна подтверждающая процедура",
}

# UDS Negative Response Codes — частые
NRC = {
    0x10: "общий отказ", 0x11: "сервис не поддерживается", 0x12: "подфункция не поддерживается",
    0x13: "неверная длина запроса", 0x22: "условия не выполнены (нужно зажигание/двигатель/состояние)",
    0x31: "запрос вне диапазона", 0x33: "нужен доступ по безопасности (заводская авторизация)",
    0x35: "неверный ключ", 0x7E: "подфункция недоступна в этой сессии", 0x7F: "сервис недоступен в этой сессии",
    0x78: "блок занят, отвечает с задержкой",
}


class DoIPError(Exception):
    pass


def _hdr(ptype, payload=b""):
    return struct.pack(">BBHI", PV, 0xFF ^ PV, ptype, len(payload)) + payload


def local_ipv4s():
    """Все локальные IPv4-адреса (без петлевого)."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


def _adapter_name(keyword="ASIX"):
    """Имя подключения адаптера (например 'Ethernet 4') по ключевому слову описания."""
    import subprocess
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"Get-NetAdapter | Where-Object {{$_.InterfaceDescription -like '*{keyword}*'}} | "
                              f"Select-Object -First 1 -ExpandProperty Name"],
                             capture_output=True, timeout=15, creationflags=0x08000000)
        name = out.stdout.decode("utf-8", "ignore").strip() or out.stdout.decode("cp866", "ignore").strip()
        return name or None
    except Exception:
        return None


def set_static_ip(ip="192.168.1.2", mask="255.255.255.0", keyword="ASIX", log=None):
    """Прописать статический IP адаптеру ASIX (как делает Launch). При нехватке прав — запрос UAC."""
    import subprocess
    import sys
    log = log or (lambda *a: None)
    if sys.platform != "win32":
        return {"ok": False, "error": "только Windows"}
    name = _adapter_name(keyword)
    if not name:
        return {"ok": False, "error": "адаптер ASIX не найден. Подключите кабель в USB и повторите."}
    log("info", f"Адаптер: «{name}». Прописываю {ip} / {mask} ...")
    cmd = ["netsh", "interface", "ip", "set", "address", f"name={name}", "static", ip, mask]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=25, creationflags=0x08000000)
        err = (out.stdout + out.stderr).decode("cp866", "ignore")
        if out.returncode == 0 and "треб" not in err.lower() and "requested" not in err.lower() and "отказ" not in err.lower():
            log("info", "IP задан без прав администратора")
            return {"ok": True, "name": name, "ip": ip}
    except Exception as e:  # noqa
        err = str(e)
    # не вышло без прав — поднимаем через UAC (пользователь нажмёт «Да»)
    log("info", "Нужны права администратора — появится окно Windows, нажмите «Да»")
    arg = f"interface ip set address name=\"{name}\" static {ip} {mask}"
    ps = f"Start-Process netsh -ArgumentList '{arg}' -Verb RunAs -WindowStyle Hidden -Wait"
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, timeout=60, creationflags=0x08000000)
        if out.returncode == 0:
            return {"ok": True, "name": name, "ip": ip, "elevated": True}
        return {"ok": False, "name": name, "error": "не удалось задать IP (возможно, отклонён запрос прав). "
                                                    + (out.stderr.decode('cp866', 'ignore')[:150])}
    except Exception as e:  # noqa
        return {"ok": False, "name": name, "error": f"ошибка: {e}"}


def set_dhcp(keyword="ASIX", log=None):
    """Вернуть адаптер на автоматическое получение IP (DHCP)."""
    import subprocess
    import sys
    log = log or (lambda *a: None)
    if sys.platform != "win32":
        return {"ok": False}
    name = _adapter_name(keyword)
    if not name:
        return {"ok": False, "error": "адаптер ASIX не найден"}
    arg = f"interface ip set address name=\"{name}\" dhcp"
    ps = f"Start-Process netsh -ArgumentList '{arg}' -Verb RunAs -WindowStyle Hidden -Wait"
    try:
        subprocess.run(["netsh", "interface", "ip", "set", "address", f"name={name}", "dhcp"],
                       capture_output=True, timeout=20, creationflags=0x08000000)
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=60,
                       creationflags=0x08000000)
        return {"ok": True, "name": name}
    except Exception as e:  # noqa
        return {"ok": False, "error": str(e)}


def arp_neighbors():
    """Соседи из ARP-таблицы Windows (кто реально отвечает на кабеле). Может показать IP машины."""
    import subprocess
    import re
    import sys
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, timeout=10, creationflags=0x08000000)
    except Exception:
        return []
    text = None
    for enc in ("cp866", "cp1251", "utf-8"):
        try:
            text = out.stdout.decode(enc); break
        except Exception:
            continue
    text = text or out.stdout.decode("ascii", "ignore")
    nb = []
    for line in text.splitlines():
        m = re.search(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+([0-9a-fA-F]{2}[-:][0-9a-fA-F]{2}[-:][0-9a-fA-F]{2}[-:][0-9a-fA-F]{2}[-:][0-9a-fA-F]{2}[-:][0-9a-fA-F]{2})\s+(\S+)", line)
        if m:
            nb.append({"ip": m.group(1), "mac": m.group(2).lower(), "type": m.group(3)})
    return nb


def plausible_car_ips(self_ips):
    """IP-кандидаты машины: соседи на link-local (169.254.x), кроме себя, бродкастов и мультикастов."""
    out = []
    for nb in arp_neighbors():
        ip, mac = nb["ip"], nb["mac"]
        if ip in self_ips or ip.endswith(".255") or ip.endswith(".0"):
            continue
        if mac.startswith(("ff-", "01-00-5e", "33-33")):
            continue
        octets = ip.split(".")
        if 224 <= int(octets[0]) <= 239:
            continue
        # только подсеть кабеля (link-local 169.254.x) — Wi-Fi/роутер не трогаем
        if not ip.startswith("169.254"):
            continue
        out.append(ip)
    return out


def network_info():
    """Разобрать ipconfig /all: сетевые адаптеры, их IPv4, статус. Чтобы увидеть адрес адаптера ASIX."""
    import subprocess
    import re
    import sys
    if sys.platform != "win32":
        return {"adapters": [], "note": "проверка сети только в Windows"}
    try:
        out = subprocess.run(["ipconfig", "/all"], capture_output=True, timeout=15,
                             creationflags=0x08000000)
    except Exception as e:  # noqa
        return {"adapters": [], "note": f"не удалось запустить ipconfig: {e}"}
    text = None
    for enc in ("cp866", "cp1251", "utf-8"):
        try:
            text = out.stdout.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        text = out.stdout.decode("ascii", "ignore")
    adapters, cur = [], None
    ip_re = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")
    for line in text.splitlines():
        if line and not line.startswith(" ") and line.rstrip().endswith(":"):
            cur = {"title": line.strip().rstrip(":"), "desc": "", "ipv4": [], "apipa": False,
                   "media_off": False, "dhcp": "", "dhcp_server": ""}
            adapters.append(cur)
            continue
        if cur is None:
            continue
        low = line.lower()
        if "description" in low or "описание" in low:
            cur["desc"] = line.split(":", 1)[-1].strip()
        elif "media" in low or "среды" in low or "носител" in low:
            if "disconnect" in low or "недоступ" in low or "отключ" in low:
                cur["media_off"] = True
        elif ("dhcp" in low and ("enabled" in low or "включ" in low)):
            cur["dhcp"] = "да" if ("yes" in low or "да" in low) else "нет"
        elif "dhcp" in low and ("server" in low or "сервер" in low):
            m = ip_re.search(line)
            if m:
                cur["dhcp_server"] = m.group(1)
        elif ("ipv4" in low or "ip-address" in low or "ipv4-адрес" in low or "ip-адрес" in low):
            m = ip_re.search(line)
            if m:
                ip = m.group(1)
                cur["ipv4"].append(ip)
                if ip.startswith("169.254"):
                    cur["apipa"] = True
    # оставляем только адаптеры с описанием/ip
    adapters = [a for a in adapters if a["desc"] or a["ipv4"]]
    asix = next((a for a in adapters if "asix" in a["desc"].lower() or "ax887" in a["desc"].lower()
                 or "ethernet" in a["desc"].lower() and a["ipv4"]), None)
    return {"adapters": adapters, "asix": asix}


def _bcast_for(src):
    if src.startswith("169.254"):
        return ["169.254.255.255", "255.255.255.255"]
    if src.count(".") == 3:
        return [src.rsplit(".", 1)[0] + ".255", "255.255.255.255"]
    return list(BROADCAST_DSTS)


def discover(timeout=3.0, log=None, src_ips=None):
    """Разослать DoIP-опознание ЧЕРЕЗ КАЖДЫЙ сетевой адаптер (важно: иначе запрос уходит в Wi-Fi,
    а не в кабель ASIX) и собрать ответы машины."""
    log = log or (lambda *a: None)
    found = {}
    srcs = list(dict.fromkeys((src_ips or []) + [""]))   # перечисленные адаптеры + «любой»
    socks = []
    for src in srcs:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        ok = False
        for port in (DOIP_PORT, 0):
            try:
                s.bind((src, port))
                ok = True
                break
            except OSError:
                continue
        if not ok:
            s.close()
            continue
        s.settimeout(0.25)
        socks.append((s, src))
    for s, src in socks:
        dsts = _bcast_for(src)
        log("tx", f"DoIP-запрос через {src or 'все адаптеры'} → {', '.join(dsts)}")
        for _ in range(3):
            for dst in dsts:
                try:
                    s.sendto(_hdr(T_VEHICLE_ID_REQ), (dst, DOIP_PORT))
                except OSError:
                    pass
            time.sleep(0.1)
    end = time.time() + timeout
    while time.time() < end:
        for s, src in socks:
            try:
                data, addr = s.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            if len(data) < 8:
                continue
            _, _, ptype, plen = struct.unpack(">BBHI", data[:8])
            payload = data[8:8 + plen]
            if ptype in (T_VEHICLE_ANNOUNCE,) and len(payload) >= 19:
                vin = "".join(chr(c) for c in payload[0:17] if 32 <= c < 127).strip()
                la = struct.unpack(">H", payload[17:19])[0]
                eid = payload[19:25].hex().upper() if len(payload) >= 25 else ""
                found[addr[0]] = {"ip": addr[0], "logical_address": la, "vin": vin, "eid": eid, "via": src}
                log("rx", f"Машина: IP {addr[0]}, адрес шлюза 0x{la:04X}, VIN {vin or '—'} (через {src or '?'})")
    for s, _ in socks:
        s.close()
    return list(found.values())


class DoIPClient:
    def __init__(self, ip, entity_addr, log=None, make=None):
        self.ip = ip
        self.entity = entity_addr
        self.log = log or (lambda *a: None)
        self.sock = None
        self.make = (make or "").strip()

    def connect(self, act_type=0x00):
        self.log("info", f"TCP {self.ip}:{DOIP_PORT} ...")
        self.sock = socket.create_connection((self.ip, DOIP_PORT), timeout=6)
        self.sock.settimeout(6)
        payload = struct.pack(">HB", TESTER, act_type) + b"\x00\x00\x00\x00"
        self.log("tx", f"Routing Activation (тестер 0x{TESTER:04X}, тип 0x{act_type:02X})")
        self.sock.sendall(_hdr(T_ROUTING_ACT_REQ, payload))
        ptype, pl = self._recv()
        if ptype != T_ROUTING_ACT_RESP or len(pl) < 5:
            raise DoIPError("нет корректного ответа на активацию маршрута")
        try:
            self.entity = struct.unpack(">H", pl[2:4])[0]   # реальный логический адрес шлюза из ответа
        except Exception:
            pass
        code = pl[4]
        txt = ROUTING_CODES.get(code, f"код 0x{code:02X}")
        self.log("rx", f"Активация маршрута: {txt} (шлюз 0x{self.entity:04X})")
        if code not in (0x10, 0x11):
            raise DoIPError(f"активация отклонена — {txt}")
        return True

    def _recvn(self, n):
        buf = b""
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                raise DoIPError("соединение закрыто машиной")
            buf += c
        return buf

    def _recv(self):
        hdr = self._recvn(8)
        _, _, ptype, plen = struct.unpack(">BBHI", hdr)
        return ptype, (self._recvn(plen) if plen else b"")

    def uds(self, target, data, timeout=2.0):
        """UDS-запрос к логическому адресу блока. Возвращает байты ответа или None."""
        payload = struct.pack(">HH", TESTER, target) + data
        self.sock.sendall(_hdr(T_DIAG, payload))
        self.sock.settimeout(timeout)
        end = time.time() + timeout + 1
        while time.time() < end:
            try:
                ptype, pl = self._recv()
            except (socket.timeout, DoIPError):
                return None
            if ptype == T_DIAG_ACK:
                continue                       # подтверждение приёма — ждём сам ответ
            if ptype == T_DIAG_NACK:
                return None                    # блок недоступен по этому адресу
            if ptype == T_DIAG and len(pl) >= 4:
                return pl[4:]
            if ptype in (T_ALIVE_REQ,):
                self.sock.sendall(_hdr(T_ALIVE_RESP, struct.pack(">H", TESTER)))
        return None

    def read_did(self, target, did):
        r = self.uds(target, bytes([0x22, did >> 8, did & 0xFF]))
        if r and len(r) >= 3 and r[0] == 0x62:
            return bytes(r[3:])
        return None

    def read_dtcs(self, target):
        for req in (bytes([0x19, 0x02, 0xFF]), bytes([0x10, 0x03]), bytes([0x19, 0x02, 0x08])):
            r = self.uds(target, req)
            if req[0] == 0x10:
                continue
            if r and len(r) >= 3 and r[0] == 0x59 and r[1] == 0x02:
                data, out = bytes(r[3:]), []
                for i in range(0, len(data) - 3, 4):
                    b0, b1, b2, st = data[i], data[i + 1], data[i + 2], data[i + 3]
                    if (b0, b1, b2) in ((0, 0, 0), (0xFF, 0xFF, 0xFF)):
                        continue
                    d = decode_uds_dtc(b0, b1, b2, st)
                    info = dtc_db.describe_obd(d["code"], self.make)
                    d.update(description=info["description"], system=info["system"],
                             severity="high" if d["active"] else info["severity"], hint=info.get("hint", ""))
                    out.append(d)
                return out
        return None

    def clear_dtcs(self, target, group=0xFFFFFF):
        """Стереть ошибки блока (UDS 0x14). Сначала расширенная сессия (не критично, если откажут)."""
        self.uds(target, b"\x10\x03", timeout=1.5)       # extended session — часто нужна для стирания
        self.uds(target, b"\x3E\x00", timeout=1.0)       # tester present
        r = self.uds(target, bytes([0x14, (group >> 16) & 0xFF, (group >> 8) & 0xFF, group & 0xFF]), timeout=5.0)
        if r is None:
            return {"ok": False, "reason": "нет ответа от блока"}
        if r and r[0] == 0x54:
            return {"ok": True, "reason": "очищено"}
        if len(r) >= 3 and r[0] == 0x7F:
            return {"ok": False, "nrc": r[2], "reason": NRC.get(r[2], f"код 0x{r[2]:02X}")}
        return {"ok": False, "reason": f"ответ 0x{r[0]:02X}"}

    def obd_pid(self, target, pid):
        """OBD-II режим 01 через DoIP: запрос 01 <PID>, ответ 41 <PID> <данные>."""
        r = self.uds(target, bytes([0x01, pid]), timeout=1.5)
        if r and len(r) >= 2 and r[0] == 0x41 and r[1] == pid:
            return bytes(r[2:])
        return None

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# адреса блоков для перебора по DoIP (пока общие; уточним по журналу реальной Li)
DOIP_TARGETS = ([0x1001, 0x1010, 0x1020, 0x1030, 0x1040, 0x1050, 0x1060, 0x1070, 0x1080, 0x1090,
                 0x10A0, 0x10B0, 0x10C0, 0x10D0, 0x10E0, 0x10F0] +
                list(range(0x0001, 0x0040)) + [0x0201, 0x0203, 0x0205, 0x0207, 0x0210, 0x0220])


def _find_and_connect(timeout, log, make=None):
    """Найти машину (рассылка → ARP) и активировать маршрут. Возвращает (cl, car) или (None, errdict)."""
    net0 = network_info()
    src_ips = local_ipv4s()
    asix = net0.get("asix")
    if asix and asix.get("ipv4"):
        src_ips = asix["ipv4"] + [x for x in src_ips if x not in asix["ipv4"]]  # кабель ASIX — первым
    cars = discover(timeout=timeout, log=log, src_ips=src_ips)
    cl = None
    car = None
    if cars:
        car = cars[0]
        cl = DoIPClient(car["ip"], car["logical_address"], log=log, make=make)
        try:
            cl.connect()
        except DoIPError as e:
            log("err", f"Подключение к {car['ip']} не вышло: {e}")
            cl = None
    if cl is None:
        # Машина не объявилась — пробуем прямое подключение к соседям из ARP-таблицы
        cand = plausible_car_ips(set(src_ips))
        log("info", f"Поиск по ARP: кандидатов {len(cand)} — {', '.join(cand) if cand else 'нет'}")
        for ip in cand:
            try:
                c = DoIPClient(ip, 0, log=log, make=make)
                c.connect()
                cl, car = c, {"ip": ip, "logical_address": c.entity, "vin": "", "via": "arp"}
                break
            except Exception as e:  # noqa
                log("rx", f"  {ip}: не DoIP ({e})")
    if cl is None:
        net = network_info()
        return None, {"ok": False, "ips": local_ipv4s(), "network": net, "arp": arp_neighbors(),
                      "error": "Машина не ответила по DoIP ни на рассылку, ни напрямую. В ARP-таблице ниже видно, "
                               "кто есть на кабеле. Частые причины: нужен статический IP на адаптере ASIX в подсети "
                               "машины, или шлюз не пускает без заводской авторизации. Пришлите журнал и этот экран."}
    return cl, car


def scan(timeout=3.0, deep=False, log=None, make=None):
    """Полный цикл DoIP: найти машину → подключиться → прочитать VIN и ошибки по блокам."""
    log = log or (lambda *a: None)
    cl, car = _find_and_connect(timeout, log, make)
    if cl is None:
        return car  # словарь с ошибкой
    return _read_car(cl, car, deep, log)


def _to_int_addr(a):
    if isinstance(a, str):
        a = a.strip()
        return int(a, 16) if a.lower().startswith("0x") else int(a)
    return int(a)


def _txt(b):
    return "".join(chr(c) for c in (b or b"") if 32 <= c < 127).strip()


# PID'ы для живых данных по DoIP (порядок показа)
DOIP_LIVE_PIDS = [0x0C, 0x0D, 0x05, 0x5C, 0x04, 0x0B, 0x0F, 0x10, 0x11, 0x49, 0x2F, 0x5E, 0x42, 0x46, 0x0E, 0x33]


# ============ операции на УЖЕ открытом соединении (для постоянной сессии) ============

def read_dtcs_on(cl, car, deep, log):
    """Прочитать VIN и ошибки по блокам на открытом соединении (без закрытия сокета)."""
    result = {"ok": True, "ecus": [], "block_dtcs": []}
    try:
        for did, key in ((0xF190, "vin"), (0xF187, "part_number"), (0xF18C, "serial")):
            v = cl.read_did(car["logical_address"], did)
            t = _txt(v)
            if t:
                result[key] = t
        targets = DOIP_TARGETS if deep else DOIP_TARGETS[:16]
        tested = set()
        for target in [car["logical_address"]] + targets:
            if target in tested:
                continue
            tested.add(target)
            dtcs = cl.read_dtcs(target)
            if dtcs is None:
                continue
            name = f"Блок 0x{target:04X}"
            vtxt = _txt(cl.read_did(target, 0xF190))
            result["ecus"].append({"addr": f"0x{target:04X}", "name": name, "vin": vtxt, "dtc_count": len(dtcs)})
            for d in dtcs:
                result["block_dtcs"].append({**d, "ecu": name})
            log("info", f"Блок 0x{target:04X}: ошибок {len(dtcs)}")
    except DoIPError as e:
        result["ok"] = False
        result["error"] = str(e)
    return result


def live_on(cl, car, log=None):
    """Живые показания (OBD-II PID режим 01 через шлюз) + известные DID — на открытом соединении."""
    log = log or (lambda *a: None)
    from elm327 import OBD_PIDS
    pidmap = {p[0]: p for p in OBD_PIDS}
    gw = car["logical_address"]
    rows = []
    for pid in DOIP_LIVE_PIDS:
        data = cl.obd_pid(gw, pid)
        if not data:
            continue
        spec = pidmap.get(pid)
        if not spec:
            continue
        try:
            rows.append({"name": spec[1], "value": spec[3](data), "unit": spec[2]})
        except Exception:
            continue
    dids = []
    for did, label in ((0xF190, "VIN"), (0xF187, "Номер детали"), (0xF18C, "Серийный"), (0xF195, "Версия ПО")):
        t = _txt(cl.read_did(gw, did))
        if t:
            dids.append({"label": label, "value": t})
    return {"rows": rows, "dids": dids}


def readiness_on(cl, car):
    """Готовность мониторов самодиагностики (OBD PID 01) + счётчики после сброса — по DoIP."""
    from elm327 import MONITORS_CONT, MONITORS_SPARK, MONITORS_DIESEL, OBD_STANDARDS
    gw = car["logical_address"]
    out = {"monitors": [], "diesel": None, "mil": None, "dtc_count": None}
    d = cl.obd_pid(gw, 0x01)
    if d and len(d) >= 4:
        a, b, c, dd = d[0], d[1], d[2], d[3]
        out["mil"], out["dtc_count"] = bool(a & 0x80), a & 0x7F
        diesel = bool(b & 0x08)
        out["diesel"] = diesel
        for bit, name in MONITORS_CONT:
            if b & (1 << bit):
                out["monitors"].append({"name": name, "ready": not (b & (1 << (bit + 4)))})
        for bit, name in (MONITORS_DIESEL if diesel else MONITORS_SPARK):
            if c & (1 << bit):
                out["monitors"].append({"name": name, "ready": not (dd & (1 << bit))})
    for pid, key, fn in ((0x31, "km_since_clear", lambda x: x[0] * 256 + x[1]),
                         (0x30, "warmups_since_clear", lambda x: x[0]),
                         (0x4E, "min_since_clear", lambda x: x[0] * 256 + x[1]),
                         (0x21, "km_with_mil", lambda x: x[0] * 256 + x[1]),
                         (0x4D, "min_with_mil", lambda x: x[0] * 256 + x[1]),
                         (0x1C, "obd_std", lambda x: OBD_STANDARDS.get(x[0], f"код {x[0]}"))):
        x = cl.obd_pid(gw, pid)
        if x:
            try:
                out[key] = fn(x)
            except IndexError:
                pass
    return out


def buyer_on(cl, car, make, deep, log):
    """Проверка при покупке по DoIP — тот же формат, что и OBD-II."""
    scan = read_dtcs_on(cl, car, deep, log)
    res = {"readiness": readiness_on(cl, car), "odometers": [], "ecus": [], "warnings": [], "notes": [],
           "block_dtcs": scan.get("block_dtcs", [])}
    gw = car["logical_address"]
    odo = cl.obd_pid(gw, 0xA6)
    if odo and len(odo) >= 4:
        res["odometers"].append({"source": "OBD-II стандарт (PID A6), шлюз", "km": round(int.from_bytes(odo[:4], "big") / 10)})
    for e in scan.get("ecus", []):
        res["ecus"].append({"name": e["name"], "vin": e.get("vin", ""), "dtc_count": e.get("dtc_count"),
                            "part_number": "", "software": "", "serial": "", "req": e["addr"]})
    groups = {}
    for o in res["odometers"]:
        groups.setdefault(o["km"], []).append(o["source"])
    res["odometer_groups"] = sorted(({"km": k, "sources": v} for k, v in groups.items()), key=lambda g: -len(g["sources"]))
    r = res["readiness"]
    bd = res["block_dtcs"]
    active = [d for d in bd if d.get("active")]
    if active:
        res["warnings"].append(f"Активные ошибки в блоках: {len(active)} шт. — см. «Ошибки по блокам».")
    elif bd:
        res["notes"].append(f"В блоках есть ошибки в памяти ({len(bd)} шт.), активных нет — возможно, остались после "
                            "ремонта/отключения АКБ. Сотрите и проверьте снова.")
    if r.get("km_since_clear") is not None and r["km_since_clear"] < 200:
        res["warnings"].append(f"Ошибки стирали или отключали АКБ недавно: {r['km_since_clear']} км назад"
                               + (f", {r['warmups_since_clear']} прогревов" if r.get("warmups_since_clear") is not None else "")
                               + ". Часть неисправностей может быть скрыта — проездите 50–100 км и проверьте снова.")
    not_ready = [m["name"] for m in r["monitors"] if not m["ready"]]
    if not_ready:
        res["warnings"].append("Не готовы системы самодиагностики: " + ", ".join(not_ready) +
                               ". Так бывает после сброса ошибок/АКБ или при неисправности.")
    vin_exp = (car.get("vin") or "").upper()
    for e in res["ecus"]:
        if e.get("vin") and len(e["vin"]) == 17 and vin_exp and e["vin"].upper() != vin_exp:
            res["warnings"].append(f"В блоке «{e['name']}» записан другой VIN: {e['vin']} — блок с другой машины или не прописан.")
    if not res["odometers"]:
        res["notes"].append("Пробег по стандартному OBD (PID A6) шлюз не отдал. У JLR пробег хранится в приборной "
                            "панели за шлюзом — сделайте «Глубокий перебор блоков», найдём адрес.")
    if not res["warnings"]:
        res["notes"].append("Явных признаков сброса ошибок, скрутки или чужих блоков не найдено.")
    return res


def clear_on(cl, car, targets, log):
    """Стереть ошибки в блоках на открытом соединении."""
    norm = []
    for a in (targets or []):
        try:
            norm.append(_to_int_addr(a))
        except Exception:
            pass
    gw = car["logical_address"]
    if gw not in norm:
        norm = [gw] + norm
    results = []
    for t in norm:
        res = cl.clear_dtcs(t)
        log("info", f"Блок 0x{t:04X}: стирание — {res.get('reason')}")
        results.append({"addr": f"0x{t:04X}", **res})
    return {"ok": any(r["ok"] for r in results), "cleared": results}


# ============ одноразовые обёртки (сами находят машину, читают, закрывают) ============

def clear(targets=None, make=None, timeout=3.0, log=None):
    log = log or (lambda *a: None)
    cl, car = _find_and_connect(timeout, log, make)
    if cl is None:
        return car
    try:
        return {**clear_on(cl, car, targets, log), "car": car}
    finally:
        cl.close()


def live(make=None, timeout=3.0, log=None):
    log = log or (lambda *a: None)
    cl, car = _find_and_connect(timeout, log, make)
    if cl is None:
        return car
    try:
        r = live_on(cl, car, log)
        note = "" if r["rows"] else ("Шлюз не отдаёт живые OBD-данные по DoIP напрямую. Для датчиков двигателя нужен "
                                     "адрес блока двигателя за шлюзом — сделайте «Глубокий перебор блоков».")
        return {"ok": bool(r["rows"] or r["dids"]), **r, "note": note, "car": car}
    finally:
        cl.close()


def _read_car(cl, car, deep, log):
    result = {"ok": True, "car": car, "ecus": [], "block_dtcs": [], "ips": local_ipv4s(),
              "network": network_info(), "arp": arp_neighbors()}
    r = read_dtcs_on(cl, car, deep, log)
    for k in ("ecus", "block_dtcs", "vin", "part_number", "serial", "ok", "error"):
        if k in r:
            result[k] = r[k]
    cl.close()
    return result


def tcp_scan(subnet, port=13400, log=None, exclude=()):
    """Сканировать x.x.x.1..254 по TCP-порту (по умолчанию 13400) — найти DoIP-шлюз напрямую."""
    import concurrent.futures
    hosts = []

    def chk(i):
        ip = f"{subnet}.{i}"
        if ip in exclude:
            return None
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.35)
        try:
            s.connect((ip, port))
            s.close()
            return ip
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for r in ex.map(chk, range(1, 255)):
            if r:
                hosts.append(r)
                log and log("rx", f"  порт {port} открыт на {r}")
    return hosts


def scan_manual(deep=False, log=None, make=None):
    """Ручной режим: взять статический IP адаптера, просканировать его подсеть по TCP 13400, подключиться."""
    log = log or (lambda *a: None)
    net = network_info()
    asix = net.get("asix")
    self_ips = set(local_ipv4s())
    # сканируем ТОЛЬКО подсеть кабеля ASIX, не Wi-Fi/домашнюю сеть
    asix_ips = [ip for ip in (asix or {}).get("ipv4", []) if not ip.startswith("169.254")]
    subnets = []
    for ip in asix_ips:
        base = ip.rsplit(".", 1)[0]
        if base not in subnets:
            subnets.append(base)
    if not subnets:
        cur = ((asix or {}).get("ipv4") or ["нет адреса"])[0]
        return {"ok": False, "network": net, "ips": local_ipv4s(),
                "error": f"У адаптера ASIX сейчас адрес {cur} — это аварийный/пустой, сканировать нечего. "
                         "СНАЧАЛА пропишите статический IP: откройте ncpa.cpl → свойства адаптера ASIX AX88772C → "
                         "IP версии 4 → «Использовать следующий IP»: 192.168.1.2, маска 255.255.255.0. Потом нажмите скан снова."}
    found = []
    for base in subnets:
        log("info", f"Скан TCP 13400 в {base}.0/24 (254 адреса)...")
        found += tcp_scan(base, 13400, log=log, exclude=self_ips)
    if not found:
        return {"ok": False, "network": net, "ips": local_ipv4s(), "scanned": subnets,
                "error": f"В подсетях {', '.join(s + '.0/24' for s in subnets)} никто не слушает порт 13400. "
                         "Либо машина в другой подсети (уточните IP, который использует Launch, и пропишите такой же "
                         "адаптеру), либо DoIP не поднялся."}
    for ip in found:
        try:
            cl = DoIPClient(ip, 0, log=log, make=make)
            cl.connect()
            car = {"ip": ip, "logical_address": cl.entity, "vin": "", "via": "tcp-scan"}
            log("info", f"DoIP-шлюз найден: {ip}, активация прошла")
            return _read_car(cl, car, deep, log)
        except Exception as e:  # noqa
            log("rx", f"  {ip}:13400 есть, но активация не прошла ({e})")
    return {"ok": False, "network": net, "ips": local_ipv4s(),
            "error": f"Порт 13400 открыт ({', '.join(found)}), но DoIP-активация не прошла — возможно, шлюз Li "
                     "требует заводскую авторизацию."}
