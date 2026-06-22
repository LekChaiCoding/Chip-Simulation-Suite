"""
Half-wave CPW resonator with capacitive coupling ports at both ends.

Structure (along y-axis)
------------------------
  Port 1 (bottom feed)
  ─── coupling gap (CG_UM) ───   ← capacitive gap, tunes Q_coupling
  Resonator body (LENGTH_UM)
  ─── coupling gap (CG_UM) ───
  Port 2 (top feed)

Layers
------
  0 – metal  (center conductor segments + ground planes, continuous)
  1 – gap    (CPW slots, continuous full length)
  2 – port   (port marker rectangles at feed ends → COMSOL lumped port faces)

Design parameters
-----------------
  f0    = 5.5 GHz
  W     = 5 µm   (center conductor width)
  G     = 20 µm  (CPW gap)
  CG    = 10 µm  (coupling gap — adjustable for Q_coupling tuning)
  FEED  = 500 µm (feed line on each side)
  metal = Al / Si substrate (εr = 11.9)
"""

import math
import gdstk
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ── Design parameters ──────────────────────────────────────────────────────────
FREQ_GHZ    = 5.5     # target resonance frequency (GHz)
W_UM        = 5.0     # CPW center conductor width (µm)
G_UM        = 20.0    # CPW gap width (µm)
CG_UM       = 10.0    # coupling gap at each end (µm) — tune for Q_coupling
FEED_UM     = 500.0   # feed line length beyond coupling gap (µm)
GROUND_W    = 100.0   # ground plane width each side (µm)
EPS_R       = 11.9    # Si relative permittivity

LAYER_METAL = 0
LAYER_GAP   = 1
LAYER_PORT  = 2   # port marker faces for COMSOL

# ── Resonator length (half-wave, thick-substrate CPW) ─────────────────────────
EPS_EFF    = (1.0 + EPS_R) / 2.0
C_UM_PER_S = 2.998e14                        # speed of light in µm/s
V_PH       = C_UM_PER_S / math.sqrt(EPS_EFF)
LENGTH_UM  = round(V_PH / (2.0 * FREQ_GHZ * 1e9) / 10) * 10

print(f"εeff            = {EPS_EFF:.4f}")
print(f"Phase velocity  = {V_PH:.4e} µm/s")
print(f"Resonator length= {LENGTH_UM:.1f} µm  ({LENGTH_UM/1e3:.3f} mm)")
print(f"Coupling gap    = {CG_UM} µm")
print(f"Feed length     = {FEED_UM} µm")
print(f"CPW  W={W_UM} µm  G={G_UM} µm  GND_W={GROUND_W} µm")

# ── Key y-coordinates (resonator centred at y=0) ─────────────────────────────
#
#  y_top_feed   ─ top of Port 2 feed line        ┐
#  y_res_top    ─ top of resonator body           │ top coupling gap
#  y_cg_top     ─ top of top coupling gap         ┘
#               ─ resonator body (LENGTH_UM)
#  y_cg_bot     ─ bottom of bottom coupling gap   ┐
#  y_res_bot    ─ bottom of resonator body        │ bottom coupling gap
#  y_bot_feed   ─ bottom of Port 1 feed line      ┘
#
cx        = 0.0
y_res_top =  LENGTH_UM / 2
y_res_bot = -LENGTH_UM / 2
y_cg_top  =  y_res_top + CG_UM
y_cg_bot  =  y_res_bot - CG_UM
y_top_feed=  y_cg_top  + FEED_UM
y_bot_feed=  y_cg_bot  - FEED_UM

TOTAL_LENGTH = y_top_feed - y_bot_feed

# ── GDS layout ────────────────────────────────────────────────────────────────
lib  = gdstk.Library(unit=1e-6, precision=1e-9)
cell = lib.new_cell("CPW_HALF_WAVE_PORTS")

# Center conductor — three segments (bottom feed, resonator body, top feed)
# The coupling gaps are the breaks between them.
for y0, y1 in [
    (y_bot_feed, y_cg_bot),   # Port 1 feed
    (y_res_bot,  y_res_top),  # resonator body
    (y_cg_top,   y_top_feed), # Port 2 feed
]:
    cell.add(gdstk.rectangle(
        (cx - W_UM/2, y0),
        (cx + W_UM/2, y1),
        layer=LAYER_METAL,
    ))

