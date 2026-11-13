"""
stamp_students.py  (v4)
-----------------------
بياخد مذكرة PDF اتختمت قبل كده (اسم + باركود لطالب) وملف الطلاب،
ويطلع PDF لكل طالب: يمسح الاسم والباركود القديم ويحط الجديد
بنفس الخط (Andalus 40) واللون (#808080) والمكان والـ QR (شفافية 20%).

التثبيت:
    pip install pypdf qrcode pillow uharfbuzz fonttools opencv-python
    (openpyxl لو هتستخدم ملف Excel)

الاستخدام:
    python stamp_students.py master.pdf students.csv out_folder [--first]
    python stamp_students.py master.pdf students.xlsx out_folder

students.csv (UTF-8) - الأعمدة: name,code  (أو الاسم,الكود):
    name,code
    طارق يسري احمد محمد حافظ,31323004

ملاحظات التصميم:
  * الاسم بيتشكّل بـ HarfBuzz (نفس قواعد GSUB اللي Word بيستخدمها) وبيترسم
    كـ outlines من خط Andalus نفسه -> شكل مطابق، ومفيش مشاكل تضمين خط.
  * الملف الأصلي بيتنضف مرة واحدة بس (template)، وكل طالب بيتغير له
    object واحد مشترك (Form XObject) بيتعرض على كل الصفحات -> سريع وحجم صغير.
"""

import csv
import io
import os
import re
import sys
import time

import qrcode
import uharfbuzz as hb
from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                           FloatObject, NameObject, NumberObject)

for _s in (sys.stdout, sys.stderr):   # كونسول ويندوز مش بيطبع عربي من غير كده
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# ====== القيم دي مقروءة من جوه الـ PDF نفسه (مش تقدير) ======
# الخط: Andalus. بندوّر في متغير البيئة ثم فولدر fonts/ ثم خطوط ويندوز
_BUNDLED_FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "andlso.ttf")
FONT_PATH = os.environ.get("STAMP_FONT") or (
    _BUNDLED_FONT if os.path.exists(_BUNDLED_FONT) else r"C:\Windows\Fonts\andlso.ttf"
)

NAME_FONT_SIZE = 40          # حجم الخط
NAME_GRAY = 0.50196          # #808080 معتم (بدون شفافية)
NAME_BASELINE_X = 50         # مكان خط الكتابة من الشمال (نقطة)
NAME_CENTER_Y = 400          # مركز الاسم رأسيا (نقطة)
# الأصلي متشكّل من غير kerning: عرض "طارق يسري احمد محمد حافظ" فيه 368.96pt،
# HarfBuzz من غير kern بيدي 369.3pt (ومع kern 343.2pt) -> نفس الحروف ونفس العرض
HB_FEATURES = {"kern": False}

QR_ALPHA = 0.2               # شفافية الباركود
QR_SIZE = 145                # ضلع صورة الباركود (نقطة)
QR_POSITIONS = [(430, 650),  # فوق يمين (جزء منه بره الصفحة زي الأصلي)
                (50, 50)]    # تحت شمال
QR_PX, QR_INNER, QR_MARGIN = 290, 271, 6   # أبعاد صورة الباركود الأصلية بالبكسل
# ==============================================================

# بداية الجزء المختوم في محتوى كل صفحة (الاسم + الباركود): كله بيتشال
MARK = re.compile(rb"q\s+0 1 -1 0 [\d.\-]+ [\d.\-]+ cm\s+BT")
OLD_RES = {"/XObject": ("/img0", "/img1", "/img2", "/img3"),
           "/Font": ("/F2",),
           "/ExtGState": ("/GS1",)}
STAMP_NAME = NameObject("/StStamp")

# الإيميل الجامعي اللي في راس الصفحة: (UG_31337215@science.tanta.edu.eg)
# الرقم اللي فيه بيتبدل بنفس كود الباركود، والباقي زي ما هو (نفس الخط والمكان)
EMAIL = re.compile(rb"(\([A-Za-z]+_)(\d+)(@[^()]*\))")
CODE_SLOT = b"#ST_CODE#"


