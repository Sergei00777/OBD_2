"""
Демо-адаптер: имитирует ELM327, подключённый к Isuzu с дизелем 4HK1.
Нужен, чтобы проверить программу без машины.
"""
import time

VIN = "LWLDMFMGXRL048554"


def iso_tp_frames(header, payload):
    """Разбить ответ на CAN-кадры ISO-TP, как их выводит ELM327 с ATH1 ATS1."""
    out = []
    if len(payload) <= 7:
        data = [len(payload)] + list(payload)
        data += [0x00] * (8 - len(data))
        out.append(data)
    else:
        n = len(payload)
        out.append([0x10 | (n >> 8), n & 0xFF] + list(payload[:6]))
        rest, seq = payload[6:], 1
        while rest:
            chunk = list(rest[:7])
            rest = rest[7:]
            out.append([0x20 | (seq & 0x0F)] + chunk + [0x00] * (7 - len(chunk)))
            seq += 1
    return [header + " " + " ".join(f"{b:02X}" for b in d) for d in out]


def j1939_lines(pgn, sa, data, prio=6):
    pf, ps = (pgn >> 8) & 0xFF, pgn & 0xFF
    head = f"{(prio << 2) | ((pgn >> 16) & 3):02X} {pf:02X} {ps:02X} {sa:02X}"
    if len(data) <= 8:
        return [head + " " + " ".join(f"{b:02X}" for b in data)]
    n = len(data)
    packets = (n + 6) // 7
    lines = [f"1C EC FF {sa:02X} 20 {n & 0xFF:02X} {n >> 8:02X} {packets:02X} FF "
             f"{pgn & 0xFF:02X} {(pgn >> 8) & 0xFF:02X} {(pgn >> 16) & 0xFF:02X}"]
    for i in range(packets):
        chunk = list(data[i * 7:(i + 1) * 7])
        chunk += [0xFF] * (7 - len(chunk))
        lines.append(f"1C EB FF {sa:02X} {i + 1:02X} " + " ".join(f"{b:02X}" for b in chunk))
    return lines


def spn_bytes(spn, fmi, oc):
    return [spn & 0xFF, (spn >> 8) & 0xFF, ((spn >> 16) & 0x7) << 5 | fmi, oc & 0x7F]


