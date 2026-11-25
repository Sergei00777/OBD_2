"""
Работа с адаптером ELM327 (USB / COM-порт).

Поддерживается:
  * OBD-II (ISO 15765 CAN 11/29 бит, ISO 9141, KWP2000, J1850) — режимы 01, 03, 04, 07, 09, 0A
  * SAE J1939 (грузовой CAN 250 кбит/с) — DM1, DM2, DM3, DM11, основные параметры двигателя
"""
import re
import time
import threading
from collections import deque
from datetime import datetime

import dtc_db

HEX_TOKEN = re.compile(r"^[0-9A-F]+$")
ERROR_WORDS = ("NO DATA", "UNABLE TO CONNECT", "CAN ERROR", "BUS ERROR", "BUS INIT",
               "STOPPED", "BUFFER FULL", "FB ERROR", "DATA ERROR", "ACT ALERT", "LV RESET", "ERR")

PROTOCOLS = {
    "1": "SAE J1850 PWM", "2": "SAE J1850 VPW", "3": "ISO 9141-2", "4": "ISO 14230-4 KWP (5 бод)",
    "5": "ISO 14230-4 KWP (быстрая)", "6": "ISO 15765-4 CAN 11 бит 500 кбит/с",
    "7": "ISO 15765-4 CAN 29 бит 500 кбит/с", "8": "ISO 15765-4 CAN 11 бит 250 кбит/с",
    "9": "ISO 15765-4 CAN 29 бит 250 кбит/с", "A": "SAE J1939 CAN 29 бит 250 кбит/с",
}


class AdapterError(Exception):
    pass


# --------------------------------------------------------------------------- #
#  Нижний уровень: COM-порт
# --------------------------------------------------------------------------- #
class SerialAdapter:
    BAUDRATES = (38400, 115200, 9600, 57600, 230400, 500000)

    def __init__(self, port, baudrate=None, log=None):
        try:
            import serial  # pyserial
        except ImportError:
            raise AdapterError("Не установлен pyserial. Выполните: pip install pyserial")
        self._serial_mod = serial
        self.port = port
        self.log = log or (lambda *a: None)
        self.lock = threading.Lock()
        self.ser = None
        rates = [int(baudrate)] if baudrate else self.BAUDRATES
        last_err = None
        for rate in rates:
            try:
                self._open(rate)
                self.ser.write(b"\r")
                time.sleep(0.2)
                self.ser.reset_input_buffer()
                resp = self.send("ATZ", wait=4)
                if not any("ELM" in l or "OBD" in l or "STN" in l for l in resp):
                    resp = self.send("ATI", wait=2)
                if any("ELM" in l or "OBD" in l or "STN" in l for l in resp):
                    self.baudrate = rate
                    self.log("info", f"Адаптер ответил на скорости {rate}")
                    return
                self.ser.close()
            except Exception as e:  # noqa
                last_err = e
                try:
                    if self.ser:
                        self.ser.close()
                except Exception:
                    pass
        raise AdapterError(
            f"Адаптер на порту {port} не отвечает как ELM327"
            + (f" ({last_err})" if last_err else "")
            + ". Проверьте порт, драйвер (CH340/FTDI/PL2303) и что адаптер вставлен в разъём авто.")

    def _open(self, rate):
        self.ser = self._serial_mod.Serial(self.port, rate, timeout=0.1, write_timeout=2)

    def _read_until_prompt(self, wait):
        buf = b""
        deadline = time.time() + wait
        while time.time() < deadline:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                buf += chunk
                if b">" in chunk:
                    break
        return buf

    @staticmethod
    def _split(buf):
        text = buf.decode("ascii", errors="ignore").replace(">", "")
        return [l.strip() for l in re.split(r"[\r\n]+", text) if l.strip()]

    def send(self, cmd, wait=5.0):
        with self.lock:
            self.ser.reset_input_buffer()
            self.ser.write((cmd + "\r").encode("ascii"))
            self.ser.flush()
            lines = self._split(self._read_until_prompt(wait))
        # убираем эхо команды, если ATE0 ещё не сработал
        if lines and lines[0].replace(" ", "") == cmd.replace(" ", ""):
            lines = lines[1:]
        return lines

    def monitor(self, cmd, seconds):
        """Команда, которая слушает шину (ATMA, ATDM1, ATMP...). Через N секунд прерываем."""
        with self.lock:
            self.ser.reset_input_buffer()
            self.ser.write((cmd + "\r").encode("ascii"))
            buf = b""
            end = time.time() + seconds
            while time.time() < end:
                buf += self.ser.read(self.ser.in_waiting or 1)
                if b">" in buf:
                    break
            if b">" not in buf:
                self.ser.write(b"\r")  # любой символ останавливает мониторинг
                buf += self._read_until_prompt(2)
            lines = self._split(buf)
        if lines and lines[0].replace(" ", "") == cmd.replace(" ", ""):
            lines = lines[1:]
        return lines

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Разбор ответов OBD-II
# --------------------------------------------------------------------------- #
def _hex_tokens(line):
    toks = line.split()
    if toks and all(HEX_TOKEN.match(t) for t in toks):
        return toks
    return None


def parse_obd(lines, protocol):
    """Возвращает список (заголовок, байты_ответа) — по одному на каждый ЭБУ/сообщение."""
    is_can = protocol in ("6", "7", "8", "9")
    bits29 = protocol in ("7", "9")
    out, partial = [], {}
    for line in lines:
        toks = _hex_tokens(line)
        if not toks:
            continue
        if is_can:
            if bits29:
                if len(toks) == 1 or len(toks[0]) == 8:     # без пробелов в заголовке
                    header, data = toks[0][:8], toks[1:]
                else:
                    if len(toks) < 5:
                        continue
                    header, data = "".join(toks[:4]), toks[4:]
            else:
                if len(toks[0]) != 3 or len(toks) < 2:
                    continue
                header, data = toks[0], toks[1:]
            b = [int(x, 16) for x in data]
            kind = b[0] >> 4
            if kind == 0:                                   # одиночный кадр
                ln = b[0] & 0x0F
                out.append((header, bytes(b[1:1 + ln])))
            elif kind == 1 and len(b) >= 2:                 # первый кадр длинного сообщения
                ln = ((b[0] & 0x0F) << 8) | b[1]
                partial[header] = {"len": ln, "data": b[2:]}
            elif kind == 2 and header in partial:           # последующие кадры
                p = partial[header]
                p["data"] += b[1:]
                if len(p["data"]) >= p["len"]:
                    out.append((header, bytes(p["data"][:p["len"]])))
                    del partial[header]
        else:
            if len(toks) < 5:
                continue
            header = "".join(toks[:3])
            out.append((header, bytes(int(x, 16) for x in toks[3:-1])))   # последний байт — контрольная сумма
    for header, p in partial.items():                       # недособранные — отдаём, что есть
        out.append((header, bytes(p["data"])))
    return out


def decode_obd_dtc(a, b):
    letter = "PCBU"[a >> 6]
    return f"{letter}{(a >> 4) & 0x3}{a & 0x0F:X}{b:02X}"


