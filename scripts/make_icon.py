#!/usr/bin/env python3
"""ランチャー用アイコン生成 — クリムゾン→オレンジのグラデ角丸 ＋ 斜めの音声波形（DESIGN.md v2 準拠）。

マーク = 5 本の丸バーを中心線対称に並べ、全体を斜体に倒した音声波形。
「音声を扱う」ことが一目で分かり、傾き＝スピード（すぐ起動・すぐ録音）を表す。
候補の検討過程は scripts/icon_concepts.py / icon_variants.py / icon_finalists.py と visual-qa/icons/。

usage: python3 scripts/make_icon.py   （make_launcher.sh から自動で呼ばれる）
"""
import os
import subprocess

from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIZE = 1024
SS = 2                      # スーパーサンプリング倍率（描いてから縮小して縁を滑らかに）
CRIMSON = (0xD5, 0x1F, 0x45)
ORANGE = (0xEF, 0x7D, 0x00)

# --- マークの形状パラメータ ---
# 通常サイズ: 5 本の波形
BAR_HALF_HEIGHTS = [0.095, 0.175, 0.265, 0.175, 0.095]  # 中心からの高さ（対キャンバス比）
BAR_WIDTH = 0.078
BAR_GAP = 0.052
SHEAR = 0.20                # 斜体の強さ（y に比例した x ずらし量）

# 極小サイズ用の簡略版: 5 本だと 16px で潰れるので 3 本・太めにする（Apple の作法）
SMALL_PX = 24               # このピクセル数以下は簡略版を使う
SMALL_HALF_HEIGHTS = [0.15, 0.27, 0.15]
SMALL_BAR_WIDTH = 0.165
SMALL_BAR_GAP = 0.095


def gradient(size: int) -> Image.Image:
    """対角グラデ。小さく塗ってから拡大し、帯・縞を出さない。"""
    small = 256
    g = Image.new("RGB", (small, small))
    px = g.load()
    for y in range(small):
        for x in range(small):
            t = (x + y) / (2 * small)
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(CRIMSON, ORANGE))
    return g.resize((size, size), Image.BILINEAR)


def build_master(simple: bool = False) -> Image.Image:
    halves = SMALL_HALF_HEIGHTS if simple else BAR_HALF_HEIGHTS
    bar_width = SMALL_BAR_WIDTH if simple else BAR_WIDTH
    bar_gap = SMALL_BAR_GAP if simple else BAR_GAP
    size = SIZE * SS
    # macOS 風の角丸（外周 9% は透過マージン、角丸は本体の 23%）
    margin = int(size * 0.09)
    radius = int((size - margin * 2) * 0.23)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [margin, margin, size - margin, size - margin], radius=radius, fill=255
    )
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.paste(gradient(size), (0, 0), mask)

    # 白の波形バー（別レイヤーに描いてから斜体化）
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cy = size / 2
    bw = size * bar_width
    gap = size * bar_gap
    total = len(halves) * bw + (len(halves) - 1) * gap
    x = size / 2 - total / 2
    for h in halves:
        hh = size * h
        d.rounded_rectangle([x, cy - hh, x + bw, cy + hh], radius=bw / 2, fill="white")
        x += bw + gap
    layer = layer.transform(
        (size, size), Image.AFFINE, (1, SHEAR, -SHEAR * cy, 0, 1, 0), resample=Image.BICUBIC
    )
    icon.alpha_composite(layer)
    return icon.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    iconset = os.path.join(BASE, "scripts", "icon.iconset")
    os.makedirs(iconset, exist_ok=True)
    master = build_master()
    master_small = build_master(simple=True)
    qa = os.path.join(BASE, "visual-qa", "icons")
    if os.path.isdir(qa):
        master.save(os.path.join(qa, "_adopted.png"))
    for pt in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = pt * scale
            name = f"icon_{pt}x{pt}" + ("@2x" if scale == 2 else "") + ".png"
            src = master_small if px <= SMALL_PX else master
            src.resize((px, px), Image.LANCZOS).save(os.path.join(iconset, name))
    icns = os.path.join(BASE, "scripts", "icon.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    print("created:", icns)


if __name__ == "__main__":
    main()