# ---------------------------------------------------------------- الاسم
class _PdfPen(BasePen):
    """بيحوّل outline الحرف لأوامر PDF path (وحدات الخط، مع إزاحة dx,dy)."""

    def __init__(self, glyphset, out, dx, dy):
        super().__init__(glyphset)
        self.out, self.dx, self.dy = out, dx, dy

    def _p(self, pt):
        return f"{pt[0] + self.dx:g} {pt[1] + self.dy:g}"

    def _moveTo(self, p):
        self.out.append(self._p(p) + " m")

    def _lineTo(self, p):
        self.out.append(self._p(p) + " l")

    def _curveToOne(self, a, b, c):
        self.out.append(f"{self._p(a)} {self._p(b)} {self._p(c)} c")

    def _closePath(self):
        self.out.append("h")


class NameRenderer:
    def __init__(self, path):
        if not os.path.exists(path):
            raise SystemExit(
                f"خط Andalus مش موجود: {path}\n"
                "ثبّته من Settings > Apps > Optional features > Arabic Supplemental Fonts\n"
                "أو حدد مكانه بمتغير البيئة STAMP_FONT")
        self.tt = TTFont(path)
        self.upem = self.tt["head"].unitsPerEm
        self.order = self.tt.getGlyphOrder()
        self.glyphset = self.tt.getGlyphSet()
        self.hbfont = hb.Font(hb.Face(hb.Blob.from_file_path(path)))

    def shape(self, text):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hbfont, buf, HB_FEATURES)
        return buf.glyph_infos, buf.glyph_positions

    def width_pt(self, text, size=NAME_FONT_SIZE):
        _, pos = self.shape(text)
        return sum(p.x_advance for p in pos) * size / self.upem

    def ops(self, text):
        """أوامر PDF ترسم الاسم: رأسي، متوسّط حوالين (X,Y)، رمادي معتم."""
        if re.search(r"[A-Za-z0-9\u0660-\u0669]", text):
            print(f"  تنبيه: الاسم فيه أرقام/حروف لاتيني، اتأكد من شكله: {text}")
        infos, pos = self.shape(text)
        out, x = [], 0
        for info, p in zip(infos, pos):
            ch = text[info.cluster]
            if info.codepoint == 0 and not ch.isspace():
                raise SystemExit(f"الخط مفيهوش الحرف '{ch}' - اسم: {text}")
            g = self.order[info.codepoint]
            n0 = len(out)
            self.glyphset[g].draw(_PdfPen(self.glyphset, out, x + p.x_offset, p.y_offset))
            if len(out) > n0:
                out.append("f")
            x += p.x_advance
        s = NAME_FONT_SIZE / self.upem
        return (f"q {NAME_GRAY:g} g 0 1 -1 0 {NAME_BASELINE_X:g} {NAME_CENTER_Y:g} cm "
                f"{s:.6f} 0 0 {s:.6f} {-x * s / 2:.3f} 0 cm\n" + "\n".join(out) + "\nQ\n")


# ---------------------------------------------------------------- الباركود
def qr_matrix(code):
    """نفس باركود المذكرة: Version 1 + تصحيح L + Mask 6 (بيكبر لوحده لو الكود طويل)."""
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L,
                       border=0, mask_pattern=6)
    qr.add_data(code)
    qr.make(fit=True)
    return qr.get_matrix()


