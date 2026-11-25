"""
Красивый PDF-отчёт о диагностике (бренд «Автоброкер»).
Нужна библиотека reportlab (pip install reportlab). Шрифты берутся из Windows (Segoe UI / Arial)
или DejaVu в Linux — с кириллицей.
"""
import io
import os
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

# ---------------------------------------------------------------- шрифты
FONT_CANDIDATES = [
    (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\segoeuisb.ttf"),
    (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
]
_fonts_ready = False


def _register_fonts():
    global _fonts_ready
    if _fonts_ready:
        return
    for reg, bold, semi in FONT_CANDIDATES:
        if os.path.exists(reg) and os.path.exists(bold):
            pdfmetrics.registerFont(TTFont("R", reg))
            pdfmetrics.registerFont(TTFont("B", bold))
            pdfmetrics.registerFont(TTFont("SB", semi if os.path.exists(semi) else bold))
            _fonts_ready = True
            return
    raise RuntimeError("Не найден шрифт с кириллицей (Segoe UI / Arial / DejaVu)")


# ---------------------------------------------------------------- цвета
INK = colors.HexColor("#1E2340")
MUTED = colors.HexColor("#7A809B")
LINE = colors.HexColor("#E6E8F5")
SOFT = colors.HexColor("#F5F6FF")
VIOLET = colors.HexColor("#6C5CE7")
BLUE = colors.HexColor("#4FA3FF")
OK = colors.HexColor("#12A150")
OK_BG = colors.HexColor("#E3F7EA")
WARN = colors.HexColor("#B7791F")
WARN_BG = colors.HexColor("#FFF3DC")
BAD = colors.HexColor("#E5484D")
BAD_BG = colors.HexColor("#FFE6E8")
INFO = colors.HexColor("#2F6FEB")
INFO_BG = colors.HexColor("#E4EEFF")
SEV = {"high": ("высокая", BAD, BAD_BG), "medium": ("средняя", WARN, WARN_BG), "low": ("низкая", INFO, INFO_BG)}


def _st(name, font="R", size=9.5, color=INK, leading=None, align=TA_LEFT, space=0):
    return ParagraphStyle(name, fontName=font, fontSize=size, textColor=color, leading=leading or size * 1.35,
                          alignment=align, spaceAfter=space)


S = {}


def _styles():
    S.update({
        "h2": _st("h2", "B", 13, INK, space=4),
        "body": _st("body"), "small": _st("small", size=8, color=MUTED),
        "muted": _st("muted", size=9, color=MUTED), "bold": _st("bold", "SB"),
        "code": _st("code", "B", 10.5), "kpi": _st("kpi", "B", 17, INK, leading=20),
        "kpil": _st("kpil", size=8, color=MUTED), "right": _st("right", size=9, color=MUTED, align=TA_RIGHT),
        "white": _st("white", "B", 10, colors.white),
    })


def esc(t):
    return (str(t if t is not None else "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def P(text, style="body"):
    return Paragraph(text, S[style])


def km(n):
    return f"{int(n):,}".replace(",", " ") + " км"


def _gradient(c, x, y, w, h, c1=VIOLET, c2=BLUE, steps=60):
    for i in range(steps):
        t = i / (steps - 1)
        col = colors.Color(c1.red + (c2.red - c1.red) * t, c1.green + (c2.green - c1.green) * t,
                           c1.blue + (c2.blue - c1.blue) * t)
        c.setFillColor(col)
        c.rect(x + w * i / steps, y, w / steps + 0.6, h, stroke=0, fill=1)


def _card(rows, col_widths, bg=SOFT, pad=8, extra=None):
    t = Table(rows, colWidths=col_widths)
    style = [("BACKGROUND", (0, 0), (-1, -1), bg), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LEFTPADDING", (0, 0), (-1, -1), pad), ("RIGHTPADDING", (0, 0), (-1, -1), pad),
             ("TOPPADDING", (0, 0), (-1, -1), pad * .6), ("BOTTOMPADDING", (0, 0), (-1, -1), pad * .6),
             ("ROUNDEDCORNERS", [8, 8, 8, 8])]
    t.setStyle(TableStyle(style + (extra or [])))
    return t


def _pill(text, fg, bg):
    t = Table([[Paragraph(esc(text), _st("pill", "SB", 8, fg, align=1))]], colWidths=[22 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("ROUNDEDCORNERS", [6, 6, 6, 6]),
                           ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                           ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
    return t


# ---------------------------------------------------------------- построение
def build_pdf(scan, vehicle, brand, scan_time, vin_ecu=None, protocol="", demo=False, buyer=None, live=None):
    _register_fonts()
    _styles()
    brand = {"brand": "Автоброкер", "site": "", "city": "", "phone": "", "inspector": "", **(brand or {})}
    buf = io.BytesIO()
    W, H = A4
    margin = 16 * mm
    report_no = scan_time.strftime("%y%m%d-%H%M")
    title_car = " ".join(str(x) for x in (vehicle.get("make"), vehicle.get("model"), vehicle.get("year")) if x) or "Автомобиль"

    def first_page(c, doc):
        c.saveState()
        _gradient(c, 0, H - 58 * mm, W, 58 * mm)
        c.setFillColor(colors.Color(1, 1, 1, alpha=0.10))
        c.circle(W - 30 * mm, H - 12 * mm, 38 * mm, stroke=0, fill=1)
        c.circle(W - 70 * mm, H - 60 * mm, 22 * mm, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("B", 22)
        c.drawString(margin, H - 22 * mm, brand["brand"].upper())
        c.setFont("R", 10)
        sub = " · ".join(x for x in (brand.get("city"), brand.get("site"), brand.get("phone")) if x)
        if sub:
            c.drawString(margin, H - 28.5 * mm, sub)
        c.setFont("SB", 15)
        c.drawString(margin, H - 42 * mm, "Отчёт о диагностике автомобиля")
        c.setFont("R", 10.5)
        c.drawString(margin, H - 48.5 * mm, title_car)
        c.setFont("R", 9)
        c.drawRightString(W - margin, H - 42 * mm, f"№ {report_no}")
        c.drawRightString(W - margin, H - 48.5 * mm, scan_time.strftime("%d.%m.%Y, %H:%M"))
        _footer(c, doc)
        c.restoreState()

    def later_pages(c, doc):
        c.saveState()
        _gradient(c, 0, H - 14 * mm, W, 14 * mm)
        c.setFillColor(colors.white)
        c.setFont("B", 10)
        c.drawString(margin, H - 9 * mm, brand["brand"].upper())
        c.setFont("R", 9)
        c.drawRightString(W - margin, H - 9 * mm, f"{title_car} · отчёт № {report_no}")
        _footer(c, doc)
        c.restoreState()

    def _footer(c, doc):
        if demo:
            c.saveState()
            c.setFillColor(colors.Color(0.9, 0.3, 0.3, alpha=0.10))
            c.setFont("B", 70)
            c.translate(W / 2, H / 2)
            c.rotate(35)
            c.drawCentredString(0, 0, "ДЕМО")
            c.restoreState()
        c.setStrokeColor(LINE)
        c.line(margin, 14 * mm, W - margin, 14 * mm)
        c.setFillColor(MUTED)
        c.setFont("R", 8)
        left = " · ".join(x for x in (brand["brand"], brand.get("site"), brand.get("phone")) if x)
        c.drawString(margin, 9.5 * mm, left)
        c.drawRightString(W - margin, 9.5 * mm, f"стр. {doc.page}")

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=margin, rightMargin=margin,
                            topMargin=22 * mm, bottomMargin=20 * mm,
                            title=f"Отчёт о диагностике — {title_car}", author=brand["brand"])
    CW = W - 2 * margin
    story = [Spacer(1, 44 * mm)]

    # ---- карточка автомобиля
    vin_doc = vehicle.get("vin") or ""
    vin_line = esc(vin_doc or "—")
    if vin_ecu:
        same = vin_doc and vin_ecu.upper() == vin_doc.upper()
        vin_line += (f'  <font color="#12A150">● совпадает с ЭБУ</font>' if same else
                     f'  <font color="#B7791F">ЭБУ: {esc(vin_ecu)}</font>')
    info = [("VIN", vin_line), ("Кузов", esc(vehicle.get("body"))), ("Гос. номер", esc(vehicle.get("plate"))),
            ("Двигатель", esc(vehicle.get("engine"))), ("Производитель", esc(vehicle.get("manufacturer"))),
            ("Протокол", esc(protocol))]
    info = [(k, v) for k, v in info if v not in ("—", "")]
    half = (len(info) + 1) // 2
    rows = []
    for i in range(half):
        l, r = info[i], info[i + half] if i + half < len(info) else ("", "")
        rows.append([P(l[0], "muted"), P(l[1], "bold"), P(r[0], "muted"), P(r[1], "bold")])
    story += [P("Автомобиль", "h2"), _card(rows, [25 * mm, CW / 2 - 25 * mm, 31 * mm, CW / 2 - 31 * mm]), Spacer(1, 6 * mm)]

    # ---- KPI
    codes = scan.get("codes", [])
    mil = scan.get("mil")
    odo = None
    if buyer and buyer.get("odometers"):
        odo = max(o["km"] for o in buyer["odometers"])
    if odo is None and live:
        odo = next((r["value"] for r in live if "одометр" in r["name"].lower()), None)
    since_clear = (buyer or {}).get("readiness", {}).get("km_since_clear")
    if since_clear is None and live:
        since_clear = next((r["value"] for r in live if "после сброса" in r["name"].lower()), None)
    kpis = [
        (str(len(codes)), "кодов неисправностей", BAD if any(c["severity"] == "high" for c in codes) else (WARN if codes else OK)),
        ("Горит" if mil else ("Не горит" if mil is not None else "—"), "лампа Check Engine", BAD if mil else OK),
        (km(odo) if odo is not None else "—", "пробег по блокам", INK),
        (km(since_clear) if since_clear is not None else "—", "после сброса ошибок",
         WARN if since_clear is not None and since_clear < 200 else INK),
    ]
    cells = [[Paragraph(f'<font color="{c.hexval().replace("0x", "#")}">{esc(v)}</font>', S["kpi"]), P(l, "kpil")]
             for v, l, c in kpis]
    kt = Table([[Table([[a], [b]], colWidths=[CW / 4 - 6 * mm]) for a, b in cells]], colWidths=[CW / 4] * 4)
    kt.setStyle(TableStyle([("BACKGROUND", (i, 0), (i, 0), SOFT) for i in range(4)] +
                           [("ROUNDEDCORNERS", [8, 8, 8, 8]), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                            ("LINEAFTER", (0, 0), (2, 0), 4, colors.white)]))
    story += [kt, Spacer(1, 6 * mm)]

    # ---- итог
    warnings = list((buyer or {}).get("warnings", []))
    high = [c for c in codes if c["severity"] == "high"]
    if high:
        verdict, fg, bg = "Есть серьёзные неисправности — нужен ремонт до покупки/эксплуатации.", BAD, BAD_BG
    elif codes or warnings:
        verdict, fg, bg = "Есть замечания — посмотрите детали ниже.", WARN, WARN_BG
    else:
        verdict, fg, bg = "Неисправностей не найдено.", OK, OK_BG
    vt = [[Paragraph(f"<b>Итог:</b> {esc(verdict)}", _st("v", "SB", 10.5, fg))]]
    for w in warnings:
        vt.append([Paragraph("● " + esc(w), _st("vw", "R", 9, INK))])
    story += [_card(vt, [CW], bg=bg, pad=9), Spacer(1, 7 * mm)]

    # ---- ошибки
    story.append(P("Коды неисправностей", "h2"))
    if not codes:
        story.append(_card([[P("Ошибок в памяти блоков нет.", "bold")]], [CW], bg=OK_BG))
    else:
        rows = [[P("Код", "small"), P("Описание", "small"), P("Тип / блок", "small"), P("Важность", "small")]]
        for c in codes:
            desc = f"<b>{esc(c['description'])}</b><br/><font color='#7A809B' size='8'>{esc(c['system'])}</font>"
            if c.get("hint"):
                desc += f"<br/><font color='#4B5170' size='8'>Совет: {esc(c['hint'])}</font>"
            lab, f, b = SEV[c["severity"]]
            rows.append([P(esc(c["code"]), "code"), Paragraph(desc, S["body"]),
                         Paragraph(f"{esc(c['type'])}<br/><font color='#7A809B' size='8'>{esc(c['ecu'])}</font>", S["body"]),
                         _pill(lab, f, b)])
        t = Table(rows, colWidths=[20 * mm, CW - 20 * mm - 40 * mm - 26 * mm, 40 * mm, 26 * mm], repeatRows=1)
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.6, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                               ("LEFTPADDING", (0, 0), (-1, -1), 4)]))
        story.append(t)
    story.append(Spacer(1, 7 * mm))

    # ---- проверка при покупке
    if buyer:
        r = buyer.get("readiness", {})
        groups = buyer.get("odometer_groups") or [{"km": o["km"], "sources": [o["source"]]} for o in buyer.get("odometers", [])]
        if groups:
            rows = [[P(km(g["km"]), "code"),
                     P(("Совпадает в блоках: " if len(g["sources"]) > 1 else "") + esc(", ".join(g["sources"])), "body")]
                    for g in groups]
            note = "Одинаковый пробег во всех блоках — хороший признак." if len(groups) == 1 and len(groups[0]["sources"]) > 1 \
                else "Сверьте с одометром на приборной панели."
            story.append(KeepTogether([P("Пробег по данным блоков", "h2"), _card(rows, [32 * mm, CW - 32 * mm]),
                                       Spacer(1, 2), P(note, "small"), Spacer(1, 6 * mm)]))
        mons = r.get("monitors", [])
        if mons:
            chips, row = [], []
            for m in mons:
                fg, bg = (OK, OK_BG) if m["ready"] else (WARN, WARN_BG)
                row.append(_card([[Paragraph(("● " if m["ready"] else "○ ") + esc(m["name"]) + ("" if m["ready"] else " — не готов"), _st("c", "SB", 8, fg))]],
                                 [CW / 3 - 4 * mm], bg=bg, pad=5))
                if len(row) == 3:
                    chips.append(row)
                    row = []
            if row:
                chips.append(row + [""] * (3 - len(row)))
            ct = Table(chips, colWidths=[CW / 3] * 3)
            ct.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
            facts = []
            for k, label, unit in (("km_since_clear", "Пробег после сброса ошибок", " км"),
                                   ("warmups_since_clear", "Прогревов после сброса", ""),
                                   ("min_since_clear", "Работа после сброса", " мин"),
                                   ("km_with_mil", "Пробег с горящим Check Engine", " км"), ("obd_std", "Стандарт OBD", "")):
                if r.get(k) is not None:
                    facts.append(f"{label}: <b>{esc(r[k])}{unit}</b>")
            story.append(KeepTogether([P("Готовность систем самодиагностики", "h2"), ct,
                                       P(" · ".join(facts), "muted"), Spacer(1, 6 * mm)]))
        bdtcs = buyer.get("block_dtcs", [])
        if bdtcs:
            act = sum(1 for d in bdtcs if d.get("active"))
            rows = [[P("Код", "small"), P("Блок", "small"), P("Расшифровка", "small"), P("Статус", "small")]]
            for d in bdtcs:
                lab = "high" if d.get("active") else ("medium" if d.get("stored") else "low")
                f, bg = SEV[lab][1], SEV[lab][2]
                desc = esc(d.get("description"))
                if d.get("hint"):
                    desc += f"<br/><font color='#7A809B' size='8'>{esc(d['hint'])}</font>"
                rows.append([P(esc(d.get("full")), "code"), P(esc(d.get("ecu")), "body"),
                             Paragraph(desc, S["body"]), _pill(d.get("status", ""), f, bg)])
            t = Table(rows, colWidths=[24 * mm, 40 * mm, CW - 24 * mm - 40 * mm - 26 * mm, 26 * mm], repeatRows=1)
            t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.6, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 4)]))
            story += [P(f"Ошибки по блокам ({len(bdtcs)}" + (f", активных {act}" if act else "") + ")", "h2"), t,
                      P("Коды производителя могут быть без описания.", "small"), Spacer(1, 7 * mm)]
        ecus = buyer.get("ecus", [])
        if ecus:
            vin_doc_u = vin_doc.upper()
            rows = [[P("Блок", "small"), P("VIN в блоке", "small"), P("Номер / серийный", "small"), P("Пробег", "small")]]
            for e in ecus:
                v = e.get("vin") or ""
                if len(v) == 17 and vin_doc_u:
                    vtxt = (f"<font color='#12A150'>{esc(v)}</font>" if v.upper() == vin_doc_u
                            else f"<font color='#E5484D'>{esc(v)} (другой!)</font>")
                else:
                    vtxt = esc(v or "—")
                part = " · ".join(x for x in (e.get("part_number"), e.get("software"), e.get("serial") and "s/n " + e["serial"]) if x)
                kmv = e.get("odometer_km")
                rows.append([P(esc(e["name"]), "bold"), P(vtxt, "body"), P(esc(part or "—"), "muted"),
                             P(km(kmv) if kmv else "—", "body")])
            t = Table(rows, colWidths=[42 * mm, 50 * mm, CW - 42 * mm - 50 * mm - 22 * mm, 22 * mm], repeatRows=1)
            t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.6, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 4)]))
            story += [P(f"Электронные блоки ({len(ecus)})", "h2"), t, Spacer(1, 7 * mm)]

    # ---- живые параметры
    if live:
        keep = [r for r in live if not any(w in r["name"].lower() for w in ("время работы",))][:12]
        cells = [Paragraph(f"<font size='8' color='#7A809B'>{esc(r['name'])}</font><br/><b>{esc(r['value'])}</b> "
                           f"<font size='8' color='#7A809B'>{esc(r['unit'])}</font>", S["body"]) for r in keep]
        rows = [cells[i:i + 4] + [""] * (4 - len(cells[i:i + 4])) for i in range(0, len(cells), 4)]
        t = Table(rows, colWidths=[CW / 4] * 4)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT), ("ROUNDEDCORNERS", [8, 8, 8, 8]),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEAFTER", (0, 0), (2, -1), 3, colors.white), ("LINEBELOW", (0, 0), (-1, -2), 3, colors.white),
                               ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        story.append(KeepTogether([P("Параметры двигателя на момент проверки", "h2"), t, Spacer(1, 7 * mm)]))

    # ---- подпись
    insp = brand.get("inspector") or "____________________"
    sig = Table([[P("Диагностику провёл", "muted"), P("Подпись", "muted"), P("Дата", "muted")],
                 [P(esc(insp), "bold"), P("____________________", "body"), P(scan_time.strftime("%d.%m.%Y"), "bold")]],
                colWidths=[CW * 0.45, CW * 0.33, CW * 0.22])
    sig.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 1), (-1, 1), 6)]))
    story += [KeepTogether([sig, Spacer(1, 5 * mm),
                            P("Отчёт составлен по данным электронных блоков (OBD-II/UDS) и не заменяет полную техническую "
                              "экспертизу: состояние кузова, подвески и механики проверяется отдельно.", "small")])]

    doc.build(story, onFirstPage=first_page, onLaterPages=later_pages)
    return buf.getvalue()