ECU_NAMES = {
    "7E8": "Двигатель (ECM)", "7E9": "КПП (TCM)", "7EA": "ЭБУ №3", "7EB": "ЭБУ №4",
    "7EC": "ЭБУ №5", "7ED": "ЭБУ №6", "7EE": "ЭБУ №7", "7EF": "ЭБУ №8",
}
J1939_SA = {
    0x00: "Двигатель №1", 0x01: "Двигатель №2", 0x03: "КПП", 0x0B: "Тормоза / ABS",
    0x0F: "Ретардер", 0x10: "Ретардер выхлопа", 0x17: "Приборная панель", 0x19: "Климат",
    0x21: "Кузовной контроллер", 0x27: "Управление парком", 0x31: "Освещение",
    0x3D: "Нейтрализатор / DPF / SCR", 0x47: "Подвеска", 0xEE: "Тахограф", 0xF9: "Сканер",
}


def ecu_name(header):
    if header in ECU_NAMES:
        return ECU_NAMES[header]
    if len(header) == 8:  # 29-битный OBD: 18DAF1xx — xx адрес ЭБУ
        sa = int(header[6:8], 16)
        return J1939_SA.get(sa, f"ЭБУ 0x{sa:02X}")
    return f"ЭБУ {header}"


# Параметры живых данных OBD-II: pid -> (название, ед., формула)
OBD_PIDS = [
    (0x0C, "Обороты двигателя", "об/мин", lambda d: (d[0] * 256 + d[1]) / 4),
    (0x0D, "Скорость", "км/ч", lambda d: d[0]),
    (0x05, "Температура ОЖ", "°C", lambda d: d[0] - 40),
    (0x5C, "Температура масла", "°C", lambda d: d[0] - 40),
    (0x04, "Нагрузка двигателя", "%", lambda d: round(d[0] * 100 / 255, 1)),
    (0x0B, "Давление во впуске (наддув)", "кПа", lambda d: d[0]),
    (0x0F, "Температура воздуха на впуске", "°C", lambda d: d[0] - 40),
    (0x10, "Расход воздуха (MAF)", "г/с", lambda d: round((d[0] * 256 + d[1]) / 100, 2)),
    (0x23, "Давление в топливной рампе", "бар", lambda d: round((d[0] * 256 + d[1]) * 10 / 100, 1)),
    (0x11, "Положение дросселя", "%", lambda d: round(d[0] * 100 / 255, 1)),
    (0x49, "Педаль газа", "%", lambda d: round(d[0] * 100 / 255, 1)),
    (0x06, "Краткосрочная топливная коррекция (STFT)", "%", lambda d: round((d[0] - 128) * 100 / 128, 1)),
    (0x07, "Долгосрочная топливная коррекция (LTFT)", "%", lambda d: round((d[0] - 128) * 100 / 128, 1)),
    (0x0E, "Угол опережения зажигания", "°", lambda d: d[0] / 2 - 64),
    (0x2F, "Уровень топлива", "%", lambda d: round(d[0] * 100 / 255, 1)),
    (0x5E, "Расход топлива", "л/ч", lambda d: round((d[0] * 256 + d[1]) / 20, 2)),
    (0x42, "Напряжение ЭБУ", "В", lambda d: round((d[0] * 256 + d[1]) / 1000, 2)),
    (0x33, "Атмосферное давление", "кПа", lambda d: d[0]),
    (0x46, "Температура снаружи", "°C", lambda d: d[0] - 40),
    (0x1F, "Время работы после запуска", "с", lambda d: d[0] * 256 + d[1]),
    (0x21, "Пробег с горящим Check Engine", "км", lambda d: d[0] * 256 + d[1]),
    (0x31, "Пробег после сброса ошибок", "км", lambda d: d[0] * 256 + d[1]),
    (0xA6, "Пробег (одометр, OBD)", "км", lambda d: round(int.from_bytes(d[:4], "big") / 10)),
]

# Мониторы готовности (PID 01): бит -> название
MONITORS_CONT = [(0, "Пропуски воспламенения"), (1, "Топливная система"), (2, "Компоненты")]
MONITORS_SPARK = [(0, "Катализатор"), (1, "Подогрев катализатора"), (2, "Система EVAP (пары топлива)"),
                  (3, "Вторичный воздух"), (5, "Лямбда-зонды"), (6, "Подогрев лямбда-зондов"), (7, "EGR / VVT")]
MONITORS_DIESEL = [(0, "Катализатор NMHC"), (1, "NOx / SCR"), (3, "Давление наддува"),
                   (5, "Датчик выхлопных газов"), (6, "Сажевый фильтр"), (7, "EGR / VVT")]
OBD_STANDARDS = {1: "OBD-II (CARB, США)", 2: "OBD (EPA, США)", 3: "OBD + OBD-II", 4: "OBD-I", 5: "без OBD",
                 6: "EOBD (Европа)", 7: "EOBD + OBD-II", 8: "EOBD + OBD", 9: "EOBD + OBD + OBD-II",
                 10: "JOBD (Япония)", 11: "JOBD + OBD-II", 12: "JOBD + EOBD", 13: "JOBD + EOBD + OBD-II",
                 17: "EMD", 18: "EMD+", 19: "HD OBD-C", 20: "HD OBD", 21: "WWH OBD", 23: "HD EOBD-I",
                 24: "HD EOBD-I N", 25: "HD EOBD-II", 26: "HD EOBD-II N", 28: "OBDBr-1 (Бразилия)",
                 29: "OBDBr-2 (Бразилия)", 30: "KOBD (Корея)", 31: "IOBD I (Индия)", 32: "IOBD II (Индия)",
                 33: "HD EOBD-IV"}

# UDS-идентификаторы для опроса блоков (только чтение, сервис 22)
UDS_DIDS = [(0xF190, "vin"), (0xF197, "system_name"), (0xF187, "part_number"), (0xF188, "software"),
            (0xF18C, "serial")]
# Где искать пробег по умолчанию (дополняется найденными через «Поиск пробега», файл odometer_dids.json)
ODO_DEFAULT = [{"did": "DD01", "offset": 0, "width": 3, "endian": "big", "scale": 1, "note": "Mazda/Ford"},
               {"did": "F4A6", "offset": 0, "width": 4, "endian": "big", "scale": 10, "note": "OBD A6 через UDS"}]
# Диапазоны DID для поиска пробега (часть стандартных и частых у производителей)
ODO_SEARCH_RANGES = [(0xDD00, 0xDDFF), (0xF400, 0xF4FF), (0x0100, 0x01FF), (0x0200, 0x02FF),
                     (0xE100, 0xE1FF), (0xFD00, 0xFDFF), (0xF1A0, 0xF1FF), (0x2000, 0x20FF)]


def decode_odo(data, m):
    """Достать пробег (км) из ответа блока по описанию m."""
    o, w = int(m.get("offset", 0)), int(m.get("width", 3))
    if len(data) < o + w:
        return None
    v = int.from_bytes(data[o:o + w], m.get("endian", "big"))
    if w >= 3 and v in (0xFFFFFF, 0xFFFFFFFF):
        return None
    km = v / float(m.get("scale", 1))
    return round(km) if 0 < km < 2_000_000 else None


