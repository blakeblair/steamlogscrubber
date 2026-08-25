from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout

from steamlogscrubber import cli, gui


class GuiLineStream:
    def __init__(self, window: gui.SteamLogScrubberGui) -> None:
        self.window = window
        self.buffer = ""

    def write(self, text: str) -> int:
        self.buffer += text

        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.window.handle_subprocess_line(line + "\n")

        return len(text)

    def flush(self) -> None:
        if self.buffer:
            self.window.handle_subprocess_line(self.buffer)
            self.buffer = ""


def frozen_run_scrub(self: gui.SteamLogScrubberGui, args: list[str]) -> None:
    stream = GuiLineStream(self)
    code = 1

    try:
        with redirect_stdout(stream), redirect_stderr(stream):
            code = cli.main(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        stream.write(f"Error: {exc}\n")
        code = 1
    finally:
        stream.flush()

    self.root.after(0, self.finish_scrub, code)


def main() -> int:
    gui.SteamLogScrubberGui.run_scrub_subprocess = frozen_run_scrub
    return gui.main()


if __name__ == "__main__":
    raise SystemExit(main())
