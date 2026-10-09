"""Generates src/assets/logo-sweep.json: the app's mark, drawn as Lottie
shape layers (crisp at any size, no image assets).

Motion idea: a reviewer marking the line that matters. The three lines of
code write themselves in (staggered, ease-out), the highlighter sweeps across
the middle line from the left, then the whole mark settles with one small
breath. 60 fps, 1.6 s, plays once (the success screen).

    python ui/scripts/make_logo_lottie.py
"""
import json
from pathlib import Path

S = 4                      # the SVG is 64x64; the animation is 256x256
FR, OP = 60, 96
PINE, PAPER, MARKER = [0.114, 0.31, 0.271, 1], [0.914, 0.945, 0.925, 1], [0.839, 0.933, 0.29, 1]
EASE_OUT = {"i": {"x": [0.22], "y": [1]}, "o": {"x": [0.36], "y": [0]}}


def kf(frames):
    """[(t, value), ...] -> animated property with ease-out between keys."""
    out = []
    for (t, v), nxt in zip(frames, frames[1:] + [None]):
        k = {"t": t, "s": v if isinstance(v, list) else [v]}
        if nxt:
            k.update({"i": {"x": [0.22] * len(k["s"]), "y": [1] * len(k["s"])},
                      "o": {"x": [0.36] * len(k["s"]), "y": [0] * len(k["s"])}})
        out.append(k)
    return {"a": 1, "k": out}


def static(v):
    return {"a": 0, "k": v}


def layer(ind, name, shapes, anchor, scale=None, opacity=100, parent=None):
    lay = {"ddd": 0, "ind": ind, "ty": 4, "nm": name, "sr": 1, "ip": 0, "op": OP, "st": 0, "bm": 0,
           "ks": {"o": static(opacity), "r": static(0), "p": static(anchor + [0]),
                  "a": static(anchor + [0]), "s": scale or static([100, 100, 100])},
           "shapes": shapes}
    if parent:
        lay["parent"] = parent
    return lay


def rect(x, y, w, h, r, color):
    return [{"ty": "gr", "nm": "g", "it": [
        {"ty": "rc", "nm": "r", "d": 1, "p": static([(x + w / 2) * S, (y + h / 2) * S]),
         "s": static([w * S, h * S]), "r": static(r * S)},
        {"ty": "fl", "nm": "f", "c": static(color), "o": static(100), "r": 1},
        {"ty": "tr", "p": static([0, 0]), "a": static([0, 0]), "s": static([100, 100]),
         "r": static(0), "o": static(100)}]}]


def poly(points, color):
    pts = [[x * S, y * S] for x, y in points]
    return [{"ty": "gr", "nm": "g", "it": [
        {"ty": "sh", "nm": "p", "ks": static({"c": True, "v": pts, "i": [[0, 0]] * 4, "o": [[0, 0]] * 4})},
        {"ty": "fl", "nm": "f", "c": static(color), "o": static(100), "r": 1},
        {"ty": "tr", "p": static([0, 0]), "a": static([0, 0]), "s": static([100, 100]),
         "r": static(0), "o": static(100)}]}]


def grow(start, length=22):
    """Scale X 0 -> 100 from the layer's left anchor."""
    return kf([(start, [0, 100, 100]), (start + length, [100, 100, 100])])


# a null that carries the final "breath" for every layer
null = {"ddd": 0, "ind": 1, "ty": 3, "nm": "mark", "sr": 1, "ip": 0, "op": OP, "st": 0, "bm": 0,
        "ks": {"o": static(0), "r": static(0), "p": static([128, 128, 0]), "a": static([128, 128, 0]),
               "s": kf([(0, [100, 100, 100]), (62, [100, 100, 100]), (76, [104, 104, 100]),
                        (94, [100, 100, 100])])}}

layers = [
    null,
    layer(2, "line 2", rect(13, 28.5, 36, 7, 3.5, PINE), [13 * S, 32 * S], grow(10), parent=1),
    layer(3, "highlighter", poly([(8, 26.5), (56, 23.5), (56.5, 37.5), (8.5, 40.5)], MARKER),
          [8 * S, 32 * S], grow(30, 26), parent=1),
    layer(4, "line 3", rect(12, 45, 21, 7, 3.5, PAPER), [12 * S, 48.5 * S], grow(18), opacity=72, parent=1),
    layer(5, "line 1", rect(12, 12, 30, 7, 3.5, PAPER), [12 * S, 15.5 * S], grow(2), parent=1),
    layer(6, "tile", rect(0, 0, 64, 64, 15, PINE), [128, 128], parent=1),
]
doc = {"v": "5.7.4", "fr": FR, "ip": 0, "op": OP, "w": 64 * S, "h": 64 * S, "nm": "AI Scan Repos mark",
       "ddd": 0, "assets": [], "layers": layers}
out = Path(__file__).resolve().parent.parent / "src" / "assets" / "logo-sweep.json"
out.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
print(out, out.stat().st_size, "bytes")