class SimAdapter:
    def __init__(self, vin=None, fuel=None):
        self.vin = (VIN if vin is None else vin).upper()   # "" — машина без VIN (японский рынок)
        self.diesel = (fuel or "дизель").startswith("диз")
        self.protocol = "0"
        if self.diesel:
            self.stored = ["P0087", "P2002", "P0401"]
            self.pending = ["P0299"]
        else:  # бензиновая машина
            self.stored = ["P0171", "P0302"]
            self.pending = ["P0456"]
        self.j_active = [(157, 18, 3), (3251, 0, 1)]
        self.j_prev = [(110, 0, 2)]
        self.t0 = time.time()
        self.header = None
        self.odo = 12360
        v = self.vin
        self.ecus = {  # адрес -> {DID: данные}
            "7E0": {**({0xF190: v} if v else {}), 0xF197: "PCM", 0xF187: "PY8A-18-881B", 0xF188: "PY8A-188K1-B", 0xDD01: self.odo - 4},
            "7E1": {**({0xF190: v} if v else {}), 0xF197: "TCM", 0xF187: "PY27-21-1X0"},
            "720": {0xF197: "IC", 0xF187: "KD45-55-430", 0xDD01: self.odo,
                    0x0110: bytes([0x01]) + (self.odo * 10).to_bytes(4, "big")},  # пробег «в неизвестном месте» для демо поиска
            "760": {**({0xF190: v} if v else {}), 0xF197: "ABS/DSC"},
            "737": {**({0xF190: v} if v else {}), 0xF197: "RCM (SRS)"},
        }
        # демо-ошибки блоков (адрес → список (b0,b1,b2,status)) для UDS сервиса 19
        self.block_dtcs = {"737": [(0xC0, 0x23, 0x00, 0x09)], "760": [(0x90, 0x01, 0x16, 0x08)]}

    # ------------------------------------------------------------------ #
    def close(self):
        pass

    def monitor(self, cmd, seconds):
        time.sleep(min(seconds, 0.5))
        cmd = cmd.replace(" ", "").upper()
        if self.protocol != "A":
            return ["NO DATA"]
        if cmd == "ATMA":
            return self._j(0xF004) + self._j(0xFEEE) + self._dm1()
        if cmd == "ATDM1":
            return self._dm1()
        return ["NO DATA"]

    def send(self, cmd, wait=5.0):
        time.sleep(0.03)
        c = cmd.replace(" ", "").upper()
        if c in ("ATZ", "ATWS"):
            self.protocol = "0"
            return ["ELM327 v1.5 (ДЕМО)"]
        if c == "ATI":
            return ["ELM327 v1.5 (ДЕМО)"]
        if c == "ATRV":
            return [f"{27.6 + (time.time() % 3) / 10:.1f}V"]
        if c.startswith("ATSH"):
            self.header = c[4:] or None
            return ["OK"]
        if c == "ATD":
            self.header = None
            return ["OK"]
        if c.startswith("ATSP"):
            self.protocol = c[4:] or "0"
            return ["OK"]
        if c == "ATDPN":
            return ["A6"] if self.protocol == "0" else [self.protocol]
        if c.startswith("AT"):
            return ["OK"]
        if self.protocol == "A":
            return self._j1939(c)
        return self._obd(c)

    # ------------------------------------------------------------------ #
    def _rpm(self):
        return 720 + int((time.time() * 37) % 60)

    def _uds(self, c):
        ecu = self.ecus.get(self.header)
        if not ecu:
            return ["NO DATA"]
        resp = f"{int(self.header, 16) + 8:03X}"
        if c == "3E00":
            return iso_tp_frames(resp, bytes([0x7E, 0x00]))
        if c == "1003":
            return iso_tp_frames(resp, bytes([0x50, 0x03, 0x00, 0x32, 0x01, 0xF4]))
        if c.startswith("1902"):
            body = [0x59, 0x02, 0xFF]
            for d in self.block_dtcs.get(self.header, []):
                body += list(d)
            return iso_tp_frames(resp, bytes(body))
        if c.startswith("22") and len(c) == 6:
            did = int(c[2:], 16)
            if did in ecu:
                val = ecu[did]
                data = val if isinstance(val, bytes) else (val.encode() if isinstance(val, str) else val.to_bytes(3, "big"))
                return iso_tp_frames(resp, bytes([0x62, did >> 8, did & 0xFF]) + data)
            return iso_tp_frames(resp, bytes([0x7F, 0x22, 0x31]))
        return ["NO DATA"]

    def _obd(self, c):
        if self.header and self.header != "7DF":
            return self._uds(c)
        ecm = "7E8"
        pids = {0x04, 0x05, 0x0B, 0x0C, 0x0D, 0x0F, 0x10, 0x1F, 0x21, 0x23, 0x2F, 0x31, 0x33, 0x42, 0x46, 0x5C, 0x5E,
                0x78, 0x7A, 0x7C} if self.diesel else \
               {0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x1C, 0x1F, 0x21, 0x2F, 0x30, 0x31, 0x33,
                0x42, 0x46, 0x49, 0x4D, 0x4E, 0xA6}
        # имитация прожига: температура растёт, перепад давления падает
        el = time.time() - self.t0
        egt = int((min(250 + el * 8, 620) + 40) * 10)
        dp = int(max(18 - el * 0.1, 3) * 100)
        dpf_in, dpf_out = int((min(230 + el * 7, 580) + 40) * 10), int((min(200 + el * 7, 600) + 40) * 10)
        if c.startswith("01") and len(c) >= 4:
            pid = int(c[2:4], 16)
            if pid in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0):
                sup = set(pids) | {0x20, 0x40, 0x60} | ({0x80, 0xA0} if 0xA6 in pids else set()) | ({0x01} if pid == 0 else set())
                bits = 0
                for p in sup:
                    if pid < p <= pid + 32:
                        bits |= 1 << (31 - (p - pid - 1))
                if pid >= (0xC0 if 0xA6 in pids else 0x80):
                    return ["NO DATA"]
                return iso_tp_frames(ecm, bytes([0x41, pid]) + bits.to_bytes(4, "big"))
            up = int(time.time() - self.t0)
            rpm = self._rpm() * 4 if self.diesel else (680 + int((time.time() * 41) % 50)) * 4
            vals = {
                0x01: [0x80 | len(self.stored) if self.stored else 0, 0x0F if self.diesel else 0x07, 0x65,
                       0x00 if self.diesel else 0x04],
                0x1C: [6], 0x30: [3], 0x4D: [0, 0], 0x4E: [0, 25],
                0xA6: list((self.odo * 10).to_bytes(4, "big")),
                0x04: [70], 0x05: [40 + 82], 0x0B: [105], 0x0C: [rpm >> 8, rpm & 0xFF], 0x0D: [0],
                0x0F: [40 + 18], 0x10: [0x05, 0xDC], 0x1F: [up >> 8, up & 0xFF], 0x21: [0x00, 0x2A] if self.diesel else [0, 0],
                0x23: [0x0F, 0xA0], 0x2F: [150], 0x31: [0x04, 0xD2] if self.diesel else [0, 10], 0x33: [96], 0x42: [0x6B, 0x6C],
                0x06: [128 + 12], 0x07: [128 + 5], 0x0E: [128 + 2 * 8], 0x49: [38], 0x11: [33] if not self.diesel else [0],
                0x46: [40 - 12], 0x5C: [40 + 85], 0x5E: [0x00, 0x3C],
                0x78: [0x03, egt >> 8, egt & 0xFF, (egt - 300) >> 8, (egt - 300) & 0xFF, 0, 0, 0, 0],
                0x7A: [0x01, dp >> 8, dp & 0xFF, 0, 0, 0, 0],
                0x7C: [0x03, dpf_in >> 8, dpf_in & 0xFF, dpf_out >> 8, dpf_out & 0xFF, 0, 0, 0, 0],
            }
            if pid in vals:
                return iso_tp_frames(ecm, bytes([0x41, pid] + vals[pid]))
            return ["NO DATA"]
        if c in ("03", "07", "0A"):
            src = {"03": self.stored, "07": self.pending, "0A": []}[c]
            ans = {"03": 0x43, "07": 0x47, "0A": 0x4A}[c]
            body = []
            for code in src:
                first = "PCBU".index(code[0]) << 6 | int(code[1]) << 4 | int(code[2], 16)
                body += [first, int(code[3:5], 16)]
            out = iso_tp_frames(ecm, bytes([ans, len(src)] + body))
            out += iso_tp_frames("7E9", bytes([ans, 0]))       # КПП отвечает «ошибок нет»
            return out
        if c == "04":
            self.stored, self.pending = [], []
            return iso_tp_frames(ecm, bytes([0x44])) + iso_tp_frames("7E9", bytes([0x44]))
        if c == "0902":
            if not self.vin:
                return ["NO DATA"]
            return iso_tp_frames(ecm, bytes([0x49, 0x02, 0x01]) + self.vin.encode())
        return ["NO DATA"]

    # ------------------------------------------------------------------ #
    def _dm1(self):
        lamp = 0x44 if self.j_active else 0x00   # MIL + жёлтая
        data = [lamp, 0xFF]
        for d in self.j_active or [(0, 0, 0)]:
            data += spn_bytes(*d)
        if len(data) < 8:
            data += [0xFF] * (8 - len(data))
        return j1939_lines(0xFECA, 0x00, data) + j1939_lines(0xFECA, 0x0B, [0x00, 0xFF, 0, 0, 0, 0, 0xFF, 0xFF])

    def _j(self, pgn):
        rpm = self._rpm() * 8
        hours = int(12345.6 / 0.05)
        table = {
            0xF004: [0xF0, 0x7D, 0x7D, rpm & 0xFF, rpm >> 8, 0x00, 0xF0, 0x7D],
            0xFEF1: [0xFF, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF],
            0xFEEE: [40 + 84, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF],
            0xFEEF: [0xFF, 0xFF, 0xFF, 75, 0xFF, 0xFF, 0xFF, 0xFF],
            0xFEF6: [0xFF, 55, 40 + 22, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF],
            0xFEF7: [0xFF] * 6 + [0x28, 0x02],
            0xFEF2: [0x50, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF],
            0xFEFC: [0xFF, 160, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF],
            0xFEE5: list(hours.to_bytes(4, "little")) + [0xFF] * 4,
        }
        if pgn in table:
            return j1939_lines(pgn, 0x00, table[pgn])
        if pgn == 0xFEEC:
            return j1939_lines(0xFEEC, 0x00, list((self.vin + "*").encode()))
        return []

    def _j1939(self, c):
        if len(c) != 6:
            return ["NO DATA"]
        pgn = int(c[0:2], 16) | int(c[2:4], 16) << 8 | int(c[4:6], 16) << 16
        if pgn == 0xFECA:
            return self._dm1()
        if pgn == 0xFECB:
            data = [0x00, 0xFF]
            for d in self.j_prev or [(0, 0, 0)]:
                data += spn_bytes(*d)
            data += [0xFF] * max(0, 8 - len(data))
            return j1939_lines(0xFECB, 0x00, data)
        if pgn == 0xFED3:
            self.j_active = []
            return j1939_lines(0xE800, 0x00, [0x00, 0xFF, 0xFF, 0xFF, 0xF9, 0xD3, 0xFE, 0x00])
        if pgn == 0xFECC:
            self.j_prev = []
            return j1939_lines(0xE800, 0x00, [0x00, 0xFF, 0xFF, 0xFF, 0xF9, 0xCC, 0xFE, 0x00])
        return self._j(pgn) or ["NO DATA"]
