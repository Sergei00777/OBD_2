"""
Поиск OBD-адаптера среди USB-устройств Windows (через PowerShell Get-PnpDevice).
Показывает: видит ли Windows адаптер, какой у него чип, установлен ли драйвер, какой COM-порт.
"""
import json
import re
import subprocess
import sys

# VID, PID -> (чип, какой драйвер нужен, ссылка)
KNOWN_CHIPS = {
    ("1A86", "7523"): ("CH340", "CH340/CH341 (WCH)", "https://www.wch-ic.com/downloads/CH341SER_EXE.html"),
    ("1A86", "5523"): ("CH341", "CH340/CH341 (WCH)", "https://www.wch-ic.com/downloads/CH341SER_EXE.html"),
    ("1A86", "7522"): ("CH340K", "CH340/CH341 (WCH)", "https://www.wch-ic.com/downloads/CH341SER_EXE.html"),
    ("1A86", "55D4"): ("CH9102", "CH9102/CH343 (WCH)", "https://www.wch-ic.com/downloads/CH343SER_EXE.html"),
    ("1A86", "55D3"): ("CH343", "CH9102/CH343 (WCH)", "https://www.wch-ic.com/downloads/CH343SER_EXE.html"),
    ("0403", "6001"): ("FTDI FT232R", "FTDI VCP", "https://ftdichip.com/drivers/vcp-drivers/"),
    ("0403", "6015"): ("FTDI FT231X / FT230X (OBDLink и др.)", "FTDI VCP", "https://ftdichip.com/drivers/vcp-drivers/"),
    ("0403", "6010"): ("FTDI FT2232", "FTDI VCP", "https://ftdichip.com/drivers/vcp-drivers/"),
    ("067B", "2303"): ("Prolific PL2303", "Prolific PL2303", "https://www.prolific.com.tw"),
    ("067B", "23A3"): ("Prolific PL2303GC", "Prolific PL2303", "https://www.prolific.com.tw"),
    ("10C4", "EA60"): ("Silicon Labs CP210x", "CP210x VCP", "https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers"),
}
# USB-сетевые переходники (их ставят в кабели BMW ENET)
NET_VIDS = {"0B95": "ASIX", "0BDA": "Realtek", "05AC": "Apple", "2001": "D-Link", "0FE6": "Kontron/ICS", "13B1": "Linksys"}
VIDPID = re.compile(r"VID_([0-9A-F]{4})[&+]PID_([0-9A-F]{4})", re.I)
COM = re.compile(r"\((COM\d+)\)")
SYSTEM_WORDS = ("Root Hub", "корневой", "Host Controller", "хост-контроллер", "Composite", "составное",
                "Generic USB Hub", "USB-концентратор", "UCM-UCSI", "xHCI")

PS_SCRIPT = (
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
    "Get-PnpDevice | Where-Object { $_.InstanceId -like 'USB\\*' -or $_.InstanceId -like 'FTDIBUS\\*' "
    "-or $_.Class -eq 'Ports' -or ($_.Class -eq 'Net' -and $_.InstanceId -like 'USB*') } | Select-Object Status,Class,FriendlyName,InstanceId,Present,Problem "
    "| ConvertTo-Json -Compress"
)


def _run_powershell():
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS_SCRIPT],
                         capture_output=True, timeout=40, creationflags=flags)
    text = out.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise RuntimeError(out.stderr.decode("utf-8", errors="replace")[:300] or "PowerShell ничего не вернул")
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


