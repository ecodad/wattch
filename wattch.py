"""Wattch: a small live NVIDIA GPU monitor. Plots nvidia-smi's GPU use, power and more on one graph.

Run:  pythonw wattch.py                              (Windows, no console window)
      python  wattch.py --interval 1000 --seconds 300

- One long-running `nvidia-smi -lms <interval>` process supplies the samples; closing the window stops it.
- Left axis: percent. GPU use, memory-bandwidth use, VRAM used (% of total), fan, SM clock (% of the
  card's max SM clock) and temperature (°C, read straight off the 0-100 scale) all share it.
- Right axis: watts, fixed at 0 .. the card's max power limit + 15% (rounded up). A dashed line marks
  the enforced (current) power limit when it is lower than the max.
- Each series has a checkbox under the graph showing its current value in the series' own color;
  untick it to hide that line.
- Only the Python standard library (tkinter): nothing for Smart App Control to block.
"""
import argparse
import math
import queue
import subprocess
import threading
import time
import tkinter as tk
from collections import deque

FIELDS = ["utilization.gpu", "utilization.memory", "power.draw", "memory.used", "memory.total",
          "temperature.gpu", "fan.speed", "clocks.sm", "name"]
BG, GRID, TEXT, DIM = "#1e1e1e", "#333333", "#d0d0d0", "#8a8a8a"
C_W = "#ffa040"
PAD_L, PAD_R, PAD_T, PAD_B = 48, 52, 14, 24
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# key, color, shown by default, line width, readout width (chars)
SERIES = [
    ("gpu",   "#4fa3ff", True,  2, 10),
    ("watts", C_W,       True,  2, 12),
    ("mem",   "#6cc070", True,  1, 12),
    ("vram",  "#c58cff", False, 1, 19),
    ("temp",  "#ff6b6b", False, 1, 8),
    ("clock", "#40d0d0", False, 1, 13),
    ("fan",   "#e0e0e0", False, 1, 9),
]


def num(s):
    """'98 %' / '54.51 W' / '[N/A]' -> float or None."""
    try:
        return float(s.strip().split()[0])
    except (ValueError, IndexError):
        return None