def find_value(data, km_target):
    """Все способы, которыми число km_target может быть записано в data."""
    tol = max(2.0, km_target * 0.003)
    out = []
    for w in (2, 3, 4):
        for o in range(0, len(data) - w + 1):
            for endian in ("big", "little"):
                v = int.from_bytes(data[o:o + w], endian)
                for scale in (1, 10, 100):
                    km = v / scale
                    if km > 0 and abs(km - km_target) <= tol:
                        out.append({"offset": o, "width": w, "endian": endian, "scale": scale, "km": round(km, 1)})
    return out
# Подсказки по адресам (для Mazda/Ford и Nissan — ориентировочно)
ECU_ADDR_HINTS = {0x7E0: "Двигатель (7E0)", 0x7E1: "КПП (7E1)", 0x7E2: "Гибрид / доп. блок (7E2)",
                  0x720: "Приборная панель? (720)", 0x726: "Кузовной блок? (726)", 0x730: "Электроусилитель руля? (730)",
                  0x737: "Подушки безопасности? (737)", 0x760: "ABS / DSC? (760)", 0x706: "Камера? (706)",
                  0x764: "Радар? (764)", 0x740: "Кузовной блок BCM? (740)", 0x743: "Приборная панель? (743)"}

# Силовые блоки (двигатель/КПП) — отвечают по OBD
POWERTRAIN = {"7E0", "7E1", "7E2", "7E3", "7E4", "7E5", "7E6", "7E7"}
# Быстрый набор адресов блоков (частые у большинства марок)
QUICK_ADDRS = [0x7E0, 0x7E1, 0x7E2, 0x7E3, 0x720, 0x721, 0x726, 0x730, 0x737,
               0x740, 0x743, 0x750, 0x760, 0x764, 0x670]
# Полный, но разумный набор (≈70 адресов разных производителей) — не все 231 подряд, иначе очень долго
MODULE_ADDRS = sorted(set(
    list(range(0x7E0, 0x7E8)) +
    [0x700, 0x701, 0x703, 0x706, 0x708, 0x70C, 0x70E,
     0x710, 0x711, 0x712, 0x713, 0x714, 0x715, 0x716, 0x718, 0x71A, 0x71C, 0x71E,
     0x720, 0x721, 0x724, 0x725, 0x726, 0x727, 0x728, 0x72C, 0x72E,
     0x730, 0x731, 0x732, 0x733, 0x737, 0x738, 0x73A,
     0x740, 0x741, 0x742, 0x743, 0x744, 0x745, 0x746, 0x748, 0x74C,
     0x750, 0x751, 0x752, 0x754, 0x758, 0x760, 0x761, 0x764, 0x765, 0x766, 0x768,
     0x770, 0x772, 0x776, 0x778, 0x780, 0x782, 0x788, 0x790, 0x793, 0x7A0, 0x7A1, 0x7B0,
     0x670, 0x6F0, 0x6F1]))


def decode_uds_dtc(b0, b1, b2, status):
    """3-байтный DTC + байт статуса (UDS сервис 19) → код, статус, важность."""
    letter = "PCBU"[b0 >> 6]
    code = f"{letter}{(b0 >> 4) & 0x3}{b0 & 0x0F:X}{b1:02X}"   # 4 знака после буквы — для расшифровки
    full = f"{code}-{b2:02X}" if b2 else code                   # с подтипом (FTB), как в заводских сканерах
    active = bool(status & 0x01)
    stored = bool(status & 0x08)
    warn = bool(status & 0x80)
    st = "активная" if active else ("в памяти" if stored else "разовая")
    if warn:
        st += ", горит значок"
    return {"code": code, "full": full, "status": st, "status_byte": status,
            "active": active, "stored": stored, "warn": warn}


# Живые данные J1939: (PGN, название, ед., функция)
def _u16(d, i):
    return d[i] | (d[i + 1] << 8)


J1939_PARAMS = [
    (0xF004, "Обороты двигателя", "об/мин", lambda d: None if _u16(d, 3) >= 0xFB00 else round(_u16(d, 3) * 0.125)),
    (0xFEF1, "Скорость", "км/ч", lambda d: None if _u16(d, 1) >= 0xFB00 else round(_u16(d, 1) / 256, 1)),
    (0xFEEE, "Температура ОЖ", "°C", lambda d: None if d[0] >= 0xFB else d[0] - 40),
    (0xFEEF, "Давление масла", "кПа", lambda d: None if d[3] >= 0xFB else d[3] * 4),
    (0xFEF6, "Давление наддува", "кПа", lambda d: None if d[1] >= 0xFB else d[1] * 2),
    (0xFEF7, "Напряжение бортсети", "В", lambda d: None if _u16(d, 6) >= 0xFB00 else round(_u16(d, 6) * 0.05, 1)),
    (0xFEF2, "Расход топлива", "л/ч", lambda d: None if _u16(d, 0) >= 0xFB00 else round(_u16(d, 0) * 0.05, 1)),
    (0xFEFC, "Уровень топлива", "%", lambda d: None if d[1] >= 0xFB else round(d[1] * 0.4, 1)),
    (0xFEE5, "Моточасы", "ч", lambda d: None if d[3] >= 0xFB else round((d[0] | d[1] << 8 | d[2] << 16 | d[3] << 24) * 0.05, 1)),
]


# --------------------------------------------------------------------------- #
#  Разбор ответов J1939
# --------------------------------------------------------------------------- #
def _pgn_from_id(prio_dp, pf, ps):
    dp = prio_dp & 0x03
    return (dp << 16) | (pf << 8) | (ps if pf >= 0xF0 else 0)


