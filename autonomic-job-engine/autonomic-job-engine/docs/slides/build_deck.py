"""Build the AJE slide deck (docs/slides/aje-deck.pptx).

    uv run --no-project --with python-pptx python docs/slides/build_deck.py

Diagrams are drawn as native, editable PowerPoint shapes in the style of Mermaid
flowcharts; their Mermaid sources live in docs/slides/diagrams/*.mmd.
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.dml import MSO_LINE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

OUT = Path(__file__).with_name("aje-deck.pptx")

# ── palette ────────────────────────────────────────────────────────────────
NAVY = "0B1F33"     # dark slides, code blocks
INK = "1E293B"      # body text
MUTED = "5B6B7F"    # captions
TEAL = "0D9488"     # primary accent: the autonomic / control side
TEAL_L = "CCFBF1"
TEAL_D = "115E59"
AMBER = "B45309"    # secondary accent: Kubernetes / workers
AMBER_L = "FEF3C7"
SLATE_L = "F1F5F9"  # neutral node fill
LINE = "CBD5E1"
WHITE = "FFFFFF"
CODE_FG = "E2E8F0"
CODE_DIM = "94A3B8"
CODE_KEY = "5EEAD4"

HEAD = "Calibri"
BODY = "Calibri"
MONO = "Consolas"

W, H = 13.333, 7.5


def rgb(h: str) -> RGBColor:
    return RGBColor.from_string(h)


prs = Presentation()
prs.slide_width = Inches(W)
prs.slide_height = Inches(H)
BLANK = prs.slide_layouts[6]
slide_no = 0


# ── primitives ─────────────────────────────────────────────────────────────
def new_slide(bg: str = WHITE, footer: bool = True):
    global slide_no
    slide_no += 1
    s = prs.slides.add_slide(BLANK)
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = rgb(bg)
    if footer:
        text(s, W - 4.6, H - 0.45, 4.0, 0.3,
             [{"runs": [("Autonomic Job Engine   ", {}), (str(slide_no), {"bold": True})]}],
             size=10, color=MUTED, align=PP_ALIGN.RIGHT)
    return s


def _apply_run(run, size, color, bold, italic, font):
    f = run.font
    f.size = Pt(size)
    f.color.rgb = rgb(color)
    f.bold = bold
    f.italic = italic
    f.name = font


def text(s, x, y, w, h, paras, size=16, color=INK, bold=False, italic=False, font=BODY,
         align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, margin=0.0, space_after=0, name=None):
    """paras: list of str or dict(text|runs, size, color, bold, italic, font, bullet, space_after, align)."""
    tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    if name:
        tb.name = name
    fill_frame(tb.text_frame, paras, size, color, bold, italic, font, align, anchor, margin, space_after)
    return tb


def fill_frame(tf, paras, size=16, color=INK, bold=False, italic=False, font=BODY,
               align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, margin=0.0, space_after=0):
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    for side in ("left", "right", "top", "bottom"):
        setattr(tf, f"margin_{side}", Inches(margin))
    if isinstance(paras, str):
        paras = [paras]
    for i, p in enumerate(paras):
        if isinstance(p, str):
            p = {"text": p}
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.alignment = p.get("align", align)
        sa = p.get("space_after", space_after)
        if sa:
            para.space_after = Pt(sa)
        psize = p.get("size", size)
        runs = p.get("runs") or [(p.get("text", ""), {})]
        for t, o in runs:
            r = para.add_run()
            r.text = t
            _apply_run(r, o.get("size", psize), o.get("color", p.get("color", color)),
                       o.get("bold", p.get("bold", bold)), o.get("italic", p.get("italic", italic)),
                       o.get("font", p.get("font", font)))
        if p.get("bullet"):
            pPr = para._p.get_or_add_pPr()
            indent = Inches(0.28)
            pPr.set("marL", str(indent))
            pPr.set("indent", str(-indent))
            buClr = pPr.makeelement(qn("a:buClr"), {})
            clr = buClr.makeelement(qn("a:srgbClr"), {"val": p.get("bullet_color", TEAL)})
            buClr.append(clr)
            pPr.append(buClr)
            pPr.append(pPr.makeelement(qn("a:buFont"), {"typeface": "Arial"}))
            pPr.append(pPr.makeelement(qn("a:buChar"), {"char": "•"}))
    return tf


def box(s, x, y, w, h, fill=SLATE_L, line=None, radius=0.12, shape=MSO_SHAPE.ROUNDED_RECTANGLE,
        line_w=1.25, dash=False, name=None, shadow=False):
    sh = s.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if name:
        sh.name = name
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        sh.adjustments[0] = radius
    if fill is None:
        sh.fill.background()
    else:
        sh.fill.solid()
        sh.fill.fore_color.rgb = rgb(fill)
    if line is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = rgb(line)
        sh.line.width = Pt(line_w)
        if dash:
            sh.line.dash_style = MSO_LINE.DASH
    if not shadow:
        sh.shadow.inherit = False
    sh.text_frame.text = ""
    return sh


NODE_STYLES = {
    "neutral": (SLATE_L, "94A3B8", INK),
    "teal": (TEAL_L, TEAL, TEAL_D),
    "amber": (AMBER_L, "D97706", "78350F"),
    "dark": (NAVY, NAVY, WHITE),
    "white": (WHITE, "94A3B8", INK),
}


def node(s, x, y, w, h, title, sub=None, kind="neutral", size=14, sub_size=12, name=None):
    """A Mermaid-style flowchart node: rounded box with a bold title and an optional subtitle."""
    fill, line, fg = NODE_STYLES[kind]
    sh = box(s, x, y, w, h, fill=fill, line=line, radius=0.18, name=name or f"node {title}")
    paras = [{"text": title, "bold": True, "size": size, "color": fg, "align": PP_ALIGN.CENTER}]
    if sub:
        for line_ in sub.split("\n"):
            paras.append({"text": line_, "size": sub_size, "color": fg if kind == "dark" else MUTED,
                          "align": PP_ALIGN.CENTER})
    fill_frame(sh.text_frame, paras, anchor=MSO_ANCHOR.MIDDLE, margin=0.06)
    return sh


def arrow(s, x1, y1, x2, y2, color="64748B", width=1.5, dashed=False, both=False, label=None,
          label_dx=0.0, label_dy=-0.3, label_w=1.6):
    c = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    c.line.color.rgb = rgb(color)
    c.line.width = Pt(width)
    if dashed:
        c.line.dash_style = MSO_LINE.DASH
    ln = c.line._get_or_add_ln()
    if both:
        ln.append(ln.makeelement(qn("a:headEnd"), {"type": "triangle", "w": "med", "len": "med"}))
    ln.append(ln.makeelement(qn("a:tailEnd"), {"type": "triangle", "w": "med", "len": "med"}))
    if label:
        mx, my = (x1 + x2) / 2 + label_dx, (y1 + y2) / 2 + label_dy
        text(s, mx - label_w / 2, my, label_w, 0.28, label, size=11, color=MUTED, italic=True,
             align=PP_ALIGN.CENTER)
    return c


def circle(s, x, y, d, label, fill=TEAL, fg=WHITE, size=16):
    sh = box(s, x, y, d, d, fill=fill, shape=MSO_SHAPE.OVAL, name=f"badge {label}")
    fill_frame(sh.text_frame, [{"text": label, "bold": True, "align": PP_ALIGN.CENTER}],
               size=size, color=fg, anchor=MSO_ANCHOR.MIDDLE)
    return sh


def title(s, t, kicker=None, dark=False):
    if kicker:
        text(s, 0.6, 0.38, 12.1, 0.35, kicker.upper(), size=12, bold=True,
             color=CODE_KEY if dark else TEAL, name="kicker")
    text(s, 0.6, 0.68, 12.1, 0.8, t, size=34, bold=True, font=HEAD,
         color=WHITE if dark else NAVY, name="title")


def code(s, x, y, w, h, lines, size=12):
    sh = box(s, x, y, w, h, fill=NAVY, radius=0.04, name="code")
    paras = []
    for ln_ in lines:
        stripped = ln_.lstrip()
        if stripped.startswith(("#", "//")):
            paras.append({"text": ln_, "color": CODE_DIM})
        elif "#" in ln_ and not stripped.startswith('"'):
            body, comment = ln_.split("#", 1)
            paras.append({"runs": [(body, {}), ("#" + comment, {"color": CODE_DIM})]})
        else:
            paras.append({"text": ln_})
    fill_frame(sh.text_frame, paras, size=size, color=CODE_FG, font=MONO, margin=0.22)
    return sh


def notes(s, t):
    s.notes_slide.notes_text_frame.text = t


def divider(num, t, sub):
    s = new_slide(NAVY, footer=False)
    text(s, 0.9, 2.1, 3.0, 1.2, num, size=72, bold=True, color=CODE_KEY, font=HEAD)
    text(s, 0.9, 3.35, 11.5, 1.0, t, size=42, bold=True, color=WHITE, font=HEAD)
    text(s, 0.9, 4.4, 10.5, 1.0, sub, size=18, color="CBD5E1")
    return s


# ═══ 1. Title ═══════════════════════════════════════════════════════════════
s = new_slide(NAVY, footer=False)
text(s, 0.9, 1.0, 11, 0.4, "AUTONOMIC JOB ENGINE", size=14, bold=True, color=CODE_KEY)
text(s, 0.9, 1.6, 11.5, 2.0, ["Self-managing parallel jobs", "on Kubernetes"], size=50, bold=True,
     color=WHITE, font=HEAD)
text(s, 0.9, 3.75, 10.5, 0.9,
     "Algorithmic skeletons + rule contracts + one autonomic manager per job: "
     "you declare how a job should behave, the engine keeps it there.",
     size=18, color="CBD5E1")
# mini skeleton motif: emitter → workers → collector
mx, my = 0.9, 5.25
node(s, mx, my + 0.35, 1.5, 0.6, "tasks", kind="teal", size=13)
for i in range(3):
    node(s, mx + 2.3, my + i * 0.62, 1.3, 0.48, f"pod {i + 1}", kind="amber", size=12)
    arrow(s, mx + 1.5, my + 0.65, mx + 2.3, my + i * 0.62 + 0.24, color="64748B")
    arrow(s, mx + 3.6, my + i * 0.62 + 0.24, mx + 4.4, my + 0.65, color="64748B")
node(s, mx + 4.4, my + 0.35, 1.5, 0.6, "results", kind="teal", size=13)
text(s, 7.6, 5.55, 5.0, 1.0,
     ["Kubernetes Jobs · farm & data-parallel skeletons", "Jobs console & Fractal Lab demo"],
     size=15, color="CBD5E1", align=PP_ALIGN.RIGHT)
text(s, 7.6, 6.55, 5.0, 0.4, "Giorgio Zoppi · November 2026", size=13, color=CODE_DIM,
     align=PP_ALIGN.RIGHT)
notes(s, "AJE submits, controls and monitors self-managing jobs on Kubernetes. Today: the idea, "
         "the two skeletons, how they map to Kubernetes Jobs, and the UI that drives it.")

# ═══ 2. The idea ════════════════════════════════════════════════════════════
s = new_slide()
title(s, "Jobs that manage themselves", "The idea")
text(s, 0.6, 1.55, 11.8, 0.8,
     "You give AJE what to compute and the rules for how it should run. "
     "A manager per job keeps it on contract while it runs, so nobody hand-tunes parallelism.",
     size=18, color=INK)
cards = [
    ("1", "Pattern", "An algorithmic skeleton: farm or data_parallel. A fixed dispatch / collect "
                     "structure with a known set of non-functional knobs.", '"pattern": "farm"'),
    ("2", "Contract", "Drools-like rules over live facts (Job, Metrics): when the backlog grows, "
                      "then scale(). Type-checked before the job is accepted.", "farm-throughput"),
    ("3", "Manager", "One per job. Monitor → Analyse → Plan → Execute every few seconds, acting "
                     "only through the control interface.", "scale · suspend · resume"),
]
for i, (n, h_, body, ex) in enumerate(cards):
    x = 0.6 + i * 4.15
    box(s, x, 2.65, 3.85, 3.45, fill=SLATE_L, radius=0.06, name=f"card {h_}")
    circle(s, x + 0.3, 2.95, 0.6, n)
    text(s, x + 1.05, 3.0, 2.6, 0.5, h_, size=22, bold=True, color=NAVY)
    text(s, x + 0.3, 3.75, 3.3, 1.7, body, size=15, color=INK)
    text(s, x + 0.3, 5.55, 3.3, 0.35, ex, size=13, color=TEAL_D, font=MONO)
text(s, 0.6, 6.4, 12.1, 0.5,
     "Groups add a second level: member jobs share a worker budget and escalate to the group "
     "manager when they can't meet their contract alone.", size=14, italic=True, color=MUTED)
notes(s, "Three ingredients. The pattern fixes the structure, the contract states the goals, and "
         "the manager closes the loop. Only the worker code is functional; everything else is "
         "non-functional and can be changed safely at runtime.")

# ═══ 3. Architecture ════════════════════════════════════════════════════════
s = new_slide()
title(s, "Architecture at a glance", "Design")
node(s, 0.6, 1.7, 2.8, 1.0, "Clients", "Jobs Console · Fractal Lab · curl", kind="white")
node(s, 4.1, 1.7, 3.6, 1.0, "AJE API (FastAPI)", "REST · SSE · Prometheus", kind="teal")
arrow(s, 3.4, 2.2, 4.1, 2.2, both=True)
# engine container
box(s, 3.7, 3.15, 4.4, 2.35, fill=None, line=TEAL, dash=True, radius=0.06, name="engine")
text(s, 3.85, 3.2, 3.0, 0.3, "Engine", size=12, bold=True, color=TEAL_D)
node(s, 3.95, 3.55, 3.9, 0.7, "GroupManager", "budget · escalations", kind="teal", size=13, sub_size=11)
node(s, 3.95, 4.5, 3.9, 0.8, "JobManager ×N", "Monitor → Analyse → Plan → Execute", kind="teal",
     size=13, sub_size=11)
arrow(s, 5.9, 2.7, 5.9, 3.15)
node(s, 9.0, 1.7, 3.7, 1.0, "PostgreSQL", "state: jobs · tasks · batches · events", kind="neutral")
node(s, 9.0, 3.6, 3.7, 1.0, "Kubernetes API", "create · patch one batch/v1 Job per job", kind="amber")
arrow(s, 8.1, 3.4, 9.0, 2.45)
arrow(s, 8.1, 4.1, 9.0, 4.1)
node(s, 0.6, 5.95, 3.6, 0.9, "RabbitMQ", "tasks · results · control (drain)", kind="neutral")
node(s, 5.0, 5.95, 7.7, 0.9, "Worker pods  ×workers", "aje_worker SDK · stateless handler f(payload) → result",
     kind="amber")
arrow(s, 10.85, 4.6, 10.85, 5.95, color="D97706", label="pods", label_dx=0.55, label_dy=-0.15,
      label_w=0.8)
arrow(s, 4.2, 6.4, 5.0, 6.4, both=True)
arrow(s, 3.95, 4.9, 2.4, 5.95, both=True, label="dispatch / collect", label_dx=-1.05, label_dy=-0.25)
notes(s, "The API process hosts the engine: one JobManager per job and one GroupManager per group. "
         "State is in PostgreSQL, work travels through RabbitMQ, and workers are Kubernetes Job pods "
         "(or local processes with AJE_BACKEND=local, same semantics).")

# ═══ 4. Divider: skeletons ══════════════════════════════════════════════════
s = divider("01", "Algorithmic skeletons",
            "Reusable shapes of parallel work. The structure is fixed; only non-functional knobs move.")
notes(s, "Skeletons are the classic structured parallel programming idea: farm and data-parallel.")

# ═══ 5. Farm ════════════════════════════════════════════════════════════════
s = new_slide()
title(s, "Farm: a stream of independent tasks", "Skeleton 1")
node(s, 0.6, 3.35, 1.75, 1.0, "Task stream", "POST /tasks", kind="white", size=14)
node(s, 2.85, 3.35, 2.0, 1.0, "Emitter", "aje.<job>.tasks\nprefetch 1 · manual ack", kind="teal",
     size=14, sub_size=11)
arrow(s, 2.35, 3.85, 2.85, 3.85)
wy = [1.75, 2.75, 4.35, 5.35]
labels = ["worker 1", "worker 2", "worker n−1", "worker n"]
for y_, lab in zip(wy, labels):
    node(s, 5.55, y_, 1.5, 0.62, lab, kind="amber", size=13)
    arrow(s, 4.85, 3.85, 5.55, y_ + 0.31)
    arrow(s, 7.05, y_ + 0.31, 7.75, 3.85)
text(s, 5.55, 3.5, 1.5, 0.5, "⋮", size=24, color=MUTED, align=PP_ALIGN.CENTER)
node(s, 7.75, 3.35, 2.0, 1.0, "Collector", "aje.<job>.results", kind="teal", size=14, sub_size=11)
node(s, 7.75, 5.25, 2.0, 0.9, "Output stream", "aje.output.<id>", kind="white", size=13, sub_size=11)
arrow(s, 8.75, 4.35, 8.75, 5.25)
# knobs panel
box(s, 10.15, 1.65, 2.6, 4.6, fill=SLATE_L, radius=0.05, name="knobs")
text(s, 10.4, 1.85, 2.2, 4.3, [
    {"text": "Knobs", "bold": True, "size": 18, "color": NAVY, "space_after": 6},
    {"text": "workers (scale)", "bullet": True, "space_after": 4},
    {"text": "suspend / resume", "bullet": True, "space_after": 12},
    {"text": "Behaviour", "bold": True, "size": 18, "color": NAVY, "space_after": 6},
    {"text": "starts with 1 worker", "bullet": True, "space_after": 4},
    {"text": "a free worker takes the next task", "bullet": True, "space_after": 4},
    {"text": "close ends the stream", "bullet": True},
], size=14)
notes(s, "Every task goes to whichever worker is free: one consumer per worker, prefetch 1, "
         "manual ack. More workers means more throughput, never different results.")

# ═══ 6. Data parallel ═══════════════════════════════════════════════════════
s = new_slide()
title(s, "Data-parallel: split, compute, gather", "Skeleton 2")
node(s, 0.6, 3.35, 1.7, 1.0, "Batch", "items [ … ]", kind="white")
node(s, 2.8, 3.35, 1.9, 1.0, "Scatter", "P contiguous slices", kind="teal")
arrow(s, 2.3, 3.85, 2.8, 3.85)
py = [1.75, 2.75, 4.35, 5.35]
plabs = ["slice 1", "slice 2", "slice P−1", "slice P"]
for y_, lab in zip(py, plabs):
    node(s, 5.4, y_, 1.6, 0.62, lab, "→ any worker", kind="amber", size=13, sub_size=10)
    arrow(s, 4.7, 3.85, 5.4, y_ + 0.31)
    arrow(s, 7.0, y_ + 0.31, 7.7, 3.85)
text(s, 5.4, 3.5, 1.6, 0.5, "⋮", size=24, color=MUTED, align=PP_ALIGN.CENTER)
node(s, 7.7, 3.35, 2.0, 1.0, "Gather", "concat · list · sum · merge", kind="teal", sub_size=11)
node(s, 7.7, 5.25, 2.0, 0.9, "1 result / batch", "batch.result", kind="white", size=13, sub_size=11)
arrow(s, 8.7, 4.35, 8.7, 5.25)
box(s, 10.15, 1.65, 2.6, 4.6, fill=SLATE_L, radius=0.05, name="knobs")
text(s, 10.4, 1.85, 2.2, 4.3, [
    {"text": "Knobs", "bold": True, "size": 18, "color": NAVY, "space_after": 6},
    {"text": "workers, any time", "bullet": True, "space_after": 4},
    {"text": "partitions, from the next batch (a safe point)", "bullet": True, "space_after": 4},
    {"text": "suspend / resume", "bullet": True, "space_after": 12},
    {"text": "Failure", "bold": True, "size": 18, "color": NAVY, "space_after": 6},
    {"text": "a slice failing for good fails its batch only", "bullet": True},
], size=14)
notes(s, "Each batch is cut into P slices processed in parallel and gathered into one result. "
         "Partitions change only between batches, so the gather is always consistent.")

# ═══ 7. Guarantee ═══════════════════════════════════════════════════════════
s = new_slide()
title(s, "Management never changes the results", "The guarantee")
box(s, 0.6, 1.65, 5.85, 2.75, fill=WHITE, line=LINE, radius=0.05, name="functional")
text(s, 0.9, 1.85, 5.3, 2.4, [
    {"text": "What it computes", "bold": True, "size": 20, "color": NAVY},
    {"text": "fixed at submission", "size": 13, "color": MUTED, "space_after": 8},
    {"text": "worker image and command", "bullet": True, "space_after": 4},
    {"text": "task payloads and batch items", "bullet": True, "space_after": 4},
    {"text": "gather mode", "bullet": True},
], size=15)
box(s, 6.85, 1.65, 5.85, 2.75, fill=TEAL_L, radius=0.05, name="non-functional")
text(s, 7.15, 1.85, 5.3, 2.4, [
    {"text": "How it runs", "bold": True, "size": 20, "color": TEAL_D},
    {"text": "the only things a contract can touch", "size": 13, "color": TEAL_D, "space_after": 8},
    {"text": "workers: scale, scale_up, scale_down", "bullet": True, "space_after": 4},
    {"text": "partitions: set_partitions (data-parallel)", "bullet": True, "space_after": 4},
    {"text": "lifecycle: suspend, resume, escalate", "bullet": True},
], size=15)
facts = [("422", "a rule that touches a functional field is rejected at submission, with its line number"),
         ("stateless", "workers pull from a queue, so more workers or new partitions can't change the output"),
         ("tested", "a property-based test scales and re-partitions at random mid-run: results stay identical")]
for i, (big, small) in enumerate(facts):
    x = 0.6 + i * 4.15
    text(s, x, 4.75, 3.85, 0.8, big, size=36, bold=True, color=AMBER)
    text(s, x, 5.55, 3.7, 1.2, small, size=14, color=INK)
notes(s, "Contracts are type-checked against a catalog per scope. A farm contract calling "
         "set_partitions, or anything touching image, command or payloads, gets HTTP 422.")

# ═══ 8. MAPE loop ═══════════════════════════════════════════════════════════
s = new_slide()
title(s, "The manager: a MAPE loop driven by a contract", "Autonomic control")
node(s, 2.35, 1.65, 2.5, 0.95, "Monitor", "metrics every monitor_interval", kind="teal", sub_size=11)
node(s, 4.4, 3.4, 2.3, 0.95, "Analyse", "match rules against facts", kind="teal", sub_size=11)
node(s, 2.35, 5.2, 2.5, 0.95, "Plan", "salience · cooldown · group", kind="teal", sub_size=11)
node(s, 0.5, 3.4, 2.3, 0.95, "Execute", "control interface", kind="teal", sub_size=11)
node(s, 2.95, 3.45, 1.3, 0.85, "Contract", "+ facts", kind="dark", size=13, sub_size=11)
arrow(s, 4.85, 2.35, 5.55, 3.4, color=TEAL)
arrow(s, 5.55, 4.35, 4.85, 5.45, color=TEAL)
arrow(s, 2.35, 5.45, 1.65, 4.35, color=TEAL)
arrow(s, 1.65, 3.4, 2.35, 2.35, color=TEAL)
code(s, 7.1, 1.65, 5.6, 3.9, [
    "global int backlog_per_worker = 5;",
    "",
    'rule "grow fast on backlog"',
    "  salience 30",
    '  cooldown "10s"',
    '  activation-group "scaling"',
    "when",
    '  $j : Job( status == "running",',
    "            workers < max_workers )",
    "  $m : Metrics( queue_depth >",
    "        backlog_per_worker * $j.workers )",
    "then",
    "  scale(min($j.max_workers, $j.workers * 2));",
    "end",
], size=12)
text(s, 7.1, 5.7, 5.6, 0.6, "A rule in the AJE contract language: double the workers "
                            "when the backlog outgrows them.", size=12, color=MUTED, italic=True)
notes(s, "Each tick builds a snapshot of facts, computes all activations, orders them by salience, "
         "applies activation groups and cooldowns, and executes the actions. Effects are observed at "
         "the next tick. Management can be switched between active and passive at runtime.")

# ═══ 9. Divider: Kubernetes ═════════════════════════════════════════════════
s = divider("02", "Kubernetes Jobs",
            "Each AJE job runs its workers as one batch/v1 Job in work-queue mode.")
notes(s, "Now the Kubernetes backend: AJE_BACKEND=kubernetes.")

# ═══ 10. Mapping table ══════════════════════════════════════════════════════
s = new_slide()
title(s, "How AJE maps onto Kubernetes", "Kubernetes backend")
rows = [
    ("AJE", "Kubernetes"),
    ("a job's workers", "one batch/v1 Job in work-queue mode (completions unset)"),
    ("workers knob", "spec.parallelism, patched live; surplus pods get SIGTERM"),
    ("suspend / resume", "spec.suspend; queued tasks stay in RabbitMQ"),
    ("scale-down exit", "exit 143 → podFailurePolicy Ignore (not a failure, not a success)"),
    ("normal end", "drain broadcast → workers exit 0 → Job becomes Complete"),
    ("worker crash", "unacked task redelivered; pod replaced up to backoffLimit"),
    ("worker identity", "AJE_WORKER_ID = pod name, from the downward API"),
    ("retention", "ttlSecondsAfterFinished (AJE_K8S_TTL_AFTER_FINISHED, 3600 s)"),
]
tbl_shape = s.shapes.add_table(len(rows), 2, Inches(0.6), Inches(1.6), Inches(12.1), Inches(4.7))
tbl_shape.name = "mapping table"
tbl = tbl_shape.table
tbl.columns[0].width = Inches(3.3)
tbl.columns[1].width = Inches(8.8)
for r, (a, b) in enumerate(rows):
    tbl.rows[r].height = Inches(0.52)
    for c, val in enumerate((a, b)):
        cell = tbl.cell(r, c)
        cell.fill.solid()
        cell.fill.fore_color.rgb = rgb(NAVY if r == 0 else (SLATE_L if r % 2 else WHITE))
        cell.margin_left = cell.margin_right = Inches(0.15)
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        fill_frame(cell.text_frame, [{"text": val}], size=15,
                   color=WHITE if r == 0 else (TEAL_D if c == 0 else INK),
                   bold=(r == 0 or c == 0), font=MONO if (c == 0 and r > 0) else BODY,
                   anchor=MSO_ANCHOR.MIDDLE)
text(s, 0.6, 6.5, 12.1, 0.5,
     [{"runs": [("RBAC (deploy/k8s/rbac.yaml, namespace aje-jobs): ", {"bold": True}),
                ("jobs create/get/list/watch/patch/delete · jobs/status get · pods get/list/watch · "
                 "pods/log get", {})]}], size=12, color=MUTED)
notes(s, "The key trick: no completions count. Workers pull from the queue, so the Job is a pool "
         "whose size is spec.parallelism. Scaling is a PATCH; exit 143 is ignored by the "
         "failure policy so scale-downs don't burn the backoff budget.")

# ═══ 11. Manifest ═══════════════════════════════════════════════════════════
s = new_slide()
title(s, "The Job the engine generates", "Kubernetes backend")
code(s, 0.6, 1.6, 7.6, 5.4, [
    "apiVersion: batch/v1",
    "kind: Job",
    "metadata:",
    "  name: aje-<job-id>",
    "  labels: {aje.io/job-id: <id>, aje.io/pattern: farm}",
    "spec:",
    "  parallelism: 4          # = workers, patched live",
    "  backoffLimit: 20        # completions unset",
    "  ttlSecondsAfterFinished: 3600",
    "  podFailurePolicy:",
    "    rules:",
    "    - action: Ignore      # scale-down, suspend",
    "      onExitCodes: {operator: In, values: [143]}",
    "    - action: Ignore",
    "      onPodConditions: [{type: DisruptionTarget}]",
    "  template:",
    "    spec:",
    "      restartPolicy: Never",
    "      containers:",
    "      - name: worker",
    "        image: localhost:32000/aje-worker:latest",
    "        env: [AJE_AMQP_URL, AJE_JOB_ID, AJE_WORKER_ID]",
], size=12)
callouts = [
    ("1", "Work-queue mode", "parallelism without completions: the Job is a pool of identical "
                             "workers pulling from RabbitMQ."),
    ("2", "Cheap scale-down", "SIGTERM exits (143) and disruptions are ignored, so they never use "
                              "up backoffLimit."),
    ("3", "Wired by env", "broker URL, job id, worker id from the downward API, and secrets via "
                          "secretKeyRef, never in the spec."),
]
for i, (n, h_, body) in enumerate(callouts):
    y = 1.65 + i * 1.8
    circle(s, 8.6, y, 0.55, n, fill=AMBER)
    text(s, 9.35, y, 3.4, 0.45, h_, size=18, bold=True, color=NAVY)
    text(s, 9.35, y + 0.5, 3.4, 1.2, body, size=14, color=INK)
notes(s, "Simplified from build_job_manifest in src/aje/infrastructure/backends/kubernetes.py. "
         "Resources, nodeSelector, serviceAccount and imagePullSecrets come from the job request.")

# ═══ 12. Lifecycle ══════════════════════════════════════════════════════════
s = new_slide()
title(s, "A worker pool's life on Kubernetes", "Lifecycle")
steps = [
    ("Create", "POST batch/v1 Job with parallelism = workers; pods start and connect to RabbitMQ."),
    ("Run", "each pod takes one task at a time (prefetch 1, manual ack) and sends results "
            "and heartbeats."),
    ("Scale", "the manager PATCHes spec.parallelism; surplus pods get SIGTERM, finish the task, "
              "exit 143."),
    ("Suspend", "spec.suspend = true stops the pods; queued tasks wait in RabbitMQ until resume."),
    ("Drain", "input closed and work done: a drain broadcast, workers exit 0, the Job is Complete."),
    ("Clean up", "ttlSecondsAfterFinished removes the Job and its pods; the output stream stays."),
]
for i, (h_, body) in enumerate(steps):
    col, row = i % 3, i // 3
    x, y = 0.6 + col * 4.15, 1.65 + row * 2.6
    box(s, x, y, 3.85, 2.3, fill=SLATE_L if row == 0 else AMBER_L, radius=0.06, name=f"step {h_}")
    circle(s, x + 0.25, y + 0.25, 0.55, str(i + 1), fill=TEAL if row == 0 else AMBER)
    text(s, x + 0.95, y + 0.3, 2.7, 0.45, h_, size=20, bold=True, color=NAVY)
    text(s, x + 0.25, y + 0.95, 3.4, 1.3, body, size=14, color=INK)
notes(s, "Top row: the steady state. Bottom row: control and termination. Worker exit codes "
         "carry meaning: 0 = drained, 143 = terminated by the engine.")

# ═══ 13. Self-healing ═══════════════════════════════════════════════════════
s = new_slide()
title(s, "Self-healing and durability", "Reliability")
heal = [
    ("!", "Task failure", "retried up to max_retries, then marked failed; a data-parallel batch fails."),
    ("↻", "Worker crash", "the unacked task is redelivered; the Job controller replaces the pod, "
                          "up to backoff_limit."),
    ("?", "Orphaned tasks", "outstanding work, an empty queue and idle workers for 30 s: "
                            "re-dispatched; stale duplicates are ignored."),
    ("↺", "Engine restart", "state in PostgreSQL, durable queues: managers are rebuilt and missing "
                            "workers recreated."),
]
for i, (ic, h_, body) in enumerate(heal):
    y = 1.7 + i * 1.3
    circle(s, 0.6, y, 0.75, ic, fill=TEAL if i % 2 == 0 else AMBER, size=20)
    text(s, 1.65, y + 0.02, 3.2, 0.7, h_, size=20, bold=True, color=NAVY, anchor=MSO_ANCHOR.MIDDLE)
    text(s, 4.9, y + 0.02, 7.8, 0.75, body, size=15, color=INK, anchor=MSO_ANCHOR.MIDDLE)
notes(s, "Delivery is at least once. Write idempotently (upserts) so a redelivered task is harmless.")

# ═══ 14. Submitting ═════════════════════════════════════════════════════════
s = new_slide()
title(s, "Submitting a job to Kubernetes", "Hands-on")
steps2 = [
    ("Cluster + registry", r".\setup\setup.ps1 -BuildWorker"),
    ("Permissions", "kubectl apply -f deploy/k8s/rbac.yaml"),
    ("Engine on k8s", "docker compose -f docker-compose.yml\n  -f docker-compose.k8s.yml up --build"),
    ("Submit", "POST /v1/jobs with worker.image"),
]
for i, (h_, cmd) in enumerate(steps2):
    y = 1.65 + i * 1.3
    circle(s, 0.6, y + 0.05, 0.55, str(i + 1))
    text(s, 1.35, y, 4.6, 0.4, h_, size=18, bold=True, color=NAVY)
    text(s, 1.35, y + 0.42, 4.9, 0.8, cmd, size=13, color=TEAL_D, font=MONO)
code(s, 6.6, 1.6, 6.1, 5.0, [
    "POST /v1/jobs",
    "{",
    '  "name": "squares",',
    '  "pattern": "farm",',
    '  "worker": {',
    '    "image": "localhost:32000/aje-worker:latest",',
    '    "image_pull_policy": "IfNotPresent",',
    '    "command": ["python", "-m", "aje_worker",',
    '                "aje_worker.examples:square"],',
    '    "resources": {"cpu_request": "250m"}',
    "  },",
    '  "workers": 2, "min_workers": 1, "max_workers": 10,',
    '  "tasks": [1, 2, 3, 4, 5, 6, 7, 8],',
    '  "close_when_done": true',
    "}",
], size=12)
text(s, 6.6, 6.7, 6.1, 0.4, "AJE_WORKER_AMQP_URL must be a RabbitMQ address the pods can reach.",
     size=12, color=MUTED, italic=True)
notes(s, "Then watch with kubectl -n aje-jobs get jobs,pods, or GET /v1/jobs/{id}, /workers, /logs "
         "and /stream.")

# ═══ 15. Divider: UI ════════════════════════════════════════════════════════
s = divider("03", "The UI demo",
            "A jobs console and the Fractal Lab: what they show and how they talk to the backend.")
notes(s, "Two React front-ends over the same API: src/demo (jobs console) and examples/demo (Fractal Lab).")

# ═══ 16. Jobs console ═══════════════════════════════════════════════════════
s = new_slide()
title(s, "Jobs Console: submit and track every job", "src/demo · React + MUI")
# wireframe
wx, wy0, ww, wh = 0.6, 1.65, 7.4, 5.0
box(s, wx, wy0, ww, wh, fill=WHITE, line=LINE, radius=0.03, name="wireframe window")
box(s, wx, wy0, ww, 0.5, fill=NAVY, radius=0.0, shape=MSO_SHAPE.RECTANGLE, name="wire appbar")
text(s, wx + 0.2, wy0 + 0.08, 3, 0.35, "AJE Jobs", size=13, bold=True, color=WHITE)
btn = box(s, wx + ww - 1.5, wy0 + 0.1, 1.3, 0.32, fill=TEAL, radius=0.3, name="wire submit")
fill_frame(btn.text_frame, [{"text": "+ Submit", "bold": True, "align": PP_ALIGN.CENTER}], size=11,
           color=WHITE, anchor=MSO_ANCHOR.MIDDLE)
cols = ["name", "status", "progress", "workers", "queue"]
cx = [0.2, 1.9, 3.1, 4.75, 5.85]
for c_, x_ in zip(cols, cx):
    text(s, wx + x_, wy0 + 0.7, 1.4, 0.3, c_, size=11, bold=True, color=MUTED)
jobs = [("squares", "running", 0.7, "4"), ("nightly-etl", "running", 0.35, "8"),
        ("fractal", "draining", 0.95, "2"), ("word-count", "succeeded", 1.0, "0"),
        ("thumbs", "suspended", 0.5, "0")]
status_col = {"running": (TEAL_L, TEAL_D), "draining": (AMBER_L, "78350F"),
              "succeeded": (SLATE_L, INK), "suspended": (SLATE_L, MUTED)}
for i, (n_, st, prog, wk) in enumerate(jobs):
    y_ = wy0 + 1.1 + i * 0.6
    if i % 2 == 0:
        box(s, wx + 0.1, y_ - 0.06, ww - 0.2, 0.5, fill="F8FAFC", radius=0.0,
            shape=MSO_SHAPE.RECTANGLE, name="wire row")
    text(s, wx + cx[0], y_, 1.6, 0.35, n_, size=12, color=INK, font=MONO)
    f_, fg_ = status_col[st]
    chip = box(s, wx + cx[1], y_ + 0.02, 1.05, 0.3, fill=f_, radius=0.5, name="wire chip")
    fill_frame(chip.text_frame, [{"text": st, "align": PP_ALIGN.CENTER}], size=10, color=fg_,
               anchor=MSO_ANCHOR.MIDDLE)
    box(s, wx + cx[2], y_ + 0.11, 1.4, 0.13, fill="E2E8F0", radius=0.5, name="wire bar")
    box(s, wx + cx[2], y_ + 0.11, 1.4 * prog, 0.13, fill=TEAL, radius=0.5, name="wire bar fill")
    text(s, wx + cx[3], y_, 0.8, 0.35, wk, size=12, color=INK)
    text(s, wx + cx[4], y_, 1.0, 0.35, str([12, 140, 0, 0, 30][i]), size=12, color=INK)
text(s, wx + 0.2, wy0 + wh - 0.55, ww - 0.4, 0.35,
     "sort · filter · quick search · CSV export · double-click a row for details",
     size=11, color=MUTED, italic=True)
feats = [("Grid", "@mui/x-data-grid: status, progress, workers, throughput, queue depth; per-row "
                  "suspend, resume, cancel."),
         ("Submit dialog", "a form for farm and data_parallel jobs, or a raw JSON tab for any "
                           "field of the request."),
         ("Detail drawer", "live metrics and the event timeline; scale, change management, add "
                           "tasks or batches, close input.")]
for i, (h_, body) in enumerate(feats):
    y_ = 1.65 + i * 1.7
    text(s, 8.4, y_, 4.3, 0.45, h_, size=20, bold=True, color=NAVY)
    text(s, 8.4, y_ + 0.48, 4.3, 1.1, body, size=14, color=INK)
notes(s, "Run it with: cd src/demo, npm install, AJE_API_URL=http://localhost:8000 npm run dev "
         "(port 5174). The wireframe is schematic, not a screenshot.")

# ═══ 17. UI ↔ backend ═══════════════════════════════════════════════════════
s = new_slide()
title(s, "How the UI talks to the backend", "Client interface")
node(s, 0.6, 1.7, 2.5, 1.0, "Browser", "React + MUI", kind="white")
node(s, 3.7, 1.7, 2.5, 1.0, "Vite / nginx", "proxies /v1, /internal", kind="neutral")
node(s, 6.8, 1.7, 2.7, 1.0, "AJE API", "REST + Server-Sent Events", kind="teal")
node(s, 10.1, 1.7, 2.6, 1.0, "EventBus", "persist → push", kind="teal")
arrow(s, 3.1, 2.2, 3.7, 2.2, both=True)
arrow(s, 6.2, 2.2, 6.8, 2.2, both=True)
arrow(s, 9.5, 2.2, 10.1, 2.2, both=True)
rows = [
    ("What the UI needs", "Endpoint", "How"),
    ("initial list", "GET /v1/jobs?limit=500", "once at load"),
    ("live grid", "GET /internal/events?snapshot_every=2", "SSE snapshot messages"),
    ("final states", "GET /v1/jobs/{id}", "on a job.* event or a job leaving the snapshot"),
    ("detail drawer", "GET /v1/jobs/{id}/stream?metrics_every=2", "SSE: history, live, then end"),
    ("stream down", "GET /v1/jobs", "poll every 5 s until EventSource reconnects"),
]
tshape = s.shapes.add_table(len(rows), 3, Inches(0.6), Inches(3.1), Inches(12.1), Inches(3.3))
tshape.name = "channels table"
t = tshape.table
for ci, wdt in enumerate((2.4, 5.3, 4.4)):
    t.columns[ci].width = Inches(wdt)
for r, vals in enumerate(rows):
    t.rows[r].height = Inches(0.5)
    for c, val in enumerate(vals):
        cell = t.cell(r, c)
        cell.fill.solid()
        cell.fill.fore_color.rgb = rgb(NAVY if r == 0 else (SLATE_L if r % 2 else WHITE))
        cell.margin_left = cell.margin_right = Inches(0.12)
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        fill_frame(cell.text_frame, [{"text": val}], size=13 if c == 1 else 14,
                   color=WHITE if r == 0 else (TEAL_D if c == 1 else INK), bold=(r == 0),
                   font=MONO if (c == 1 and r > 0) else BODY, anchor=MSO_ANCHOR.MIDDLE)
text(s, 0.6, 6.55, 12.1, 0.5,
     "No CORS: the dev server proxies the API. With AJE_API_KEY set, streams pass ?api_key= "
     "because EventSource cannot send headers.", size=12, color=MUTED, italic=True)
notes(s, "Every lifecycle or management decision is an event: persisted to PostgreSQL first, then "
         "pushed to subscribers. Metrics come from the manager's last tick (default every 5 s).")

# ═══ 18. Fractal Lab ════════════════════════════════════════════════════════
s = new_slide()
title(s, "Fractal Lab: watch the contract at work", "examples/demo")
# tile grid motif: each tile is one task, coloured by the worker that computed it
gx, gy, n, sz, gap = 0.6, 1.7, 8, 0.56, 0.06
palette = [TEAL, "14B8A6", AMBER, "D97706", TEAL_D]
order = [(r, c) for r in range(n) for c in range(n)]
for k, (r, c) in enumerate(order):
    done = k < 46
    fill_ = palette[(r * 3 + c * 5) % len(palette)] if done else "E2E8F0"
    box(s, gx + c * (sz + gap), gy + r * (sz + gap), sz, sz, fill=fill_, radius=0.08,
        name="tile")
text(s, gx, gy + n * (sz + gap) + 0.05, 5.0, 0.35, "one tile = one farm task; colour = worker",
     size=11, color=MUTED, italic=True)
text(s, 6.0, 1.7, 6.7, 5.0, [
    {"text": "What you see", "bold": True, "size": 20, "color": NAVY, "space_after": 6},
    {"text": "aje_worker.examples.fractal:tile renders Mandelbrot or Julia tiles", "bullet": True,
     "space_after": 4},
    {"text": "GET /internal/events?tasks=true streams task (each tile), snapshot (every second) "
             "and event (rule firings, job.scaled)", "bullet": True, "space_after": 14},
    {"text": "The game", "bold": True, "size": 20, "color": NAVY, "space_after": 6},
    {"text": "Contract: farm-throughput scales on backlog", "bullet": True, "space_after": 4},
    {"text": "You: passive management, scale with + / − while it renders", "bullet": True,
     "space_after": 4},
    {"text": "Score: tiles per worker-second. Be fast and frugal.", "bullet": True,
     "bullet_color": AMBER},
], size=15)
code(s, 6.0, 6.0, 6.7, 0.75, ["docker compose --profile demo up --build   # :8080"], size=13)
notes(s, "Click to zoom x2 and render again. Many workers is fast but wasteful: the contract "
         "usually wins on tiles per worker-second.")

# ═══ 19. Closing ════════════════════════════════════════════════════════════
s = new_slide(NAVY, footer=False)
title(s, "Takeaways", "Autonomic Job Engine", dark=True)
take = [
    ("Skeletons", "farm and data_parallel fix the structure; only non-functional knobs move, so "
                  "results never change."),
    ("Contracts", "rules over live facts drive a MAPE loop per job, and per group for shared budgets."),
    ("Kubernetes", "one work-queue Job per AJE job: parallelism is the knob, exit codes carry "
                   "meaning, crashes heal."),
]
for i, (h_, body) in enumerate(take):
    x = 0.6 + i * 4.15
    circle(s, x, 1.85, 0.6, str(i + 1), fill=CODE_KEY, fg=NAVY)
    text(s, x, 2.65, 3.8, 0.5, h_, size=22, bold=True, color=WHITE)
    text(s, x, 3.2, 3.75, 1.6, body, size=15, color="CBD5E1")
code(s, 0.6, 5.1, 12.1, 1.55, [
    "docker compose up --build                 # API :8000, local workers",
    "./examples/demo.sh                        # farm, control, data-parallel, group",
    "docker compose -f docker-compose.yml -f docker-compose.k8s.yml up --build",
], size=13)
notes(s, "Questions. Interactive API docs are at /docs; client guidance in docs/clientinterface.md.")

prs.save(OUT)
print(f"wrote {OUT} ({slide_no} slides)")