def qr_pixels(code):
    """صورة رمادي 290x290 (bytes) بنفس توزيع الأصلي: الباركود 271px وهامش 6px."""
    m = qr_matrix(code)
    n = len(m)
    rows = []
    for py in range(QR_PX):
        y = py - QR_MARGIN
        row = bytearray(b"\xff" * QR_PX)
        if 0 <= y < QR_INNER:
            mr = m[y * n // QR_INNER]
            for px in range(QR_MARGIN, QR_MARGIN + QR_INNER):
                if mr[(px - QR_MARGIN) * n // QR_INNER]:
                    row[px] = 0
        rows.append(bytes(row))
    return b"".join(rows)


def verify_qr(code, pixels):
    """يقرا الباركود اللي اتعمل بـ OpenCV ويتأكد إنه = الكود (لو OpenCV متسطب)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    img = np.frombuffer(pixels, np.uint8).reshape(QR_PX, QR_PX)
    img = cv2.copyMakeBorder(img, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
    txt, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
    if txt != code:
        raise SystemExit(f"الباركود اللي اتعمل مش بيتقري صح: '{txt}' بدل '{code}'")
    return True


# ---------------------------------------------------------------- القالب
def _stream(writer, data, extra=None):
    s = DecodedStreamObject()
    s.set_data(data)
    if extra:
        s.update(extra)
    return writer._add_object(s.flate_encode())


def build_template(master):
    """ينضف الملف الأصلي مرة واحدة: يشيل الختم القديم ويحط مكانه نداء لـ /StStamp.
    يقبل مسار ملف أو BytesIO / bytes."""
    src = io.BytesIO(master) if isinstance(master, (bytes, bytearray)) else master
    writer = PdfWriter(clone_from=PdfReader(src))

    qr_ref = _stream(writer, b"\xff" * QR_PX * QR_PX, {
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(QR_PX),
        NameObject("/Height"): NumberObject(QR_PX),
        NameObject("/ColorSpace"): NameObject("/DeviceGray"),
        NameObject("/BitsPerComponent"): NumberObject(8)})
    gs = DictionaryObject({NameObject("/Type"): NameObject("/ExtGState"),
                           NameObject("/ca"): FloatObject(QR_ALPHA),
                           NameObject("/CA"): FloatObject(QR_ALPHA)})
    form_ref = _stream(writer, b"", {
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject([NumberObject(v) for v in (-2000, -2000, 4000, 4000)]),
        NameObject("/Resources"): DictionaryObject({
            NameObject("/XObject"): DictionaryObject({NameObject("/QR"): qr_ref}),
            NameObject("/ExtGState"): DictionaryObject({NameObject("/GSqr"): gs})})})
    call_ref = _stream(writer, b"q /StStamp Do Q\n")

    missing, emails = [], 0
    for i, page in enumerate(writer.pages, 1):
        data = page.get_contents().get_data()
        m = MARK.search(data)
        if m:
            data = data[: m.start()]
        else:
            missing.append(i)
        data, n_mail = EMAIL.subn(lambda e: e.group(1) + CODE_SLOT + e.group(3), data)
        emails += n_mail
        page[NameObject("/Contents")] = ArrayObject([_stream(writer, data + b"\n"), call_ref])

        res = page.get("/Resources")
        if res is None:
            res = DictionaryObject()
            page[NameObject("/Resources")] = res
        res = res.get_object()
        if m:
            for group, names in OLD_RES.items():
                d = res.get(group)
                if d is not None:
                    d = d.get_object()
                    for n in names:
                        d.pop(n, None)
        xo = res.get("/XObject")
        if xo is None:
            xo = DictionaryObject()
            res[NameObject("/XObject")] = xo
        xo.get_object()[STAMP_NAME] = form_ref

    if missing:
        print(f"  تنبيه: {len(missing)} صفحة مفيهاش ختم قديم بالشكل المتوقع "
              f"(الختم الجديد هيتحط فوقها بس): {missing[:20]}")
    if emails:
        print(f"  لقينا {emails} إيميل جامعي - الكود اللي فيهم هيتغير لكل طالب")
    try:
        writer.compress_identical_objects(remove_identicals=False, remove_orphans=True)
    except Exception as e:  # noqa: BLE001
        print(f"  (تجاهل تنظيف الـ objects: {e})")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- لكل طالب
def stamp_pdf_bytes(template, renderer, name, code):
    """يختم القالب ويرجّع الـ PDF كـ bytes في الذاكرة (مناسب للسيرفر وVercel)."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(template)))
    form = writer.pages[0]["/Resources"]["/XObject"][STAMP_NAME].get_object()
    qr = form["/Resources"]["/XObject"]["/QR"].get_object()

    pixels = qr_pixels(code)
    verify_qr(code, pixels)
    qr.set_data(pixels)

    content = renderer.ops(name)
    for x, y in QR_POSITIONS:
        content += f"q /GSqr gs {QR_SIZE} 0 0 {QR_SIZE} {x} {y} cm /QR Do Q\n"
    form.set_data(content.encode("ascii"))

    # الإيميل: نفس كود الباركود (مع escape لأي أقواس أو \ في الكود)
    pdf_code = (code.encode("latin-1", "replace")
                .replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)"))
    for page in writer.pages:
        contents = page.get("/Contents")
        if not isinstance(contents.get_object(), ArrayObject):
            continue
        s = contents.get_object()[0].get_object()
        data = s.get_data()
        if CODE_SLOT in data:
            s.set_data(data.replace(CODE_SLOT, pdf_code))

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def stamp_pdf(template, renderer, name, code, out_path):
    data = stamp_pdf_bytes(template, renderer, name, code)
    with open(out_path, "wb") as f:
        f.write(data)


# ---------------------------------------------------------------- الطلاب
NAME_KEYS = ("name", "الاسم", "اسم", "اسم الطالب")
CODE_KEYS = ("code", "الكود", "كود", "الرقم", "barcode", "qr")


def read_students(path):
    if path.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        ws = load_workbook(path, read_only=True, data_only=True).active
        rows = [["" if v is None else str(v) for v in r] for r in ws.iter_rows(values_only=True)]
    else:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise SystemExit("ملف الطلاب فاضي")
    head = [c.strip().lower() for c in rows[0]]
    ni = next((head.index(k) for k in NAME_KEYS if k in head), None)
    ci = next((head.index(k) for k in CODE_KEYS if k in head), None)
    if ni is None or ci is None:      # مفيش عناوين: أول عمود اسم وتاني عمود كود
        ni, ci, body = 0, 1, rows
    else:
        body = rows[1:]
    out, seen = [], set()
    for n, r in enumerate(body, 1):
        name = r[ni].strip() if len(r) > ni else ""
        code = r[ci].strip() if len(r) > ci else ""
        if code.endswith(".0"):       # Excel بيحوّل الأرقام لـ float
            code = code[:-2]
        if not name or not code:
            print(f"  تنبيه: سطر {n} ناقص اسم أو كود - اتساب")
            continue
        if code in seen:
            print(f"  تنبيه: الكود {code} متكرر")
        seen.add(code)
        out.append((name, code))
    return out


def safe(s):
    return "".join(ch for ch in s if ch not in '\\/:*?"<>|').strip()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 3:
        print(__doc__)
        sys.exit(1)
    master, students_path, out_dir = args[:3]
    os.makedirs(out_dir, exist_ok=True)

    students = read_students(students_path)
    renderer = NameRenderer(FONT_PATH)
    for name, _ in students:          # اتأكد إن كل الأسماء حروفها موجودة قبل ما نبدأ
        renderer.ops(name)

    t0 = time.time()
    print("بنضّف الملف الأصلي...")
    template = build_template(master)
    print(f"  خلص في {time.time() - t0:.1f} ث")

    if "--first" in sys.argv:
        students = students[:1]
    for i, (name, code) in enumerate(students, 1):
        out = os.path.join(out_dir, f"{safe(code)}_{safe(name)}.pdf")
        stamp_pdf(template, renderer, name, code, out)
        print(f"[{i}/{len(students)}] {out}")


if __name__ == "__main__":
    main()