def card_limits():
    """(max power limit W, enforced power limit W, max SM clock MHz); any may be None."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=power.max_limit,enforced.power.limit,power.limit,clocks.max.sm",
             "--format=csv,noheader"], capture_output=True, text=True, timeout=10, creationflags=NOWIN).stdout
        mx, enf, lim, clk = (num(x) for x in out.splitlines()[0].split(","))
        return mx or enf or lim, enf or lim, clk
    except Exception:
        return None, None, None


def reader(proc, q):
    for line in proc.stdout:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= len(FIELDS):
            q.put((time.time(), parts))
    q.put(None)  # nvidia-smi exited


class Monitor:
    def __init__(self, root, interval_ms, seconds):
        self.root, self.interval = root, interval_ms
        self.n = max(10, seconds * 1000 // interval_ms)
        self.data = {k: deque(maxlen=self.n) for k, *_ in SERIES}
        pmax, self.penf, self.clkmax = card_limits()
        # Fixed watts axis: max limit + 15%, rounded up to a multiple of 20 (so quarter ticks are whole).
        self.wmax = math.ceil((pmax or 100) * 1.15 / 20) * 20
        self.q = queue.Queue()
        self.proc = subprocess.Popen(
            ["nvidia-smi", f"--query-gpu={','.join(FIELDS)}", "--format=csv,noheader", f"-lms={interval_ms}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, creationflags=NOWIN)
        threading.Thread(target=reader, args=(self.proc, self.q), daemon=True).start()

        root.title("Wattch")
        root.configure(bg=BG)
        root.geometry("860x380")
        root.minsize(480, 260)
        self.title = tk.Label(root, text="GPU", bg=BG, fg=TEXT, font=("Segoe UI", 10, "bold"), anchor="w")
        self.title.pack(fill="x", padx=8, pady=(6, 0))
        bar = tk.Frame(root, bg=BG)  # packed before the canvas so a short window squeezes the graph, not the bar
        bar.pack(side="bottom", fill="x", padx=4, pady=(0, 6))
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=4)
        self.show, self.boxes = {}, {}
        for key, color, on, _, width in SERIES:
            self.show[key] = tk.BooleanVar(value=on)
            self.boxes[key] = tk.Checkbutton(
                bar, text=key, variable=self.show[key], command=self.draw, bg=BG, fg=color, width=width,
                anchor="w", selectcolor=BG, activebackground=BG, activeforeground=color, font=("Segoe UI", 9))
            self.boxes[key].pack(side="left")
        self.status = tk.Label(bar, bg=BG, fg=DIM, font=("Segoe UI", 9))
        self.status.pack(side="right")
        self.canvas.bind("<Configure>", lambda e: self.draw())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()

    def poll(self):
        got = False
        try:
            while True:
                item = self.q.get_nowait()
                if item is None:
                    self.status.config(text="nvidia-smi stopped")
                    break
                _, p = item
                gpu, mem, w, used, total, temp, fan, clk = (num(x) for x in p[:8])
                vram = used / total * 100 if used is not None and total else None
                clk_pct = clk / self.clkmax * 100 if clk is not None and self.clkmax else None
                for k, v in (("gpu", gpu), ("watts", w), ("mem", mem), ("vram", vram),
                             ("temp", temp), ("clock", clk_pct), ("fan", fan)):
                    self.data[k].append(v)
                self.title.config(text=p[8])
                txt = {
                    "gpu": f"GPU {gpu:.0f} %" if gpu is not None else "GPU n/a",
                    "watts": f"power {w:.1f} W" if w is not None else "power n/a",
                    "mem": f"mem bw {mem:.0f} %" if mem is not None else "mem bw n/a",
                    "vram": f"VRAM {used / 1024:.1f} / {total / 1024:.1f} GB" if vram is not None else "VRAM n/a",
                    "temp": f"{temp:.0f} °C" if temp is not None else "temp n/a",
                    "clock": f"SM {clk:.0f} MHz" if clk is not None else "SM n/a",
                    "fan": f"fan {fan:.0f} %" if fan is not None else "fan n/a",
                }
                for k, t in txt.items():
                    self.boxes[k].config(text=t)
                got = True
        except queue.Empty:
            pass
        if got:
            self.draw()
        self.root.after(250, self.poll)

    def draw(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        x0, x1, y0, y1 = PAD_L, w - PAD_R, PAD_T, h - PAD_B
        if x1 - x0 < 20 or y1 - y0 < 20:
            return
        for i in range(5):  # grid + both axes' labels
            y = y1 - (y1 - y0) * i / 4
            c.create_line(x0, y, x1, y, fill=GRID)
            c.create_text(x0 - 6, y, text=f"{25 * i}%", fill=TEXT, anchor="e", font=("Segoe UI", 8))
            c.create_text(x1 + 6, y, text=f"{self.wmax * i / 4:.0f} W", fill=C_W, anchor="w", font=("Segoe UI", 8))
        secs = self.n * self.interval / 1000
        for frac in (0, 0.25, 0.5, 0.75):
            x = x0 + (x1 - x0) * frac
            c.create_line(x, y0, x, y1, fill=GRID)
            c.create_text(x, y1 + 4, text=f"-{secs * (1 - frac):.0f}s", fill=DIM, anchor="n", font=("Segoe UI", 8))
        c.create_text(x1, y1 + 4, text="now", fill=DIM, anchor="ne", font=("Segoe UI", 8))
        if self.show["watts"].get() and self.penf and self.penf < self.wmax * 0.95:
            y = y1 - (y1 - y0) * self.penf / self.wmax
            c.create_line(x0, y, x1, y, fill=C_W, dash=(3, 4))
            c.create_text(x0 + 4, y - 2, text=f"power limit {self.penf:.0f} W", fill=C_W, anchor="sw",
                          font=("Segoe UI", 8))

        def line(data, scale, color, width):
            pts = []
            start = self.n - len(data)
            for i, v in enumerate(data):
                if v is None:
                    continue
                pts += [x0 + (x1 - x0) * (start + i) / (self.n - 1), y1 - (y1 - y0) * max(0, min(v / scale, 1))]
            if len(pts) >= 4:
                c.create_line(*pts, fill=color, width=width)

        # Draw thin lines first, then the bold ones (GPU on top) so the main series stay readable.
        for key, color, _, width, _ in sorted(SERIES, key=lambda s: (s[3], s[0] == "gpu")):
            if self.show[key].get():
                line(self.data[key], self.wmax if key == "watts" else 100, color, width)

    def close(self):
        self.proc.terminate()
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=500, help="sample interval in ms (default 500)")
    ap.add_argument("--seconds", type=int, default=120, help="history shown (default 120)")
    a = ap.parse_args()
    root = tk.Tk()
    Monitor(root, a.interval, a.seconds)
    root.mainloop()


if __name__ == "__main__":
    main()