def parse_j1939(lines):
    """Возвращает список (pgn, адрес_источника, байты). Собирает многокадровые (TP.BAM / TP.CM)."""
    msgs, tp = [], {}
    current = None  # для формата, где ELM сам собрал сообщение и выдаёт строки "1: ..."
    for raw in lines:
        line = raw.strip()
        m = re.match(r"^([0-9A-F]{1,2}):\s*(.*)$", line)
        if m and current is not None:
            toks = _hex_tokens(m.group(2))
            if toks:
                current[2].extend(int(t, 16) for t in toks)
            continue
        toks = _hex_tokens(line)
        if not toks:
            continue
        # формат ELM: "6 0FECA 00 xx xx ..."
        if len(toks) >= 3 and len(toks[0]) == 1 and len(toks[1]) == 5:
            pgn, sa, data = int(toks[1], 16), int(toks[2], 16), toks[3:]
            pf, ps = (pgn >> 8) & 0xFF, pgn & 0xFF
            prio_dp = pgn >> 16
        elif len(toks[0]) == 8:
            hid = int(toks[0], 16)
            prio_dp, pf, ps, sa = (hid >> 24) & 0xFF, (hid >> 16) & 0xFF, (hid >> 8) & 0xFF, hid & 0xFF
            data = toks[1:]
        elif len(toks) >= 4 and all(len(t) == 2 for t in toks[:4]):
            prio_dp, pf, ps, sa = (int(t, 16) for t in toks[:4])
            data = toks[4:]
        else:
            continue
        b = [int(x, 16) for x in data]
        pgn = _pgn_from_id(prio_dp, pf, ps)
        if pf == 0xEC and b:                                 # TP.CM (BAM / RTS)
            if b[0] in (0x20, 0x10) and len(b) >= 8:
                size = b[1] | (b[2] << 8)
                tpgn = b[5] | (b[6] << 8) | (b[7] << 16)
                tp[sa] = {"size": size, "pgn": tpgn, "data": []}
            current = None
            continue
        if pf == 0xEB and b:                                 # TP.DT
            if sa in tp:
                t = tp[sa]
                t["data"] += b[1:8]
                if len(t["data"]) >= t["size"]:
                    msgs.append((t["pgn"], sa, bytes(t["data"][:t["size"]])))
                    del tp[sa]
            continue
        entry = [pgn, sa, list(b)]
        msgs.append(entry)
        current = entry
    return [(int(p), int(s), bytes(d)) for p, s, d in msgs]


def decode_dm(data):
    """DM1/DM2: лампы + список (SPN, FMI, счётчик)."""
    lamps = {}
    if len(data) >= 1:
        b0 = data[0]
        lamps = {
            "Check Engine (MIL)": (b0 >> 6) & 3 == 1,
            "Красная STOP": (b0 >> 4) & 3 == 1,
            "Жёлтая предупреждение": (b0 >> 2) & 3 == 1,
            "Защита": b0 & 3 == 1,
        }
    dtcs = []
    for i in range(2, len(data) - 3, 4):
        c = data[i:i + 4]
        if c == b"\x00\x00\x00\x00" or c == b"\xff\xff\xff\xff":
            continue
        spn = c[0] | (c[1] << 8) | ((c[2] >> 5) << 16)
        fmi = c[2] & 0x1F
        oc = c[3] & 0x7F
        if spn == 0 and fmi == 0:
            continue
        dtcs.append((spn, fmi, oc))
    return lamps, dtcs


