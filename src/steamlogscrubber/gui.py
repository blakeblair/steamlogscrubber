#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import cli


ASSETS_DIR = Path(__file__).resolve().parent / "assets"
HEADER_GIF = ASSETS_DIR / "header.gif"
PLACEHOLDER_GIF = ASSETS_DIR / "placeholder.gif"

CARAMELLDANSEN_BG_COLORS = [
    "#ff4fd8",
    "#00e5ff",
    "#ffff33",
    "#7cff00",
    "#ff8c00",
    "#b967ff",
    "#ff2f6d",
    "#00ffb3",
]

RULE_CHOICES = {
    "Default": [],
    "Strict": ["--strict"],
    "Relaxed": ["--relaxed"],
}


def open_with_os_editor(path: Path) -> None:
    if sys.platform.startswith("linux"):
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen(
                [opener, str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return

    if sys.platform == "darwin":
        subprocess.Popen(
            ["open", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    if os.name == "nt":
        os.startfile(str(path))
        return

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        subprocess.Popen([editor, str(path)])
        return

    raise RuntimeError("No system file opener or EDITOR was found.")


def ensure_user_template() -> Path:
    cli.ensure_app_folders()
    destination = cli.USER_CONFIG_DIR / f"custom.template{cli.RULE_EXT}"

    if not destination.exists():
        shutil.copy2(cli.TEMPLATE_RULES, destination)

    return destination


class SteamLogScrubberGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Steam Log Scrubber")
        self.root.geometry("720x620")
        self.root.minsize(640, 560)

        self.rules_var = tk.StringVar(value="Default")
        self.input_var = tk.StringVar(value="")
        self.dry_run_var = tk.BooleanVar(value=False)
        self.no_archive_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready.")
        self.progress_text_var = tk.StringVar(value="No scrub running.")

        self.header_animation_after_id: str | None = None
        self.header_frames: list[tk.PhotoImage] = []
        self.header_frame_index = 0
        self.header_visible = False

        self.build_ui()

    def build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)

        header = ttk.Frame(frame)
        header.pack(fill="x", pady=(0, 18))
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=0)

        title_block = ttk.Frame(header)
        title_block.grid(row=0, column=0, sticky="nw")

        title = ttk.Label(
            title_block,
            text="Steam Log Scrubber",
            font=("TkDefaultFont", 18, "bold"),
        )
        title.pack(anchor="w")

        subtitle = ttk.Label(
            title_block,
            text="Scrub Steam logs and detected Proton logs before sharing them.",
        )
        subtitle.pack(anchor="w", pady=(4, 0))

        self.header_frames = self.load_header_frames()
        self.header_image_label = ttk.Label(
            header,
            image=self.header_frames[0],
        )

        controls = ttk.Frame(frame)
        controls.pack(fill="x")

        ttk.Label(controls, text="Input folder (optional)").grid(row=0, column=0, sticky="w")

        input_entry = ttk.Entry(
            controls,
            textvariable=self.input_var,
            width=56,
        )
        input_entry.grid(row=1, column=0, sticky="ew", pady=(4, 12))

        browse_button = ttk.Button(
            controls,
            text="Browse...",
            command=self.choose_input_folder,
        )
        browse_button.grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(4, 12))

        controls.columnconfigure(0, weight=1)

        ttk.Label(controls, text="Ruleset").grid(row=2, column=0, sticky="w")

        rules = ttk.Combobox(
            controls,
            textvariable=self.rules_var,
            values=list(RULE_CHOICES.keys()),
            state="readonly",
            width=24,
        )
        rules.grid(row=3, column=0, sticky="w", pady=(4, 12))

        template_button = ttk.Button(
            controls,
            text="Open custom template",
            command=self.open_template,
        )
        template_button.grid(row=3, column=1, sticky="w", padx=(12, 0), pady=(4, 12))

        dry_run = ttk.Checkbutton(
            controls,
            text="Dry run",
            variable=self.dry_run_var,
        )
        dry_run.grid(row=4, column=0, sticky="w", pady=(0, 8))

        no_archive = ttk.Checkbutton(
            controls,
            text="No archive",
            variable=self.no_archive_var,
        )
        no_archive.grid(row=4, column=1, sticky="w", padx=(12, 0), pady=(0, 8))

        if os.name == "nt":
            hint_text = (
                "Leave input blank for Steam logs. Include a game's detectable logs with "
                "its Windows Steam launch option: --steamlogscrubber"
            )
        else:
            hint_text = (
                "Leave input blank for Steam logs and all detected Proton logs. "
                "Enable a game's Proton log with: PROTON_LOG=1 %command%"
            )

        hint = ttk.Label(
            frame,
            text=hint_text,
            wraplength=680,
        )
        hint.pack(anchor="w", pady=(6, 12))

        self.redact_button = tk.Button(
            frame,
            text="REDACT",
            command=self.start_scrub,
            bg="#b00020",
            fg="white",
            activebackground="#7f0018",
            activeforeground="white",
            font=("TkDefaultFont", 18, "bold"),
            height=2,
            relief="raised",
            bd=4,
        )
        self.redact_button.pack(fill="x", pady=(0, 10))

        self.progress = tk.Canvas(
            frame,
            height=18,
            highlightthickness=0,
            bg="#000000",
        )
        self.progress.pack(fill="x", pady=(0, 4))
        self.progress_rect = self.progress.create_rectangle(
            0,
            0,
            0,
            18,
            fill="#000000",
            outline="",
        )
        self.progress_complete_letter_items: list[int] = []

        progress_label = ttk.Label(frame, textvariable=self.progress_text_var)
        progress_label.pack(anchor="w", pady=(0, 12))

        self.output = scrolledtext.ScrolledText(
            frame,
            wrap="word",
            height=12,
            state="disabled",
        )
        self.output.pack(fill="both", expand=True)

        status = ttk.Label(frame, textvariable=self.status_var)
        status.pack(anchor="w", pady=(10, 0))

    def load_header_frames(self) -> list[tk.PhotoImage]:
        image_path = HEADER_GIF if HEADER_GIF.exists() else PLACEHOLDER_GIF
        frames: list[tk.PhotoImage] = []

        if image_path.exists():
            index = 0
            while True:
                try:
                    frame = tk.PhotoImage(file=str(image_path), format=f"gif -index {index}")
                except tk.TclError:
                    break

                frames.append(frame)
                index += 1

        if frames:
            return frames

        image = tk.PhotoImage(width=220, height=124)
        image.put("#000000", to=(0, 0, 220, 124))
        return [image]

    def show_header_image(self) -> None:
        if not self.header_visible:
            self.header_image_label.grid(row=0, column=1, sticky="ne", padx=(18, 0))
            self.header_visible = True

        self.start_header_animation()

    def start_header_animation(self) -> None:
        if len(self.header_frames) <= 1:
            return

        if self.header_animation_after_id is not None:
            return

        self.animate_header()

    def animate_header(self) -> None:
        if len(self.header_frames) <= 1:
            return

        self.header_frame_index = (self.header_frame_index + 1) % len(self.header_frames)
        self.header_image_label.configure(image=self.header_frames[self.header_frame_index])
        self.header_animation_after_id = self.root.after(80, self.animate_header)

    def open_template(self) -> None:
        try:
            template = ensure_user_template()
            open_with_os_editor(template)
            self.status_var.set(f"Opened template: {template}")
        except Exception as exc:
            messagebox.showerror("Editor error", str(exc))
            self.status_var.set("Could not open template.")

    def choose_input_folder(self) -> None:
        selected = filedialog.askdirectory(title="Choose a folder of logs to scrub")
        if selected:
            self.input_var.set(selected)

    def start_scrub(self) -> None:
        args: list[str] = []

        input_folder = self.input_var.get().strip()
        if input_folder:
            args.append(input_folder)

        args.extend(RULE_CHOICES.get(self.rules_var.get(), []))

        if self.dry_run_var.get():
            args.append("--dry-run")

        if self.no_archive_var.get():
            args.append("--no-archive")

        args.append("--json-progress")

        self.show_header_image()
        self.clear_completion_bar()
        self.progress.configure(bg="#000000")
        self.progress.coords(self.progress_rect, 0, 0, 0, 18)

        self.set_running(True)
        self.clear_output()
        self.append_output("Starting scrub. This may take a moment...\n\n")
        self.status_var.set("Running scrub...")
        self.progress_text_var.set("Starting...")
        self.root.update_idletasks()

        worker = threading.Thread(target=self.run_scrub_subprocess, args=(args,), daemon=True)
        worker.start()

    def run_scrub_subprocess(self, args: list[str]) -> None:
        command = [
            sys.executable,
            "-m",
            "steamlogscrubber",
            *args,
        ]

        code = 1

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            assert process.stdout is not None

            for line in process.stdout:
                self.handle_subprocess_line(line)

            code = process.wait()

        except Exception as exc:
            self.root.after(0, self.append_output, f"Error: {exc}\n")
            code = 1

        self.root.after(0, self.finish_scrub, code)

    def handle_subprocess_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self.root.after(0, self.append_output, line)
            return

        if event.get("type") == "progress":
            self.root.after(0, self.handle_progress_event, event)
            return

        self.root.after(0, self.append_output, line)

    def handle_progress_event(self, event: dict[str, object]) -> None:
        current = int(event.get("current", 0))
        total = int(event.get("total", 0))
        filename = str(event.get("file", ""))

        self.randomize_progress_bar()
        self.progress_text_var.set(f"Processed {current} / {total}: {filename}")

    def randomize_progress_bar(self) -> None:
        self.clear_completion_bar()

        width = max(self.progress.winfo_width(), 1)
        height = max(self.progress.winfo_height(), 18)

        self.progress.configure(bg=random.choice(CARAMELLDANSEN_BG_COLORS))

        if width <= 1:
            return

        min_bar = max(30, width // 10)
        max_bar = max(min_bar + 1, width // 2)
        bar_width = random.randint(min_bar, max_bar)
        x0 = random.randint(0, max(0, width - bar_width))
        x1 = x0 + bar_width

        self.progress.coords(self.progress_rect, x0, 0, x1, height)

    def clear_completion_bar(self) -> None:
        for item in self.progress_complete_letter_items:
            self.progress.delete(item)

        self.progress_complete_letter_items.clear()

    def show_completion_bar(self) -> None:
        self.clear_completion_bar()

        width = max(self.progress.winfo_width(), 1)
        height = max(self.progress.winfo_height(), 18)

        self.progress.configure(bg="#000000")
        self.progress.coords(self.progress_rect, 0, 0, 0, height)

        text = "UNDID IRIDIUM"
        start_x = width * 0.06
        end_x = width * 0.94
        usable_width = max(end_x - start_x, 1)
        divisor = max(len(text) - 1, 1)
        font_size = max(9, min(14, height - 4))

        for index, character in enumerate(text):
            if character == " ":
                continue

            x = start_x + usable_width * index / divisor
            item = self.progress.create_text(
                x,
                height / 2,
                text=character,
                fill="#ffffff",
                font=("TkDefaultFont", font_size, "bold"),
                anchor="center",
            )
            self.progress_complete_letter_items.append(item)


    def finish_scrub(self, code: int) -> None:
        if code == 0:
            self.status_var.set("Complete.")
        elif code == 1:
            self.status_var.set("Finished with warnings or possible leftovers. Review output.")
        else:
            self.status_var.set(f"Failed with exit code {code}.")

        self.set_running(False)
        self.show_completion_bar()

    def set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.redact_button.configure(state=state)

        if running:
            self.redact_button.configure(text="REDACTING...")
        else:
            self.redact_button.configure(text="REDACT")

    def clear_output(self) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def append_output(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.see("end")
        self.output.configure(state="disabled")


def main() -> int:
    root = tk.Tk()
    SteamLogScrubberGui(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
