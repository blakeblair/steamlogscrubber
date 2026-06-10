from __future__ import annotations

import subprocess
import sys

from steamlogscrubber import cli, gui


def frozen_run_scrub_subprocess(self, args: list[str]) -> None:
    command = [
        sys.executable,
        "--cli-subprocess",
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


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--cli-subprocess":
        return cli.main(sys.argv[2:])

    gui.SteamLogScrubberGui.run_scrub_subprocess = frozen_run_scrub_subprocess
    return gui.main()


if __name__ == "__main__":
    raise SystemExit(main())