# CPW slots — continuous across the full structure (including over coupling gaps)
for sign in (-1, 1):
    x_inner = cx + sign * W_UM / 2
    x_outer = x_inner + sign * G_UM
    cell.add(gdstk.rectangle(
        (min(x_inner, x_outer), y_bot_feed),
        (max(x_inner, x_outer), y_top_feed),
        layer=LAYER_GAP,
    ))

# Ground planes — continuous full length
for sign in (-1, 1):
    x_inner = cx + sign * (W_UM/2 + G_UM)
    x_outer = x_inner + sign * GROUND_W
    cell.add(gdstk.rectangle(
        (min(x_inner, x_outer), y_bot_feed),
        (max(x_inner, x_outer), y_top_feed),
        layer=LAYER_METAL,
    ))

# Port markers — thin rectangles at the very ends of each feed line
# COMSOL will place lumped ports on these faces.
PORT_H = 1.0   # 1 µm thick marker (face in the yz-plane of COMSOL)
for y0, y1 in [
    (y_bot_feed,          y_bot_feed + PORT_H),  # Port 1 (bottom)
    (y_top_feed - PORT_H, y_top_feed),            # Port 2 (top)
]:
    cell.add(gdstk.rectangle(
        (cx - W_UM/2, y0),
        (cx + W_UM/2, y1),
        layer=LAYER_PORT,
    ))

print(f"Total structure height: {TOTAL_LENGTH:.1f} µm  ({TOTAL_LENGTH/1e3:.3f} mm)")

# Output paths — redirected by run_custom_cad
OUT_GDS = "resonator_halfwave.gds"
OUT_PNG = "resonator_halfwave.png"

lib.write_gds(OUT_GDS)
print(f"Wrote GDS → {OUT_GDS}")

# ── Preview PNG ────────────────────────────────────────────────────────────────
# Top-down schematic. Resonator is 10+ mm so we compress it with a break symbol.
BG   = "#1a1a2e"
MCOL = "#c0c0c0"   # Al metal
GCOL = BG          # gap / substrate
PCOL = "#e6a817"   # port marker

fig, ax = plt.subplots(figsize=(4, 10))
fig.patch.set_facecolor(BG)
ax.set_facecolor(BG)

# Compressed y mapping: show feed + CG at true scale, squash resonator body
FEED_SHOW = FEED_UM          # full feed shown
CG_SHOW   = CG_UM * 4       # exaggerate coupling gap for visibility
RES_SHOW  = 300.0            # resonator body shown compressed
BREAK_Y   = 30.0             # break symbol height

# Map true y → display y (bottom to top)
def disp(y_true):
    """Piecewise linear map: preserves feed/CG scale, compresses resonator body."""
    # Segments (true_start, true_end, disp_start, disp_end)
    segs = [
        (y_bot_feed, y_cg_bot, 0,
         FEED_SHOW + CG_SHOW),
        (y_cg_bot,   y_res_bot, FEED_SHOW + CG_SHOW,
         FEED_SHOW + CG_SHOW + BREAK_Y),
        (y_res_bot,  y_res_top, FEED_SHOW + CG_SHOW + BREAK_Y,
         FEED_SHOW + CG_SHOW + BREAK_Y + RES_SHOW),
        (y_res_top,  y_cg_top, FEED_SHOW + CG_SHOW + BREAK_Y + RES_SHOW,
         FEED_SHOW + CG_SHOW + BREAK_Y + RES_SHOW + BREAK_Y),
        (y_cg_top,   y_top_feed,
         FEED_SHOW + CG_SHOW + BREAK_Y + RES_SHOW + BREAK_Y,
         FEED_SHOW + CG_SHOW + BREAK_Y + RES_SHOW + BREAK_Y + CG_SHOW + FEED_SHOW),
    ]
    for ts, te, ds, de in segs:
        if ts <= y_true <= te:
            frac = (y_true - ts) / (te - ts) if te != ts else 0
            return ds + frac * (de - ds)
    return 0

TOTAL_DISP = FEED_SHOW*2 + CG_SHOW*2 + RES_SHOW + BREAK_Y*2

