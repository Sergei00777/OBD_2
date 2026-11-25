"""
Расшифровка VIN: страна, производитель (по WMI), модельный год, проверка контрольной цифры.
"""
from datetime import date

WMI = {
    # Китай
    "LVR": "Changan Mazda (Китай)", "LWL": "Qingling Isuzu (Китай)", "LVS": "Changan Ford (Китай)",
    "LW4": "Li Auto / Lixiang (Китай)", "LW3": "Li Auto / Lixiang (Китай)", "LFP": "Leapmotor (Китай)",
    "LPA": "Xpeng (Китай)", "LNP": "Nio (Китай)", "LJ1": "Zeekr / Geely (Китай)", "L2C": "Voyah / Dongfeng (Китай)",
    "LS5": "Changan (Китай)", "LS4": "Changan (Китай, Чунцин)", "LS6": "Changan (Китай)", "LVV": "Chery (Китай)", "LB3": "Geely (Китай)", "L6T": "Geely (Китай)",
    "LGW": "Great Wall / Haval (Китай)", "LFV": "FAW-Volkswagen (Китай)", "LFM": "FAW Toyota (Китай)",
    "LHG": "GAC Honda (Китай)", "LVG": "GAC Toyota (Китай)", "LSV": "SAIC Volkswagen (Китай)",
    "LBV": "BMW Brilliance (Китай)", "LGX": "BYD (Китай)", "LC0": "BYD (Китай)", "LRW": "Tesla (Китай)",
    "LNB": "BAIC (Китай)", "LDC": "Dongfeng (Китай)", "LZW": "SAIC-GM-Wuling (Китай)",
    # Япония
    "JM1": "Mazda (Япония)", "JMZ": "Mazda (Япония, экспорт)", "JM3": "Mazda (Япония, внедорожники)",
    "JMB": "Mitsubishi (Япония)", "JA3": "Mitsubishi (Япония)", "JT": "Toyota (Япония)", "JTD": "Toyota (Япония)",
    "JTE": "Toyota (Япония)", "JTM": "Toyota (Япония)", "JHM": "Honda (Япония)", "JHL": "Honda (Япония)",
    "JN1": "Nissan (Япония)", "JN8": "Nissan (Япония)", "JF1": "Subaru (Япония)", "JF2": "Subaru (Япония)",
    "JS3": "Suzuki (Япония)", "JAL": "Isuzu (Япония)", "JAA": "Isuzu (Япония)", "JTH": "Lexus (Япония)",
    # Корея
    "KMH": "Hyundai (Корея)", "KNA": "Kia (Корея)", "KND": "Kia (Корея)", "KMT": "Genesis (Корея)",
    # Россия / СНГ
    "XTA": "АвтоВАЗ / Lada (Россия)", "X7L": "Renault Россия", "XW8": "Volkswagen Group Rus", "Z94": "Hyundai (Россия)",
    "XWE": "Kia / Автотор (Россия)", "X4X": "BMW / Автотор (Россия)", "Z8N": "Nissan (Россия)", "XTT": "УАЗ (Россия)",
    # Европа / США
    "WBA": "BMW (Германия)", "WBS": "BMW M (Германия)", "WDD": "Mercedes-Benz (Германия)", "WDB": "Mercedes-Benz (Германия)",
    "WVW": "Volkswagen (Германия)", "WAU": "Audi (Германия)", "WP0": "Porsche (Германия)", "TMB": "Škoda (Чехия)",
    "VF1": "Renault (Франция)", "VF3": "Peugeot (Франция)", "YV1": "Volvo (Швеция)", "SAL": "Land Rover (Великобритания)",
    "1FA": "Ford (США)", "1G1": "Chevrolet (США)", "5YJ": "Tesla (США)",
}
COUNTRY = {"L": "Китай", "J": "Япония", "K": "Корея", "X": "Россия/СНГ", "Z": "Россия/Италия", "W": "Германия",
           "V": "Франция/Испания", "S": "Великобритания", "T": "Швейцария/Чехия", "Y": "Швеция/Финляндия",
           "1": "США", "4": "США", "5": "США", "2": "Канада", "3": "Мексика", "9": "Бразилия"}

YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"  # 30-летний цикл, A=1980/2010
TRANS = {**{str(i): i for i in range(10)}, **dict(zip("ABCDEFGH", range(1, 9))), **dict(zip("JKLMN", range(1, 6))),
         "P": 7, "R": 9, **dict(zip("STUVWXYZ", range(2, 10)))}
WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def check_digit(vin):
    s = sum(TRANS.get(c, 0) * WEIGHTS[i] for i, c in enumerate(vin)) % 11
    return "X" if s == 10 else str(s)


def model_year(code):
    if code not in YEAR_CODES:
        return None
    idx = YEAR_CODES.index(code)
    limit = date.today().year + 1
    candidates = [1980 + idx + 30 * k for k in range(3)]
    candidates = [y for y in candidates if y <= limit]
    return max(candidates) if candidates else None


def decode(vin):
    vin = (vin or "").strip().upper().replace(" ", "")
    res = {"vin": vin, "valid": False, "errors": []}
    if len(vin) != 17:
        res["errors"].append(f"В VIN должно быть 17 символов, сейчас {len(vin)}")
        return res
    bad = sorted(set(c for c in vin if c in "IOQ" or not c.isalnum()))
    if bad:
        res["errors"].append("Недопустимые символы: " + ", ".join(bad) + " (в VIN не бывает I, O, Q — проверьте, нет ли 0/1)")
    res["manufacturer"] = WMI.get(vin[:3]) or WMI.get(vin[:2]) or "неизвестный производитель"
    res["country"] = COUNTRY.get(vin[0], "—")
    res["year"] = model_year(vin[9])
    cd = check_digit(vin)
    res["check_ok"] = vin[8] == cd
    res["check_note"] = ("контрольная цифра верна" if res["check_ok"] else
                         f"контрольная цифра не сходится (ожидалась {cd}). Для европейских и японских VIN это бывает "
                         f"нормой, для китайских и американских — повод перепроверить номер")
    res["serial"] = vin[11:]
    res["valid"] = not res["errors"]
    return res