# --------------------------------------------------------------------------- #
#  Верхний уровень: сканер
# --------------------------------------------------------------------------- #
class Scanner:
    def __init__(self):
        self.adapter = None
        self.mode = None            # "obd" | "j1939"
        self.protocol = None
        self.protocol_name = ""
        self.elm_version = ""
        self.supported_pids = set()
        self.j1939_sae_format = True
        self.demo = False
        self.make = ""              # марка выбранной машины — для расшифровки кодов производителя
        self.port = None
        self.logbuf = deque(maxlen=600)

    # ---------- журнал ----------
    def log(self, kind, text):
        self.logbuf.append({"t": datetime.now().strftime("%H:%M:%S"), "kind": kind, "text": text})

    def _send(self, cmd, wait=5.0):
        if not self.adapter:
            raise AdapterError("Адаптер не подключён")
        self.log("tx", cmd)
        lines = self.adapter.send(cmd, wait)
        self.log("rx", " | ".join(lines) if lines else "(нет ответа)")
        return lines

    def _monitor(self, cmd, seconds):
        self.log("tx", f"{cmd}  (слушаю {seconds} с)")
        lines = self.adapter.monitor(cmd, seconds)
        self.log("rx", " | ".join(lines[:40]) + (" ..." if len(lines) > 40 else "") if lines else "(нет ответа)")
        return lines

    @staticmethod
    def _has_error(lines):
        return next((l for l in lines if any(w in l.upper() for w in ERROR_WORDS)), None)

    @property
    def connected(self):
        return self.adapter is not None and self.mode is not None

    # ---------- подключение ----------
    def connect(self, port, mode="auto", demo=False, baudrate=None, demo_vin=None, demo_fuel=None):
        self.disconnect()
        self.demo = demo
        self.port = port
        if demo:
            from simulator import SimAdapter
            self.adapter = SimAdapter(vin=demo_vin, fuel=demo_fuel)
            self.log("info", "ДЕМО-РЕЖИМ: машина имитируется, реальный адаптер не используется")
        else:
            if not port:
                raise AdapterError(
                    "COM-порт не выбран: Windows не видит адаптер. Вставьте адаптер в USB, откройте "
                    "«Диспетчер устройств» → «Порты (COM и LPT)». Если там пусто, а в «Другие устройства» "
                    "есть устройство с жёлтым значком — установите драйвер (чаще всего CH340), затем нажмите ⟳.")
            self.log("info", f"Открываю порт {port}...")
            self.adapter = SerialAdapter(port, baudrate, log=self.log)

        ver = self._send("ATZ", wait=4)
        self.elm_version = next((l for l in ver if "ELM" in l or "STN" in l or "OBD" in l), " ".join(ver))
        for c in ("ATE0", "ATL0", "ATS1", "ATH1", "ATAT1"):
            self._send(c, wait=2)

        errors = []
        if mode in ("auto", "obd"):
            ok, err = self._try_obd()
            if ok:
                return self.status()
            errors.append(f"OBD-II: {err}")
        if mode in ("auto", "j1939"):
            ok, err = self._try_j1939()
            if ok:
                return self.status()
            errors.append(f"J1939: {err}")
        self.disconnect(keep_log=True)
        raise AdapterError("Машина не ответила. " + "; ".join(errors) +
                           ". Проверьте: зажигание включено, адаптер плотно в разъёме, адаптер рассчитан на 24 В.")

    def _try_obd(self):
        self.log("info", "Пробую OBD-II (автоопределение протокола)...")
        self._send("ATSP0", wait=2)
        lines = self._send("0100", wait=15)
        err = self._has_error(lines)
        dpn = "".join(self._send("ATDPN", wait=2)).replace("A", "", 1).strip()
        proto = dpn[:1] if dpn else ""
        if proto not in PROTOCOLS or proto == "A":
            return False, err or "протокол не определён"
        frames = [p for h, p in parse_obd(lines, proto) if len(p) >= 2 and p[0] == 0x41 and p[1] == 0x00]
        if not frames:
            return False, err or "нет ответа на запрос 0100"
        self.mode, self.protocol = "obd", proto
        self.protocol_name = PROTOCOLS[proto]
        self.log("info", f"Связь установлена: {self.protocol_name}")
        self._load_supported_pids(lines)
        return True, None

    def _load_supported_pids(self, first_lines):
        self.supported_pids = set()
        base, lines = 0x00, first_lines
        while True:
            got = False
            for h, p in parse_obd(lines, self.protocol):
                if len(p) >= 6 and p[0] == 0x41 and p[1] == base:
                    bits = int.from_bytes(p[2:6], "big")
                    for i in range(32):
                        if bits & (1 << (31 - i)):
                            self.supported_pids.add(base + i + 1)
                    got = True
            if not got or (base + 0x20) not in self.supported_pids or base >= 0xC0:
                break
            base += 0x20
            lines = self._send(f"01{base:02X}", wait=5)
        self.log("info", f"Поддерживаемые PID: {', '.join(f'{p:02X}' for p in sorted(self.supported_pids))}")

    def _try_j1939(self):
        self.log("info", "Пробую грузовой протокол J1939 (CAN 250 кбит/с)...")
        r = self._send("ATSP A", wait=2)
        if not any("OK" in l for l in r):
            return False, "адаптер не поддерживает J1939 (нужен оригинальный ELM327 v1.4+ / OBDLink / vLinker)"
        r = self._send("ATJS", wait=2)
        self.j1939_sae_format = any("OK" in l for l in r)
        self._send("ATCP 18", wait=2)
        self._send("ATSH EA FF F9", wait=2)
        lines = self._monitor("ATMA", 1.5)
        msgs = parse_j1939(lines)
        if not msgs:
            msgs = self._j1939_request(0xFEEE)
        if not msgs:
            err = self._has_error(lines) or "на шине J1939 тишина"
            return False, err
        self.mode, self.protocol = "j1939", "A"
        self.protocol_name = PROTOCOLS["A"]
        sources = sorted({sa for _, sa, _ in msgs})
        self.log("info", "Связь J1939. Блоки на шине: " +
                 ", ".join(J1939_SA.get(s, f"0x{s:02X}") for s in sources))
        return True, None

    def disconnect(self, keep_log=True):
        if self.adapter:
            try:
                if self.mode:
                    self.adapter.send("ATPC", 1)
            except Exception:
                pass
            self.adapter.close()
            self.log("info", "Отключено")
        self.adapter, self.mode, self.protocol, self.protocol_name = None, None, None, ""
        self.supported_pids = set()

    # ---------- статус ----------
    def battery_voltage(self):
        try:
            r = self._send("ATRV", wait=2)
            m = re.search(r"(\d+(?:\.\d+)?)\s*V", " ".join(r))
            return float(m.group(1)) if m else None
        except Exception:
            return None

    def status(self):
        return {
            "connected": self.connected, "demo": self.demo, "port": self.port, "mode": self.mode,
            "protocol": self.protocol_name, "elm": self.elm_version,
            "voltage": self.battery_voltage() if self.connected else None,
        }

    # ---------- VIN ----------
    def read_vin(self):
        if self.mode == "obd":
            lines = self._send("0902", wait=6)
            chars = b""
            for h, p in parse_obd(lines, self.protocol):
                if len(p) > 3 and p[0] == 0x49 and p[1] == 0x02:
                    chars += p[3:]
            vin = "".join(chr(c) for c in chars if 48 <= c <= 90)
            return vin[-17:] if len(vin) >= 17 else (vin or None)
        if self.mode == "j1939":
            for pgn, sa, d in self._j1939_request(0xFEEC, wait=3):
                if pgn == 0xFEEC:
                    vin = "".join(chr(c) for c in d if 48 <= c <= 90)
                    return vin[:17] or None
        return None

    # ---------- ошибки ----------
    def read_dtcs(self):
        if not self.connected:
            raise AdapterError("Сначала подключитесь к машине")
        return self._read_obd_dtcs() if self.mode == "obd" else self._read_j1939_dtcs()

    def _read_obd_dtcs(self):
        result = {"mode": "obd", "codes": [], "mil": None, "mil_count": None, "lamps": {}}
        # статус лампы Check Engine
        for h, p in parse_obd(self._send("0101", wait=5), self.protocol):
            if len(p) >= 3 and p[0] == 0x41 and p[1] == 0x01:
                result["mil"] = bool(p[2] & 0x80) or bool(result["mil"])
                result["mil_count"] = (result["mil_count"] or 0) + (p[2] & 0x7F)
        if result["mil"] is not None:
            result["lamps"]["Check Engine (MIL)"] = result["mil"]
        is_can = self.protocol in ("6", "7", "8", "9")
        kinds = [("03", 0x43, "Сохранённая"), ("07", 0x47, "Ожидающая (текущий цикл)"), ("0A", 0x4A, "Постоянная")]
        seen = set()
        for cmd, ans, label in kinds:
            lines = self._send(cmd, wait=8)
            for header, p in parse_obd(lines, self.protocol):
                if not p or p[0] != ans:
                    continue
                body = p[2:] if is_can else p[1:]
                if is_can and len(p) >= 2:
                    body = p[2:2 + p[1] * 2]
                for i in range(0, len(body) - 1, 2):
                    if body[i] == 0 and body[i + 1] == 0:
                        continue
                    code = decode_obd_dtc(body[i], body[i + 1])
                    key = (code, label, header)
                    if key in seen:
                        continue
                    seen.add(key)
                    info = dtc_db.describe_obd(code, self.make)
                    result["codes"].append({
                        "code": code, "type": label, "ecu": ecu_name(header),
                        "description": info["description"], "system": info["system"],
                        "severity": info["severity"], "hint": info.get("hint", ""),
                    })
        return result

    def _j1939_request(self, pgn, wait=2.0):
        if self.j1939_sae_format:
            data = f"{pgn & 0xFF:02X}{(pgn >> 8) & 0xFF:02X}{(pgn >> 16) & 0xFF:02X}"
        else:
            data = f"{pgn:06X}"
        return parse_j1939(self._send(data, wait=wait))

    def _read_j1939_dtcs(self):
        result = {"mode": "j1939", "codes": [], "lamps": {}, "mil": None}
        msgs = self._j1939_request(0xFECA, wait=3)
        dm1 = [m for m in msgs if m[0] == 0xFECA]
        if not dm1:  # DM1 и так рассылается раз в секунду — послушаем шину
            dm1 = [m for m in parse_j1939(self._monitor("ATDM1", 3)) if m[0] == 0xFECA]
        dm2 = [m for m in self._j1939_request(0xFECB, wait=3) if m[0] == 0xFECB]
        for group, label in ((dm1, "Активная (DM1)"), (dm2, "Неактивная, в памяти (DM2)")):
            seen = set()
            for pgn, sa, data in group:
                lamps, dtcs = decode_dm(data)
                if label.startswith("Активная"):
                    for k, v in lamps.items():
                        result["lamps"][k] = result["lamps"].get(k, False) or v
                for spn, fmi, oc in dtcs:
                    if (sa, spn, fmi) in seen:
                        continue
                    seen.add((sa, spn, fmi))
                    info = dtc_db.describe_j1939(spn, fmi)
                    result["codes"].append({
                        "code": f"SPN {spn} / FMI {fmi}", "type": label + (f", раз: {oc}" if oc else ""),
                        "ecu": J1939_SA.get(sa, f"Блок 0x{sa:02X}"),
                        "description": info["description"], "system": info["system"],
                        "severity": info["severity"], "hint": info.get("hint", ""),
                    })
        result["mil"] = result["lamps"].get("Check Engine (MIL)")
        return result

    def clear_dtcs(self):
        if not self.connected:
            raise AdapterError("Сначала подключитесь к машине")
        if self.mode == "obd":
            lines = self._send("04", wait=10)
            ok = any(p and p[0] == 0x44 for h, p in parse_obd(lines, self.protocol))
            if not ok:
                err = self._has_error(lines)
                raise AdapterError("ЭБУ не подтвердил стирание" + (f": {err}" if err else
                                   ". Заглушите двигатель, включите зажигание и повторите."))
            return {"ok": True, "message": "Ошибки стёрты (OBD-II, режим 04)"}
        # J1939: DM11 — стереть активные, DM3 — стереть неактивные
        self._j1939_request(0xFED3, wait=3)
        self._j1939_request(0xFECC, wait=3)
        return {"ok": True, "message": "Отправлены команды стирания J1939 (DM11 + DM3). Проверьте результат повторным сканированием."}

    # ---------- живые данные ----------
    def live_data(self):
        if not self.connected:
            raise AdapterError("Сначала подключитесь к машине")
        rows = []
        if self.mode == "obd":
            for pid, name, unit, fn in OBD_PIDS:
                if self.supported_pids and pid not in self.supported_pids:
                    continue
                for h, p in parse_obd(self._send(f"01{pid:02X}", wait=3), self.protocol):
                    if len(p) >= 3 and p[0] == 0x41 and p[1] == pid:
                        try:
                            rows.append({"name": name, "value": fn(p[2:]), "unit": unit})
                        except IndexError:
                            pass
                        break
        else:
            for pgn, name, unit, fn in J1939_PARAMS:
                for mpgn, sa, d in self._j1939_request(pgn, wait=1.5):
                    if mpgn == pgn and len(d) >= 8:
                        v = fn(d)
                        if v is not None:
                            rows.append({"name": name, "value": v, "unit": unit})
                        break
        v = self.battery_voltage()
        if v is not None:
            rows.append({"name": "Напряжение на разъёме (адаптер)", "value": v, "unit": "В"})
        return rows

    # ---------- сажевый фильтр (DPF / DPD) ----------
    def _obd_pid(self, pid):
        for h, p in parse_obd(self._send(f"01{pid:02X}", wait=3), self.protocol):
            if len(p) >= 3 and p[0] == 0x41 and p[1] == pid:
                return p[2:]
        return None

    def dpf_status(self):
        """Параметры для контроля регенерации сажевого фильтра + проверка условий."""
        if not self.connected:
            raise AdapterError("Сначала подключитесь к машине")
        v = {"coolant": None, "rpm": None, "speed": None, "voltage": None,
             "egt": [], "dpf_dp": None, "dpf_in": None, "dpf_out": None}
        if self.mode == "obd":
            sp = self.supported_pids
            has = lambda pid: not sp or pid in sp
            if has(0x05) and (d := self._obd_pid(0x05)):
                v["coolant"] = d[0] - 40
            if has(0x0C) and (d := self._obd_pid(0x0C)) and len(d) >= 2:
                v["rpm"] = round((d[0] * 256 + d[1]) / 4)
            if has(0x0D) and (d := self._obd_pid(0x0D)):
                v["speed"] = d[0]
            if 0x78 in sp and (d := self._obd_pid(0x78)) and len(d) >= 9:      # температура выхлопа, 4 датчика
                for i in range(4):
                    if d[0] & (1 << i):
                        v["egt"].append(round((d[1 + 2 * i] * 256 + d[2 + 2 * i]) / 10 - 40))
            if 0x7A in sp and (d := self._obd_pid(0x7A)) and len(d) >= 3 and d[0] & 1:   # перепад давления DPF
                raw = d[1] * 256 + d[2]
                if raw >= 0x8000:
                    raw -= 0x10000
                v["dpf_dp"] = round(raw * 0.01, 2)
            if 0x7C in sp and (d := self._obd_pid(0x7C)) and len(d) >= 5:      # температура на входе/выходе DPF
                if d[0] & 1:
                    v["dpf_in"] = round((d[1] * 256 + d[2]) / 10 - 40)
                if d[0] & 2:
                    v["dpf_out"] = round((d[3] * 256 + d[4]) / 10 - 40)
        else:
            for pgn, key, fn in ((0xFEEE, "coolant", J1939_PARAMS[2][3]), (0xF004, "rpm", J1939_PARAMS[0][3]),
                                 (0xFEF1, "speed", J1939_PARAMS[1][3])):
                for mpgn, sa, d in self._j1939_request(pgn, wait=1.5):
                    if mpgn == pgn and len(d) >= 8:
                        v[key] = fn(d)
                        break
        v["voltage"] = self.battery_voltage()

        checks = [
            ("Двигатель прогрет (ОЖ ≥ 70 °C)", v["coolant"] is not None and v["coolant"] >= 70,
             f"{v['coolant']} °C" if v["coolant"] is not None else "нет данных"),
            ("Машина стоит (скорость 0)", v["speed"] is not None and v["speed"] == 0,
             f"{v['speed']} км/ч" if v["speed"] is not None else "нет данных"),
            ("Двигатель работает", v["rpm"] is not None and v["rpm"] > 400,
             f"{v['rpm']} об/мин" if v["rpm"] is not None else "нет данных"),
            ("Напряжение в норме", v["voltage"] is not None and v["voltage"] > 11.5,
             f"{v['voltage']} В" if v["voltage"] is not None else "нет данных"),
        ]
        hot = max([t for t in v["egt"] + [v["dpf_in"], v["dpf_out"]] if t is not None], default=None)
        return {"values": v, "checks": [{"name": n, "ok": ok, "value": val} for n, ok, val in checks],
                "ready": all(ok for _, ok, _ in checks), "max_temp": hot,
                "regen_likely": hot is not None and hot >= 450}

    # ---------- проверка при покупке ----------
    def _ecm_pid(self, pid):
        """Ответ на PID режима 01 от блока двигателя (если поддерживается)."""
        if self.supported_pids and pid not in self.supported_pids:
            return None
        return self._obd_pid(pid)

    def readiness(self):
        """Готовность мониторов самодиагностики (PID 01) + счётчики после сброса ошибок."""
        if self.mode != "obd":
            raise AdapterError("Проверка готовности доступна для OBD-II")
        d = self._ecm_pid(0x01)
        out = {"monitors": [], "diesel": None, "mil": None, "dtc_count": None}
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
        cnt = {}
        for pid, key, fn in ((0x31, "km_since_clear", lambda x: x[0] * 256 + x[1]),
                             (0x30, "warmups_since_clear", lambda x: x[0]),
                             (0x4E, "min_since_clear", lambda x: x[0] * 256 + x[1]),
                             (0x21, "km_with_mil", lambda x: x[0] * 256 + x[1]),
                             (0x4D, "min_with_mil", lambda x: x[0] * 256 + x[1]),
                             (0x1C, "obd_std", lambda x: OBD_STANDARDS.get(x[0], f"код {x[0]} (нет в таблице стандартов)"))):
            x = self._ecm_pid(pid)
            if x:
                try:
                    cnt[key] = fn(x)
                except IndexError:
                    pass
        out.update(cnt)
        return out

    def odometer_obd(self):
        """Стандартный пробег OBD (PID A6) — есть на многих машинах с 2019–2021 г."""
        x = self._ecm_pid(0xA6)
        if x and len(x) >= 4:
            return round(int.from_bytes(x[:4], "big") / 10)
        return None

    def _uds(self, did, wait=1.5):
        """UDS 22 <DID> по текущему заголовку. Возвращает {заголовок_ответа: байты} или {}."""
        res = {}
        for h, p in parse_obd(self._send(f"22{did:04X}", wait=wait), self.protocol):
            if len(p) >= 3 and p[0] == 0x62 and (p[1] << 8 | p[2]) == did:
                res[h] = bytes(p[3:])
        return res

    def _ecu_session(self, e):
        """Настроить адаптер на конкретный блок с явным Flow Control (для длинных ответов)."""
        self._send("ATAR", 1)
        self._send(f"ATSH {e['req']}", 1)
        self._send(f"ATCRA {e['resp']}", 1)
        self._send(f"ATFCSH {e['req']}", 1)
        self._send("ATFCSD 300000", 1)
        self._send("ATFCSM1", 1)

    def _restore_obd(self):
        for c in ("ATD", "ATE0", "ATL0", "ATS1", "ATH1", "ATAT1", f"ATSP{self.protocol}"):
            self._send(c, 2)

    def _probe_addr(self, rid, probes):
        """Точечно проверить один блок (приёмный фильтр ровно на его адрес — без «призраков»).
        Flow Control тут не настраиваем — для обнаружения хватает первого кадра, это сильно быстрее."""
        resp = f"{rid + 8:03X}"
        self.adapter.send(f"ATSH {rid:03X}", 1)
        self.adapter.send(f"ATCRA {resp}", 1)
        for probe in probes:
            frames = parse_obd(self.adapter.send(probe, 0.6), self.protocol)
            if any(p for h, p in frames):   # любой ответ (в т.ч. 7F «не поддерживается») = блок есть
                return resp
        return None

    def read_block_dtcs(self, e):
        """Ошибки (DTC) одного блока через UDS сервис 19 02 (reportDTCByStatusMask). Только чтение."""
        self._ecu_session(e)
        data = None
        for seq in (["1902FF"], ["1003", "1902FF"], ["1902AF"], ["1003", "190208"]):
            lines = []
            for cmd in seq:
                lines = self.adapter.send(cmd, 1.5)
            for h, p in parse_obd(lines, self.protocol):
                if len(p) >= 3 and p[0] == 0x59 and p[1] == 0x02:
                    data = bytes(p[3:])     # пропускаем 59 02 <маска доступности>
                    break
            if data is not None:
                break
        dtcs = []
        if data:
            for i in range(0, len(data) - 3, 4):
                b0, b1, b2, status = data[i], data[i + 1], data[i + 2], data[i + 3]
                if (b0, b1, b2) == (0, 0, 0) or (b0, b1, b2) == (0xFF, 0xFF, 0xFF):
                    continue
                d = decode_uds_dtc(b0, b1, b2, status)
                info = dtc_db.describe_obd(d["code"], self.make)
                d["description"] = info["description"]
                d["system"] = info["system"]
                d["severity"] = "high" if d["active"] else info["severity"]
                d["hint"] = info.get("hint", "")
                dtcs.append(d)
        return dtcs

    def scan_ecus(self, full=True, odo_dids=None, read_dtcs=False):
        """Опрос блоков (только чтение): какие блоки есть, их VIN, номера, пробег.
        Фаза 1 — широковещательный OBD-запрос 0100 (двигатель/КПП отзываются сами).
        Фаза 2 — перебор адресов UDS-запросами с точным приёмным фильтром (кузов, ABS, подушки, панель)."""
        if self.mode != "obd" or self.protocol not in ("6", "8"):
            raise AdapterError("Опрос блоков пока работает только на CAN 11 бит (большинство легковых с 2008 г.)")
        ecus, seen = [], set()
        try:
            self._send("ATAR", 1)
            self._send("ATST 28", 1)
            self._send("ATSH 7DF", 1)
            for h, p in parse_obd(self._send("0100", 2.5), self.protocol):
                if p and p[0] == 0x41 and len(h) == 3 and h not in seen:
                    seen.add(h)
                    req = f"{int(h, 16) - 8:03X}"
                    ecus.append({"req": req, "resp": h, "name": ECU_ADDR_HINTS.get(int(req, 16), f"Блок {req}")})
            self.log("info", f"Фаза 1 (OBD 0100): отозвалось блоков {len(ecus)}")
            ids = MODULE_ADDRS if full else QUICK_ADDRS
            ids = [i for i in ids if f"{i + 8:03X}" not in seen]
            probes = ("22F190", "1003")
            self.log("info", f"Фаза 2: перебор {len(ids)} адресов блоков (кузов, ABS, подушки, панель)...")
            self.adapter.send("ATST 14", 1)   # ~80 мс на попытку
            done = 0
            for rid in ids:
                done += 1
                resp = self._probe_addr(rid, probes)
                if resp and resp not in seen:
                    seen.add(resp)
                    ecus.append({"req": f"{rid:03X}", "resp": resp, "name": ECU_ADDR_HINTS.get(rid, f"Блок {rid:03X}")})
                    self.log("info", f"  блок {rid:03X} отвечает")
                if done % 20 == 0:
                    self.log("info", f"  проверено {done}/{len(ids)}, блоков {len(ecus)}")
            self.log("info", f"Отвечающих блоков: {len(ecus)}. Читаю VIN/номера/пробег...")
            self._send("ATST 32", 1)
            odo_dids = odo_dids if odo_dids is not None else ODO_DEFAULT
            for e in ecus:
                # точный приёмный фильтр + явный Flow Control — длинные ответы (VIN, номера) собираются целиком
                self._ecu_session(e)
                for did, key in UDS_DIDS:
                    vals = self._uds(did)
                    v = vals.get(e["resp"]) or next(iter(vals.values()), None)
                    if v is None:
                        continue
                    txt = "".join(chr(c) for c in v if 32 <= c < 127).strip()
                    if txt:
                        e[key] = txt
                cache = {}
                for m in odo_dids:
                    if m.get("ecu") and m["ecu"].upper() != e["req"]:
                        continue
                    did = int(m["did"], 16)
                    if did not in cache:
                        vals = self._uds(did)
                        cache[did] = vals.get(e["resp"]) or next(iter(vals.values()), None)
                    v = cache[did]
                    km = decode_odo(v, m) if v is not None else None
                    if km is not None and not any(o["did"] == m["did"].upper() for o in e.get("odometers", [])):
                        e.setdefault("odometers", []).append({"did": m["did"].upper(), "km": km})
                if e.get("odometers"):
                    e["odometer_km"] = e["odometers"][0]["km"]
                if e.get("system_name"):
                    e["name"] = f"{e['system_name']} ({e['req']})"
                if read_dtcs:
                    try:
                        e["dtcs"] = self.read_block_dtcs(e)
                    except Exception as ex:  # noqa
                        e["dtcs"] = []
                    e["dtc_count"] = len(e["dtcs"])
                    if e["dtcs"]:
                        self.log("info", f"  {e['name']}: ошибок {len(e['dtcs'])}")
        finally:
            self._restore_obd()
        self.log("info", f"Найдено блоков: {len(ecus)}" +
                 (f", ошибок по блокам: {sum(e.get('dtc_count', 0) for e in ecus)}" if read_dtcs else ""))
        return ecus

    def odo_search(self, km_target, ecus, ranges=None):
        """Найти, в каком DID блока записан пробег (по значению с приборной панели). Только чтение."""
        if self.mode != "obd" or self.protocol not in ("6", "8"):
            raise AdapterError("Поиск пробега работает на CAN 11 бит")
        ranges = ranges or ODO_SEARCH_RANGES
        total = sum(hi - lo + 1 for lo, hi in ranges)
        matches = []
        self.log("info", f"Поиск пробега {km_target} км: {len(ecus)} блок(ов) × {total} ячеек. Это займёт время...")
        try:
            for e in ecus:
                self._ecu_session(e)
                self._send("ATST 08", 1)
                done = 0
                for lo, hi in ranges:
                    for did in range(lo, hi + 1):
                        lines = self.adapter.send(f"22{did:04X}", 0.6)   # без записи в журнал — слишком много строк
                        for h, p in parse_obd(lines, self.protocol):
                            if len(p) >= 4 and p[0] == 0x62 and (p[1] << 8 | p[2]) == did:
                                for f in find_value(bytes(p[3:]), km_target):
                                    matches.append({"ecu": e["req"], "ecu_name": e.get("name", e["req"]),
                                                    "did": f"{did:04X}", "raw": bytes(p[3:]).hex(" ").upper(), **f})
                        done += 1
                    self.log("info", f"  {e.get('name', e['req'])}: проверено {done}/{total}, совпадений: {len(matches)}")
        finally:
            self._restore_obd()
        # одна ячейка — один вариант: точнее, шире (не переполнится), ближе к началу
        matches.sort(key=lambda m: (abs(m["km"] - km_target), -m["width"], m["offset"], m["scale"]))
        best = {}
        for m in matches:
            best.setdefault((m["ecu"], m["did"]), m)
        matches = list(best.values())
        self.log("info", f"Поиск пробега завершён: найдено вариантов {len(matches)}")
        return matches

    def buyer_check(self, full_scan=False, expected_vin=None, odo_dids=None, read_dtcs=False):
        """Сводная проверка машины перед покупкой."""
        if not self.connected:
            raise AdapterError("Сначала подключитесь к машине")
        if self.mode != "obd":
            raise AdapterError("Проверка при покупке пока для легковых (OBD-II)")
        res = {"readiness": self.readiness(), "odometers": [], "ecus": [], "warnings": [], "notes": [], "block_dtcs": []}
        odo = self.odometer_obd()
        if odo is not None:
            res["odometers"].append({"source": "OBD-II стандарт (PID A6), блок двигателя", "km": odo})
        try:
            res["ecus"] = self.scan_ecus(full=full_scan, odo_dids=odo_dids, read_dtcs=read_dtcs)
        except AdapterError as e:
            res["notes"].append(str(e))
        for e in res["ecus"]:
            for d in e.get("dtcs", []):
                res["block_dtcs"].append({**d, "ecu": e["name"]})
        if read_dtcs:
            active = [d for d in res["block_dtcs"] if d["active"]]
            if active:
                res["warnings"].append(f"Активные ошибки в блоках: {len(active)} шт. — см. раздел «Ошибки по блокам».")
            elif res["block_dtcs"]:
                res["notes"].append(f"В блоках есть ошибки в памяти ({len(res['block_dtcs'])} шт.), активных нет — "
                                    "возможно, остались после ремонта/отключения АКБ. Сотрите и проверьте снова.")
        for e in res["ecus"]:
            for o in e.get("odometers", []):
                res["odometers"].append({"source": f"{e['name']} — DID {o['did']}", "km": o["km"]})
        groups = {}
        for o in res["odometers"]:
            groups.setdefault(o["km"], []).append(o["source"].replace(" — ", " · "))
        res["odometer_groups"] = sorted(({"km": k, "sources": v} for k, v in groups.items()),
                                        key=lambda g: -len(g["sources"]))
        # --- выводы ---
        r = res["readiness"]
        kms = [o["km"] for o in res["odometers"]]
        if len(kms) >= 2 and max(kms) - min(kms) > 500:
            diff = f"{max(kms) - min(kms):,}".replace(",", " ")
            res["warnings"].append(f"Пробег в разных блоках отличается на {diff} км — "
                                   f"возможна скрутка или замена блока. Сверьте с приборной панелью.")
        if res["ecus"] and all(e["req"] in POWERTRAIN for e in res["ecus"]):
            res["notes"].append("Отвечает только силовой блок (двигатель-генератор). На этой машине остальные блоки — "
                                "батарея, электромоторы, кузов, ABS, ADAS — закрыты шлюзом и через обычный OBD-разъём "
                                "недоступны: их читает только заводской сканер (для Li — «Лисик»). Это нормально для "
                                "электромобилей и гибридов Li, Zeekr, Voyah и т.п.")
        if not kms:
            res["notes"].append("Пробег не найден: машина не отдаёт стандартный OBD-пробег, а место хранения в блоках "
                                "для этой марки ещё неизвестно. Сделайте полную проверку, затем «Поиск пробега» — "
                                "введите пробег с панели, программа найдёт, где он записан, и запомнит.")
        if r.get("km_since_clear") is not None and r["km_since_clear"] < 200:
            res["warnings"].append(f"Ошибки стирали или отключали АКБ недавно: {r['km_since_clear']} км назад"
                                   + (f", {r['warmups_since_clear']} прогревов" if r.get("warmups_since_clear") is not None else "")
                                   + ". Часть неисправностей может быть скрыта — проездите 50–100 км и проверьте снова.")
        not_ready = [m["name"] for m in r["monitors"] if not m["ready"]]
        if not_ready:
            res["warnings"].append("Не готовы системы самодиагностики: " + ", ".join(not_ready) +
                                   ". Так бывает после сброса ошибок/АКБ или при неисправности.")
        if expected_vin:
            for e in res["ecus"]:
                if e.get("vin") and len(e["vin"]) == 17 and e["vin"].upper() != expected_vin.upper():
                    res["warnings"].append(f"В блоке «{e['name']}» записан другой VIN: {e['vin']} — блок с другой машины "
                                           f"или не прописан после замены.")
        if not res["warnings"]:
            res["notes"].append("Явных признаков сброса ошибок, скрутки или чужих блоков не найдено.")
        return res

    def raw(self, cmd):
        if not self.adapter:
            raise AdapterError("Адаптер не подключён")
        return self._send(cmd.strip().upper(), wait=6)


def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    ports = []
    for p in lp.comports():
        ports.append({"device": p.device, "description": p.description or ""})
    return ports