# Draw each column (left GND | left gap | center cond | right gap | right GND)
cols = [
    dict(x=-W_UM/2-G_UM-GROUND_W, w=GROUND_W, col=MCOL, lbl="GND"),
    dict(x=-W_UM/2-G_UM,           w=G_UM,     col=GCOL, lbl="gap"),
    dict(x=-W_UM/2,                w=W_UM,     col=MCOL, lbl="W"),
    dict(x= W_UM/2,                w=G_UM,     col=GCOL, lbl="gap"),
    dict(x= W_UM/2+G_UM,           w=GROUND_W, col=MCOL, lbl="GND"),
]

def draw_segment(ax, x, w, col, y_disp_bot, y_disp_top):
    ax.add_patch(mpatches.Rectangle(
        (x, y_disp_bot), w, y_disp_top - y_disp_bot,
        facecolor=col, edgecolor="#555", linewidth=0.3,
    ))

# Port 1 feed (bottom)
for c in cols:
    col = PCOL if c["lbl"] == "W" else c["col"]
    draw_segment(ax, cx + c["x"], c["w"], col,
                 disp(y_bot_feed), disp(y_bot_feed + PORT_H))
    draw_segment(ax, cx + c["x"], c["w"], c["col"],
                 disp(y_bot_feed + PORT_H), disp(y_cg_bot))

# Coupling gap bottom (air break — show as dark region in center only)
for c in cols:
    gap_col = "#333" if c["lbl"] == "W" else c["col"]
    draw_segment(ax, cx + c["x"], c["w"], gap_col,
                 disp(y_cg_bot), disp(y_cg_bot) + BREAK_Y)

# Resonator body
for c in cols:
    draw_segment(ax, cx + c["x"], c["w"], c["col"],
                 disp(y_cg_bot) + BREAK_Y, disp(y_res_top))

# Coupling gap top
for c in cols:
    gap_col = "#333" if c["lbl"] == "W" else c["col"]
    draw_segment(ax, cx + c["x"], c["w"], gap_col,
                 disp(y_res_top), disp(y_res_top) + BREAK_Y)

# Port 2 feed (top)
for c in cols:
    draw_segment(ax, cx + c["x"], c["w"], c["col"],
                 disp(y_res_top) + BREAK_Y, disp(y_top_feed - PORT_H))
    col = PCOL if c["lbl"] == "W" else c["col"]
    draw_segment(ax, cx + c["x"], c["w"], col,
                 disp(y_top_feed - PORT_H), disp(y_top_feed))

# Break zigzag lines
for y_d in [disp(y_cg_bot) + BREAK_Y/2, disp(y_res_top) + BREAK_Y/2]:
    xs = [-W_UM/2-G_UM-GROUND_W, W_UM/2+G_UM+GROUND_W]
    ax.plot(xs, [y_d, y_d], color="#888", lw=0.8, linestyle="--")

# Annotations
ann_x = cx + W_UM/2 + G_UM + GROUND_W + 6
for label, y_d in [
    ("Port 1", (disp(y_bot_feed) + disp(y_cg_bot))/2),
    (f"CG = {CG_UM} µm", disp(y_cg_bot) + BREAK_Y/2),
    (f"L = {LENGTH_UM/1e3:.2f} mm\n(compressed)", (disp(y_res_bot) + disp(y_res_top))/2),
    (f"CG = {CG_UM} µm", disp(y_res_top) + BREAK_Y/2),
    ("Port 2", (disp(y_cg_top) + disp(y_top_feed))/2),
]:
    ax.text(ann_x, y_d, label, va="center", ha="left",
            color="white", fontsize=7.5)

total_w = W_UM + 2*G_UM + 2*GROUND_W
ax.set_xlim(cx - total_w/2 - 5, cx + total_w/2 + 80)
ax.set_ylim(-10, TOTAL_DISP + 10)
ax.set_aspect("equal")
ax.set_xlabel("x (µm)", color="white")
ax.tick_params(colors="white")
for sp in ax.spines.values():
    sp.set_edgecolor("#444")
ax.set_title(
    f"Half-wave CPW Resonator + Ports\n"
    f"f₀={FREQ_GHZ} GHz  L={LENGTH_UM:.0f} µm  W={W_UM} µm  G={G_UM} µm  CG={CG_UM} µm  Si/Al",
    color="white", fontsize=7.5, pad=6,
)
plt.tight_layout()
plt.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor=BG)
plt.close()
print(f"Wrote PNG  → {OUT_PNG}")
