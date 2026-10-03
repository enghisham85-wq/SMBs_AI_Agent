"""Sample supplier invoices plus ground-truth JSON for the offline extractor."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("C:/Windows/Fonts/tahoma.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
]
AR_DIGITS = str.maketrans("0123456789.", "٠١٢٣٤٥٦٧٨٩٫")
Q = Decimal("0.01")


def font_path() -> Path:
    for p in FONT_CANDIDATES:
        if p.exists():
            return p
    raise FileNotFoundError("no Arabic-capable TrueType font found; install Arial, Tahoma, DejaVu or Noto Sans Arabic")


def shape(text: str) -> str:
    """Reshape Arabic for LTR renderers."""
    import arabic_reshaper
    from bidi.algorithm import get_display

    return str(get_display(arabic_reshaper.reshape(text)))


@dataclass
class Line:
    desc_en: str
    desc_ar: str
    qty: Decimal
    unit: str
    unit_price: Decimal
    vat_rate: Decimal = Decimal("14")

    @property
    def total(self) -> Decimal:
        return (self.qty * self.unit_price).quantize(Q, ROUND_HALF_UP)


@dataclass
class InvoiceSpec:
    name: str
    group: str  # en | bilingual | ar
    supplier_en: str
    supplier_ar: str
    vat_number: str | None
    number: str
    invoice_date: date
    due_days: int
    lines: list[Line]
    fmt: str = "pdf"  # pdf | jpg
    date_printed: str | None = None
    date_format_observed: str = "DMY"
    total_printed: Decimal | None = None
    total_alt_printed: Decimal | None = None  # Arabic total, when it disagrees
    arabic_digits: bool = False
    expected: list[str] = field(default_factory=list)  # checks this sample should trip

    @property
    def subtotal(self) -> Decimal:
        return sum((ln.total for ln in self.lines), Decimal(0))

    @property
    def vat(self) -> Decimal:
        return sum(((ln.total * ln.vat_rate / 100).quantize(Q, ROUND_HALF_UP) for ln in self.lines), Decimal(0))

    @property
    def total(self) -> Decimal:
        return self.total_printed if self.total_printed is not None else self.subtotal + self.vat

    @property
    def due_date(self) -> date:
        return self.invoice_date + timedelta(days=self.due_days)


def _num(v: Decimal | str, arabic: bool) -> str:
    text = f"{Decimal(v):,.2f}" if not isinstance(v, str) else v
    return text.translate(AR_DIGITS).replace(",", "٬") if arabic else text


def sample_specs(base: date) -> list[InvoiceSpec]:
    d = base
    coffee = ("Cairo Coffee Roasters", "محمصة القاهرة للبن", "284615290")
    produce = ("Fresh Farm Produce", "مزارع فريش للخضروات والفاكهة", "402918375")
    dairy = ("Al Noor Dairy", "مزرعة النور للألبان", "310245781")
    pack = ("Gulf Packaging", "الخليج للتغليف", "199273845")
    bakery = ("Golden Bakery", "مخبز الذهبي", "518302944")
    cups = Line("Paper cup 8oz (pack of 50)", "أكواب ورقية ٨ أونصة", Decimal("1000"), "piece", Decimal("2.5"))
    water = Line("Water bottle 600 ml", "مياه ٦٠٠ مل", Decimal("48"), "bottle", Decimal("6"))
    milk40 = Line("Fresh milk", "حليب طازج", Decimal("40"), "L", Decimal("45"))
    return [
        InvoiceSpec("en_coffee", "en", *coffee, "CCR-2026-0412", d - timedelta(days=3), 30,
                    [Line("Coffee beans - house blend", "بن", Decimal("10"), "kg", Decimal("1100")),
                     Line("Tea bags (box of 100)", "شاي", Decimal("200"), "bag", Decimal("1.5"))]),
        InvoiceSpec("en_produce", "en", *produce, "FF-7781", d - timedelta(days=2), 14,
                    [Line("Oranges", "برتقال", Decimal("30"), "kg", Decimal("30"), Decimal("0"))]),
        InvoiceSpec("bi_dairy", "bilingual", *dairy, "AND-10233", d - timedelta(days=2), 14,
                    [milk40, Line("White cheese", "جبنة بيضاء", Decimal("2"), "kg", Decimal("320"))]),
        InvoiceSpec("bi_packaging", "bilingual", *pack, "GP-5521", d - timedelta(days=4), 30, [cups, water]),
        InvoiceSpec("ar_bakery", "ar", *bakery, "GB-3302", d - timedelta(days=1), 7,
                    [Line("Croissant", "كرواسون", Decimal("30"), "piece", Decimal("25")),
                     Line("Chocolate muffin", "مافن شوكولاتة", Decimal("20"), "piece", Decimal("28")),
                     Line("Bread roll", "خبز فينو", Decimal("40"), "piece", Decimal("8"))],
                    fmt="jpg", arabic_digits=True),
        # faulty ones
        InvoiceSpec("ar_bakery_wrong_total", "ar", *bakery, "GB-3310", d - timedelta(days=1), 7,
                    [Line("Croissant", "كرواسون", Decimal("40"), "piece", Decimal("25"))],
                    fmt="jpg", arabic_digits=True, total_printed=Decimal("1150.00"), expected=["extraction_arithmetic"]),
        InvoiceSpec("bi_packaging_duplicate", "bilingual", *pack, "GP-5521", d - timedelta(days=4), 30, [cups, water],
                    fmt="jpg", expected=["duplicate_invoice"]),
        InvoiceSpec("bi_packaging_soft_duplicate", "bilingual", *pack, "GP-5522", d - timedelta(days=4), 30, [cups, water],
                    expected=["duplicate_invoice"]),
        InvoiceSpec("en_produce_ambiguous_date", "en", *produce, "FF-7790", date(base.year, 10, 5), 14,
                    [Line("Oranges", "برتقال", Decimal("20"), "kg", Decimal("30"), Decimal("0"))],
                    date_printed="05/10/" + str(base.year), date_format_observed="ambiguous", expected=["date_sanity"]),
        InvoiceSpec("bi_dairy_total_mismatch", "bilingual", *dairy, "AND-10240", d - timedelta(days=1), 14, [milk40],
                    total_alt_printed=Decimal("2152.00"), expected=["bilingual_conflict"]),
        InvoiceSpec("en_produce_missing_vat_number", "en", produce[0], produce[1], None, "FF-7795", d - timedelta(days=1), 14,
                    [Line("Lemons", "ليمون", Decimal("10"), "kg", Decimal("40"))], expected=["supplier_vat_validity"]),
        InvoiceSpec("bi_dairy_full_qty", "bilingual", *dairy, "AND-10250", d, 14, [milk40], expected=["three_way_match"]),
        InvoiceSpec("bi_packaging_duplicate_wrong_total", "bilingual", *pack, "GP-5521", d - timedelta(days=4), 30,
                    [cups, water], total_printed=Decimal("3200.00"),
                    expected=["duplicate_invoice", "extraction_arithmetic"]),
    ]


def truth(spec: InvoiceSpec) -> dict[str, Any]:
    printed_date = spec.date_printed or spec.invoice_date.strftime("%d/%m/%Y")
    ar = spec.arabic_digits

    def f(value: Any, raw: str | None = None, conf: float = 0.97) -> dict[str, Any]:
        return {"value": value, "raw_text": raw if raw is not None else (str(value) if value is not None else None),
                "confidence": conf}

    primary_name = spec.supplier_ar if spec.group == "ar" else spec.supplier_en
    number_raw = spec.number.translate(AR_DIGITS) if ar else spec.number
    return {
        "language": spec.group if spec.group != "bilingual" else "bilingual",
        "supplier_name": f(primary_name),
        "supplier_name_alt": f(spec.supplier_ar) if spec.group == "bilingual" else None,
        "supplier_vat_number": f(spec.vat_number, _num(spec.vat_number, ar) if spec.vat_number and ar else spec.vat_number)
        if spec.vat_number else None,
        "invoice_number": f(spec.number, number_raw),
        "invoice_date": f(spec.invoice_date.isoformat(), printed_date.translate(AR_DIGITS) if ar else printed_date,
                          0.6 if spec.date_format_observed == "ambiguous" else 0.95),
        "due_date": f(spec.due_date.isoformat(), spec.due_date.strftime("%d/%m/%Y")),
        "currency": f("EGP", "ج.م" if spec.group == "ar" else "EGP"),
        "lines": [{
            "description": f(ln.desc_ar if spec.group == "ar" else ln.desc_en),
            "qty": f(str(ln.qty), _num(str(ln.qty), ar)),
            "unit": f(ln.unit),
            "unit_price": f(str(ln.unit_price), _num(ln.unit_price, ar)),
            "vat_rate_percent": f(str(ln.vat_rate), _num(str(ln.vat_rate), ar)),
            "line_total": f(str(ln.total), _num(ln.total, ar)),
        } for ln in spec.lines],
        "subtotal": f(str(spec.subtotal), _num(spec.subtotal, ar)),
        "vat_amount": f(str(spec.vat), _num(spec.vat, ar)),
        "total": f(str(spec.total), _num(spec.total, ar)),
        "total_alt": f(str(spec.total_alt_printed), _num(spec.total_alt_printed, True)) if spec.total_alt_printed else (
            f(str(spec.total), _num(spec.total, True)) if spec.group == "bilingual" else None),
        "date_format_observed": spec.date_format_observed,
        "notes": [],
        "_expected_checks": spec.expected,
        "_group": spec.group,
    }


def _rows(spec: InvoiceSpec) -> list[tuple[str, str]]:
    ar = spec.arabic_digits
    date_text = spec.date_printed or spec.invoice_date.strftime("%d/%m/%Y")
    total_ar = spec.total_alt_printed if spec.total_alt_printed is not None else spec.total
    rows = [
        (f"TAX INVOICE  {spec.number}", f"فاتورة ضريبية  {spec.number.translate(AR_DIGITS) if ar else spec.number}"),
        (spec.supplier_en, spec.supplier_ar),
        (f"Tax reg. no: {spec.vat_number}" if spec.vat_number else "", f"رقم التسجيل الضريبي: {_num(spec.vat_number, ar)}"
         if spec.vat_number else ""),
        (f"Date: {date_text}", f"التاريخ: {date_text.translate(AR_DIGITS) if ar else date_text}"),
        (f"Due: {spec.due_date.strftime('%d/%m/%Y')}",
         f"الاستحقاق: {spec.due_date.strftime('%d/%m/%Y').translate(AR_DIGITS) if ar else spec.due_date.strftime('%d/%m/%Y')}"),
        ("", ""),
    ]
    for ln in spec.lines:
        rows.append((f"{ln.desc_en}   {ln.qty} {ln.unit} x {ln.unit_price:,.2f} = {ln.total:,.2f}",
                     f"{ln.desc_ar}   {_num(str(ln.qty), ar)} × {_num(ln.unit_price, ar)} = {_num(ln.total, ar)}"))
    rows += [
        ("", ""),
        (f"Subtotal: {spec.subtotal:,.2f}", f"الإجمالي قبل الضريبة: {_num(spec.subtotal, ar)}"),
        (f"VAT 14%: {spec.vat:,.2f}", f"ضريبة القيمة المضافة: {_num(spec.vat, ar)}"),
        (f"TOTAL EGP {spec.total:,.2f}", f"الإجمالي ج.م {_num(total_ar, True if spec.group != 'en' else ar)}"),
    ]
    return rows


def render_pdf(spec: InvoiceSpec, path: Path) -> None:
    from reportlab.lib.pagesizes import A5
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    if "InvFont" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("InvFont", str(font_path())))
    c = canvas.Canvas(str(path), pagesize=A5)
    w, h = A5
    y = h - 40
    for en, ar in _rows(spec):
        if spec.group in ("en", "bilingual") and en:
            c.setFont("InvFont", 9)
            c.drawString(30, y, en)
        if spec.group in ("ar", "bilingual") and ar:
            c.setFont("InvFont", 9)
            c.drawRightString(w - 30, y - (11 if spec.group == "bilingual" else 0), shape(ar))
        y -= 26 if spec.group == "bilingual" else 16
    c.showPage()
    c.save()


def render_jpg(spec: InvoiceSpec, path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    rng = random.Random(spec.name)
    rows = _rows(spec)
    img = Image.new("RGB", (900, 70 + 42 * len(rows) * (2 if spec.group == "bilingual" else 1)), (250, 248, 240))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(font_path()), 26)
    y = 30
    for en, ar in rows:
        if spec.group in ("en", "bilingual") and en:
            draw.text((30, y), en, font=font, fill=(30, 30, 30))
            y += 40 if spec.group == "bilingual" else 0
        if spec.group in ("ar", "bilingual") and ar:
            text = shape(ar)
            tw = draw.textlength(text, font=font)
            draw.text((870 - tw, y), text, font=font, fill=(30, 30, 30))
        y += 42
    for _ in range(1500):  # paper noise
        x, yy = rng.randrange(img.width), rng.randrange(img.height)
        img.putpixel((x, yy), (rng.randrange(200, 240),) * 3)
    img = img.rotate(rng.uniform(-2, 2), expand=True, fillcolor=(90, 90, 90)).filter(ImageFilter.GaussianBlur(0.6))
    img.save(path, "JPEG", quality=85)


def generate(out_dir: Path, base: date) -> list[dict[str, Any]]:
    """Render every sample invoice into out_dir and write index.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    index: dict[str, str] = {}
    files: list[dict[str, Any]] = []
    for spec in sample_specs(base):
        path = out_dir / f"{spec.name}.{spec.fmt}"
        (render_pdf if spec.fmt == "pdf" else render_jpg)(spec, path)  # dates depend on `base`
        t = truth(spec)
        (out_dir / f"{spec.name}.truth.json").write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        index[sha] = f"{spec.name}.truth.json"
        files.append({"name": spec.name, "file": path.name, "group": spec.group, "sha256": sha, "expected": spec.expected,
                      "mime": "application/pdf" if spec.fmt == "pdf" else "image/jpeg"})
    (out_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    (out_dir / "files.json").write_text(json.dumps(files, indent=2), encoding="utf-8")
    return files


def lookup_truth(dirs: list[Path], sha256: str) -> dict[str, Any] | None:
    for d in dirs:
        idx = d / "index.json"
        if idx.exists():
            name = json.loads(idx.read_text(encoding="utf-8")).get(sha256)
            if name and (d / name).exists():
                return json.loads((d / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    return None