def analyze(raw):
    devices = []
    for d in raw:
        iid = d.get("InstanceId") or ""
        name = d.get("FriendlyName") or "(без имени)"
        status = d.get("Status") or ""
        present = d.get("Present")
        if present is None:
            present = status != "Unknown"
        m = VIDPID.search(iid)
        vid, pid = (m.group(1).upper(), m.group(2).upper()) if m else (None, None)
        chip = KNOWN_CHIPS.get((vid, pid))
        com = COM.search(name)
        problem = status in ("Error", "Degraded") or bool(d.get("Problem")) and str(d.get("Problem")) not in ("0", "CM_PROB_NONE", "None")
        system = any(w.lower() in name.lower() for w in SYSTEM_WORDS)
        failed = vid == "0000" or "дескриптор" in name.lower() or "descriptor" in name.lower()
        net = d.get("Class") == "Net" or (vid in NET_VIDS and not chip)
        candidate = bool(chip) or bool(com) or d.get("Class") == "Ports" or (problem and not system)
        devices.append({
            "name": name, "status": status, "class": d.get("Class") or "", "present": bool(present),
            "instance_id": iid, "vid": vid, "pid": pid, "com": com.group(1) if com else None,
            "chip": chip[0] if chip else None, "driver": chip[1] if chip else None, "driver_url": chip[2] if chip else None,
            "problem": problem, "system": system, "candidate": candidate or failed or net,
            "failed": failed, "net": net,
        })
    # убираем дубли (один и тот же чип часто виден как USB\... и как FTDIBUS\...)
    present = [d for d in devices if d["present"]]
    past = [d for d in devices if not d["present"] and (d["chip"] or d["com"] or d["class"] == "Ports")]
    return present, past


def verdict(present, past):
    if any(d["net"] for d in present) and not any(d["com"] for d in present):
        return "driver", ("Найден USB-СЕТЕВОЙ переходник, а не ELM327. Похоже на кабель BMW ENET (OBD → RJ45 → USB): "
                          "он работает только с BMW F/G-серий через E-Sys/ISTA. Для Isuzu и других марок нужен "
                          "адаптер ELM327 (для грузовика — с поддержкой 24 В).")
    if any(d["failed"] for d in present) and not any(d["com"] for d in present):
        return "driver", ("USB-устройство подключено, но не смогло представиться Windows (сбой запроса дескриптора). "
                          "Это не драйвер: плохой контакт, порт или неисправный кабель/переходник. Вставьте напрямую в другой "
                          "USB-разъём ноутбука без хабов. Если повторится — переходник неисправен.")
    ok = [d for d in present if d["com"] and not d["problem"]]
    if ok:
        d = ok[0]
        return "ok", (f"Адаптер найден: {d['name']}. Выберите порт {d['com']} в блоке «Подключение» "
                      f"(нажмите ⟳, чтобы обновить список).")
    bad = [d for d in present if d["candidate"] and d["problem"]]
    if bad:
        d = bad[0]
        if d["driver"]:
            return "driver", (f"Windows видит адаптер ({d['chip']}), но драйвер не установлен или с ошибкой. "
                              f"Установите драйвер {d['driver']} по ссылке ниже, затем выньте и вставьте адаптер.")
        return "driver", (f"Есть устройство без драйвера: «{d['name']}» (VID {d['vid'] or '?'}, PID {d['pid'] or '?'}). "
                          f"Пришлите эту строку — скажу, какой драйвер нужен.")
    chips = [d for d in present if d["chip"]]
    if chips:
        d = chips[0]
        return "driver", (f"Найден чип адаптера {d['chip']}, но COM-порт не создан. "
                          f"Переустановите драйвер {d['driver']}.")
    msg = ("Windows не видит адаптер. Многие USB-адаптеры ELM327 получают питание от машины: вставьте его в разъём OBD, "
           "включите зажигание (на адаптере должен загореться светодиод) и нажмите «Найти адаптер» ещё раз. "
           "Также попробуйте другой USB-разъём и проверьте кабель.")
    if past:
        msg += " Раньше к ноутбуку подключался: " + "; ".join(sorted({d['name'] for d in past})) + "."
    return "none", msg


def scan():
    if sys.platform != "win32":
        raise RuntimeError("Поиск USB-устройств работает только в Windows")
    present, past = analyze(_run_powershell())
    level, text = verdict(present, past)
    return {"level": level, "verdict": text,
            "devices": [d for d in present if not d["system"]] , "system_count": sum(d["system"] for d in present),
            "past": past}
