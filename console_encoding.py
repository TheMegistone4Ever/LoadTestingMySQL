import sys


def enable_utf8_console_output():
    for output_stream in (sys.stdout, sys.stderr):
        if hasattr(output_stream, "reconfigure"):
            output_stream.reconfigure(encoding="utf-8", errors="replace")
